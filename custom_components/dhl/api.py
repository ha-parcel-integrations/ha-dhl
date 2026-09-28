"""Compatibility import for the account source's transport client.

New code imports from :mod:`.account.client`; this module preserves the
pre-tracking-flow public import path (``custom_components.dhl.api``) for
custom automations and any test still patching it there.
"""
from .account.client import DHLApiClient, DHLApiError, DHLAuthError

__all__ = ["DHLApiClient", "DHLApiError", "DHLAuthError"]
