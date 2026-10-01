"""Shared fixtures for the API-source tests."""
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.dhl.api import parcels as api_parcels


@pytest.fixture(autouse=True)
def reset_api_one_shot_warnings():
    """The one-shot WARNING flags are module-level; keep them per test."""
    api_parcels._warned_status_codes.clear()
    api_parcels._warned_weight_units.clear()
    api_parcels._warned_multiple_shipments = False
    yield


@pytest.fixture(autouse=True)
def api_sleep():
    """Never actually wait out the 5 s request spacing in tests."""
    with patch(
        "custom_components.dhl.api.coordinator.asyncio.sleep", AsyncMock()
    ) as sleep:
        yield sleep
