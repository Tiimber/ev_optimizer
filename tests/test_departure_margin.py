"""Departure margin: plan to finish ahead of departure, when there is time to.

2026-11-17: the plan needed 03:15-05:20 at full power for a 05:20 departure.
House load on one phase throttled and then paused the charger for ~45 min,
and the car left at 67 % instead of 80 %. A plan with no slack cannot absorb
load balancing.
"""

from datetime import datetime, timedelta


def _window(prices, start=datetime(2026, 11, 17, 0, 0), interval_min=15):
    return [
        {
            "start": start + timedelta(minutes=i * interval_min),
            "end": start + timedelta(minutes=(i + 1) * interval_min),
            "price": p,
        }
        for i, p in enumerate(prices)
    ]


def test_zero_margin_is_unchanged(pkg_loader):
    planner = pkg_loader("planner")
    w = _window([1.0] * 8)
    out, applied = planner.window_with_departure_margin(w, w[-1]["end"], 0, 4)
    assert out == w and not applied


def test_margin_drops_the_last_slots(pkg_loader):
    planner = pkg_loader("planner")
    w = _window([1.0] * 8)  # 00:00-02:00
    out, applied = planner.window_with_departure_margin(w, w[-1]["end"], 45, 4)
    assert applied
    assert len(out) == 5
    assert out[-1]["end"] == datetime(2026, 11, 17, 1, 15)


def test_margin_with_late_preference_finishes_early(pkg_loader):
    planner = pkg_loader("planner")
    w = _window([1.0] * 8)
    out, _ = planner.window_with_departure_margin(w, w[-1]["end"], 45, 4)
    chosen = planner.select_charging_slots(out, 4, prefer_late=True)
    assert max(s["end"] for s in chosen) == datetime(2026, 11, 17, 1, 15)


def test_not_enough_time_falls_back_to_the_full_window(pkg_loader):
    planner = pkg_loader("planner")
    w = _window([1.0] * 8)
    out, applied = planner.window_with_departure_margin(w, w[-1]["end"], 45, 6)
    assert out == w and not applied
