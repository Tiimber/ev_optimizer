"""Proper test suite for debug dump functionality with mocking."""
import pytest
from datetime import time, datetime
from unittest.mock import Mock, MagicMock, patch
import sys


@pytest.fixture
def mock_homeassistant():
    """Mock the homeassistant module."""
    # Create mock modules
    ha_mock = MagicMock()
    ha_config_entries = MagicMock()
    ha_core = MagicMock()
    ha_helpers = MagicMock()
    ha_helpers_update_coordinator = MagicMock()
    ha_helpers_storage = MagicMock()
    ha_helpers_event = MagicMock()
    
    # Set up module structure
    sys.modules['homeassistant'] = ha_mock
    sys.modules['homeassistant.config_entries'] = ha_config_entries
    sys.modules['homeassistant.core'] = ha_core
    sys.modules['homeassistant.helpers'] = ha_helpers
    sys.modules['homeassistant.helpers.update_coordinator'] = ha_helpers_update_coordinator
    sys.modules['homeassistant.helpers.storage'] = ha_helpers_storage
    sys.modules['homeassistant.helpers.event'] = ha_helpers_event
    sys.modules['homeassistant.const'] = MagicMock()
    
    # Mock classes
    ha_helpers_update_coordinator.DataUpdateCoordinator = object
    ha_helpers_update_coordinator.UpdateFailed = Exception
    
    # Store needs to be a callable that returns a mock, not a Mock itself
    def mock_store_factory(*args, **kwargs):
        store = Mock()
        store.async_load = Mock(return_value=None)
        store.async_delay_save = Mock(return_value=None)
        return store
    
    ha_helpers_storage.Store = mock_store_factory
    
    yield ha_mock
    
    # Cleanup
    for module in list(sys.modules.keys()):
        if module.startswith('homeassistant'):
            del sys.modules[module]


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.config.path = Mock(return_value="/config/test")
    hass.data = {}
    hass.async_add_executor_job = Mock()
    hass.bus.async_fire = Mock()
    hass.services.async_call = Mock()
    return hass


@pytest.fixture
def mock_entry():
    """Create a mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {
        "max_fuse": 16.0,
        "charger_loss": 10.0,
        "car_capacity": 50.0,
        "currency": "SEK",
        "price_sensor": "sensor.nordpool",
    }
    entry.options = {}
    entry.async_on_unload = Mock(return_value=None)
    entry.add_update_listener = Mock(return_value=None)
    return entry


def test_constants_import(mock_homeassistant):
    """Test that all constants can be imported."""
    from custom_components.ev_smart_charger.const import (
        ENTITY_TARGET_SOC,
        ENTITY_MIN_SOC,
        ENTITY_PRICE_LIMIT_1,
        ENTITY_TARGET_SOC_1,
        ENTITY_PRICE_LIMIT_2,
        ENTITY_TARGET_SOC_2,
        ENTITY_PRICE_EXTRA_FEE,
        ENTITY_PRICE_VAT,
        ENTITY_DEPARTURE_TIME,
        ENTITY_DEPARTURE_OVERRIDE,
        ENTITY_SMART_SWITCH,
        ENTITY_TARGET_OVERRIDE,
    )
    
    # Verify they're all strings
    constants = [
        ENTITY_TARGET_SOC,
        ENTITY_MIN_SOC,
        ENTITY_PRICE_LIMIT_1,
        ENTITY_TARGET_SOC_1,
        ENTITY_PRICE_LIMIT_2,
        ENTITY_TARGET_SOC_2,
        ENTITY_PRICE_EXTRA_FEE,
        ENTITY_PRICE_VAT,
    ]
    
    for const in constants:
        assert isinstance(const, str)
        assert len(const) > 0
    
    print("✅ All constants imported successfully")


def test_coordinator_imports(mock_homeassistant):
    """Test that coordinator imports without errors."""
    try:
        from custom_components.ev_smart_charger.coordinator import EVSmartChargerCoordinator
        print("✅ Coordinator imported successfully")
    except ImportError as e:
        pytest.fail(f"Failed to import coordinator: {e}")


def test_session_manager_structure(mock_homeassistant):
    """Test that SessionManager has expected attributes."""
    from custom_components.ev_smart_charger.session_manager import SessionManager
    
    hass = MagicMock()
    sm = SessionManager(hass)
    
    # Check expected attributes exist
    assert hasattr(sm, 'current_session')
    assert hasattr(sm, 'action_log')
    assert hasattr(sm, 'overload_prevention_minutes')
    assert hasattr(sm, 'last_session_data')
    
    # Verify current_session can be checked for None
    assert sm.current_session is None or isinstance(sm.current_session, dict)
    
    print("✅ SessionManager has expected structure")


def test_dump_debug_state_basic(mock_homeassistant, mock_hass, mock_entry):
    """Test basic dump_debug_state functionality."""
    from custom_components.ev_smart_charger.coordinator import EVSmartChargerCoordinator
    from custom_components.ev_smart_charger.const import (
        ENTITY_TARGET_SOC,
        ENTITY_MIN_SOC,
        ENTITY_PRICE_LIMIT_1,
        ENTITY_TARGET_SOC_1,
    )
    
    # Create coordinator
    coordinator = EVSmartChargerCoordinator(mock_hass, mock_entry)
    
    # Set up minimal data
    coordinator.user_settings = {
        ENTITY_TARGET_SOC: 80,
        ENTITY_MIN_SOC: 20,
        ENTITY_PRICE_LIMIT_1: 0.5,
        ENTITY_TARGET_SOC_1: 100,
    }
    
    coordinator.data = {
        "car_soc": 75,
        "car_plugged": True,
        "price_data": {
            "today": [0.5, 0.6, 0.7],
            "tomorrow": [0.4, 0.5, 0.6],
            "tomorrow_valid": True,
        },
    }
    
    # Call dump_debug_state - should not raise any errors
    try:
        dump = coordinator.dump_debug_state()
        
        # Verify structure
        assert isinstance(dump, dict)
        assert "timestamp" in dump
        assert "config_settings" in dump
        assert "user_settings" in dump
        assert "sensor_data" in dump
        assert "price_data" in dump
        assert "session_info" in dump
        
        # Verify session_info structure
        assert "overload_prevention_minutes" in dump["session_info"]
        assert "session_active" in dump["session_info"]
        assert isinstance(dump["session_info"]["session_active"], bool)
        
        # Verify user_settings has the constants as keys
        assert ENTITY_TARGET_SOC in dump["user_settings"]
        assert ENTITY_PRICE_LIMIT_1 in dump["user_settings"]
        
        print("✅ dump_debug_state executed successfully")
        print(f"   Session active: {dump['session_info']['session_active']}")
        print(f"   Target SOC: {dump['user_settings'][ENTITY_TARGET_SOC]}")
        
    except Exception as e:
        pytest.fail(f"dump_debug_state raised exception: {e}")


def test_dump_debug_state_empty_data(mock_homeassistant, mock_hass, mock_entry):
    """Test dump_debug_state with minimal/empty data."""
    from custom_components.ev_smart_charger.coordinator import EVSmartChargerCoordinator
    
    coordinator = EVSmartChargerCoordinator(mock_hass, mock_entry)
    
    # Minimal setup
    coordinator.user_settings = {}
    coordinator.data = {}
    
    # Should not crash
    try:
        dump = coordinator.dump_debug_state()
        assert dump is not None
        assert "user_settings" in dump
        
        print("✅ dump_debug_state handles empty data gracefully")
        
    except Exception as e:
        pytest.fail(f"dump_debug_state failed with empty data: {e}")


def test_dump_debug_state_with_session(mock_homeassistant, mock_hass, mock_entry):
    """Test dump_debug_state when a session is active."""
    from custom_components.ev_smart_charger.coordinator import EVSmartChargerCoordinator
    
    coordinator = EVSmartChargerCoordinator(mock_hass, mock_entry)
    coordinator.user_settings = {}
    coordinator.data = {}
    
    # Start a session
    coordinator.session_manager.start_session(75.0)
    
    # Dump should show session as active
    dump = coordinator.dump_debug_state()
    assert dump["session_info"]["session_active"] is True
    
    print("✅ dump_debug_state correctly reports active session")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
