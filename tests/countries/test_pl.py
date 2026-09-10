"""Regression tests for DHL Parcel Polska's local mapping and inbox transport."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.dhl.const import DHLApiError, DHLAuthError, ParcelStatus
from custom_components.dhl.countries.pl import async_get_incoming, normalize_parcel_pl
from custom_components.dhl.countries.pl.session import DHLPlSession


def test_raw_code_is_the_primary_status_source():
    """`status` (the TT_*/SP_* code) wins even when a coarse-ladder value for
    a *different* state is also present — tracking.md's status-vocabulary
    section says the ladder is only a fallback for an unrecognised code."""
    parcel = normalize_parcel_pl({
        "shipmentNumber": "30413196282", "status": "TT_LK",
        "menuTimelineLabel": {"status": "Route", "dateUtc": "2026-09-04T10:00:00Z"},
    })
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["delivered"] is False


def test_coarse_ladder_is_the_fallback_for_an_unrecognised_raw_code():
    parcel = normalize_parcel_pl({
        "shipmentNumber": "30413196282", "status": "TT_NEW_CODE_NOT_YET_MAPPED",
        "menuTimelineLabel": {"status": "Delivery", "dateUtc": "2026-09-04T10:00:00Z"},
    })
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False


def test_unrecognised_raw_code_and_ladder_value_fall_back_to_unknown():
    parcel = normalize_parcel_pl({
        "shipmentNumber": "30413196282", "status": "TT_NEW_CODE_NOT_YET_MAPPED",
        "menuTimelineLabel": {"status": "SomeFutureLadderValue"},
    })
    assert parcel["status"] is ParcelStatus.UNKNOWN


def test_raw_status_is_a_safe_fallback_with_no_timeline_at_all():
    parcel = normalize_parcel_pl({"shipmentNumber": "30413196282", "status": "TT_DOR"})
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True


def test_locker_wait_is_ready_for_pickup_not_delivered():
    parcel = normalize_parcel_pl({
        "shipmentNumber": "30413196282", "status": "TT_LK", "sender": "Sender",
        "packageType": "Locker", "menuTimelineLabel": {
            "status": "Route", "dateUtc": "2026-09-04T10:00:00Z"},
    })
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def _pl_session(*, incoming, observed=None, incoming_status=200, observed_status=200):
    session = MagicMock()

    def _ctx(status, body):
        response = AsyncMock()
        response.status = status
        response.json = AsyncMock(return_value=body)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session.post = MagicMock(side_effect=[
        _ctx(incoming_status, incoming),
        _ctx(observed_status, observed if observed is not None else []),
    ])
    pl_session = MagicMock(spec=DHLPlSession)
    pl_session.async_refresh = AsyncMock(return_value="a.b.c")
    return session, pl_session


async def test_async_get_incoming_merges_own_and_observed_deduplicating_by_number():
    session, pl_session = _pl_session(
        incoming={"shipments": [{"shipmentNumber": "1", "status": "TT_DOR"}]},
        observed=[{"shipmentNumber": "1", "status": "TT_LK"}, {"shipmentNumber": "2", "status": "TT_LK"}],
    )

    elements = await async_get_incoming(session, pl_session, "device-1")

    by_number = {e["shipmentNumber"]: e for e in elements}
    assert set(by_number) == {"1", "2"}
    # the own-inbox version of a duplicate number wins over the observed one
    assert by_number["1"]["status"] == "TT_DOR"


async def test_async_get_incoming_raises_dhl_auth_error_on_expired_list_session():
    session, pl_session = _pl_session(incoming={}, incoming_status=401)

    with pytest.raises(DHLAuthError):
        await async_get_incoming(session, pl_session, "device-1")


async def test_async_get_incoming_raises_dhl_auth_error_on_expired_observed_session():
    session, pl_session = _pl_session(
        incoming={"shipments": []}, observed=[], observed_status=403,
    )

    with pytest.raises(DHLAuthError):
        await async_get_incoming(session, pl_session, "device-1")


async def test_async_get_incoming_raises_dhl_api_error_on_other_list_failure():
    session, pl_session = _pl_session(incoming={}, incoming_status=500)

    with pytest.raises(DHLApiError):
        await async_get_incoming(session, pl_session, "device-1")
