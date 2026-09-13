"""Savings put a number on what the scheduling actually bought."""

from datetime import datetime, time as dtime, timedelta

import pytest


def _plan(planner, const, prices, now=None, **overrides):
    now = now or datetime(2024, 1, 15, 18, 0)
    data = {
        "car_plugged": True,
        "car_soc": 40,
        "price_data": {"today": prices, "tomorrow": prices, "tomorrow_valid": True},
        const.ENTITY_DEPARTURE_TIME: dtime(7, 0),
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
    }
    data.update(overrides)
    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0,
              "currency": "SEK", "has_price_sensor": True}
    return planner.generate_charging_plan(data, config, False, now=now)


def test_savings_reported_against_charging_now(pkg_loader):
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    prices = [9.0] * 24
    for hour in (2, 3, 4, 5):
        prices[hour] = 1.0

    plan = _plan(planner, const, prices)

    assert plan["planned_cost"] > 0
    assert plan["baseline_cost"] > plan["planned_cost"]
    assert plan["estimated_savings"] == pytest.approx(
        plan["baseline_cost"] - plan["planned_cost"], abs=0.01
    )
    assert "Saved vs charging now" in plan["charging_summary"]


def test_no_savings_when_prices_are_flat(pkg_loader):
    """A flat curve means waiting bought nothing, and should say so."""
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    plan = _plan(planner, const, [2.0] * 24)

    assert plan["estimated_savings"] == pytest.approx(0.0, abs=0.01)
    assert "Saved vs charging now" not in plan["charging_summary"]


def test_savings_include_fees_and_vat_on_both_sides(pkg_loader):
    """Both figures must be costed on the same terms, or the delta is fiction."""
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    prices = [9.0] * 24
    for hour in (2, 3, 4, 5):
        prices[hour] = 1.0

    plain = _plan(planner, const, prices)
    with_fees = _plan(
        planner, const, prices,
        **{const.ENTITY_PRICE_EXTRA_FEE: 0.5, const.ENTITY_PRICE_VAT: 25.0},
    )

    # A per-kWh fee applies to the same energy either way, so it cancels out of
    # the difference; VAT scales it. Savings must therefore grow by 25%, not by
    # the fee.
    assert with_fees["estimated_savings"] == pytest.approx(
        plain["estimated_savings"] * 1.25, rel=0.02
    )


def test_cost_of_slots_stops_once_the_energy_is_covered(pkg_loader):
    planner = pkg_loader("planner")

    start = datetime(2024, 1, 15, 18, 0)
    slots = [
        {"start": start + timedelta(hours=i), "end": start + timedelta(hours=i + 1),
         "price": p}
        for i, p in enumerate([1.0, 2.0, 100.0])
    ]

    # 15 kWh at 10 kWh per slot uses the first slot fully and half the second.
    cost = planner.cost_of_slots(slots, kwh_to_pull=15.0, kwh_per_slot_max=10.0)

    assert cost == pytest.approx(10.0 * 1.0 + 5.0 * 2.0)


def test_departure_source_is_data_not_parsed_text(pkg_loader):
    """A maintenance-mode plan must still name its departure source correctly.

    It used to be inferred by looking for "(Calendar)" in the summary, which
    only appears on the detailed scheduling summary — so a plan that returned
    early reported "standard departure time" while actually following a
    calendar event.
    """
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    now = datetime(2026, 8, 30, 16, 44)
    events = [{"start": "2026-08-31T05:20:00+02:00", "summary": "Climate"}]

    # SoC already above target: the plan short-circuits into maintenance mode.
    plan = _plan(
        planner, const, [2.0] * 24, now=now,
        car_soc=95, calendar_events=events,
        **{const.ENTITY_TARGET_SOC: 80},
    )

    assert "Maintenance mode" in plan["charging_summary"]
    assert plan["departure_source"] == "calendar"
    assert plan["plan_departure_time"].startswith("2026-08-31T05:20")


def test_departure_source_names_the_override(pkg_loader):
    from datetime import time as dtime

    planner = pkg_loader("planner")
    const = pkg_loader("const")

    plan = _plan(
        planner, const, [2.0] * 24,
        **{const.ENTITY_DEPARTURE_OVERRIDE: dtime(5, 30)},
    )
    assert plan["departure_source"] == "next session override"

    plan = _plan(planner, const, [2.0] * 24)
    assert plan["departure_source"] == "standard departure time"


def test_session_savings_hold_while_the_session_delivers(pkg_loader):
    """Delivery shrinks the remaining job; it must not shrink the headline.

    Each tick the planner costs only the energy still to be pulled, so its
    estimate falls towards zero over the session. Latching keeps the sensor
    answering "what did waiting save me" rather than "what is left to save".
    """
    coordinator = pkg_loader("coordinator")
    track = coordinator.EVSmartChargerCoordinator._track_session_savings
    reset = coordinator.EVSmartChargerCoordinator._reset_session_savings

    class Stub:
        session_savings = None
        session_savings_planned = None
        session_savings_baseline = None
        _session_savings_kwh = 0.0

    c = Stub()

    # Plug-in: the whole job is planned.
    track(c, {"estimated_savings": 18.0, "planned_cost": 30.0,
              "baseline_cost": 48.0, "planned_grid_kwh": 20.0})
    assert c.session_savings == 18.0

    # Halfway through, only half the energy is left to plan for.
    track(c, {"estimated_savings": 9.0, "planned_cost": 15.0,
              "baseline_cost": 24.0, "planned_grid_kwh": 10.0})
    assert c.session_savings == 18.0
    assert c.session_savings_planned == 30.0

    # Finished: nothing left to pull, so nothing left to save.
    track(c, {"estimated_savings": 0.0, "planned_cost": 0.0,
              "baseline_cost": 0.0, "planned_grid_kwh": 0.0})
    assert c.session_savings == 18.0

    # A genuinely bigger job — target raised, or the car reported lower than the
    # estimator assumed — is a new answer, not delivery.
    track(c, {"estimated_savings": 25.0, "planned_cost": 40.0,
              "baseline_cost": 65.0, "planned_grid_kwh": 28.0})
    assert c.session_savings == 25.0

    # Unplugging clears it for the next session.
    reset(c)
    assert c.session_savings is None
    assert c._session_savings_kwh == 0.0


def test_session_savings_ignore_ticks_with_no_plan(pkg_loader):
    """Maintenance mode and no-price ticks publish no cost figures at all."""
    coordinator = pkg_loader("coordinator")
    track = coordinator.EVSmartChargerCoordinator._track_session_savings

    class Stub:
        session_savings = None
        session_savings_planned = None
        session_savings_baseline = None
        _session_savings_kwh = 0.0

    c = Stub()
    track(c, {"charging_summary": "Target reached. Maintenance mode active."})
    assert c.session_savings is None

    track(c, {"estimated_savings": 4.0, "planned_cost": 6.0,
              "baseline_cost": 10.0, "planned_grid_kwh": 5.0})
    assert c.session_savings == 4.0

    # A later tick with no plan must not wipe the latched figure either.
    track(c, {})
    assert c.session_savings == 4.0


def test_plan_publishes_the_energy_its_costs_were_computed_over(pkg_loader):
    """Without it, a shrinking cost is indistinguishable from a shrinking plan."""
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    prices = [9.0] * 24
    for hour in (2, 3, 4, 5):
        prices[hour] = 1.0

    plan = _plan(planner, const, prices)

    assert plan["planned_grid_kwh"] > 0
    # 40% -> 80% of 64 kWh, grossed up by the 10% charger loss in _plan's config
    assert plan["planned_grid_kwh"] == pytest.approx(25.6 / 0.9, abs=0.05)
