"""Delivery-window helpers shared by the account and tracking sources."""
from __future__ import annotations

from datetime import datetime, tzinfo

from homeassistant.util import dt as dt_util


def end_of_day(moment: str, zone: tzinfo | None = None) -> str | None:
    """Return 23:59:59 on ``moment``'s day in ``zone`` (Home Assistant's own).

    The day is the recipient's: a moment DHL gives in UTC can fall on another
    calendar day there. A moment without an offset keeps its own day.
    """
    try:
        parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(zone or dt_util.get_default_time_zone())
    return parsed.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
