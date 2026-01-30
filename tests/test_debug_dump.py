"""Test debug dump functionality."""
import pytest
from datetime import time
from unittest.mock import Mock, MagicMock
from custom_components.ev_smart_charger.coordinator import EVSmartChargerCoordinator
from custom_components.ev_smart_charger.const import (
    ENTITY_TARGET_SOC,
    ENTITY_MIN_SOC,
    ENTITY_DEPARTURE_TIME,
    ENTITY_DEPARTURE_OVERRIDE,
    ENTITY_SMART_SWITCH,
    ENTITY_TARGET_OVERRIDE,
    ENTITY_PRICE_LIMIT_1,
    ENTITY_TARGET_SOC_1,
    ENTITY_PRICE_LIMIT_2,
    ENTITY_TARGET_SOC_2,
    ENTITY_PRICE_EXTRA_FEE,
    ENTITY_PRICE_VAT,
)


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.config.path = Mock(return_value="/config/test")
    hass.data = {}
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
    entry.async_on_unload = Mock()
    entry.add_update_listener = Mock()
    return entry


@pytest.mark.asyncio
async def test_debug_dump_all_constants_defined(mock_hass, mock_entry):
    """Test that dump_debug_state can access all required constants."""
    # Create coordinator
    coordinator = EVSmartChargerCoordinator(mock_hass, mock_entry)
    
    # Set up some user settings
    coordinator.user_settings = {
        ENTITY_TARGET_SOC: 80,
        ENTITY_MIN_SOC: 20,
        ENTITY_DEPARTURE_TIME: time(7, 0),
        ENTITY_DEPARTURE_OVERRIDE: time(8, 0),
        ENTITY_SMART_SWITCH: True,
        ENTITY_TARGET_OVERRIDE: 90,
        ENTITY_PRICE_LIMIT_1: 0.5,
        ENTITY_TARGET_SOC_1: 100,
        ENTITY_PRICE_LIMIT_2: 1.5,
        ENTITY_TARGET_SOC_2: 80,
        ENTITY_PRICE_EXTRA_FEE: 0.1,
        ENTITY_PRICE_VAT: 25.0,
    }
    
    # Set up some data
    coordinator.data = {
        "car_soc": 75,
        "car_plugged": True,
        "price_data": {
            "today": [0.5, 0.6, 0.7],
            "tomorrow": [0.4, 0.5, 0.6],
            "tomorrow_valid": True,
        },
        "should_charge_now": False,
    }
    
    # This should not raise any NameError
    try:
        dump = coordinator.dump_debug_state()
        
        # Verify the dump contains expected data
        assert dump is not None
        assert "timestamp" in dump
        assert "config_settings" in dump
        assert "user_settings" in dump
        assert "sensor_data" in dump
        assert "price_data" in dump
        
        # Verify all constants are accessible in user_settings
        user_settings = dump["user_settings"]
        assert ENTITY_TARGET_SOC in user_settings
        assert ENTITY_MIN_SOC in user_settings
        assert ENTITY_PRICE_LIMIT_1 in user_settings
        assert ENTITY_TARGET_SOC_1 in user_settings
        assert ENTITY_PRICE_LIMIT_2 in user_settings
        assert ENTITY_TARGET_SOC_2 in user_settings
        assert ENTITY_PRICE_EXTRA_FEE in user_settings
        assert ENTITY_PRICE_VAT in user_settings
        
        # Verify values are correct
        assert user_settings[ENTITY_TARGET_SOC] == 80
        assert user_settings[ENTITY_PRICE_LIMIT_1] == 0.5
        assert user_settings[ENTITY_TARGET_SOC_1] == 100
        
        print("✅ Debug dump test passed - all constants are properly imported and accessible")
        
    except NameError as e:
        pytest.fail(f"NameError raised during dump_debug_state: {e}")


@pytest.mark.asyncio
async def test_debug_dump_with_minimal_data(mock_hass, mock_entry):
    """Test dump_debug_state works even with minimal/empty data."""
    coordinator = EVSmartChargerCoordinator(mock_hass, mock_entry)
    
    # Minimal setup - empty user_settings and data
    coordinator.user_settings = {}
    coordinator.data = {}
    
    # Should not crash even with empty data
    try:
        dump = coordinator.dump_debug_state()
        assert dump is not None
        assert "user_settings" in dump
        
        # Should use defaults
        user_settings = dump["user_settings"]
        assert user_settings[ENTITY_TARGET_SOC] == 80  # default
        assert user_settings[ENTITY_MIN_SOC] == 20  # default
        
        print("✅ Debug dump with minimal data test passed")
        
    except Exception as e:
        pytest.fail(f"Exception raised with minimal data: {e}")


def test_all_required_constants_exist():
    """Test that all required constants are defined in const.py."""
    # This test verifies the imports don't fail
    constants_to_check = [
        ENTITY_TARGET_SOC,
        ENTITY_MIN_SOC,
        ENTITY_DEPARTURE_TIME,
        ENTITY_DEPARTURE_OVERRIDE,
        ENTITY_SMART_SWITCH,
        ENTITY_TARGET_OVERRIDE,
        ENTITY_PRICE_LIMIT_1,
        ENTITY_TARGET_SOC_1,
        ENTITY_PRICE_LIMIT_2,
        ENTITY_TARGET_SOC_2,
        ENTITY_PRICE_EXTRA_FEE,
        ENTITY_PRICE_VAT,
    ]
    
    for const in constants_to_check:
        assert isinstance(const, str), f"Constant should be a string: {const}"
        assert len(const) > 0, f"Constant should not be empty: {const}"
    
    print("✅ All required constants exist and are properly defined")
