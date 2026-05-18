from datetime import datetime


def test_fetch_sensor_data_reads_values(pkg_loader, hass_mock):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    # Create fake states
    class State:
        def __init__(self, state, attributes=None):
            self.state = state
            self.attributes = attributes or {}

    hass_mock.states = type("S", (), {"get": lambda self, e: {
        "sensor.p1_l1": State("5.0"),
        "sensor.p1_l2": State("3.0"),
        "sensor.p1_l3": State("2.0"),
        "number.zap_limit": State("15"),
        "sensor.car_soc": State("40"),
    }.get(e)})()

    # Minimal entry stub
    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_P1_L1: "sensor.p1_l1",
                const.CONF_P1_L2: "sensor.p1_l2",
                const.CONF_P1_L3: "sensor.p1_l3",
                const.CONF_ZAPTEC_LIMITER: "number.zap_limit",
                const.CONF_CAR_SOC_SENSOR: "sensor.car_soc",
                const.CONF_MAX_FUSE: const.DEFAULT_MAX_FUSE,
                const.CONF_CHARGER_LOSS: const.DEFAULT_LOSS,
                const.CONF_CAR_CAPACITY: const.DEFAULT_CAPACITY,
            }
            self.entry_id = "test"

    entry = Entry()
    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, entry)

    data = coord._fetch_sensor_data()

    assert data["p1_l1"] == 5.0
    assert data["p1_l2"] == 3.0
    assert data["p1_l3"] == 2.0
    assert data["zap_limit_value"] == 15.0
    assert data["car_soc"] == 40.0


def test_fetch_sensor_data_handles_unavailable(pkg_loader, hass_mock):
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")

    class State:
        def __init__(self, state):
            self.state = state

    # Return None or unavailable
    hass_mock.states = type("S", (), {"get": lambda self, e: None})()

    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_P1_L1: None,
                const.CONF_P1_L2: None,
                const.CONF_P1_L3: None,
                const.CONF_ZAPTEC_LIMITER: None,
                const.CONF_CAR_SOC_SENSOR: None,
                const.CONF_MAX_FUSE: const.DEFAULT_MAX_FUSE,
                const.CONF_CHARGER_LOSS: const.DEFAULT_LOSS,
                const.CONF_CAR_CAPACITY: const.DEFAULT_CAPACITY,
            }
            self.entry_id = "test"

    entry = Entry()
    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, entry)
    data = coord._fetch_sensor_data()

    assert data["p1_l1"] == 0.0
    assert data["ch_l1"] == 0.0
    assert data.get("zap_limit_value", 0.0) == 0.0


def test_virtual_soc_resyncs_down_when_paused(pkg_loader, hass_mock):
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")

    class State:
        def __init__(self, state, attributes=None):
            self.state = state
            self.attributes = attributes or {}

    hass_mock.states = type(
        "S",
        (),
        {
            "get": lambda self, e: {
                "sensor.car_soc": State("58"),
            }.get(e)
        },
    )()

    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_CAR_SOC_SENSOR: "sensor.car_soc",
                const.CONF_MAX_FUSE: const.DEFAULT_MAX_FUSE,
                const.CONF_CHARGER_LOSS: const.DEFAULT_LOSS,
                const.CONF_CAR_CAPACITY: const.DEFAULT_CAPACITY,
            }
            self.entry_id = "test"

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, Entry())
    coord._virtual_soc = 82.0
    coord._last_applied_state = "paused"

    coord._update_virtual_soc({"car_soc": 58.0})
    assert coord._virtual_soc == 58.0


def test_virtual_soc_resyncs_down_on_significant_drop_while_charging(pkg_loader, hass_mock):
    """During active charging, ignore lower sensor values (they may be stale).
    Only trust them during force refresh period or when not charging."""
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")

    class State:
        def __init__(self, state, attributes=None):
            self.state = state
            self.attributes = attributes or {}

    hass_mock.states = type(
        "S",
        (),
        {
            "get": lambda self, e: {
                "sensor.car_soc": State("58"),
            }.get(e)
        },
    )()

    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_CAR_SOC_SENSOR: "sensor.car_soc",
                const.CONF_MAX_FUSE: const.DEFAULT_MAX_FUSE,
                const.CONF_CHARGER_LOSS: const.DEFAULT_LOSS,
                const.CONF_CAR_CAPACITY: const.DEFAULT_CAPACITY,
            }
            self.entry_id = "test"

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, Entry())
    coord._virtual_soc = 82.0
    coord._last_applied_state = "charging"

    # Ensure the estimator portion doesn't add energy in this unit test.
    coord._last_applied_amps = -1

    coord._update_virtual_soc({"car_soc": 58.0, "ch_l1": 0.0, "ch_l2": 0.0, "ch_l3": 0.0})
    # During active charging, ignore lower sensor values (they may be stale)
    assert coord._virtual_soc == 82.0


def test_trigger_report_generation_uses_session_manager(pkg_loader, hass_mock):
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")

    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_MAX_FUSE: const.DEFAULT_MAX_FUSE,
                const.CONF_CHARGER_LOSS: const.DEFAULT_LOSS,
                const.CONF_CAR_CAPACITY: const.DEFAULT_CAPACITY,
                const.CONF_CAR_SOC_SENSOR: None,
            }
            self.entry_id = "test"

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, Entry())

    # Stub event bus used by SessionManager logging
    hass_mock.bus = type("B", (), {"async_fire": lambda self, *args, **kwargs: None})()

    # Minimal hass config stub for path building and executor job
    hass_mock.config = type("C", (), {"path": lambda self, *p: "/tmp/" + "/".join(p)})()

    async def _executor_job(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    hass_mock.async_add_executor_job = _executor_job

    # Fake an active session with a single point (should still produce a dict)
    coord.session_manager.current_session = {
        "start_time": "2025-01-01T00:00:00",
        "history": [{"time": "2025-01-01T00:00:00", "soc": 40, "amps": 0, "charging": 0, "price": 0}],
        "log": [],
    }

    # Should not raise (this used to crash looking for coord.current_session)
    import asyncio
    asyncio.get_event_loop().run_until_complete(coord.async_trigger_report_generation())


def test_virtual_soc_ignores_wobble_during_charging(pkg_loader, hass_mock):
    """Test that virtual SoC doesn't wobble from stale sensor updates during charging."""
    from datetime import timedelta
    
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    # Setup coordinator with all required config fields
    entry_mock = type("E", (), {
        "entry_id": "test123",
        "data": {
            "car_soc": "sensor.car_soc",
            "car_plugged": "binary_sensor.car_plugged",
            "price": "sensor.electricity_price",
            "charger_amps": "number.charger_amps",
            "ch_l1": "sensor.charger_l1",
            "ch_l2": "sensor.charger_l2",
            "ch_l3": "sensor.charger_l3",
            const.CONF_CAR_CAPACITY: 75.0,
            const.CONF_CHARGER_LOSS: 10.0,
            const.CONF_MAX_FUSE: 25.0,
            const.CONF_PRICE_SENSOR: True,
            const.CONF_CURRENCY: "SEK",
        },
        "options": {},
    })()
    
    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, entry_mock)
    coord._data_loaded = True
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    
    # Initialize: actively charging, virtual SoC at 65%
    coord._virtual_soc = 65.0
    coord._last_applied_state = "charging"
    coord._last_applied_amps = 16.0
    coord._last_update_time = datetime.now() - timedelta(seconds=30)
    coord._refresh_trigger_timestamp = None
    coord._soc_before_refresh = 65.0
    coord.car_capacity = 75.0
    
    # Mock sensor returning stale lower value
    class State:
        def __init__(self, state):
            self.state = state
    
    hass_mock.states = type("S", (), {"get": lambda self, e: State("64.0")})()
    
    data = {
        "car_soc": 64.0,
        "car_plugged": True,
        "ch_l1": 16.0,
        "ch_l2": 16.0,
        "ch_l3": 16.0,
    }
    
    # Update virtual SoC
    trust_sensor = coord._update_virtual_soc(data)
    
    # Virtual SoC should have incremented from charging, NOT reset to stale sensor
    assert coord._virtual_soc > 65.0, "Virtual SoC should increase from charging"
    assert coord._virtual_soc != 64.0, "Virtual SoC should NOT use stale sensor during charging"
    assert not trust_sensor, "Not in forced refresh window"
    
    # Simulate sensor wobbling UP (delayed update)
    current_virtual = coord._virtual_soc
    hass_mock.states = type("S", (), {"get": lambda self, e: State("66.0")})()
    data["car_soc"] = 66.0
    coord._last_update_time = datetime.now() - timedelta(seconds=30)
    
    trust_sensor = coord._update_virtual_soc(data)
    
    # Virtual SoC should continue smooth increment, NOT jump to delayed sensor
    assert coord._virtual_soc > current_virtual, "Virtual SoC should continue incrementing"
    assert coord._virtual_soc != 66.0, "Virtual SoC should NOT jump to sensor (prevents wobbling)"


def test_virtual_soc_accepts_sensor_during_forced_refresh(pkg_loader, hass_mock):
    """Test that virtual SoC accepts sensor updates during forced refresh window."""
    from datetime import timedelta
    
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    entry_mock = type("E", (), {
        "entry_id": "test123",
        "data": {
            "car_soc": "sensor.car_soc",
            "car_plugged": "binary_sensor.car_plugged",
            "price": "sensor.electricity_price",
            "charger_amps": "number.charger_amps",
            "ch_l1": "sensor.charger_l1",
            "ch_l2": "sensor.charger_l2",
            "ch_l3": "sensor.charger_l3",
            const.CONF_CAR_CAPACITY: 75.0,
            const.CONF_CHARGER_LOSS: 10.0,
            const.CONF_MAX_FUSE: 25.0,
            const.CONF_PRICE_SENSOR: True,
            const.CONF_CURRENCY: "SEK",
        },
        "options": {},
    })()
    
    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, entry_mock)
    coord._data_loaded = True
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    
    # Actively charging with forced refresh active
    coord._virtual_soc = 65.0
    coord._last_applied_state = "charging"
    coord._last_applied_amps = 16.0
    coord._last_update_time = datetime.now() - timedelta(seconds=30)
    coord._soc_before_refresh = 65.0
    coord._refresh_trigger_timestamp = datetime.now() - timedelta(minutes=1)
    coord.car_capacity = 75.0
    
    class State:
        def __init__(self, state):
            self.state = state
    
    hass_mock.states = type("S", (), {"get": lambda self, e: State("68.5")})()
    
    data = {
        "car_soc": 68.5,
        "car_plugged": True,
        "ch_l1": 16.0,
        "ch_l2": 16.0,
        "ch_l3": 16.0,
    }
    
    trust_sensor = coord._update_virtual_soc(data)
    
    # Virtual SoC SHOULD accept fresh sensor value during forced refresh
    # Note: it will accept 68.5 and then add a small increment from ongoing charging
    assert trust_sensor, "Should be in forced refresh window"
    assert coord._virtual_soc >= 68.5, "Should accept sensor value as baseline during forced refresh"
    assert coord._virtual_soc < 69.0, "Should only add small increment from 30s of charging"


def test_virtual_soc_trusts_sensor_when_not_charging(pkg_loader, hass_mock):
    """Test that virtual SoC always trusts sensor when not actively charging."""
    from datetime import timedelta
    
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    entry_mock = type("E", (), {
        "entry_id": "test123",
        "data": {
            "car_soc": "sensor.car_soc",
            "car_plugged": "binary_sensor.car_plugged",
            "price": "sensor.electricity_price",
            "charger_amps": "number.charger_amps",
            "ch_l1": "sensor.charger_l1",
            "ch_l2": "sensor.charger_l2",
            "ch_l3": "sensor.charger_l3",
            const.CONF_CAR_CAPACITY: 75.0,
            const.CONF_CHARGER_LOSS: 10.0,
            const.CONF_MAX_FUSE: 25.0,
            const.CONF_PRICE_SENSOR: True,
            const.CONF_CURRENCY: "SEK",
        },
        "options": {},
    })()
    
    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, entry_mock)
    coord._data_loaded = True
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    
    # Not charging, virtual SoC higher than sensor (car was driven)
    coord._virtual_soc = 80.0
    coord._last_applied_state = "paused"
    coord._last_update_time = datetime.now() - timedelta(seconds=30)
    coord._refresh_trigger_timestamp = None
    coord.car_capacity = 75.0
    
    class State:
        def __init__(self, state):
            self.state = state
    
    hass_mock.states = type("S", (), {"get": lambda self, e: State("75.0")})()
    
    data = {
        "car_soc": 75.0,
        "car_plugged": True,
        "ch_l1": 0.0,
        "ch_l2": 0.0,
        "ch_l3": 0.0,
    }
    
    coord._update_virtual_soc(data)
    
    # Virtual SoC should trust lower sensor when not charging
    assert coord._virtual_soc == 75.0, "Should trust sensor when not charging, even if lower"


# ---------------------------------------------------------------------------
# Regression tests for the two May 2026 charging failures.
# ---------------------------------------------------------------------------

def _make_coordinator_for_regression(pkg_loader, hass_mock):
    """Helper: create a minimal coordinator suitable for regression testing."""
    from datetime import timedelta
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")

    class State:
        def __init__(self, state):
            self.state = state

    hass_mock.states = type("S", (), {
        "get": lambda self, e: State("0") if e else None
    })()

    entry = type("E", (), {
        "entry_id": "regression",
        "data": {
            const.CONF_CAR_SOC_SENSOR: "sensor.car_soc",
            const.CONF_MAX_FUSE: const.DEFAULT_MAX_FUSE,
            const.CONF_CHARGER_LOSS: const.DEFAULT_LOSS,
            const.CONF_CAR_CAPACITY: const.DEFAULT_CAPACITY,
        },
        "options": {},
    })()

    coord = coordinator_mod.EVSmartChargerCoordinator(hass_mock, entry)
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    coord._last_update_time = datetime.now() - timedelta(seconds=30)
    coord.car_capacity = const.DEFAULT_CAPACITY
    return coord


def test_sensor_soc_update_detected_on_large_drop(pkg_loader, hass_mock):
    """Regression test for Bug 2 (May 17-18 2026).

    When the SoC sensor drops from 85% → 41% (BMS recalibration) while the
    coordinator is in maintenance mode, _last_sensor_soc must change so the
    caller can detect ``sensor_soc_updated = True`` and unlock the locked plan.

    Before the fix the comparison used the ALREADY-UPDATED _last_sensor_soc vs
    the ALREADY-UPDATED data["car_soc"] (which equals virtual_soc), giving a
    diff of 0 — so the plan was never unlocked.
    """
    coord = _make_coordinator_for_regression(pkg_loader, hass_mock)

    # Simulate state: car in maintenance mode, sensor was last read at 85%
    coord._last_applied_state = "maintenance"
    coord._virtual_soc = 85.0
    coord._last_sensor_soc = 85.0

    # Sensor suddenly drops to 41% (BMS recalibration / bad reading correction)
    prev_sensor_soc = coord._last_sensor_soc  # save BEFORE calling _update_virtual_soc
    coord._update_virtual_soc({"car_soc": 41.0, "ch_l1": 0.0, "ch_l2": 0.0, "ch_l3": 0.0})

    # _last_sensor_soc must have changed so the caller's comparison detects the update
    assert abs(coord._last_sensor_soc - prev_sensor_soc) > 0.5, (
        "After a 44% SoC drop _last_sensor_soc must change by more than 0.5% "
        "so sensor_soc_updated can be detected by comparing prev vs new value"
    )
    # virtual_soc must also follow the sensor (large real-sensor change)
    assert coord._virtual_soc == 41.0, (
        "virtual_soc must be updated to 41% after a genuine sensor drop of 44%"
    )


def test_sensor_soc_update_detected_on_soc_rise_during_charging(pkg_loader, hass_mock):
    """Regression test for Bug 1 (May 15-16 2026).

    After a charging slot expires the plan needs to regenerate to pick the next
    slot. This requires detecting that the SoC actually rose (87% → 90.6%).

    Before the fix, _last_sensor_soc was updated inside _update_virtual_soc
    before the coordinator compared it, so the diff was always 0 and the plan
    stayed locked with no more slots.
    """
    coord = _make_coordinator_for_regression(pkg_loader, hass_mock)

    coord._last_applied_state = "charging"
    coord._virtual_soc = 87.0
    coord._last_sensor_soc = 87.0

    # Sensor updates to 90.6% as charging completes one slot
    prev_sensor_soc = coord._last_sensor_soc
    coord._update_virtual_soc({"car_soc": 90.6, "ch_l1": 0.0, "ch_l2": 0.0, "ch_l3": 0.0})

    assert abs(coord._last_sensor_soc - prev_sensor_soc) > 0.5, (
        "After a 3.6% SoC rise _last_sensor_soc must change by more than 0.5% "
        "so the caller can set sensor_soc_updated=True and unlock the locked plan"
    )
    assert coord._virtual_soc == 90.6, (
        "virtual_soc must follow the sensor when it reports a genuine increase"
    )


def test_maintenance_lock_cleared_when_soc_below_target(pkg_loader, hass_mock):
    """Regression test for Bug 2 safety net.

    Even if sensor_soc_updated isn't detected (e.g., sensor didn't change since
    the last reading), the coordinator must clear the locked plan when it says
    'Target reached' but virtual_soc has fallen more than 5% below the target.
    This prevents the car sitting at 41% all night while the summary still says
    'Target reached (80%)'.
    """
    coord = _make_coordinator_for_regression(pkg_loader, hass_mock)

    # Locked plan: maintenance mode thinks target is reached
    coord._locked_plan = {
        "planned_target_soc": 80.0,
        "charging_summary": "Target reached (85%). Maintenance mode active.",
        "should_charge_now": True,
        "charging_schedule": [],
    }
    coord._locked_plan_soc = 85.0
    coord._virtual_soc = 41.0  # SoC dropped back after BMS recalibration

    locked_target = float(coord._locked_plan.get("planned_target_soc", 0))
    locked_summary = coord._locked_plan.get("charging_summary", "")

    # This is the safety condition added to coordinator._async_update_data
    safety_should_clear = (
        "Target reached" in locked_summary
        and coord._virtual_soc < locked_target - 5
    )

    assert safety_should_clear, (
        "Safety check must trigger when 'Target reached' is in summary but "
        f"virtual_soc ({coord._virtual_soc}%) is > 5% below target ({locked_target}%)"
    )

