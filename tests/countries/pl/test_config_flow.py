"""Tests for the PL leg of the config/reauth flow — the phone/SMS dance."""
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.config_flow import normalize_polish_phone
from custom_components.dhl.const import (
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DHL_PL_COOKIES,
    CONF_DHL_PL_DEVICE_ID,
    CONF_DHL_PL_PHONE,
    CONF_INCLUDE_HISTORY,
    CONF_TRACKED_CODES,
    DOMAIN,
    DHLApiError,
    DHLAuthError,
)

PL_SESSION_CLASS = "custom_components.dhl.config_flow.DHLPlSession"


def test_normalize_polish_phone_accepts_international_prefixes():
    assert normalize_polish_phone("501 234 567") == "501234567"
    assert normalize_polish_phone("+48 501 234 567") == "501234567"
    assert normalize_polish_phone("0048 501 234 567") == "501234567"
    assert normalize_polish_phone("+31 501 234 567") is None


def _fake_pl_session(
    *, send_sms_side_effect=None, verify_sms_side_effect=None, cookies=None
) -> MagicMock:
    session = MagicMock()
    session.async_send_sms = AsyncMock(
        side_effect=send_sms_side_effect, return_value=60
    )
    session.async_verify_sms = AsyncMock(side_effect=verify_sms_side_effect)
    session.export_cookies = MagicMock(return_value=cookies or [{"name": "access-token"}])
    session.aclose = AsyncMock()
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


async def test_abandoned_pl_flow_closes_its_throwaway_session(hass):
    """Quitting between the phone and SMS steps must not leak the session."""
    session = _fake_pl_session()

    with patch(PL_SESSION_CLASS, return_value=session):
        result = await _start_pl_flow(hass)
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": "501234567"}
        )
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()

    session.aclose.assert_awaited_once()


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
