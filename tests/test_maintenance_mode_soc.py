"""
Test that maintenance mode properly handles virtual SoC updates.

This verifies the fix for bugs where:
1. Virtual SoC would dip when entering maintenance mode due to trusting stale sensor values
2. No forced refresh was triggered when transitioning to maintenance mode
"""

from datetime import datetime, timedelta
import asyncio


def test_maintenance_mode_does_not_trust_stale_downward_sensor(pkg_loader, hass_mock):
    """
    CRITICAL: Verify maintenance mode treats SoC same as charging mode.
    
    BUG SCENARIO:
    - Car reaches target (80%) and enters maintenance mode (0A, keep charger enabled)
    - Sensor still shows old stale value (e.g., 75%)
    - Old code treated maintenance as "not charging" and blindly trusted stale sensor
    - Graph would show a dip even though car didn't actually lose charge
    
    FIX: Maintenance mode should protect virtual SoC same as charging mode.
    """
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    # Setup coordinator
    entry_mock = type("E", (), {
        "entry_id": "test123",
        "data": {
            "car_soc": "sensor.car_soc",
            const.CONF_CAR_CAPACITY: 64.0,
            const.CONF_CHARGER_LOSS: 10.0,
            const.CONF_MAX_FUSE: 20.0,
            const.CONF_PRICE_SENSOR: True,
            const.CONF_CURRENCY: "SEK",
        },
        "options": {},
    })()
    
    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, entry_mock)
    coord._data_loaded = True
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    
    # Car was charging and reached 80% (virtual SoC)
    coord._virtual_soc = 80.0
    coord._last_applied_state = "charging"
    coord._last_applied_amps = 16.0
    coord._last_sensor_soc = 75.0  # Sensor is stale
    coord._refresh_trigger_timestamp = None
    
    # Now transition to maintenance mode (charger keeps running at 0A)
    coord._last_applied_state = "maintenance"
    
    # Sensor still reports stale 75% (hasn't updated yet)
    data = {
        "car_soc": 75.0,
        "ch_l1": 0.0,
        "ch_l2": 0.0,
        "ch_l3": 0.0,
    }
    
    trust_sensor = coord._update_virtual_soc(data)
    
    # Virtual SoC should NOT drop to 75% just because we're in maintenance
    # It should stay at 80% until there's a forced refresh or sensor value changes
    assert coord._virtual_soc == 80.0, (
        f"REGRESSION: Virtual SoC dropped from 80% to {coord._virtual_soc:.1f}% "
        f"in maintenance mode due to stale sensor! Should ignore stale values."
    )
    assert trust_sensor is False, "Should not trust sensor outside forced refresh window"
    
    print("✅ Maintenance mode correctly ignores stale downward sensor values")


def test_maintenance_mode_accepts_sensor_after_forced_refresh(pkg_loader, hass_mock):
    """Verify maintenance mode accepts sensor value during forced refresh window."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    entry_mock = type("E", (), {
        "entry_id": "test123",
        "data": {
            "car_soc": "sensor.car_soc",
            const.CONF_CAR_CAPACITY: 64.0,
            const.CONF_CHARGER_LOSS: 10.0,
            const.CONF_MAX_FUSE: 20.0,
            const.CONF_PRICE_SENSOR: True,
            const.CONF_CURRENCY: "SEK",
        },
        "options": {},
    })()
    
    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, entry_mock)
    coord._data_loaded = True
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    
    # In maintenance mode with virtual SoC at 80%
    coord._virtual_soc = 80.0
    coord._last_applied_state = "maintenance"
    coord._last_sensor_soc = 75.0
    coord._soc_before_refresh = 80.0
    
    # Trigger forced refresh (within 5 minute window)
    coord._refresh_trigger_timestamp = datetime.now() - timedelta(seconds=30)
    
    # Sensor now shows actual 78% after refresh
    data = {
        "car_soc": 78.0,
        "ch_l1": 0.0,
        "ch_l2": 0.0,
        "ch_l3": 0.0,
    }
    
    trust_sensor = coord._update_virtual_soc(data)
    
    # Should accept the fresh sensor value from forced refresh
    assert coord._virtual_soc == 78.0, (
        f"Should accept sensor value during forced refresh window, got {coord._virtual_soc:.1f}%"
    )
    assert trust_sensor is True, "Should trust sensor during forced refresh window"
    
    print("✅ Maintenance mode accepts fresh sensor values during forced refresh")


def test_force_refresh_triggered_on_entering_maintenance(pkg_loader):
    """
    CRITICAL: Verify force refresh is triggered when entering maintenance mode.
    
    BUG: No refresh was triggered when reaching target and entering maintenance,
    causing the sensor to remain stale and potentially show wrong values.
    
    FIX: Immediately trigger refresh when transitioning charging -> maintenance.
    """
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    class MockHass:
        def __init__(self):
            self.states = type("S", (), {"get": lambda self, e: None})()
            self.data = {}
            self.bus = type("B", (), {"async_fire": lambda self, *a, **k: None})()
            
            self.refresh_called = False
            self.refresh_service = None
            self.refresh_entity = None
            
            hass_self = self
            async def mock_service_call(domain, service, payload, blocking=False, return_response=False):
                if service in ["force_update", "update_vehicle"]:
                    hass_self.refresh_called = True
                    hass_self.refresh_service = f"{domain}.{service}"
                    hass_self.refresh_entity = payload.get("entity_id") or payload.get("device_id")
            
            class Services:
                async_call = staticmethod(mock_service_call)
            
            self.services = Services()
            self.config = type("CFG", (), {"path": lambda *args: "/tmp/" + "_".join(args)})()
            def async_add_executor_job(f, *a):
                return f(*a)
            self.async_add_executor_job = async_add_executor_job
            def async_create_task(coro):
                return asyncio.create_task(coro)
            self.async_create_task = async_create_task
    
    hass = MockHass()
    
    entry_mock = type("E", (), {
        "entry_id": "test123",
        "data": {
            "car_soc": "sensor.car_soc",
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
    
    # Currently charging
    coord._last_applied_state = "charging"
    coord._virtual_soc = 79.5
    
    # Plan says we've reached target and should enter maintenance
    data = {
        "car_plugged": True,
        "car_soc": 75.0,  # Stale sensor value
    }
    plan = {
        "charging_summary": "Target reached (80%). Maintenance mode active.",
        "should_charge_now": True,
        "planned_target_soc": 80,
    }
    
    # Call refresh management (happens before applying charger control)
    asyncio.run(coord._manage_car_refresh(data, plan))
    
    # Should have triggered a refresh when entering maintenance
    assert hass.refresh_called, (
        "REGRESSION: No refresh triggered when entering maintenance mode! "
        "This leaves sensor stale and can cause graph dips."
    )
    assert hass.refresh_service == "kia_uvo.force_update"
    assert hass.refresh_entity == "device_id_123"
    
    print("✅ Force refresh correctly triggered when entering maintenance mode")


def test_no_duplicate_refresh_when_staying_in_maintenance(pkg_loader):
    """Verify we don't spam refreshes on every cycle while in maintenance."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    class MockHass:
        def __init__(self):
            self.states = type("S", (), {"get": lambda self, e: None})()
            self.data = {}
            self.bus = type("B", (), {"async_fire": lambda self, *a, **k: None})()
            
            self.refresh_count = 0
            
            hass_self = self
            async def mock_service_call(domain, service, payload, blocking=False, return_response=False):
                if service in ["force_update", "update_vehicle"]:
                    hass_self.refresh_count += 1
            
            class Services:
                async_call = staticmethod(mock_service_call)
            
            self.services = Services()
            self.config = type("CFG", (), {"path": lambda *args: "/tmp/" + "_".join(args)})()
            def async_add_executor_job(f, *a):
                return f(*a)
            self.async_add_executor_job = async_add_executor_job
            def async_create_task(coro):
                return asyncio.create_task(coro)
            self.async_create_task = async_create_task
    
    hass = MockHass()
    
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
    
    # Already in maintenance mode
    coord._last_applied_state = "maintenance"
    coord._virtual_soc = 80.0
    
    data = {"car_plugged": True, "car_soc": 78.0}
    plan = {
        "charging_summary": "Target reached (80%). Maintenance mode active.",
        "should_charge_now": True,
        "planned_target_soc": 80,
    }
    
    # Call refresh management multiple times (simulating update cycles)
    asyncio.run(coord._manage_car_refresh(data, plan))
    asyncio.run(coord._manage_car_refresh(data, plan))
    asyncio.run(coord._manage_car_refresh(data, plan))
    
    # Should only trigger once (on first detection), not spam on every cycle
    assert hass.refresh_count == 0, (
        f"Should not trigger refresh when already in maintenance mode "
        f"(only on transition), but got {hass.refresh_count} refreshes"
    )
    
    print("✅ No duplicate refreshes while staying in maintenance mode")
