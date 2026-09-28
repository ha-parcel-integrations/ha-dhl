"""The pre-restructure top-level import paths must keep resolving.

``api.py``/``coordinator.py``/``parcels.py`` at the domain root are
compatibility re-exports onto ``account/*`` — this only asserts the objects
resolved through them are the *same* objects the real modules define, not a
stale copy.
"""
from custom_components.dhl import api, coordinator, parcels
from custom_components.dhl.account import client as account_client
from custom_components.dhl.account import coordinator as account_coordinator
from custom_components.dhl.account import parcels as account_parcels


def test_api_shim_reexports_the_same_classes():
    assert api.DHLApiClient is account_client.DHLApiClient
    assert api.DHLApiError is account_client.DHLApiError
    assert api.DHLAuthError is account_client.DHLAuthError


def test_coordinator_shim_reexports_the_same_objects():
    assert coordinator.DHLCoordinator is account_coordinator.DHLCoordinator
    assert coordinator.compute_poll_interval is account_coordinator.compute_poll_interval


def test_parcels_shim_reexports_the_same_function():
    assert parcels.normalize_parcel is account_parcels.normalize_parcel
    assert parcels.sort_parcels_by_ts is account_parcels.sort_parcels_by_ts
