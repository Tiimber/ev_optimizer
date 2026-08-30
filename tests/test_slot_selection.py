"""Slot-selection preferences. Both default off; the default must not change."""

from datetime import datetime, timedelta

import pytest


def _window(prices, start=None, interval_min=60):
    start = start or datetime(2024, 1, 15, 18, 0)
    return [
        {
            "start": start + timedelta(minutes=i * interval_min),
            "end": start + timedelta(minutes=(i + 1) * interval_min),
            "price": p,
        }
        for i, p in enumerate(prices)
    ]


def test_default_is_the_cheapest_slots_wherever_they_fall(pkg_loader):
    planner = pkg_loader("planner")

    window = _window([5.0, 1.0, 9.0, 1.0, 9.0, 1.0])
    chosen = planner.select_charging_slots(window, 3)

    assert sorted(s["price"] for s in chosen) == [1.0, 1.0, 1.0]


def test_default_breaks_ties_towards_the_earliest(pkg_loader):
    """Unchanged behaviour: equal prices resolve to the earlier slot."""
    planner = pkg_loader("planner")

    window = _window([2.0, 2.0, 2.0, 2.0])
    chosen = planner.select_charging_slots(window, 2)

    assert [s["start"].hour for s in chosen] == [18, 19]


def test_prefer_late_only_moves_ties(pkg_loader):
    """It must never buy a more expensive slot to sit later."""
    planner = pkg_loader("planner")

    window = _window([2.0, 2.0, 2.0, 2.0])
    late = planner.select_charging_slots(window, 2, prefer_late=True)
    assert sorted(s["start"].hour for s in late) == [20, 21]

    # A genuinely cheaper early slot still wins.
    window = _window([1.0, 5.0, 5.0, 5.0])
    late = planner.select_charging_slots(window, 2, prefer_late=True)
    assert min(s["price"] for s in late) == 1.0
    assert 18 in [s["start"].hour for s in late]


def test_prefer_late_costs_nothing(pkg_loader):
    """Same total price, later placement — that is the whole claim."""
    planner = pkg_loader("planner")

    window = _window([3.0, 1.0, 1.0, 1.0, 1.0, 3.0])
    default = planner.select_charging_slots(window, 2)
    late = planner.select_charging_slots(window, 2, prefer_late=True)

    assert sum(s["price"] for s in default) == sum(s["price"] for s in late)
    assert max(s["start"] for s in late) > max(s["start"] for s in default)


def test_contiguous_picks_one_unbroken_run(pkg_loader):
    planner = pkg_loader("planner")

    # Cheapest individual slots are scattered; cheapest run of 3 is 3.0+3.0+3.0.
    window = _window([1.0, 9.0, 1.0, 9.0, 3.0, 3.0, 3.0, 9.0, 1.0])
    chosen = planner.select_charging_slots(window, 3, contiguous=True)

    starts = sorted(s["start"] for s in chosen)
    gaps = {(b - a).total_seconds() for a, b in zip(starts, starts[1:])}
    assert gaps == {3600.0}, "must be one unbroken run"
    assert sum(s["price"] for s in chosen) == pytest.approx(9.0)


def test_contiguous_costs_more_than_cherry_picking(pkg_loader):
    """The honest trade: fewer start/stops, a higher bill."""
    planner = pkg_loader("planner")

    window = _window([1.0, 9.0, 1.0, 9.0, 3.0, 3.0, 3.0, 9.0, 1.0])
    scattered = planner.select_charging_slots(window, 3)
    block = planner.select_charging_slots(window, 3, contiguous=True)

    assert sum(s["price"] for s in scattered) == pytest.approx(3.0)
    assert sum(s["price"] for s in block) == pytest.approx(9.0)


def test_contiguous_handles_a_window_shorter_than_the_need(pkg_loader):
    planner = pkg_loader("planner")

    window = _window([1.0, 2.0])
    assert len(planner.select_charging_slots(window, 5, contiguous=True)) == 2


def test_empty_inputs(pkg_loader):
    planner = pkg_loader("planner")

    assert planner.select_charging_slots([], 3) == []
    assert planner.select_charging_slots(_window([1.0]), 0) == []


def test_plan_defaults_leave_selection_unchanged(pkg_loader):
    """End to end: an install that sets neither option plans as before."""
    from datetime import time as dtime

    planner = pkg_loader("planner")
    const = pkg_loader("const")

    now = datetime(2024, 1, 15, 18, 0)
    prices = [9.0] * 24
    for hour in (2, 3, 4):
        prices[hour] = 1.0

    data = {
        "car_plugged": True,
        "car_soc": 50,
        "price_data": {"today": prices, "tomorrow": prices, "tomorrow_valid": True},
        const.ENTITY_DEPARTURE_TIME: dtime(7, 0),
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
    }
    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0,
              "currency": "SEK", "has_price_sensor": True}

    plan = planner.generate_charging_plan(dict(data), dict(config), False, now=now)
    active = [s for s in plan["charging_schedule"] if s["active"]]

    assert active, "should have scheduled something"
    assert all(datetime.fromisoformat(s["start"]).hour in (2, 3, 4) for s in active), \
        "default selection still takes the cheap night hours"
