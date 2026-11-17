"""
Safety-cutoff damping: the start/stop loop that spams the driver's phone.

Measured live on 2026-09-24, 15:51-15:54: ten pauses in three and a half
minutes, every one a "connected but not charging" push from the car's app.
The action log read, every twenty seconds:

    Safety Cutoff: Available 0A is below minimum 6A. Pausing.
    Pausing: Set Zaptec limit to 0A
    ... ten seconds later ...
    Switched Charging state to: CHARGING
    Setting charger to 10A

Cause: the house meter ticks every ten seconds and the coordinator refreshes
on each tick, but the charger integration reports its per-phase current one to
three seconds *after* the meter shows the same draw. So on the first tick after
a resume the meter carries the car's current and the charger sensor still
reads 0.05 A; load balancing books the car as house load (meter 18.1 A,
charger 0.05 A, house "18 A", available 0 A — with 12.9 A of real house load),
pauses, and the next tick — without the car — reads as headroom again.

Three things pin it down here:

  * a charger reading that predates the last limiter write is corrected from
    the meter's own change since that write, never invented from the command;
  * marginal headroom (below the floor but above 0 A) holds the floor for one
    more reading before it pauses;
  * a pause caused by a cutoff is held for a minimum time, so a resume can
    never follow on the very next tick.

Everything degrades to the old behaviour for installs without the sensors.
"""

from datetime import datetime, timedelta, timezone
import asyncio


def _make_coord(pkg_loader, with_charger_sensors=True):
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")
    calls = []

    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_MAX_FUSE: 20.0,
                const.CONF_CHARGER_LOSS: 10.0,
                const.CONF_CAR_CAPACITY: 64.0,
                const.CONF_CURRENCY: "SEK",
                const.CONF_PRICE_SENSOR: True,
                const.CONF_ZAPTEC_LIMITER: "number.limiter",
                const.CONF_ZAPTEC_SWITCH: "switch.charger",
                const.CONF_P1_L1: "sensor.p1_l1",
                const.CONF_P1_L2: "sensor.p1_l2",
                const.CONF_P1_L3: "sensor.p1_l3",
            }
            if with_charger_sensors:
                self.data.update({
                    const.CONF_CHARGER_CURRENT_L1: "sensor.ch_l1",
                    const.CONF_CHARGER_CURRENT_L2: "sensor.ch_l2",
                    const.CONF_CHARGER_CURRENT_L3: "sensor.ch_l3",
                })
            self.entry_id = "test"

    class MockHass:
        def __init__(self):
            self.states = type("S", (), {"get": lambda self, e: None})()
            self.data = {}
            self.bus = type("B", (), {"async_fire": lambda self, *a, **k: None})()

            def async_call(_self, domain, service, payload, **kwargs):
                calls.append((domain, service, dict(payload)))
                return asyncio.sleep(0)

            self.services = type("SV", (), {"async_call": async_call})()
            self.config = type(
                "CFG", (), {"path": lambda *args: "/tmp/" + "_".join(args)}
            )()
            self.async_add_executor_job = lambda f, *a: f(*a)
            self.async_create_task = lambda coro: asyncio.create_task(coro)

    coord = coordinator_mod.EVSmartChargerCoordinator(MockHass(), Entry())
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    return coord, calls, pkg_loader("planner"), const


T0 = datetime(2026, 9, 24, 13, 51, 24, tzinfo=timezone.utc)


def _arm_write(coord, commanded, meter, when=None):
    """Pretend the limiter was just written to `commanded` with the meter at `meter`."""
    coord._last_applied_amps = commanded
    coord._limiter_write_meter = meter
    coord._last_limiter_write_time = when or (datetime.now(timezone.utc) - timedelta(seconds=6))


def _tick(coord, p1, ch, meter_after_write=True, charger_after_write=False):
    write = coord._last_limiter_write_time
    meter_at = write + timedelta(seconds=6) if meter_after_write else write - timedelta(seconds=1)
    ch_at = write + timedelta(seconds=7) if charger_after_write else write - timedelta(seconds=5)
    return {
        "p1_l1": p1[0], "p1_l2": p1[1], "p1_l3": p1[2],
        "ch_l1": ch[0], "ch_l2": ch[1], "ch_l3": ch[2],
        "p1_reported_at": meter_at,
        "charger_reported_at": {"ch_l1": ch_at, "ch_l2": ch_at, "ch_l3": ch_at},
    }


# --- stale charger readings ---------------------------------------------------


def test_live_reproduction_is_corrected_from_the_meter_rise(pkg_loader):
    """The 15:51:30 sample: resume to 6 A, meter up by 5.2 A, charger still 0.05 A."""
    coord, _, planner, _ = _make_coord(pkg_loader)
    _arm_write(coord, 6, (12.9, 1.8, 0.6))
    data = _tick(coord, p1=(18.1, 6.7, 5.6), ch=(0.05, 0.05, 0.05))

    raw = planner.calculate_load_balancing(data, 20.0)
    assert raw < 6, "without the correction this sample reads as a cutoff"

    adjusted, stale = coord._charger_currents_for_balancing(data)
    assert stale is True
    assert abs(adjusted["ch_l1"] - 5.25) < 1e-9  # 0.05 + (18.1 - 12.9), under the 6 A ceiling
    corrected = planner.calculate_load_balancing(adjusted, 20.0)
    assert corrected >= 6
    assert data["ch_l1"] == 0.05, "the raw reading is left for the estimator"


def test_a_reading_taken_after_the_write_is_trusted(pkg_loader):
    coord, _, _, _ = _make_coord(pkg_loader)
    _arm_write(coord, 6, (12.9, 1.8, 0.6))
    data = _tick(coord, p1=(18.1, 6.7, 5.6), ch=(5.3, 5.0, 5.0), charger_after_write=True)
    adjusted, stale = coord._charger_currents_for_balancing(data)
    assert stale is False and adjusted is data


def test_meter_that_has_not_ticked_since_the_write_is_left_alone(pkg_loader):
    coord, _, _, _ = _make_coord(pkg_loader)
    _arm_write(coord, 6, (12.9, 1.8, 0.6))
    data = _tick(coord, p1=(12.9, 1.8, 0.6), ch=(0.0, 0.0, 0.0), meter_after_write=False)
    _, stale = coord._charger_currents_for_balancing(data)
    assert stale is False


def test_correction_expires_with_the_grace_window(pkg_loader):
    coord, _, _, const = _make_coord(pkg_loader)
    long_ago = datetime.now(timezone.utc) - timedelta(
        seconds=const.STALE_CHARGER_READING_GRACE_SECONDS + 10
    )
    _arm_write(coord, 6, (12.9, 1.8, 0.6), when=long_ago)
    data = _tick(coord, p1=(18.1, 6.7, 5.6), ch=(0.05, 0.05, 0.05))
    _, stale = coord._charger_currents_for_balancing(data)
    assert stale is False


def test_single_phase_car_does_not_get_current_invented_on_idle_phases(pkg_loader):
    """The meter rose on L1 only, so only L1 is attributed to the charger."""
    coord, _, _, _ = _make_coord(pkg_loader)
    _arm_write(coord, 16, (3.0, 3.0, 3.0))
    data = _tick(coord, p1=(19.0, 3.0, 3.1), ch=(0.0, 0.0, 0.0))
    adjusted, stale = coord._charger_currents_for_balancing(data)
    assert stale is True
    assert adjusted["ch_l1"] == 16.0
    assert adjusted["ch_l2"] == 0.0
    assert abs(adjusted["ch_l3"] - 0.1) < 1e-9


def test_correction_never_exceeds_the_command_or_the_old_reading(pkg_loader):
    """A house load switching on in the same tick is not booked as the charger."""
    coord, _, _, _ = _make_coord(pkg_loader)
    _arm_write(coord, 6, (3.0, 3.0, 3.0))
    data = _tick(coord, p1=(15.0, 3.0, 3.0), ch=(0.0, 0.0, 0.0))
    adjusted, _ = coord._charger_currents_for_balancing(data)
    assert adjusted["ch_l1"] == 6.0


def test_pause_direction_walks_the_old_reading_down(pkg_loader):
    """Limiter written to 0: stale 10 A reading, meter fell by 10 A -> 0 A."""
    coord, _, _, _ = _make_coord(pkg_loader)
    _arm_write(coord, 0, (18.0, 11.7, 10.6))
    data = _tick(coord, p1=(7.6, 1.3, 0.2), ch=(10.4, 10.4, 10.4))
    adjusted, stale = coord._charger_currents_for_balancing(data)
    assert stale is True
    assert adjusted["ch_l1"] == 0.0


def test_install_without_charger_current_sensors_is_untouched(pkg_loader):
    coord, _, _, _ = _make_coord(pkg_loader, with_charger_sensors=False)
    _arm_write(coord, 6, (12.9, 1.8, 0.6))
    data = _tick(coord, p1=(18.1, 6.7, 5.6), ch=(0.0, 0.0, 0.0))
    adjusted, stale = coord._charger_currents_for_balancing(data)
    assert stale is False and adjusted is data


def test_no_write_yet_means_no_correction(pkg_loader):
    coord, _, _, _ = _make_coord(pkg_loader)
    data = {
        "p1_l1": 18.1, "p1_l2": 6.7, "p1_l3": 5.6,
        "ch_l1": 0.0, "ch_l2": 0.0, "ch_l3": 0.0,
        "p1_reported_at": datetime.now(timezone.utc),
        "charger_reported_at": {},
    }
    _, stale = coord._charger_currents_for_balancing(data)
    assert stale is False


# --- confirm before pausing, then hold the pause ------------------------------

PLAN = {
    "should_charge_now": True,
    "charging_summary": "Charging",
    "planned_target_soc": 80,
}


def _data(amps):
    return {
        "car_plugged": True,
        "should_charge_now": True,
        "max_available_current": amps,
        "car_soc": 70.0,
    }


def _limiter_values(calls):
    return [
        p["value"] for d, s, p in calls
        if d == "number" and s == "set_value" and p["entity_id"] == "number.limiter"
    ]


def _log(coord):
    return "\n".join(coord.session_manager.action_log)


def test_marginal_headroom_holds_the_floor_for_one_reading(pkg_loader):
    coord, calls, _, const = _make_coord(pkg_loader)
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    assert _limiter_values(calls) == [11]

    asyncio.run(coord._apply_charger_control(_data(4), PLAN))
    assert _limiter_values(calls) == [11, const.MIN_CHARGE_AMPS]
    assert "Safety Cutoff" not in _log(coord)
    assert "holding 6A" in _log(coord)
    assert coord._last_applied_state == "charging"


def test_persisting_low_headroom_pauses_and_holds(pkg_loader):
    coord, calls, _, const = _make_coord(pkg_loader)
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    asyncio.run(coord._apply_charger_control(_data(4), PLAN))
    coord._cutoff_pending_since -= timedelta(seconds=const.SAFETY_CUTOFF_CONFIRM_SECONDS + 1)

    asyncio.run(coord._apply_charger_control(_data(4), PLAN))
    assert _limiter_values(calls)[-1] == 0
    assert "Safety Cutoff: Available 4A" in _log(coord)
    assert coord._safety_hold_until is not None
    assert coord._last_applied_state == "paused"


def test_zero_headroom_pauses_at_once(pkg_loader):
    """0 A means the house alone is at the fuse; there is nothing to confirm."""
    coord, calls, _, _ = _make_coord(pkg_loader)
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    asyncio.run(coord._apply_charger_control(_data(0), PLAN))
    assert _limiter_values(calls) == [11, 0]
    assert "Safety Cutoff: Available 0A" in _log(coord)


def test_headroom_returning_before_confirmation_clears_the_pending_cutoff(pkg_loader):
    coord, calls, _, _ = _make_coord(pkg_loader)
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    asyncio.run(coord._apply_charger_control(_data(4), PLAN))
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    assert coord._cutoff_pending_since is None
    assert 0 not in _limiter_values(calls)
    assert coord._last_applied_state == "charging"


def test_hold_blocks_a_resume_on_the_next_tick(pkg_loader):
    """The loop itself: pause, next tick reads free, resume — no longer."""
    coord, calls, _, _ = _make_coord(pkg_loader)
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    asyncio.run(coord._apply_charger_control(_data(0), PLAN))
    assert _limiter_values(calls) == [11, 0]

    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    assert _limiter_values(calls) == [11, 0], "held: no resume inside the hold"
    assert coord._last_applied_state == "paused"

    coord._safety_hold_until = datetime.now() - timedelta(seconds=1)
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    assert _limiter_values(calls) == [11, 0, 11]
    assert coord._last_applied_state == "charging"
    assert coord._safety_hold_until is None


def test_cutoff_is_logged_once_per_hold_not_once_per_tick(pkg_loader):
    coord, _, _, _ = _make_coord(pkg_loader)
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    for _ in range(5):
        asyncio.run(coord._apply_charger_control(_data(0), PLAN))
    assert _log(coord).count("Safety Cutoff") == 1


def test_overload_minutes_still_accrue_during_the_hold(pkg_loader):
    coord, _, _, _ = _make_coord(pkg_loader)
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    asyncio.run(coord._apply_charger_control(_data(0), PLAN))
    coord._last_overload_check_time -= timedelta(minutes=2)
    asyncio.run(coord._apply_charger_control(_data(0), PLAN))
    assert coord.session_manager.overload_prevention_minutes >= 2.0


def test_hold_ends_an_idle_assist_too(pkg_loader):
    coord, calls, _, _ = _make_coord(pkg_loader)
    coord.config_settings["idle_request_assist"] = True
    coord.conf_keys["charger_status"] = "sensor.charger_mode"
    asyncio.run(coord._apply_charger_control(_data(11), PLAN))
    asyncio.run(coord._apply_charger_control(_data(0), PLAN))
    done = {"should_charge_now": False, "charging_summary": "Target reached", "planned_target_soc": 80}
    asking = {
        "car_plugged": True, "should_charge_now": False, "max_available_current": 11,
        "car_soc": 80.0, "charger_status": "connected_requesting",
    }
    asyncio.run(coord._apply_charger_control(asking, done))
    assert coord._idle_assist_active is False
    assert _limiter_values(calls) == [11, 0]
