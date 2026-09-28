"""Shared fixtures for the tracking-source tests."""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def fetch_hamta():
    """Keep the Freight fallback offline; a test sets ``return_value`` to use it."""
    with patch(
        "custom_components.dhl.tracking.coordinator.async_fetch_hamta",
        AsyncMock(return_value={}),
    ) as mock:
        yield mock
