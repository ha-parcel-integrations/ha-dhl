"""Tests for DHL diagnostics.

Includes the structure-preserving redaction test: a synthetic populated body
run through the redactor must keep the exact same key set, at every depth,
before and after — the thing a naive top-level redactor misses on
``sendungsdetails.zustellung.empfaenger.name``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.diagnostics import async_redact_data

from custom_components.dhl.const import CONF_TRACKED_CODES
from custom_components.dhl.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)

from .payloads import active_sample


def _all_key_paths(node, prefix: str = "") -> set[str]:
    """Every key path in a nested structure, dict keys and list indices alike."""
    paths: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths |= _all_key_paths(value, path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            path = f"{prefix}[{index}]"
            paths |= _all_key_paths(value, path)
    return paths


def test_redaction_preserves_the_full_key_set():
    raw = active_sample()
    raw["sendungsdetails"]["zustellung"]["empfaenger"] = {"name": "Jane Doe"}
    parcel = {
        "carrier": "DHL",
        "barcode": raw["id"],
        "sender": None,
        "receiver": None,
        "status": "out_for_delivery",
        "raw_status": "text",
        "delivered": False,
        "url": "https://track/1?piececode=abc",
        "raw": raw,
    }

    before = _all_key_paths(parcel)
    redacted = async_redact_data(parcel, TO_REDACT)
    after = _all_key_paths(redacted)

    assert before == after
    # and values were genuinely redacted, not just left alone
    assert redacted["raw"]["sendungsdetails"]["zustellung"]["empfaenger"]["name"] == (
        "**REDACTED**"
    )
    assert redacted["raw"]["id"] == "**REDACTED**"
    assert redacted["barcode"] == "**REDACTED**"
    # every type is preserved: a redacted bool stays a bool, an int an int
    assert isinstance(redacted["delivered"], bool)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    raw = active_sample()
    raw["sendungsdetails"]["zustellung"]["empfaenger"] = {"name": "Jane Doe"}
    entry = MagicMock()
    entry.options = {CONF_TRACKED_CODES: ["3S00123456789"]}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": raw["id"],
            "sender": None,
            "receiver": None,
            "status": "out_for_delivery",
            "url": "https://track/1?piececode=abc",
            "raw": raw,
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = []
    entry.runtime_data.coordinator.delivered_outgoing = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "outgoing_active": 0,
        "outgoing_delivered": 0,
    }
    assert result["entry_options"][CONF_TRACKED_CODES] == ["**REDACTED**"]
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["url"] == "**REDACTED**"
    assert (
        result["incoming"][0]["raw"]["sendungsdetails"]["zustellung"]["empfaenger"][
            "name"
        ]
        == "**REDACTED**"
    )
    # the structure around the redacted PII survives, only the leaf is gone
    assert "empfaenger" in result["incoming"][0]["raw"]["sendungsdetails"]["zustellung"]
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"
