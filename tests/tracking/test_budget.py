"""Tests for tracking/budget.py's token bucket."""
from custom_components.dhl.tracking.budget import RequestBudget


def test_starts_full():
    budget = RequestBudget(capacity=1, refill_seconds=100)
    assert budget.available() == 1


def test_try_spend_drains_and_reports_empty():
    budget = RequestBudget(capacity=1, refill_seconds=100, tokens=1.0, updated_utc=1000.0)
    assert budget.try_spend(1000.0) is True
    assert budget.available(1000.0) == 0
    assert budget.try_spend(1000.0) is False


def test_refills_after_the_interval():
    budget = RequestBudget(capacity=1, refill_seconds=100, tokens=1.0, updated_utc=1000.0)
    budget.try_spend(1000.0)
    assert budget.available(1050.0) == 0
    assert budget.available(1100.0) == 1


def test_partial_accrual_below_capacity_advances_the_clock_by_earned_intervals():
    """Below capacity, the refill clock steps forward by whole intervals
    rather than snapping straight to ``now`` — a later partial call must not
    re-grant credit for the interval already accounted for."""
    budget = RequestBudget(capacity=2, refill_seconds=100, tokens=0.0, updated_utc=1000.0)
    assert budget.available(1150.0) == 1
    assert budget.updated_utc == 1100.0
    assert budget.available(1199.0) == 1  # still short of the next interval


def test_seconds_until_token_counts_down():
    budget = RequestBudget(capacity=1, refill_seconds=100, tokens=1.0, updated_utc=1000.0)
    budget.try_spend(1000.0)
    assert budget.seconds_until_token(1040.0) == 60
    assert budget.seconds_until_token(1000.0) > 0


def test_seconds_until_token_is_zero_when_available():
    budget = RequestBudget(capacity=1, refill_seconds=100)
    assert budget.seconds_until_token() == 0.0


def test_capacity_above_one_caps_accrual():
    budget = RequestBudget(capacity=2, refill_seconds=100, tokens=2.0, updated_utc=1000.0)
    budget.try_spend(1000.0)
    budget.try_spend(1000.0)
    # A very long idle period must not bank more than capacity allows.
    assert budget.available(11000.0) == 2


def test_clock_moving_backwards_re_anchors_instead_of_banking_negative_elapsed():
    budget = RequestBudget(capacity=1, refill_seconds=100, tokens=0.0, updated_utc=1000.0)
    assert budget.available(500.0) == 0


def test_try_spend_before_a_refill_advances_the_clock_to_the_spend_time():
    """Spending within the same refill window (no accrual happened) must
    still move the clock forward to the moment of the spend."""
    budget = RequestBudget(capacity=1, refill_seconds=100, tokens=1.0, updated_utc=1000.0)
    assert budget.try_spend(1040.0) is True
    assert budget.updated_utc == 1040.0


def test_try_spend_re_anchors_the_refill_clock_to_the_spend_time():
    """A token spent late (well past when it was earned) must not let the
    next one arrive sooner than a full refill after this spend."""
    budget = RequestBudget(capacity=1, refill_seconds=100, tokens=1.0, updated_utc=1000.0)
    # Token banked at t=1000; spent late at t=1500.
    assert budget.try_spend(1500.0) is True
    assert budget.updated_utc == 1500.0
    assert budget.available(1599.0) == 0
    assert budget.available(1600.0) == 1


def test_as_dict_round_trips_through_from_dict():
    budget = RequestBudget(capacity=1, refill_seconds=2400)
    budget.try_spend(now=budget.updated_utc)
    restored = RequestBudget.from_dict(budget.as_dict(), 1, 2400)
    assert restored.tokens == budget.tokens
    assert restored.updated_utc == budget.updated_utc


def test_from_dict_caps_tokens_at_the_current_capacity():
    restored = RequestBudget.from_dict({"tokens": 5, "updated_utc": 100.0}, 1, 2400)
    assert restored.tokens == 1.0


def test_from_dict_falls_back_to_a_full_budget_on_odd_input():
    for stored in (None, [], {"tokens": "x", "updated_utc": 1.0}, {"tokens": 1, "updated_utc": 0}):
        assert RequestBudget.from_dict(stored, 1, 2400).tokens == 1.0
