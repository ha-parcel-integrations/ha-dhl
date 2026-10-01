"""Tests for the API source's setup, reauth and options steps."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dhl.api.client import (
    DHLUnifiedError,
    DHLUnifiedKeyError,
    DHLUnifiedNotFound,
    DHLUnifiedRateLimitError,
)
from custom_components.dhl.config_flow import _api_key_digest
from custom_components.dhl.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DHL_UNIFIED_VALIDATION_CODE,
    DOMAIN,
    SOURCE_API,
)

VALIDATE = "custom_components.dhl.config_flow.DHLUnifiedClient.async_get_shipments"


def _api_entry(api_key: str = "old-key") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="API (abcdef)",
        unique_id=f"{SOURCE_API}:{_api_key_digest(api_key)}",
        data={CONF_SOURCE: SOURCE_API, CONF_API_KEY: api_key},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: "JJD1"}]},
    )


async def _start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": SOURCE_API}
    )


async def test_api_step_shows_the_key_form(hass):
    result = await _start(hass)

    assert result["type"] == "form"
    assert result["step_id"] == SOURCE_API
    assert "developer.dhl.com" in result["description_placeholders"]["portal_url"]


async def test_a_404_for_the_dummy_code_proves_the_key(hass):
    result = await _start(hass)
    with patch(VALIDATE, AsyncMock(side_effect=DHLUnifiedNotFound("HTTP 404"))) as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "  new-key  "}
        )

    validate.assert_awaited_once_with(DHL_UNIFIED_VALIDATION_CODE)
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_SOURCE: SOURCE_API, CONF_API_KEY: "new-key"}
    assert result["options"] == {
        CONF_PARCELS: [],
        CONF_DELIVERED_FILTER_TYPE: "days",
        CONF_DELIVERED_FILTER_AMOUNT: 7,
        CONF_INCLUDE_HISTORY: False,
    }
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == f"api:{_api_key_digest('new-key')}"
    assert "new-key" not in entry.unique_id
    assert "new-key" not in entry.title


async def test_a_200_for_the_dummy_code_also_proves_the_key(hass):
    result = await _start(hass)
    with patch(VALIDATE, AsyncMock(return_value={"shipments": []})):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )

    assert result["type"] == "create_entry"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (DHLUnifiedKeyError("HTTP 401"), "invalid_api_key"),
        (DHLUnifiedKeyError("HTTP 403"), "invalid_api_key"),
        (DHLUnifiedRateLimitError("HTTP 429", None), "rate_limited"),
        (DHLUnifiedError("timeout"), "cannot_connect"),
    ],
)
async def test_validation_errors(hass, error, code):
    result = await _start(hass)
    with patch(VALIDATE, AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": code}


async def test_the_same_key_twice_is_already_configured_without_a_call(hass):
    _api_entry("new-key").add_to_hass(hass)
    result = await _start(hass)
    with patch(VALIDATE, AsyncMock()) as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    validate.assert_not_awaited()


async def test_a_second_key_is_a_second_entry(hass):
    _api_entry("old-key").add_to_hass(hass)
    result = await _start(hass)
    with patch(VALIDATE, AsyncMock(side_effect=DHLUnifiedNotFound("HTTP 404"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )

    assert result["type"] == "create_entry"


async def test_reauth_accepts_a_replacement_key(hass):
    entry = _api_entry("old-key")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_api"

    with (
        patch(VALIDATE, AsyncMock(side_effect=DHLUnifiedNotFound("HTTP 404"))),
        patch("custom_components.dhl.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "rotated-key"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "rotated-key"
    assert entry.unique_id == f"api:{_api_key_digest('rotated-key')}"
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "JJD1"}]


async def test_reauth_surfaces_a_rejected_key(hass):
    entry = _api_entry("old-key")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    with patch(VALIDATE, AsyncMock(side_effect=DHLUnifiedKeyError("HTTP 401"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "still-bad"}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_api_key"}
    assert entry.data[CONF_API_KEY] == "old-key"


async def test_reauth_refuses_a_key_another_entry_uses(hass):
    entry = _api_entry("old-key")
    entry.add_to_hass(hass)
    _api_entry("other-key").add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    with patch(VALIDATE, AsyncMock()) as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "other-key"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    validate.assert_not_awaited()
    assert entry.data[CONF_API_KEY] == "old-key"


async def test_options_menu_matches_the_tracking_source(hass):
    entry = _api_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "menu"
    assert result["menu_options"] == ["incoming_parcels", "outgoing_parcels", "settings"]


async def test_options_settings_keep_the_parcel_list(hass):
    entry = _api_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "delivered": {CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 14},
            "history": {CONF_INCLUDE_HISTORY: True},
        },
    )

    assert result["type"] == "create_entry"
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "JJD1"}]
    assert entry.options[CONF_INCLUDE_HISTORY] is True
