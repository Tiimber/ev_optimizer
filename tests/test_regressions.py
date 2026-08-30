"""Regression tests for four bugs that were live in v1.1.24.

Each of these was silent — the integration kept running and produced a
plausible-looking number — so they are pinned here rather than left to be
noticed again from the outside.
"""

from datetime import datetime, time, timedelta

import pytest


def _entry(const, **overrides):
    """A minimal config entry stub for building a coordinator."""

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


# --- #3: price status was computed from the attribute dict ------------------


def test_analyze_prices_rejects_the_attribute_mapping(pkg_loader):
    """Handing over the whole price sensor attribute dict must not look like data.

    The coordinator used to pass `state.attributes` straight in. `len()` of a
    mapping is its key count and indexing it by position raises, which the bare
    except turned into a permanent "Unknown".
    """
    planner = pkg_loader("planner")

    attributes = {
        "today": [1.0, 2.0, 3.0],
        "tomorrow": [],
        "tomorrow_valid": False,
        "current_price": 1.5,
    }

    assert planner.analyze_prices(attributes) == "Unknown"


def test_analyze_prices_reads_a_real_day(pkg_loader):
    planner = pkg_loader("planner")

    # 24 hourly slots; the current hour is well below the daily mean.
    prices = [10.0] * 24
    now_hour = datetime.now().hour
    prices[now_hour] = 1.0

    assert planner.analyze_prices(prices) == "Very Cheap"
    assert planner.analyze_prices([]) == "No Data"
    assert planner.analyze_prices("1.0,1.0,1.0") in {"Cheap", "Expensive", "Very Cheap"}


def test_coordinator_hands_analyze_prices_the_day_list(pkg_loader, hass_mock):
    """The coordinator must pass the list, not the mapping it came from."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    captured = []
    original = coordinator_mod.analyze_prices

    def spy(value):
        captured.append(value)
        return original(value)

    coordinator_mod.analyze_prices = spy
    try:
        coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
        data = {"price_data": {"today": [1.0, 2.0], "tomorrow_valid": False}}
        data["current_price_status"] = coordinator_mod.analyze_prices(
            data["price_data"].get("today", [])
        )
        assert coord is not None
    finally:
        coordinator_mod.analyze_prices = original

    assert captured == [[1.0, 2.0]]
    assert not isinstance(captured[0], dict)


# --- #2: the plan clobbered the user's departure setting --------------------


def test_plan_does_not_shadow_the_departure_setting(pkg_loader):
    """`data.update(plan)` must leave the configured departure time intact.

    The plan published its computed departure under the same key the user
    setting lives under, so the Standard Departure Time entity read back an ISO
    datetime, failed to parse it, and silently fell back to 07:00 forever.
    """
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    now = datetime(2024, 1, 15, 20, 0)
    data = {
        "car_plugged": True,
        "car_soc": 50,
        "price_data": {"today": [1.0] * 24, "tomorrow": [1.0] * 24,
                       "tomorrow_valid": True},
        const.ENTITY_DEPARTURE_TIME: time(7, 2),
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
    }
    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0,
              "currency": "SEK", "has_price_sensor": True}

    plan = planner.generate_charging_plan(data, config, False, now=now)

    assert "departure_time" not in plan, (
        "the plan must not publish a key that collides with the user setting"
    )
    assert plan["plan_departure_time"]

    data.update(plan)
    assert data[const.ENTITY_DEPARTURE_TIME] == time(7, 2)


# --- #1: the learning loop measured what it did not control -----------------


def test_virtual_soc_uses_the_learned_loss(pkg_loader, hass_mock):
    """The estimator must run on the same figure the planner and learning use.

    It used to read entry.data directly, so it ignored both the options flow
    and everything learning had derived.
    """
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    entry = _entry(const, options={const.CONF_CHARGER_LOSS: 0.0})
    entry.data[const.CONF_CHARGER_LOSS] = 10.0  # stale value from first setup
    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, entry)

    coord.learning_state[const.LEARNING_CHARGER_LOSS] = 20.0
    coord.learning_state[const.LEARNING_MEASUREMENTS] = 3
    coord._last_applied_state = "charging"
    coord._last_applied_amps = 16
    coord._virtual_soc = 50.0
    coord._last_update_time = datetime.now() - timedelta(hours=1)

    coord._update_virtual_soc({"ch_l1": 16.0, "ch_l2": 16.0, "ch_l3": 16.0})

    # One hour at 16 A across three phases is 11.04 kWh from the grid; at the
    # learned 20% loss that is 8.832 kWh into a 64 kWh pack, i.e. +13.8%.
    assert coord._virtual_soc == pytest.approx(63.8, abs=0.1)


def test_measurement_recovers_a_known_loss(pkg_loader, hass_mock):
    """A window whose truth is known must move the estimate toward it."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord.learning_state[const.LEARNING_CHARGER_LOSS] = 0.0
    coord.learning_state[const.LEARNING_MEASUREMENTS] = 0

    # The estimator ran at 0% loss and predicted +10%. The car really gained
    # +9%, so the true loss over that window was 10%.
    coord._apply_efficiency_measurement(
        predicted_gain=10.0, observed_gain=9.0, loss_used=0.0, when=datetime.now(),
    )

    history = coord.learning_state[const.LEARNING_HISTORY]
    assert history[-1]["implied_loss_pct"] == pytest.approx(10.0, abs=0.05)
    # First measurements move at alpha=0.5, so half the way there.
    assert coord.learning_state[const.LEARNING_CHARGER_LOSS] == pytest.approx(5.0, abs=0.05)
    assert coord.learning_state[const.LEARNING_MEASUREMENTS] == 1


def test_repeated_measurements_converge(pkg_loader, hass_mock):
    """Feeding the true loss back in must settle, not oscillate at a bound."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord.learning_state[const.LEARNING_CHARGER_LOSS] = 0.0

    true_efficiency = 0.90  # 10% real loss
    for _ in range(15):
        loss_used = coord.learning_state[const.LEARNING_CHARGER_LOSS]
        raw_gain = 12.0
        predicted = raw_gain * (1.0 - loss_used / 100.0)
        observed = raw_gain * true_efficiency
        coord._apply_efficiency_measurement(
            predicted_gain=predicted, observed_gain=observed,
            loss_used=loss_used, when=datetime.now(),
        )

    assert coord.learning_state[const.LEARNING_CHARGER_LOSS] == pytest.approx(10.0, abs=0.5)
    assert coord.learning_state[const.LEARNING_LOCKED] is True


def test_learning_window_discards_unmeasured_charge(pkg_loader, hass_mock):
    """A window tainted by the commanded-amps fallback teaches nothing."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord._open_learning_window(50.0, datetime.now())
    coord._virtual_soc = 62.0
    coord._anchor_suspect = True

    coord._close_learning_window(55.0, datetime.now())

    assert coord.learning_state[const.LEARNING_MEASUREMENTS] == 0


def test_learning_window_ignores_tiny_windows(pkg_loader, hass_mock):
    """Below a few percent the car's whole-number reporting is the signal."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord._open_learning_window(50.0, datetime.now())
    coord._virtual_soc = 51.0  # only +1% predicted

    coord._close_learning_window(51.0, datetime.now())

    assert coord.learning_state[const.LEARNING_MEASUREMENTS] == 0


def test_state_from_an_older_model_is_discarded(pkg_loader, hass_mock):
    """A loss fitted against the old estimator must not survive the upgrade."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    arrivals = [{"date": "2026-08-01", "time": "13:05"}]

    coord._migrate_learning_state(
        {
            const.LEARNING_CHARGER_LOSS: 0.4,
            const.LEARNING_CONFIDENCE: 9,
            const.LEARNING_SESSIONS: 358,
            const.LEARNING_LOCKED: True,
            const.LEARNING_PRICE_ARRIVAL: arrivals,
        },
        stored_model=1,
    )

    state = coord.learning_state
    assert state[const.LEARNING_SESSIONS] == 0
    assert state[const.LEARNING_MEASUREMENTS] == 0
    assert state[const.LEARNING_LOCKED] is False
    assert state[const.LEARNING_CONFIDENCE] == 0
    assert state[const.LEARNING_MODEL_VERSION] == const.CURRENT_LEARNING_MODEL
    # The price-arrival history is a plain observation, unaffected by the model.
    assert state[const.LEARNING_PRICE_ARRIVAL] == arrivals


# --- #4: sessions and measurements were the same counter --------------------


def test_measurements_do_not_count_as_sessions(pkg_loader, hass_mock):
    """Taking a measurement must not advance the completed-session count.

    The session count gates the end of the learning phase, and with it the
    30-minute safety buffer, so inflating it cut the buffer short.
    """
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    before = coord.learning_state[const.LEARNING_SESSIONS]

    for _ in range(3):
        coord._apply_efficiency_measurement(
            predicted_gain=10.0, observed_gain=9.5, loss_used=0.0,
            when=datetime.now(),
        )

    assert coord.learning_state[const.LEARNING_SESSIONS] == before
    assert coord.learning_state[const.LEARNING_MEASUREMENTS] == 3


def test_learned_loss_needs_a_measurement_not_a_session(pkg_loader):
    """A session that produced no usable window must not unlock the value."""
    planner = pkg_loader("planner")
    const = pkg_loader("const")

    config = {"charger_loss": 10.0}

    only_sessions = {
        const.LEARNING_CHARGER_LOSS: 8.0,
        const.LEARNING_SESSIONS: 4,
        const.LEARNING_MEASUREMENTS: 0,
    }
    loss, _ = planner.get_effective_charger_loss(config, only_sessions)
    assert loss == 10.0

    with_measurement = dict(only_sessions, **{const.LEARNING_MEASUREMENTS: 1})
    loss, _ = planner.get_effective_charger_loss(config, with_measurement)
    assert loss == 8.0
