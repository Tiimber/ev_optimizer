"""
Taking over a session that is already running.

Seen live 2026-09-24 after `ha core restart` mid-charge: Zaptec answered the
resume with "Resuming charging failed" because the session was already
running, the applied state therefore never became "charging", and every
30-second cycle retried the switch and rewrote the car's charge limit — a
cloud call to the car each time. A charger that reports it is already
drawing needs no resume; the coordinator simply adopts the session.
"""

from datetime import datetime, timedelta
import asyncio


def _make_coord(pkg_loader, with_status=True):
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
            if with_status:
                self.data[const.CONF_CHARGER_STATUS_SENSOR] = "sensor.charger_mode"
            self.entry_id = "test"

    class MockHass:
        def __init__(self):
            self.states = type("S", (), {"get": lambda self, e: None})()
            self.data = {}
            self.bus = type("B", (), {"async_fire": lambda self, *a, **k: None})()

            def async_call(_self, domain, service, payload, **kwargs):
                calls.append((domain, service, dict(payload)))
                if domain == "switch":
                    raise RuntimeError("Resuming charging failed")
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


PLAN = {"should_charge_now": True, "charging_summary": "Charging", "planned_target_soc": 80}


def _data(status):
    return {
        "car_plugged": True,
        "should_charge_now": True,
        "max_available_current": 16,
        "car_soc": 70.0,
        "charger_status": status,
    }


def _count(calls, domain, service):
    return sum(1 for d, s, _ in calls if d == domain and s == service)


def test_running_session_is_adopted_without_a_resume(pkg_loader):
    coord, calls = _make_coord(pkg_loader)
    for _ in range(3):
        asyncio.run(coord._apply_charger_control(_data("connected_charging"), PLAN))
    assert _count(calls, "switch", "turn_on") == 0
    assert coord._last_applied_state == "charging"
    # The car limit is written once at the start, not on every cycle.
    assert _count(calls, "number", "set_value") == 2  # limiter + car limit
    assert "taking over the running session" in "\n".join(coord.session_manager.action_log)


def test_idle_charger_still_gets_the_resume(pkg_loader):
    coord, calls = _make_coord(pkg_loader)
    asyncio.run(coord._apply_charger_control(_data("connected_finished"), PLAN))
    assert _count(calls, "switch", "turn_on") == 1


def test_install_without_status_sensor_keeps_the_old_path(pkg_loader):
    coord, calls = _make_coord(pkg_loader, with_status=False)
    data = _data(None)
    for _ in range(2):
        asyncio.run(coord._apply_charger_control(data, PLAN))
    assert _count(calls, "switch", "turn_on") == 2
