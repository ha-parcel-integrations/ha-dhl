"""Compatibility import for the account source's coordinator."""
from .account.coordinator import DHLCoordinator, compute_poll_interval

__all__ = ["DHLCoordinator", "compute_poll_interval"]
