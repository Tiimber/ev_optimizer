"""Delivered energy drives the estimate, with a fallback chain beneath it.

The bottom rung — integrating the current we *commanded* — is what this
integration always did, and it is the one that can invent energy that never
flowed. Every rung above it measures reality. All of them are optional, so the
chain is tested from the top and from the bottom.
"""

from datetime import datetime, timedelta

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


def _states(hass, mapping):
    hass.states = type("S", (), {"get": lambda self, e: mapping.get(e)})()


# --- the chain picks the best available source ------------------------------


def test_energy_meter_is_preferred(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    meter = _State("100.0", {"unit_of_measurement": "kWh"})
    _states(hass_mock, {"sensor.charger_energy": meter,
                        "sensor.charger_power": _State("7000", {"unit_of_measurement": "W"})})

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(
        const,
        **{const.CONF_CHARGER_ENERGY_SENSOR: "sensor.charger_energy",
           const.CONF_CHARGER_POWER_SENSOR: "sensor.charger_power"},
    ))

    # First call only establishes the baseline.
    kwh, source = coord._measure_energy_delta({}, hours=0.5)
    assert (kwh, source) == (0.0, "energy")

    meter.state = "103.25"
    kwh, source = coord._measure_energy_delta({}, hours=0.5)
    assert source == "energy"
    assert kwh == pytest.approx(3.25)


def test_energy_meter_reset_does_not_book_a_negative(pkg_loader, hass_mock):
    """A meter that restarts must not subtract a session's worth of energy."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    meter = _State("500.0", {"unit_of_measurement": "kWh"})
    _states(hass_mock, {"sensor.charger_energy": meter})
    coord = coordinator_mod.EVSmartChargerCoordinator(
        hass_mock, _entry(const, **{const.CONF_CHARGER_ENERGY_SENSOR: "sensor.charger_energy"})
    )

    coord._measure_energy_delta({}, 0.5)
    meter.state = "0.0"  # integration reloaded, counter restarted
    kwh, source = coord._measure_energy_delta({}, 0.5)

    assert kwh == 0.0
    assert source == "energy"
    # Re-baselined, so the next interval measures from the new zero.
    meter.state = "1.5"
    assert coord._measure_energy_delta({}, 0.5)[0] == pytest.approx(1.5)


def test_wh_meters_are_converted(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    meter = _State("100000", {"unit_of_measurement": "Wh"})
    _states(hass_mock, {"sensor.charger_energy": meter})
    coord = coordinator_mod.EVSmartChargerCoordinator(
        hass_mock, _entry(const, **{const.CONF_CHARGER_ENERGY_SENSOR: "sensor.charger_energy"})
    )

    coord._measure_energy_delta({}, 0.5)
    meter.state = "102500"
    assert coord._measure_energy_delta({}, 0.5)[0] == pytest.approx(2.5)


def test_falls_through_to_power_then_current_then_commanded(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    _states(hass_mock, {"sensor.charger_power": _State("7000", {"unit_of_measurement": "W"})})
    coord = coordinator_mod.EVSmartChargerCoordinator(
        hass_mock, _entry(const, **{const.CONF_CHARGER_POWER_SENSOR: "sensor.charger_power"})
    )
    kwh, source = coord._measure_energy_delta({}, hours=1.0)
    assert source == "power" and kwh == pytest.approx(7.0)

    # No power sensor: sum the phases (right for one or three phases alike).
    coord2 = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    kwh, source = coord2._measure_energy_delta(
        {"ch_l1": 10.0, "ch_l2": 10.0, "ch_l3": 10.0}, hours=1.0
    )
    assert source == "current" and kwh == pytest.approx(6.9)

    # Nothing measured at all: fall back to what we commanded.
    coord3 = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord3._last_applied_amps = 16
    kwh, source = coord3._measure_energy_delta({}, hours=1.0)
    assert source == "commanded" and kwh == pytest.approx(11.04)


def test_nothing_configured_and_nothing_commanded(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    assert coord._measure_energy_delta({}, hours=1.0) == (0.0, "none")


# --- the drift this was built to remove -------------------------------------


def test_a_car_that_stops_accepting_charge_no_longer_inflates_the_estimate(
    pkg_loader, hass_mock
):
    """With a meter configured, a stalled session adds nothing.

    Previously the estimate kept climbing from the commanded current for as
    long as the charger was told to charge, whether or not the car took it.
    """
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    meter = _State("50.0", {"unit_of_measurement": "kWh"})
    _states(hass_mock, {"sensor.charger_energy": meter, "sensor.car_soc": _State("60.0")})

    coord = coordinator_mod.EVSmartChargerCoordinator(
        hass_mock, _entry(const, **{const.CONF_CHARGER_ENERGY_SENSOR: "sensor.charger_energy"})
    )
    coord._last_applied_state = "charging"
    coord._last_applied_amps = 16
    coord._virtual_soc = 60.0
    coord._last_sensor_soc = 60.0

    # Six half-hour intervals during which the meter never moves.
    for _ in range(6):
        coord._last_update_time = datetime.now() - timedelta(minutes=30)
        coord._update_virtual_soc({"car_soc": 60.0})

    assert coord._virtual_soc == pytest.approx(60.0)
    assert coord.energy_source == "energy"
    assert coord.charge_estimate_drift_active is False


def test_without_a_meter_the_old_drift_is_still_flagged(pkg_loader, hass_mock):
    """The commanded fallback still advances — and still raises the alarm."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    _states(hass_mock, {"sensor.car_soc": _State("60.0")})
    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, _entry(const))
    coord._last_applied_state = "charging"
    coord._last_applied_amps = 16
    coord._virtual_soc = 60.0
    coord._last_sensor_soc = 60.0

    for i in range(4):
        coord._last_update_time = datetime.now() - timedelta(minutes=30)
        coord._update_virtual_soc({"car_soc": 60.0})
        if i == 0:
            # The alarm is deliberately not instant — it waits for the fallback
            # to have been in use for five minutes, since the charger's sensors
            # are allowed to lag briefly. Age it rather than sleeping.
            coord._amps_fallback_since = datetime.now() - timedelta(minutes=6)

    assert coord._virtual_soc > 60.0, "commanded fallback still advances"
    assert coord.energy_source == "commanded"
    assert coord.charge_estimate_drift_active is True
    assert coord._anchor_suspect is True, "must not teach the model from this"


def test_meter_is_rebaselined_while_paused(pkg_loader, hass_mock):
    """Energy metered during a pause must not land on the next session."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    meter = _State("10.0", {"unit_of_measurement": "kWh"})
    _states(hass_mock, {"sensor.charger_energy": meter, "sensor.car_soc": _State("50.0")})
    coord = coordinator_mod.EVSmartChargerCoordinator(
        hass_mock, _entry(const, **{const.CONF_CHARGER_ENERGY_SENSOR: "sensor.charger_energy"})
    )
    coord._virtual_soc = 50.0
    coord._last_sensor_soc = 50.0

    coord._last_applied_state = "paused"
    meter.state = "18.0"  # something else used the meter while we were idle
    coord._update_virtual_soc({"car_soc": 50.0})

    coord._last_applied_state = "charging"
    coord._last_update_time = datetime.now() - timedelta(minutes=30)
    meter.state = "19.0"
    coord._update_virtual_soc({"car_soc": 50.0})

    # Only the 1 kWh delivered while charging counts, not the 8 kWh gap.
    assert coord._virtual_soc == pytest.approx(50.0 + (1.0 / 64.0) * 100.0, abs=0.05)


# --- session totals ----------------------------------------------------------


def test_session_totals_prefer_measured_energy(pkg_loader):
    session_mod = pkg_loader("session_manager")
    mgr = session_mod.SessionManager(None)

    now = datetime.now()
    mgr.current_session = {
        "start_time": now.isoformat(),
        "log": [],
        "history": [
            {"time": now.isoformat(), "soc": 50, "amps": 16, "charging": 1,
             "price": 2.0, "grid_kwh": 0.0, "energy_source": "energy"},
            {"time": (now + timedelta(minutes=30)).isoformat(), "soc": 55,
             "amps": 16, "charging": 1, "price": 2.0,
             "grid_kwh": 3.0, "energy_source": "energy"},
            {"time": (now + timedelta(minutes=60)).isoformat(), "soc": 60,
             "amps": 16, "charging": 1, "price": 1.0,
             "grid_kwh": 2.0, "energy_source": "energy"},
        ],
    }

    report = mgr.calculate_session_totals("SEK")

    assert report["added_kwh"] == pytest.approx(5.0)
    assert report["total_cost"] == pytest.approx(3.0 * 2.0 + 2.0 * 1.0)
    assert report["energy_basis"] == "measured"


def test_session_totals_fall_back_to_commanded_current(pkg_loader):
    """Histories with no measurement — including ones recorded before this."""
    session_mod = pkg_loader("session_manager")
    mgr = session_mod.SessionManager(None)

    now = datetime.now()
    mgr.current_session = {
        "start_time": now.isoformat(),
        "log": [],
        "history": [
            {"time": now.isoformat(), "soc": 50, "amps": 16, "charging": 1, "price": 2.0},
            {"time": (now + timedelta(hours=1)).isoformat(), "soc": 60,
             "amps": 16, "charging": 1, "price": 2.0},
        ],
    }

    report = mgr.calculate_session_totals("SEK")

    assert report["added_kwh"] == pytest.approx(11.04, abs=0.01)
    assert report["energy_basis"] == "commanded current"
