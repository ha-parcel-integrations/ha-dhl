"""API source (DHL's official Unified Shipment Tracking API), and a shim.

This package also keeps the pre-tracking-flow import path
``custom_components.dhl.api`` resolving to the *account* transport client:
``DHLApiClient``/``DHLApiError``/``DHLAuthError`` here always mean the account
source's, never this package's ``DHLUnified…`` names. New code imports from
:mod:`..account.client` directly.
"""
from ..account.client import DHLApiClient, DHLApiError, DHLAuthError

__all__ = ["DHLApiClient", "DHLApiError", "DHLAuthError"]
