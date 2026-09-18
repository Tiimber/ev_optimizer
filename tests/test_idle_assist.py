"""
Idle assist: granting current to a car that asks for a session after the plan
is finished, instead of leaving the limiter on a 0 A hold.

Background — measured on a real install, three consecutive mornings:

  * The night charge finished hours earlier and the limiter was held at 0 A.
    At the preconditioning time the car moved to `connected_requesting` and
    stayed there for the whole ten-minute window, drawing nothing. The cabin
    never heated: a car refused a session does NOT fall back to the battery,
    it waits.
  * On the one morning the limiter happened to be open, the car opened a
    session, and the coordinator clamped back to 0 A thirty-four seconds later
    when the SoC touched the target — cutting the supply mid-preconditioning.

Both behaviours are what these tests pin down. The feature is opt-in and inert
without a charger status sensor, so the last test covers the untouched path.
"""

from datetime import datetime, timedelta
import asyncio


def _make_coord(pkg_loader, options=None):
    """Build a coordinator over stub hass/entry, recording service calls."""
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")

    calls = []

    class Entry:
        def __init__(self):
            self.options = dict(options or {})
            self.data = {
                const.CONF_MAX_FUSE: 20.0,
                const.CONF_CHARGER_LOSS: 10.0,
                const.CONF_CAR_CAPACITY: 64.0,
                const.CONF_CURRENCY: "SEK",
                const.CONF_PRICE_SENSOR: True,
                const.CONF_ZAPTEC_LIMITER: "number.limiter",
                const.CONF_ZAPTEC_SWITCH: "switch.charger",
            }
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
    return coord, calls


ASSIST_ON = {
    "charger_status_sensor": "sensor.charger_mode",
    "idle_request_assist": True,
}

# The plan is finished: nothing left to deliver, so no charging is scheduled.
DONE_PLAN = {
    "should_charge_now": False,
    "charging_summary": "Target reached",
    "planned_target_soc": 80,
}


def _idle_data(status, soc=80.0, amps=16.0, plugged=True):
    return {
        "car_plugged": plugged,
        "should_charge_now": False,
        "max_available_current": amps,
        "car_soc": soc,
        "charger_status": status,
    }


def _limiter_values(calls):
    return [
        c[2].get("value")
        for c in calls
        if c[1] == "set_value" and c[2].get("entity_id") == "number.limiter"
    ]


def test_request_after_plan_is_answered_with_current(pkg_loader):
    """The morning that failed: car asks, limiter is on a 0 A hold."""
    coord, calls = _make_coord(pkg_loader, ASSIST_ON)
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 80}))

    # The hold is in place after the night charge.
    coord._last_applied_state = "paused"
    coord._last_applied_amps = 0

    data = _idle_data("connected_requesting")
    asyncio.run(coord._apply_charger_control(data, DONE_PLAN))

    assert coord._idle_assist_active is True
    assert data["idle_assist_active"] is True
    assert _limiter_values(calls) == [16], "car asked for a session and got nothing"
    assert coord._last_applied_state == "assist"


def test_assist_survives_the_move_to_charging(pkg_loader):
    """The 34-second bug: the grant must not be cut when current starts flowing.

    `connected_requesting` becomes `connected_charging` within seconds of the
    car being answered. Treating only the request as the trigger withdraws the
    grant on the very next cycle, which is the failure being fixed.
    """
    coord, calls = _make_coord(pkg_loader, ASSIST_ON)
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 80}))
    coord._last_applied_state = "paused"
    coord._last_applied_amps = 0

    asyncio.run(coord._apply_charger_control(_idle_data("connected_requesting"), DONE_PLAN))
    assert coord._idle_assist_active is True

    calls.clear()
    data = _idle_data("connected_charging")
    asyncio.run(coord._apply_charger_control(data, DONE_PLAN))

    assert coord._idle_assist_active is True, "grant withdrawn as soon as it was used"
    assert 0 not in _limiter_values(calls)


def test_assist_ends_when_the_car_stops_asking(pkg_loader):
    """The car ends the session at its own limit; the hold comes back."""
    coord, calls = _make_coord(pkg_loader, ASSIST_ON)
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 80}))
    coord._last_applied_state = "paused"

    asyncio.run(coord._apply_charger_control(_idle_data("connected_requesting"), DONE_PLAN))
    calls.clear()

    data = _idle_data("connected_finished")
    asyncio.run(coord._apply_charger_control(data, DONE_PLAN))

    assert coord._idle_assist_active is False
    assert data["idle_assist_active"] is False
    assert _limiter_values(calls) == [0]
    assert coord._last_applied_state == "paused"


def test_no_assist_while_the_plan_still_owes_energy(pkg_loader):
    """Below target we are waiting for a cheap hour, and that must still win."""
    coord, calls = _make_coord(pkg_loader, ASSIST_ON)
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 55}))
    coord._last_applied_state = "paused"

    data = _idle_data("connected_requesting", soc=55.0)
    asyncio.run(coord._apply_charger_control(data, DONE_PLAN))

    assert coord._idle_assist_active is False
    assert 16 not in _limiter_values(calls)


def test_assist_never_charges_past_the_target(pkg_loader):
    """The user's hard constraint: the configured limit is a ceiling.

    Normally the car stops itself at its own charge limit. This is the backstop
    for an install where that limit sits above the plan's target.
    """
    coord, calls = _make_coord(pkg_loader, ASSIST_ON)
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 80}))
    coord._last_applied_state = "paused"

    asyncio.run(coord._apply_charger_control(_idle_data("connected_requesting"), DONE_PLAN))
    assert coord._idle_assist_active is True

    calls.clear()
    over = _idle_data("connected_charging", soc=82.0)
    asyncio.run(coord._apply_charger_control(over, DONE_PLAN))

    assert coord._idle_assist_active is False
    assert _limiter_values(calls) == [0]


def test_assist_yields_to_load_balancing(pkg_loader):
    """House load outranks the cabin. Below the 6 A floor there is nothing to give."""
    coord, calls = _make_coord(pkg_loader, ASSIST_ON)
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 80}))
    coord._last_applied_state = "paused"

    data = _idle_data("connected_requesting", amps=4.0)
    asyncio.run(coord._apply_charger_control(data, DONE_PLAN))

    assert coord._idle_assist_active is False
    assert 4 not in _limiter_values(calls)


def test_assist_budget_is_capped_per_session(pkg_loader):
    """A car that asks in a loop must not hold the charger open all night."""
    coord, calls = _make_coord(pkg_loader, {**ASSIST_ON, "idle_assist_max_minutes": 10})
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 80}))
    coord._last_applied_state = "paused"

    asyncio.run(coord._apply_charger_control(_idle_data("connected_requesting"), DONE_PLAN))
    assert coord._idle_assist_active is True

    # Bill the budget as if the grant had been running for a quarter of an hour.
    coord._idle_assist_last_tick = datetime.now() - timedelta(minutes=15)
    calls.clear()
    data = _idle_data("connected_charging")
    asyncio.run(coord._apply_charger_control(data, DONE_PLAN))

    assert coord._idle_assist_active is False
    assert _limiter_values(calls) == [0]

    # And a fresh cable restores the budget.
    asyncio.run(coord._handle_plugged_event(False, {"car_soc": 80}))
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 80}))
    assert coord._idle_assist_minutes == 0.0


def test_unconfigured_install_is_untouched(pkg_loader):
    """No status sensor, no option: the 0 A hold behaves exactly as before."""
    coord, calls = _make_coord(pkg_loader)  # no options at all
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 80}))
    coord._last_applied_state = "charging"

    data = _idle_data("connected_requesting")
    asyncio.run(coord._apply_charger_control(data, DONE_PLAN))

    assert coord._idle_assist_active is False
    assert data["idle_assist_active"] is False
    assert _limiter_values(calls) == [0], "an unconfigured install must still pause"


def test_energy_during_an_assist_is_counted(pkg_loader):
    """The virtual SoC advances on measured energy while assisting.

    The estimator used to advance only in the `charging` state, so anything
    delivered under a different state would have gone missing from the model.
    """
    coord, _ = _make_coord(pkg_loader, ASSIST_ON)
    coord._last_applied_state = "assist"
    coord._virtual_soc = 79.0
    coord._last_applied_car_limit = 80
    coord._last_update_time = datetime.now() - timedelta(minutes=6)
    coord._last_sensor_soc = 79.0

    data = {
        "car_plugged": True,
        "car_soc": 79.0,
        "charger_power": 3450.0,
        "ch_l1": 5.0,
        "ch_l2": 5.0,
        "ch_l3": 5.0,
    }
    coord._update_virtual_soc(data)

    assert coord._virtual_soc > 79.0, "energy delivered during an assist was dropped"
