"""Tests for the DHL config and options flow — the browser-paste OIDC dance."""
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.config_flow import DHLConfigFlow, normalize_polish_phone
from custom_components.dhl.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DHL_PL_COOKIES,
    CONF_DHL_PL_DEVICE_ID,
    CONF_DHL_PL_PHONE,
    CONF_INCLUDE_HISTORY,
    CONF_REFRESH_TOKEN,
    CONF_TRACKED_CODES,
    DOMAIN,
    DHLApiError,
    DHLAuthError,
)
from custom_components.dhl.countries.de.session import DHLDeAuthError, DHLDeSessionError

AUTH_URL = "https://login.dhl.de/x/login/authorize?client_id=x&state=state123"
REDIRECT = "dhllogin://de.dhl.paket/login?code=abc123&state=state123"

SESSION_CLASS = "custom_components.dhl.config_flow.DHLDeSession"
PL_SESSION_CLASS = "custom_components.dhl.config_flow.DHLPlSession"


def test_normalize_polish_phone_accepts_international_prefixes():
    assert normalize_polish_phone("501 234 567") == "501234567"
    assert normalize_polish_phone("+48 501 234 567") == "501234567"
    assert normalize_polish_phone("0048 501 234 567") == "501234567"
    assert normalize_polish_phone("+31 501 234 567") is None


def _fake_session(
    *,
    url: str = AUTH_URL,
    verifier: str = "verifier",
    state: str = "state123",
    exchange_side_effect=None,
    refresh_token: str = "the-refresh-token",
    id_token: str = "the-id-token",
) -> MagicMock:
    session = MagicMock()
    session.async_authorization_url = AsyncMock(return_value=(url, verifier, state))
    session.async_exchange_code = AsyncMock(side_effect=exchange_side_effect)
    session.refresh_token = refresh_token
    session.id_token = id_token
    return session


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="DHL (DE)",
        unique_id="DE:subject-abc",
        data={
            CONF_COUNTRY: "DE",
            CONF_REFRESH_TOKEN: "old-refresh-token",
            CONF_ACCOUNT_SUBJECT: "subject-abc",
        },
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
            CONF_TRACKED_CODES: ["EXISTING000001"],
        },
    )


def _jwt_for(sub: str) -> str:
    import base64
    import json

    encoded = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=")
    return f"h.{encoded.decode()}.s"


async def _start_de_flow(hass):
    """Init the flow and pick Germany — the two-step shape every test needs."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "de"}
    )


def _fake_pl_session(
    *, send_sms_side_effect=None, verify_sms_side_effect=None, cookies=None
) -> MagicMock:
    session = MagicMock()
    session.async_send_sms = AsyncMock(
        side_effect=send_sms_side_effect, return_value=60
    )
    session.async_verify_sms = AsyncMock(side_effect=verify_sms_side_effect)
    session.export_cookies = MagicMock(return_value=cookies or [{"name": "access-token"}])
    return session


def _pl_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="DHL (PL)",
        unique_id="PL:501234567",
        data={
            CONF_COUNTRY: "PL",
            CONF_DHL_PL_PHONE: "501234567",
            CONF_DHL_PL_DEVICE_ID: "device-1",
            CONF_DHL_PL_COOKIES: [{"name": "access-token"}],
        },
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
            CONF_TRACKED_CODES: [],
        },
    )


async def _start_pl_flow(hass):
    """Init the flow and pick Poland — the two-step shape every test needs."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "pl"}
    )


# ---------------------------------------------------------------------------
# user step — country picker
# ---------------------------------------------------------------------------


async def test_user_flow_shows_country_picker(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["step_id"] == "user"
    assert result["type"] == "form"


async def test_user_flow_dispatches_to_de(hass):
    with patch(SESSION_CLASS, return_value=_fake_session()):
        result = await _start_de_flow(hass)

    assert result["step_id"] == "de"
    assert result["description_placeholders"]["authorize_url"] == AUTH_URL


async def test_user_flow_rejects_unsupported_country(hass):
    flow = DHLConfigFlow()
    flow.hass = hass

    result = await flow.async_step_user({"country": "fr"})

    assert result["type"] == "abort"
    assert result["reason"] == "unsupported_country"


# ---------------------------------------------------------------------------
# de step
# ---------------------------------------------------------------------------


async def test_user_flow_shows_authorize_url(hass):
    with patch(SESSION_CLASS, return_value=_fake_session()):
        result = await _start_de_flow(hass)

    assert result["step_id"] == "de"
    assert result["description_placeholders"]["authorize_url"] == AUTH_URL


async def test_user_flow_creates_entry(hass):
    session = _fake_session(id_token=_jwt_for("subject-1"))
    with patch(SESSION_CLASS, return_value=session):
        result = await _start_de_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": REDIRECT}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_COUNTRY] == "DE"
    assert result["data"][CONF_REFRESH_TOKEN] == "the-refresh-token"
    assert result["data"][CONF_ACCOUNT_SUBJECT] == "subject-1"
    assert result["options"][CONF_TRACKED_CODES] == []
    session.async_exchange_code.assert_awaited_once_with("abc123", "verifier")


async def test_user_flow_pasted_url_with_whitespace_and_extra_params(hass):
    """Users paste messily — leading/trailing whitespace and extra query params."""
    messy = f"  {REDIRECT}&extra=1&another=two \n"
    session = _fake_session()
    with patch(SESSION_CLASS, return_value=session):
        result = await _start_de_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": messy}
        )

    assert result["type"] == "create_entry"


async def test_user_flow_state_mismatch_is_invalid_redirect(hass):
    session = _fake_session(state="expected-state")
    with patch(SESSION_CLASS, return_value=session):
        result = await _start_de_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"redirect_url": "dhllogin://x/login?code=abc&state=wrong-state"},
        )

    assert result["errors"] == {"base": "invalid_redirect"}
    session.async_exchange_code.assert_not_called()


async def test_user_flow_missing_code_is_invalid_redirect(hass):
    session = _fake_session()
    with patch(SESSION_CLASS, return_value=session):
        result = await _start_de_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": "dhllogin://x/login?state=state123"}
        )

    assert result["errors"] == {"base": "invalid_redirect"}


async def test_user_flow_surfaces_auth_error(hass):
    session = _fake_session(exchange_side_effect=DHLDeAuthError("rejected"))
    with patch(SESSION_CLASS, return_value=session):
        result = await _start_de_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": REDIRECT}
        )

    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_surfaces_connection_error(hass):
    session = _fake_session(exchange_side_effect=DHLDeSessionError("outage"))
    with patch(SESSION_CLASS, return_value=session):
        result = await _start_de_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": REDIRECT}
        )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_aborts_when_discovery_fails(hass):
    session = _fake_session()
    session.async_authorization_url = AsyncMock(
        side_effect=DHLDeSessionError("discovery unreachable")
    )
    with patch(SESSION_CLASS, return_value=session):
        result = await _start_de_flow(hass)

    assert result["type"] == "abort"
    assert result["reason"] == "cannot_connect"


async def test_user_flow_aborts_on_duplicate_account(hass):
    _entry().add_to_hass(hass)
    session = _fake_session(id_token=_jwt_for("subject-abc"))

    with patch(SESSION_CLASS, return_value=session):
        result = await _start_de_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": REDIRECT}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_authorize_url_generated_once_per_flow(hass):
    """A retry after an error must not invalidate the URL the user already opened."""
    session = _fake_session(state="expected-state")
    with patch(SESSION_CLASS, return_value=session):
        result = await _start_de_flow(hass)
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"redirect_url": "dhllogin://x/login?code=abc&state=wrong"},
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"redirect_url": "dhllogin://x/login?code=abc&state=wrong-again"},
        )

    session.async_authorization_url.assert_awaited_once()


# ---------------------------------------------------------------------------
# reauth
# ---------------------------------------------------------------------------


async def test_reauth_updates_the_refresh_token(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    session = _fake_session(
        refresh_token="rotated-refresh-token", id_token=_jwt_for("subject-abc")
    )

    with patch(SESSION_CLASS, return_value=session):
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": REDIRECT}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "rotated-refresh-token"


async def test_reauth_rejects_a_different_account(hass):
    """Reauthorizing as another DHL account must not silently rebind the entry."""
    entry = _entry()
    entry.add_to_hass(hass)
    session = _fake_session(id_token=_jwt_for("someone-else"))

    with patch(SESSION_CLASS, return_value=session):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": REDIRECT}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_REFRESH_TOKEN] == "old-refresh-token"


async def test_reauth_surfaces_invalid_auth(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    session = _fake_session(exchange_side_effect=DHLDeAuthError("rejected"))

    with patch(SESSION_CLASS, return_value=session):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect_url": REDIRECT}
        )

    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_aborts_when_discovery_fails(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    session = _fake_session()
    session.async_authorization_url = AsyncMock(
        side_effect=DHLDeSessionError("outage")
    )

    with patch(SESSION_CLASS, return_value=session):
        result = await entry.start_reauth_flow(hass)

    assert result["type"] == "abort"
    assert result["reason"] == "cannot_connect"


# ---------------------------------------------------------------------------
# pl step — phone + SMS
# ---------------------------------------------------------------------------


async def test_user_flow_dispatches_to_pl(hass):
    result = await _start_pl_flow(hass)

    assert result["step_id"] == "pl"


async def test_pl_flow_rejects_invalid_phone(hass):
    result = await _start_pl_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "not-a-phone"}
    )

    assert result["step_id"] == "pl"
    assert result["errors"] == {"phone": "invalid_phone"}


async def test_pl_flow_surfaces_connection_error(hass):
    result = await _start_pl_flow(hass)
    session = _fake_pl_session(send_sms_side_effect=DHLApiError("boom"))

    with patch(PL_SESSION_CLASS, return_value=session):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": "501234567"}
        )

    assert result["step_id"] == "pl"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_pl_flow_sends_sms_and_advances_to_code_step(hass):
    result = await _start_pl_flow(hass)
    session = _fake_pl_session()

    with patch(PL_SESSION_CLASS, return_value=session):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": "501234567"}
        )

    assert result["step_id"] == "pl_sms"
    session.async_send_sms.assert_awaited_once_with("501234567")


async def test_pl_flow_rejects_invalid_sms_code(hass):
    result = await _start_pl_flow(hass)
    session = _fake_pl_session(verify_sms_side_effect=DHLAuthError("rejected"))

    with patch(PL_SESSION_CLASS, return_value=session):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": "501234567"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "000000"}
        )

    assert result["step_id"] == "pl_sms"
    assert result["errors"] == {"sms_code": "invalid_sms_code"}


async def test_pl_flow_creates_entry(hass):
    result = await _start_pl_flow(hass)
    session = _fake_pl_session(cookies=[{"name": "access-token", "value": "v"}])

    with patch(PL_SESSION_CLASS, return_value=session):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": "501234567"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_COUNTRY] == "PL"
    assert result["data"][CONF_DHL_PL_PHONE] == "501234567"
    assert result["data"][CONF_DHL_PL_COOKIES] == [{"name": "access-token", "value": "v"}]
    assert result["options"][CONF_TRACKED_CODES] == []


async def test_pl_flow_aborts_on_duplicate_account(hass):
    entry = _pl_entry()
    entry.add_to_hass(hass)
    result = await _start_pl_flow(hass)
    session = _fake_pl_session()

    with patch(PL_SESSION_CLASS, return_value=session):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": "501234567"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_reauth_dispatches_pl_entries_to_the_phone_step(hass):
    entry = _pl_entry()
    entry.add_to_hass(hass)
    session = _fake_pl_session()

    with patch(PL_SESSION_CLASS, return_value=session):
        result = await entry.start_reauth_flow(hass)

    assert result["step_id"] == "pl"


async def test_reauth_updates_pl_cookies(hass):
    entry = _pl_entry()
    entry.add_to_hass(hass)
    session = _fake_pl_session(cookies=[{"name": "access-token", "value": "rotated"}])

    with patch(PL_SESSION_CLASS, return_value=session):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": "501234567"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_DHL_PL_COOKIES] == [{"name": "access-token", "value": "rotated"}]


async def test_reauth_rejects_a_different_pl_account(hass):
    """Reauthorizing with a different phone number must not silently rebind the entry."""
    entry = _pl_entry()
    entry.add_to_hass(hass)
    session = _fake_pl_session()

    with patch(PL_SESSION_CLASS, return_value=session):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": "501234568"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "wrong_account"


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


async def test_options_flow_saves_and_reloads(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "delivered": {
                    CONF_DELIVERED_FILTER_TYPE: "parcels",
                    CONF_DELIVERED_FILTER_AMOUNT: 5,
                },
                "history": {CONF_INCLUDE_HISTORY: True},
            },
        )

    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_DELIVERED_FILTER_TYPE: "parcels",
        CONF_DELIVERED_FILTER_AMOUNT: 5,
        CONF_INCLUDE_HISTORY: True,
        # Not part of the form — carried through untouched.
        CONF_TRACKED_CODES: ["EXISTING000001"],
    }
    schedule_reload.assert_called_once_with(entry.entry_id)
