
from unittest.mock import MagicMock
from datetime import datetime
import pytest

# Use dynamic loading fixture
def test_session_lifecyle(pkg_loader):
    session_mod = pkg_loader("session_manager")
    const = pkg_loader("const")
    
    hass = MagicMock()
    manager = session_mod.SessionManager(hass)

    # Start
    manager.start_session(50.0)
    assert manager.current_session is not None
    assert "start_time" in manager.current_session
    assert len(manager.current_session["history"]) == 0
    assert len(manager.current_session["log"]) == 0
    
    # Record data
    data = {
        "car_soc": 55.0,
        "price_data": {}
    }
    user_settings = {const.ENTITY_PRICE_EXTRA_FEE: 0.1}
    
    manager.record_data_point(data, user_settings, 16.0, "charging")
    
    assert len(manager.current_session["history"]) == 1
    point = manager.current_session["history"][0]
    assert point["soc"] == 55.0
    assert point["amps"] == 16.0
    assert point["charging"] == 1
    
    # Mark interval
    manager.mark_charging_in_interval()
    assert manager._was_charging_in_interval is True
    
    # Stop
    report = manager.stop_session(user_settings, "SEK")
    assert manager.current_session is None
    assert manager.last_session_data == report
    assert report["currency"] == "SEK"
    assert report["added_kwh"] == 0.0 # because only 1 point, no duration

def test_persistence(pkg_loader):
    session_mod = pkg_loader("session_manager")
    hass = MagicMock()
    manager = session_mod.SessionManager(hass)
    
    manager.add_log("Test log")
    # Note: overload_prevention_minutes is no longer persisted (session-only)
    
    exported = manager.to_dict()
    assert "overload_prevention_minutes" not in exported  # Should not be persisted
    assert len(exported["action_log"]) == 1
    
    # Load into new manager
    manager2 = session_mod.SessionManager(hass)
    manager2.load_from_dict(exported)
    assert manager2.overload_prevention_minutes == 0.0  # Starts fresh
    assert len(manager2.action_log) == 1
