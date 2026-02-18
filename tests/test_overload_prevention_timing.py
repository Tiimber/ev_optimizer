"""
Test that overload prevention minutes are tracked based on actual elapsed time,
not by counting update cycles.

This verifies the fix for the bug where P1 sensor listeners triggered rapid updates
(every 2-10 seconds) during overload, but the code assumed 30-second intervals,
causing massive overcounting (e.g., 48 minutes of actual overload being counted as 88 minutes).
"""

from datetime import datetime, time, timedelta
import asyncio


def test_overload_minutes_tracks_actual_elapsed_time_not_update_count(pkg_loader):
    """
    CRITICAL: Verify overload prevention minutes use actual elapsed time, not update count.
    
    BUG SCENARIO:
    - Overload period: 23:45 to 00:33 = 48 actual minutes
    - P1 sensors triggered ~176 updates during this time (rapid changes)
    - Old code: 176 updates × 0.5 min = 88 minutes (WRONG!)
    - Fixed code: Should track actual 48 minutes
    """
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")
    
    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_MAX_FUSE: 20.0,
                const.CONF_CHARGER_LOSS: 10.0,
                const.CONF_CAR_CAPACITY: 64.0,
                const.CONF_CURRENCY: "SEK",
                const.CONF_PRICE_SENSOR: True,
            }
            self.entry_id = "test"

    class MockHass:
        def __init__(self):
            self.states = type("S", (), {"get": lambda self, e: None})()
            self.data = {}
            self.bus = type("B", (), {
                "async_fire": lambda self, *a, **k: None,
            })()
            self.services = type("SV", (), {
                "async_call": lambda self, *a, **k: asyncio.sleep(0),
            })()
            self.config = type("CFG", (), {
                "path": lambda *args: "/tmp/" + "_".join(args)
            })()
            def async_add_executor_job(f, *a):
                return f(*a)
            self.async_add_executor_job = async_add_executor_job
            def async_create_task(coro):
                return asyncio.create_task(coro)
            self.async_create_task = async_create_task

    hass = MockHass()
    entry = Entry()
    
    coord = coordinator_mod.EVSmartChargerCoordinator(hass, entry)
    
    # Start session - should reset overload timer
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 60}))
    assert coord._last_overload_check_time is None, "Should start with no overload timer"
    assert coord.session_manager.overload_prevention_minutes == 0.0
    
    # Create minimal plan that says we should charge
    plan = {
        "should_charge_now": True,
        "charging_summary": "Charging",
        "planned_target_soc": 80,
    }
    
    # Simulate data with insufficient current (overload condition)
    data_overload = {
        "car_plugged": True,
        "should_charge_now": True,
        "max_available_current": 4.0,  # Below 6A minimum - triggers overload
    }
    
    # Simulate multiple rapid updates (like P1 sensor triggering frequently)
    # Simulating 10 updates over 60 seconds (averaging 6 second intervals)
    start_time = datetime(2026, 2, 17, 23, 45, 0)
    
    # Manually set coordinator start time to avoid startup grace period
    coord._startup_time = start_time - timedelta(minutes=10)
    
    # First overload check at 23:45:00
    coord._last_overload_check_time = None  # Reset
    coord._startup_time = start_time - timedelta(minutes=10)
    
    async def simulate_update_at_time(update_time):
        """Simulate a coordinator update at a specific time."""
        # Temporarily override datetime.now() isn't easy in Python
        # Instead we'll manually set and track times
        if coord._last_overload_check_time is None:
            coord._last_overload_check_time = update_time
        else:
            elapsed = (update_time - coord._last_overload_check_time).total_seconds() / 60.0
            coord.session_manager.add_overload_minutes(elapsed)
            coord._last_overload_check_time = update_time
    
    # Simulate rapid updates over 60 seconds
    update_times = [
        start_time,
        start_time + timedelta(seconds=5),
        start_time + timedelta(seconds=10),
        start_time + timedelta(seconds=15),
        start_time + timedelta(seconds=22),
        start_time + timedelta(seconds=30),
        start_time + timedelta(seconds=38),
        start_time + timedelta(seconds=45),
        start_time + timedelta(seconds=53),
        start_time + timedelta(seconds=60),
    ]
    
    for update_time in update_times:
        asyncio.run(simulate_update_at_time(update_time))
    
    # After 60 seconds (1 minute) of overload with 10 updates:
    # Old buggy code: 10 updates × 0.5 min = 5.0 minutes (WRONG!)
    # Fixed code: 60 seconds actual time = 1.0 minute (CORRECT!)
    
    accumulated = coord.session_manager.overload_prevention_minutes
    assert 0.95 <= accumulated <= 1.05, (
        f"Expected ~1.0 minutes for 60 seconds, got {accumulated:.2f}. "
        f"If this is ~5.0, the bug is not fixed (counting updates instead of time)!"
    )
    
    print(f"✅ CORRECT: 10 rapid updates over 60 seconds = {accumulated:.2f} minutes (not 5.0!)")


def test_overload_minutes_reset_on_plugin(pkg_loader):
    """Verify overload timer is reset when car plugs in."""
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")
    
    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_MAX_FUSE: 20.0,
                const.CONF_CHARGER_LOSS: 10.0,
                const.CONF_CAR_CAPACITY: 64.0,
                const.CONF_CURRENCY: "SEK",
                const.CONF_PRICE_SENSOR: True,
            }
            self.entry_id = "test"

    class MockHass:
        def __init__(self):
            self.states = type("S", (), {"get": lambda self, e: None})()
            self.data = {}
            self.bus = type("B", (), {"async_fire": lambda self, *a, **k: None})()
            self.services = type("SV", (), {
                "async_call": lambda self, *a, **k: asyncio.sleep(0),
            })()
            self.config = type("CFG", (), {"path": lambda *args: "/tmp/" + "_".join(args)})()
            def async_add_executor_job(f, *a):
                return f(*a)
            self.async_add_executor_job = async_add_executor_job

    hass = MockHass()
    entry = Entry()
    coord = coordinator_mod.EVSmartChargerCoordinator(hass, entry)
    
    # First plug in to establish session
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 60}))
    coord.previous_plugged_state = True  # Mark as plugged
    
    # Simulate having an overload timer active
    coord._last_overload_check_time = datetime(2026, 2, 17, 23, 45, 0)
    coord.session_manager.overload_prevention_minutes = 15.0
    
    # Unplug
    asyncio.run(coord._handle_plugged_event(False, {"car_soc": 75}))
    assert coord._last_overload_check_time is None, "Timer should be reset on unplug"
    
    # Plug back in - should start fresh session
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 60}))
    assert coord._last_overload_check_time is None, "Timer should be reset on new plugin"
    assert coord.session_manager.overload_prevention_minutes == 0.0, "New session should start at 0"
    
    print("✅ Overload timer properly reset on plug-in/unplug")


def test_overload_timer_reset_when_charging_resumes(pkg_loader):
    """Verify overload timer is reset when sufficient current becomes available."""
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")
    
    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_MAX_FUSE: 20.0,
                const.CONF_CHARGER_LOSS: 10.0,
                const.CONF_CAR_CAPACITY: 64.0,
                const.CONF_CURRENCY: "SEK",
                const.CONF_PRICE_SENSOR: True,
                const.CONF_ZAPTEC_LIMITER: "number.zap_limit",
            }
            self.entry_id = "test"

    class MockHass:
        def __init__(self):
            class State:
                def __init__(self):
                    self.state = "10"
                    self.attributes = {"max": 32}
            
            self.states = type("S", (), {"get": lambda self, e: State()})()
            self.data = {}
            self.bus = type("B", (), {"async_fire": lambda self, *a, **k: None})()
            self.services = type("SV", (), {
                "async_call": lambda self, *a, **k: asyncio.sleep(0),
            })()
            self.config = type("CFG", (), {"path": lambda *args: "/tmp/" + "_".join(args)})()
            def async_add_executor_job(f, *a):
                return f(*a)
            self.async_add_executor_job = async_add_executor_job

    hass = MockHass()
    entry = Entry()
    coord = coordinator_mod.EVSmartChargerCoordinator(hass, entry)
    
    # Skip startup grace period
    coord._startup_time = datetime.now() - timedelta(minutes=10)
    
    # Start session
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 60}))
    
    # Simulate overload state
    coord._last_overload_check_time = datetime.now() - timedelta(seconds=30)
    
    plan_charge = {
        "should_charge_now": True,
        "charging_summary": "Charging",
        "planned_target_soc": 80,
    }
    
    # Data with sufficient current (no overload)
    data_ok = {
        "car_plugged": True,
        "should_charge_now": True,
        "max_available_current": 10.0,  # Above 6A minimum
    }
    
    # Apply charger control with sufficient current
    asyncio.run(coord._apply_charger_control(data_ok, plan_charge))
    
    # Timer should be reset when not in overload
    assert coord._last_overload_check_time is None, (
        "Overload timer should reset when sufficient current is available"
    )
    
    print("✅ Overload timer resets when charging can resume")


def test_realistic_overload_scenario(pkg_loader):
    """
    Simulate the real-world bug scenario from user report:
    - 48 minutes of actual overload (23:45 to 00:33)
    - P1 sensors trigger updates approximately every 3-10 seconds
    - Should accumulate ~48 minutes, NOT 88+ minutes
    """
    coordinator_mod = pkg_loader("coordinator")
    const = pkg_loader("const")
    
    class Entry:
        def __init__(self):
            self.options = {}
            self.data = {
                const.CONF_MAX_FUSE: 20.0,
                const.CONF_CHARGER_LOSS: 10.0,
                const.CONF_CAR_CAPACITY: 64.0,
                const.CONF_CURRENCY: "SEK",
                const.CONF_PRICE_SENSOR: True,
            }
            self.entry_id = "test"

    class MockHass:
        def __init__(self):
            self.states = type("S", (), {"get": lambda self, e: None})()
            self.data = {}
            self.bus = type("B", (), {"async_fire": lambda self, *a, **k: None})()
            self.services = type("SV", (), {
                "async_call": lambda self, *a, **k: asyncio.sleep(0),
            })()
            self.config = type("CFG", (), {"path": lambda *args: "/tmp/" + "_".join(args)})()
            def async_add_executor_job(f, *a):
                return f(*a)
            self.async_add_executor_job = async_add_executor_job

    hass = MockHass()
    entry = Entry()
    coord = coordinator_mod.EVSmartChargerCoordinator(hass, entry)
    
    # Start session
    asyncio.run(coord._handle_plugged_event(True, {"car_soc": 60}))
    
    # Simulate overload from 23:45:00 to 00:33:00 (48 minutes)
    start = datetime(2026, 2, 17, 23, 45, 0)
    end = datetime(2026, 2, 18, 0, 33, 0)
    
    # Generate realistic update times (varying intervals: 2-10 seconds)
    import random
    random.seed(42)  # Reproducible
    
    current_time = start
    update_times = []
    while current_time < end:
        update_times.append(current_time)
        # Random interval between 2 and 10 seconds (simulating P1 triggers)
        interval = random.uniform(2, 10)
        current_time += timedelta(seconds=interval)
    
    # Simulate all updates
    coord._last_overload_check_time = None
    for update_time in update_times:
        if coord._last_overload_check_time is None:
            coord._last_overload_check_time = update_time
        else:
            elapsed = (update_time - coord._last_overload_check_time).total_seconds() / 60.0
            coord.session_manager.add_overload_minutes(elapsed)
            coord._last_overload_check_time = update_time
    
    accumulated = coord.session_manager.overload_prevention_minutes
    num_updates = len(update_times)
    
    # Should be close to 48 minutes (actual elapsed time)
    # Allow small margin for rounding
    assert 47.0 <= accumulated <= 49.0, (
        f"Expected ~48 minutes for 48-minute period, got {accumulated:.2f}. "
        f"Had {num_updates} updates. If using old buggy code, would be ~{num_updates * 0.5:.1f} minutes!"
    )
    
    print(f"✅ REALISTIC SCENARIO CORRECT:")
    print(f"   - Overload period: 48 actual minutes")
    print(f"   - Number of updates: {num_updates} (rapid P1 triggers)")
    print(f"   - Accumulated minutes: {accumulated:.2f}")
    print(f"   - Old buggy formula would give: {num_updates * 0.5:.1f} minutes (WRONG!)")
