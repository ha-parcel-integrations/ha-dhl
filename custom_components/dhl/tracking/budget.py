"""The request budget the Express app backend's abuse heuristic grants.

Modelled on ha-ups's ``RequestBudget`` (a token bucket, not a fixed delay) —
the trip threshold measured inconsistent (two of three timed trials tripped
on the *second* call), so pacing has to be accounted rather than merely
delayed. Persisted by the coordinator, like ha-ups's: a budget that starts
full on every boot would let each restart spend a request the backend never
granted.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class RequestBudget:
    """A refilling allowance of requests."""

    capacity: int
    refill_seconds: float
    tokens: float = 0.0
    updated_utc: float = 0.0

    def __post_init__(self) -> None:
        """Start a fresh budget at full capacity."""
        if not self.updated_utc:
            self.tokens = float(self.capacity)
            self.updated_utc = time.time()

    def _accrue(self, now: float) -> None:
        elapsed = now - self.updated_utc
        if elapsed < 0:
            self.updated_utc = now
            return
        if elapsed < self.refill_seconds:
            return
        earned = elapsed // self.refill_seconds
        self.tokens = min(float(self.capacity), self.tokens + earned)
        if self.tokens >= self.capacity:
            self.updated_utc = now
        else:
            self.updated_utc += earned * self.refill_seconds

    def available(self, now: float | None = None) -> int:
        """Whole tokens spendable right now."""
        self._accrue(time.time() if now is None else now)
        return int(self.tokens)

    def try_spend(self, now: float | None = None) -> bool:
        """Spend one token, or report that there was none to spend."""
        now = time.time() if now is None else now
        if self.available(now) < 1:
            return False
        self.tokens -= 1
        if self.updated_utc < now:
            self.updated_utc = now
        return True

    def seconds_until_token(self, now: float | None = None) -> float:
        """How long until at least one token is spendable."""
        now = time.time() if now is None else now
        if self.available(now) >= 1:
            return 0.0
        return max(0.0, self.updated_utc + self.refill_seconds - now)

    def as_dict(self) -> dict[str, Any]:
        """Return the balance in a form the coordinator's Store can hold."""
        return {"tokens": self.tokens, "updated_utc": self.updated_utc}

    @classmethod
    def from_dict(
        cls, stored: Any, capacity: int, refill_seconds: float
    ) -> RequestBudget:
        """Restore a balance, falling back to a full one on anything odd.

        Capacity and refill come from the constants, never from disk: a stored
        refill interval would silently outlive the measurement that set it.
        """
        budget = cls(capacity=capacity, refill_seconds=refill_seconds)
        if not isinstance(stored, dict):
            return budget
        tokens = stored.get("tokens")
        updated = stored.get("updated_utc")
        if not isinstance(tokens, (int, float)) or not isinstance(
            updated, (int, float)
        ):
            return budget
        if updated <= 0 or tokens < 0:
            return budget
        budget.tokens = min(float(capacity), float(tokens))
        budget.updated_utc = float(updated)
        return budget
