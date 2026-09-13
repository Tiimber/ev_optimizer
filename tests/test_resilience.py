"""Tier-1 hardening: the update path, plan sizing, and the calendar cache.

A running theme here is partial setups. Not every installation configures every
optional entity, so each of these paths is tested with the sensors present *and*
absent — an install with neither charger power nor per-phase current sensors
must behave exactly as it did before.
"""

import asyncio
from datetime import datetime, time, timedelta

import pytest


def _entry(const, **overrides):
    class Entry:
        def __init__(self):
            self.entry_id = "test"
            self.options = overrides.pop("options", {})
            self.data = {
                const.CONF_CAR_SOC_SENSOR: "sensor.car_soc",
                const.CONF_CAR_PLUGGED_SENSOR: "binary_sensor.plugged",
                const.CONF_MAX_FUSE: const.DEFAULT_MAX_FUSE,
                const.CONF_CHARGER_LOSS: const.DEFAULT_LOSS,
                const.CONF_CAR_CAPACITY: const.DEFAULT_CAPACITY,
            }
            self.data.update(overrides)

    return Entry()


class _State:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


# --- plan sizing: the fallback chain -----------------------------------------


def test_power_estimate_falls_back_to_the_fuse(pkg_loader):
    """No observations at all — behave exactly as before this feature existed."""
    planner = pkg_loader("planner")

    config = {"max_fuse": 20.0}
    value, source = planner.get_charging_power_estimate(config, {})

    assert value == pytest.approx(11.0)  # min(3*230*20/1000, 11.0)
    assert source == "fuse"


def test_power_estimate_ignores_a_thin_sample_count(pkg_loader):
    """A handful of samples is not yet evidence; keep the fuse figure."""
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    config = {"max_fuse": 20.0}
    state = {
        const.LEARNING_CHARGE_POWER: 7.4,
        const.LEARNING_POWER_SAMPLES: const.MIN_POWER_SAMPLES - 1,
    }
    value, source = planner.get_charging_power_estimate(config, state)

    assert value == pytest.approx(11.0)
    assert source == "fuse"


def test_power_estimate_uses_observations_once_established(pkg_loader):
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    config = {"max_fuse": 20.0}
    state = {
        const.LEARNING_CHARGE_POWER: 9.2,
        const.LEARNING_POWER_SAMPLES: const.MIN_POWER_SAMPLES,
    }
    value, source = planner.get_charging_power_estimate(config, state)

    assert value == pytest.approx(9.2)
    assert "observed" in source


def test_power_estimate_never_exceeds_the_fuse(pkg_loader):
    """A sensor reading high must not talk the plan into fewer slots."""
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    config = {"max_fuse": 20.0}
    state = {
        const.LEARNING_CHARGE_POWER: 22.0,
        const.LEARNING_POWER_SAMPLES: 500,
    }
    value, _ = planner.get_charging_power_estimate(config, state)

    assert value == pytest.approx(11.0)


def test_lower_observed_power_buys_more_slots(pkg_loader):
    """The point of the whole thing: a throttled setup gets a longer plan."""
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    now = datetime(2024, 1, 15, 18, 0)
    base = {
        "car_plugged": True,
        "car_soc": 40,
        "price_data": {"today": [1.0] * 24, "tomorrow": [1.0] * 24,
                       "tomorrow_valid": True},
        const.ENTITY_DEPARTURE_TIME: time(7, 0),
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
    }
    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0,
              "currency": "SEK", "has_price_sensor": True}

    on_fuse = planner.generate_charging_plan(dict(base), config, False, now=now)
    throttled = planner.generate_charging_plan(
        dict(base), config, False,
        learning_state={
            const.LEARNING_CHARGE_POWER: 7.0,
            const.LEARNING_POWER_SAMPLES: 200,
        },
        now=now,
    )

    fuse_slots = sum(1 for s in on_fuse["charging_schedule"] if s["active"])
    throttled_slots = sum(1 for s in throttled["charging_schedule"] if s["active"])

    assert throttled_slots > fuse_slots
    assert throttled["charge_power_kw"] == pytest.approx(7.0)
    assert on_fuse["charge_power_source"] == "fuse"


# --- power observation: works with, and without, each sensor -----------------


def test_observes_power_from_a_configured_power_sensor(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    hass_mock.states = type("S", (), {"get": lambda self, e: {
        "sensor.charger_power": _State("7400", {"unit_of_measurement": "W"}),
    }.get(e)})()

    coord = coordinator_mod.EVSmartChargerCoordinator(
        hass_mock, _entry(const, **{const.CONF_CHARGER_POWER_SENSOR: "sensor.charger_power"})
    )
    coord._last_applied_state = "charging"
    coord._observe_charge_power({})

    assert coord.learning_state[const.LEARNING_CHARGE_POWER] == pytest.approx(7.4)
    assert coord.learning_state[const.LEARNING_POWER_SAMPLES] == 1


def test_observes_power_from_phase_currents_when_no_power_sensor(pkg_loader, hass_mock):
    """Summing phases is right for one-phase and three-phase setups alike."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord._last_applied_state = "charging"

    # Single-phase charger: only L1 draws.
    coord._observe_charge_power({"ch_l1": 16.0, "ch_l2": 0.0, "ch_l3": 0.0})
    assert coord.learning_state[const.LEARNING_CHARGE_POWER] == pytest.approx(3.68)

    coord.learning_state[const.LEARNING_POWER_SAMPLES] = 0
    coord.learning_state[const.LEARNING_CHARGE_POWER] = None
    # Three-phase: all three draw.
    coord._observe_charge_power({"ch_l1": 16.0, "ch_l2": 16.0, "ch_l3": 16.0})
    assert coord.learning_state[const.LEARNING_CHARGE_POWER] == pytest.approx(11.04)


def test_observes_nothing_without_any_sensors(pkg_loader, hass_mock):
    """A setup with neither kind of sensor simply never learns — and must not raise."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord._last_applied_state = "charging"
    coord._observe_charge_power({})

    assert coord.learning_state[const.LEARNING_POWER_SAMPLES] == 0
    assert not coord.learning_state[const.LEARNING_CHARGE_POWER]


def test_ignores_implausible_and_idle_power(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord._last_applied_state = "charging"

    coord._observe_charge_power({"ch_l1": 0.5})       # trickle, below the floor
    coord._observe_charge_power({"ch_l1": 500.0})     # nonsense, above the ceiling
    assert coord.learning_state[const.LEARNING_POWER_SAMPLES] == 0

    coord._last_applied_state = "paused"
    coord._observe_charge_power({"ch_l1": 16.0, "ch_l2": 16.0, "ch_l3": 16.0})
    assert coord.learning_state[const.LEARNING_POWER_SAMPLES] == 0


def test_observed_power_survives_a_pure_estimator_change(pkg_loader, hass_mock):
    """It measures the hardware, not the loss estimator, so it carries across.

    Model 3 onwards: a migration that only changes how the *loss* is fitted
    leaves the power observation alone, because nothing about how it was
    measured has changed.
    """
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord._migrate_learning_state(
        {
            const.LEARNING_CHARGER_LOSS: 0.4,
            const.LEARNING_SESSIONS: 358,
            const.LEARNING_CHARGE_POWER: 9.6,
            const.LEARNING_POWER_SAMPLES: 400,
        },
        stored_model=3,
    )

    assert coord.learning_state[const.LEARNING_CHARGE_POWER] == pytest.approx(9.6)
    assert coord.learning_state[const.LEARNING_POWER_SAMPLES] == 400
    assert coord.learning_state[const.LEARNING_SESSIONS] == 0


def test_observed_power_is_discarded_when_the_measurement_source_changed(
    pkg_loader, hass_mock
):
    """Crossing model 3 means the sensors themselves were wrong or replaced.

    A charger that publishes both its *allocated* current and its actual draw
    makes the two easy to confuse, and the allocated figure is a ceiling rather
    than a measurement. Averaging it yields a confident, wrong number that
    thousands of stored samples would defend against the first correct one, so
    it has to be discarded with everything else.
    """
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord._migrate_learning_state(
        {
            const.LEARNING_CHARGER_LOSS: 0.4,
            const.LEARNING_CHARGE_POWER: 11.04,
            const.LEARNING_POWER_SAMPLES: 1986,
        },
        stored_model=2,
    )

    assert not coord.learning_state.get(const.LEARNING_CHARGE_POWER)
    assert coord.learning_state[const.LEARNING_POWER_SAMPLES] == 0


# --- degraded planning -------------------------------------------------------


def test_degraded_plan_reuses_the_last_schedule(pkg_loader, hass_mock):
    """Falling back keeps the safety loop alive without charging open-endedly."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    now = datetime.now()
    coord.data = {
        "charging_schedule": [
            {
                "start": (now - timedelta(minutes=30)).isoformat(),
                "end": (now + timedelta(minutes=30)).isoformat(),
                "price": 1.0,
                "active": True,
            }
        ],
        "planned_target_soc": 90,
    }

    plan = coord._degraded_plan()

    assert plan["should_charge_now"] is True  # inside a still-active slot
    assert plan["planned_target_soc"] == 90
    assert "failed" in plan["charging_summary"]


def test_degraded_plan_expires_with_its_slots(pkg_loader, hass_mock):
    """A stale plan must stop charging on its own if planning never recovers."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    now = datetime.now()
    coord.data = {
        "charging_schedule": [
            {
                "start": (now - timedelta(hours=3)).isoformat(),
                "end": (now - timedelta(hours=2)).isoformat(),
                "price": 1.0,
                "active": True,
            }
        ],
    }

    assert coord._degraded_plan()["should_charge_now"] is False


def test_degraded_plan_without_history_does_not_charge(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord.data = {}

    plan = coord._degraded_plan()
    assert plan["should_charge_now"] is False
    assert plan["charging_schedule"] == []


# --- calendar cache ----------------------------------------------------------


def test_calendar_not_configured_makes_no_call(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    events = asyncio.run(coord._async_get_calendar_events())

    assert events == []
    assert hass_mock.services.calls == []


def test_calendar_is_cached_between_updates(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    async def fake_call(domain, service, data, blocking=False, return_response=False):
        hass_mock.services.calls.append((domain, service, data))
        return {"calendar.trips": {"events": [{"summary": "Trip 80%"}]}}

    hass_mock.services.async_call = fake_call

    coord = coordinator_mod.EVSmartChargerCoordinator(
        hass_mock, _entry(const, **{const.CONF_CALENDAR_ENTITY: "calendar.trips"})
    )

    first = asyncio.run(coord._async_get_calendar_events())
    second = asyncio.run(coord._async_get_calendar_events())

    assert first == second == [{"summary": "Trip 80%"}]
    assert len(hass_mock.services.calls) == 1, "second read should come from cache"


def test_calendar_failure_keeps_the_last_known_events(pkg_loader, hass_mock):
    """An empty list is not a safe substitute — it silently rewrites departure."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    responses = [{"calendar.trips": {"events": [{"summary": "Trip 80%"}]}}]

    async def flaky_call(domain, service, data, blocking=False, return_response=False):
        if responses:
            return responses.pop()
        raise RuntimeError("calendar backend unreachable")

    hass_mock.services.async_call = flaky_call

    coord = coordinator_mod.EVSmartChargerCoordinator(
        hass_mock, _entry(const, **{const.CONF_CALENDAR_ENTITY: "calendar.trips"})
    )

    good = asyncio.run(coord._async_get_calendar_events())
    coord._calendar_cache_time = datetime.now() - timedelta(hours=1)  # force a re-fetch
    after_failure = asyncio.run(coord._async_get_calendar_events())

    assert good == [{"summary": "Trip 80%"}]
    assert after_failure == good


# --- load balancing without charger current sensors --------------------------


def test_limiter_fallback_treats_the_limit_as_per_phase(pkg_loader):
    """The Zaptec limiter is amps per phase, not a total to split between them."""
    planner = pkg_loader("planner")

    # House draws 4 A/phase; the charger is commanded to 15 A and is drawing it,
    # so P1 reads 19 A/phase. With no ch_l* sensors the limiter is the only clue
    # to how much of that is the charger.
    data = {
        "p1_l1": 19.0, "p1_l2": 19.0, "p1_l3": 19.0,
        "ch_l1": 0.0, "ch_l2": 0.0, "ch_l3": 0.0,
        "zap_limit_value": 15.0,
    }
    available = planner.calculate_load_balancing(data, max_fuse=20.0)

    # 20 - 4 (real house) - 1 (buffer) = 15 A
    assert available == pytest.approx(15.0)


def test_limiter_fallback_does_not_oscillate(pkg_loader):
    """Regression: under-subtracting the charger drove a start/stop loop.

    Treating the per-phase limiter as a total made the charger's own draw look
    like house load, so every cycle in which charging was allowed produced a
    reading that forbade it, and vice versa — indefinitely, on a house with
    plenty of headroom.
    """
    import math

    planner = pkg_loader("planner")

    max_fuse, house = 20.0, 4.0
    commanded = 0.0
    decisions = []

    for _ in range(8):
        p1 = house + commanded
        available = planner.calculate_load_balancing(
            {
                "p1_l1": p1, "p1_l2": p1, "p1_l3": p1,
                "ch_l1": 0.0, "ch_l2": 0.0, "ch_l3": 0.0,
                "zap_limit_value": commanded,
            },
            max_fuse,
        )
        amps = math.floor(available)
        charging = amps >= 6
        decisions.append(charging)
        commanded = float(amps) if charging else 0.0

    assert all(decisions), f"charging should stay on, got {decisions}"
    assert commanded == pytest.approx(15.0)
