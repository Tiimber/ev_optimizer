"""
A failed forced refresh must be throttled like a successful one.

Seen live 2026-10-15: with the car's cloud login lapsed, every
`kia_uvo.force_update` raised, `_last_car_refresh_time` was never set, and
the at-target rule ("refresh if the last one is over an hour old") fired on
every coordinator tick. Hyundai logged the account out for "unusual
activity" (code 9035), which then defeated every reauth while the car was
plugged in.
"""

from datetime import datetime, timedelta
import asyncio


class _FailingHass:
    def __init__(self):
        self.states = type("S", (), {"get": lambda self, e: None})()
        self.data = {}
        self.bus = type("B", (), {"async_fire": lambda self, *a, **k: None})()
        self.refresh_count = 0
        hass_self = self

        async def mock_service_call(domain, service, payload, blocking=False, return_response=False):
            if service == "force_update":
                hass_self.refresh_count += 1
                raise RuntimeError("CCI token exchange failed: HTTP 400")

        class Services:
            async_call = staticmethod(mock_service_call)

        self.services = Services()
        self.config = type("CFG", (), {"path": lambda *args: "/tmp/" + "_".join(args)})()
        self.async_add_executor_job = lambda f, *a: f(*a)
        self.async_create_task = lambda coro: asyncio.create_task(coro)


def _coordinator(pkg_loader, hass):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    entry_mock = type("E", (), {
        "entry_id": "test123",
        "data": {
            const.CONF_CAR_CAPACITY: 64.0,
            const.CONF_CHARGER_LOSS: 10.0,
            const.CONF_MAX_FUSE: 20.0,
            const.CONF_PRICE_SENSOR: True,
            const.CONF_CURRENCY: "SEK",
            const.CONF_CAR_REFRESH_ACTION: "kia_uvo.force_update",
            const.CONF_CAR_ENTITY_ID: "device_id_123",
            const.CONF_CAR_REFRESH_INTERVAL: const.REFRESH_AT_TARGET,
        },
        "options": {},
    })()
    coord = coordinator_mod.EVSmartChargerCoordinator(hass, entry_mock)
    coord._data_loaded = True
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    # Plugged in at target with a session open and no planned end: the
    # at-target fallback asks for a refresh once the last one is stale.
    coord._virtual_soc = 89.0
    coord._last_applied_state = "maintenance"
    coord.session_manager.current_session = {"start_time": datetime.now().isoformat(), "log": []}
    return coord


def test_failed_refresh_is_not_retried_every_tick(pkg_loader):
    hass = _FailingHass()
    coord = _coordinator(pkg_loader, hass)
    coord._last_car_refresh_time = datetime.now() - timedelta(hours=13)

    data = {"car_plugged": True}
    plan = {"charging_summary": "Target reached", "planned_target_soc": 80}

    async def ticks():
        for _ in range(10):
            await coord._manage_car_refresh(data, plan)

    asyncio.run(ticks())

    assert hass.refresh_count == 1, (
        f"A failing refresh was attempted {hass.refresh_count} times in 10 ticks"
    )
    assert coord._last_car_refresh_time > datetime.now() - timedelta(minutes=1)
