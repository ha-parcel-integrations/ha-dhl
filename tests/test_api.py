"""Tests for the top-level DHL API dispatcher."""
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.dhl.api import DHLApiClient, DHLApiError

from .payloads import (
    ACTIVE_CODE,
    active_sample,
    archived_sample,
    not_found_sample,
    stub_sample,
)


def _client(de_session=None) -> DHLApiClient:
    return DHLApiClient(object(), country="DE", de_session=de_session or object())


def test_requires_de_session_for_de():
    client = DHLApiClient(object(), country="DE", de_session=None)
    with pytest.raises(RuntimeError):
        client._require_de_session()


async def test_get_incoming_filters_and_returns_rate_limited():
    with patch(
        "custom_components.dhl.api.async_get_inbox_envelope",
        new=AsyncMock(
            return_value={
                "sendungen": [active_sample(), archived_sample()],
                "rateLimited": True,
            }
        ),
    ):
        elements, rate_limited = await _client().async_get_incoming()

    assert [e["id"] for e in elements] == [ACTIVE_CODE]
    assert rate_limited is True


async def test_get_incoming_drops_not_found_markers():
    with patch(
        "custom_components.dhl.api.async_get_inbox_envelope",
        new=AsyncMock(return_value={"sendungen": [not_found_sample()]}),
    ):
        elements, _ = await _client().async_get_incoming()

    assert elements == []


async def test_get_incoming_handles_missing_sendungen_key():
    with patch(
        "custom_components.dhl.api.async_get_inbox_envelope",
        new=AsyncMock(return_value={}),
    ):
        elements, rate_limited = await _client().async_get_incoming()

    assert elements == []
    assert rate_limited is False


async def test_get_incoming_enriches_a_bare_stub():
    with (
        patch(
            "custom_components.dhl.api.async_get_inbox_envelope",
            new=AsyncMock(return_value={"sendungen": [stub_sample()]}),
        ),
        patch(
            "custom_components.dhl.api.async_get_by_number_envelope",
            new=AsyncMock(return_value={"sendungen": [active_sample()]}),
        ) as by_number,
    ):
        elements, _ = await _client().async_get_incoming()

    by_number.assert_awaited_once()
    assert [e["id"] for e in elements] == [ACTIVE_CODE]
    assert "fortschritt" in elements[0]["sendungsdetails"]["sendungsverlauf"]


async def test_get_incoming_fails_the_poll_when_enrichment_resolves_nothing():
    """An unresolvable stub is indistinguishable from a not-found parcel.

    Reporting it as an empty inbox would make a real parcel vanish silently,
    so the poll fails instead and the coordinator keeps its last good data.
    """
    with (
        patch(
            "custom_components.dhl.api.async_get_inbox_envelope",
            new=AsyncMock(return_value={"sendungen": [stub_sample()]}),
        ),
        patch(
            "custom_components.dhl.api.async_get_by_number_envelope",
            new=AsyncMock(return_value={"sendungen": []}),
        ),
        pytest.raises(DHLApiError),
    ):
        await _client().async_get_incoming()


async def test_get_incoming_fails_the_poll_when_enrichment_raises():
    with (
        patch(
            "custom_components.dhl.api.async_get_inbox_envelope",
            new=AsyncMock(return_value={"sendungen": [stub_sample()]}),
        ),
        patch(
            "custom_components.dhl.api.async_get_by_number_envelope",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(DHLApiError),
    ):
        await _client().async_get_incoming()


async def test_get_incoming_keeps_healthy_elements_when_one_stub_fails(caplog):
    """A partial failure must not take the parcels that did resolve with it."""
    with (
        patch(
            "custom_components.dhl.api.async_get_inbox_envelope",
            new=AsyncMock(return_value={"sendungen": [active_sample(), stub_sample()]}),
        ),
        patch(
            "custom_components.dhl.api.async_get_by_number_envelope",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        caplog.at_level("WARNING"),
    ):
        elements, _ = await _client().async_get_incoming()

    assert [element["id"] for element in elements] == [ACTIVE_CODE]
    assert "without details" in caplog.text


async def test_get_incoming_drops_a_genuine_not_found_without_failing(caplog):
    """A parcel DHL truly has no data for is dropped quietly, as before."""
    with (
        patch(
            "custom_components.dhl.api.async_get_inbox_envelope",
            new=AsyncMock(return_value={"sendungen": [not_found_sample()]}),
        ),
        caplog.at_level("WARNING"),
    ):
        elements, _ = await _client().async_get_incoming()

    assert elements == []
    assert "without details" not in caplog.text


async def test_get_by_number_returns_matching_element():
    with patch(
        "custom_components.dhl.api.async_get_by_number_envelope",
        new=AsyncMock(return_value={"sendungen": [active_sample()]}),
    ):
        element = await _client().async_get_by_number(ACTIVE_CODE)

    assert element["id"] == ACTIVE_CODE


async def test_get_by_number_returns_none_when_not_found():
    with patch(
        "custom_components.dhl.api.async_get_by_number_envelope",
        new=AsyncMock(return_value={"sendungen": [not_found_sample()]}),
    ):
        assert await _client().async_get_by_number("UNKNOWN00001") is None


async def test_get_by_number_returns_none_for_empty_list():
    with patch(
        "custom_components.dhl.api.async_get_by_number_envelope",
        new=AsyncMock(return_value={"sendungen": []}),
    ):
        assert await _client().async_get_by_number(ACTIVE_CODE) is None


async def test_unsupported_country_raises():
    client = DHLApiClient(object(), country="NL", de_session=object())
    with pytest.raises(RuntimeError):
        await client.async_get_incoming()
    with pytest.raises(RuntimeError):
        await client.async_get_by_number("123")
