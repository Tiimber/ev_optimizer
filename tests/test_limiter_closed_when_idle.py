"""
The limiter must be closed whenever the optimizer is not charging.

Seen live 2026-11-17: at unplug the plug sensor lagged the charger by ~50 s,
load balancing wrote 16 A into that gap, and the unplug path turned the
charger switch off but left the limiter at 16 A. At the next plug-in the
plan said "Waiting for Schedule", but pausing only wrote 0 A when stopping a
charge the optimizer had started itself, and plug-in clears that state. The
car charged at 10 kW at afternoon peak price until its own limit stopped it.
"""

from datetime import datetime, timedelta
import asyncio


def _make_coord(pkg_loader, fail_limiter=False):
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
                const.CONF_CAR_CHARGING_LEVEL_ENTITY: "number.car_limit",
            }
            self.entry_id = "test"

    class MockHass:
        def __init__(self):
            self.states = type("S", (), {"get": lambda self, e: None})()
            self.data = {}
            self.bus = type("B", (), {"async_fire": lambda self, *a, **k: None})()

            def async_call(_self, domain, service, payload, **kwargs):
                calls.append((domain, service, dict(payload)))
                if fail_limiter and payload.get("entity_id") == "number.limiter":
                    raise RuntimeError("Zaptec API unavailable")
                return asyncio.sleep(0)

            self.services = type("SV", (), {"async_call": async_call})()
            self.config = type(
                "CFG", (), {"path": lambda *args: "/tmp/" + "_".join(args)}
            )()
            self.async_add_executor_job = lambda f, *a: f(*a)
            self.async_create_task = lambda coro: asyncio.create_task(coro)

    coord = coordinator_mod.EVSmartChargerCoordinator(MockHass(), Entry())
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    coord._save_data = lambda *a, **k: None
    return coord, calls


WAIT = {"should_charge_now": False, "charging_summary": "Waiting", "planned_target_soc": 80}
DATA = {"car_plugged": True, "should_charge_now": False, "max_available_current": 16, "car_soc": 67.0}


def _limiter_writes(calls):
    return [p["value"] for d, s, p in calls
            if d == "number" and s == "set_value" and p["entity_id"] == "number.limiter"]


def test_fresh_plug_in_while_waiting_closes_the_limiter(pkg_loader):
    coord, calls = _make_coord(pkg_loader)
    # State exactly as _handle_plugged_event leaves it on plug-in.
    coord._last_applied_state = None
    coord._last_applied_amps = -1
    asyncio.run(coord._apply_charger_control(dict(DATA), WAIT))
    assert _limiter_writes(calls) == [0]


def test_closed_limiter_is_not_rewritten_every_cycle(pkg_loader):
    coord, calls = _make_coord(pkg_loader)
    coord._last_applied_state = None
    coord._last_applied_amps = -1
    for _ in range(5):
        asyncio.run(coord._apply_charger_control(dict(DATA), WAIT))
    assert _limiter_writes(calls) == [0]


def test_failed_close_is_retried_at_most_once_a_minute(pkg_loader):
    coord, calls = _make_coord(pkg_loader, fail_limiter=True)
    coord._last_applied_state = None
    coord._last_applied_amps = -1
    for _ in range(6):
        asyncio.run(coord._apply_charger_control(dict(DATA), WAIT))
    assert _limiter_writes(calls) == [0]
    # A minute later it tries again.
    coord._limiter_close_failed_at -= timedelta(seconds=61)
    asyncio.run(coord._apply_charger_control(dict(DATA), WAIT))
    assert _limiter_writes(calls) == [0, 0]


def test_unplug_closes_the_limiter(pkg_loader):
    coord, calls = _make_coord(pkg_loader)
    coord.previous_plugged_state = True
    coord._last_applied_amps = 16
    asyncio.run(coord._handle_plugged_event(False, {"car_plugged": False, "car_soc": 68.0}))
    assert 0 in _limiter_writes(calls)
    assert coord._last_applied_amps == 0
