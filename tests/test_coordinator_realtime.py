
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import pytest

# NOTE: Do NOT import homeassistant.* at top level. Use pkg_loader.

@pytest.fixture
def mock_track_state_change():
    # We patch the import path in the coordinator module, BUT since we load coordinator dynamically,
    # we might need to patch it where it is imported.
    # Actually, pkg_loader loads the module. We should patch 'homeassistant.helpers.event.async_track_state_change_event'
    # which is what our stub provides, OR patch the module after loading.
    # But since we use 'from ... import ...', we need to patch BEFORE loading or patch the imported name.
    
    # Simpler: The coordinator imports it as: 
    # from homeassistant.helpers.event import async_track_state_change_event
    # So we should patch it in the coordinator module namespace.
    # But we need to load the coordinator module first.
    pass

@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.loop.call_later = MagicMock()
    hass.states.get.return_value = None
    return hass

def test_listeners_setup_and_shutdown(pkg_loader, mock_hass):
    # Load modules
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    # Mocking config entry
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {
        const.CONF_P1_L1: "sensor.p1_l1",
        const.CONF_P1_L2: "sensor.p1_l2",
        const.CONF_P1_L3: "sensor.p1_l3",
        const.CONF_MAX_FUSE: const.DEFAULT_MAX_FUSE,
        const.CONF_CHARGER_LOSS: const.DEFAULT_LOSS,
        const.CONF_CAR_CAPACITY: const.DEFAULT_CAPACITY,
        const.CONF_CAR_SOC_SENSOR: "sensor.car_soc",
        const.CONF_ZAPTEC_LIMITER: "number.zap_limit",
        const.CONF_PRICE_SENSOR: "sensor.nordpool_kwh",
    }
    entry.options = {}

    # Patch async_track_state_change_event inside the loaded coordinator module
    with patch.object(coordinator_mod, "async_track_state_change_event") as mock_track:
        coordinator = coordinator_mod.EVSmartChargerCoordinator(mock_hass, entry)
        
        # Setup listeners
        coordinator.async_setup_listeners()
        
        # Verify call
        mock_track.assert_called_once()
        args, _ = mock_track.call_args
        assert args[0] == mock_hass
        assert "sensor.p1_l1" in args[1]
        
        # Check callback
        callback_func = args[2]
        assert callback_func == coordinator._async_p1_update_callback
        
        # Shutdown
        mock_unsub = mock_track.return_value
        coordinator.async_shutdown()
        mock_unsub.assert_called_once()
        assert len(coordinator._safety_listeners) == 0

def test_update_callback_triggers_refresh(pkg_loader, mock_hass):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    entry = MagicMock()
    entry.entry_id = "test"
    entry.data = {
        const.CONF_P1_L1: "sensor.p1", 
        const.CONF_P1_L2: "sensor.p2", 
        const.CONF_P1_L3: "sensor.p3",
        const.CONF_MAX_FUSE: 20, 
        const.CONF_CHARGER_LOSS: 10,
        const.CONF_CAR_CAPACITY: 60,
        const.CONF_CAR_SOC_SENSOR: "sensor.soc"
    }
    entry.options = {}

    with patch.object(coordinator_mod, "async_track_state_change_event"):
        coordinator = coordinator_mod.EVSmartChargerCoordinator(mock_hass, entry)
        coordinator.async_request_refresh = MagicMock()
        
        # Simulate callback
        coordinator._async_p1_update_callback(None)

        coordinator.async_request_refresh.assert_called_once()
        assert coordinator._debounce_unsub is None

@pytest.mark.asyncio
async def test_performance_latency_calculated(pkg_loader, mock_hass):
    """Verify that _async_update_data calculates and populates latency_ms."""
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    entry = MagicMock()
    entry.entry_id = "perf_test"
    entry.data = {
        const.CONF_P1_L1: "sensor.p1", 
        const.CONF_P1_L2: "sensor.p2", 
        const.CONF_P1_L3: "sensor.p3",
        const.CONF_MAX_FUSE: 25.0, 
        const.CONF_CHARGER_LOSS: 8.0,
        const.CONF_CAR_CAPACITY: 75.0,
        const.CONF_CAR_SOC_SENSOR: "sensor.soc",
        const.CONF_PRICE_SENSOR: "sensor.price",
        const.CONF_CAR_PLUGGED_SENSOR: "sensor.plugged",
        const.ENTITY_SMART_SWITCH: True,
    }
    entry.options = {}
    
    # Mock states needed for _fetch_sensor_data
    mock_hass.states.get.side_effect = lambda eid: MagicMock(state="10", attributes={})

    with patch.object(coordinator_mod, "async_track_state_change_event"):
        coordinator = coordinator_mod.EVSmartChargerCoordinator(mock_hass, entry)
        
        # We need to manually call _async_update_data since we can't easily rely on async_request_refresh 
        # in this isolated test without full HA core loop.
        
        # Mock planner to avoid complex logic during perf test if desired, 
        # BUT user wants to "feel the speed", so let's let it run if possible.
        # However, we need to mock internal calls that would fail.
        
        # We need to ensure _load_data doesn't fail
        coordinator._data_loaded = True 
        
        data = await coordinator._async_update_data()
        
        assert "latency_ms" in data
        assert isinstance(data["latency_ms"], float)
        assert data["latency_ms"] >= 0.0
        
        # Print for the user to see in test output (use -s to see it)
        print(f"\n--> Measured Latency: {data['latency_ms']} ms <--")


def test_update_callback_debounces(pkg_loader, mock_hass):
    const = pkg_loader("const")
    coordinator_mod = pkg_loader("coordinator")
    
    entry = MagicMock()
    entry.entry_id = "test"
    entry.data = {
        const.CONF_P1_L1: "sensor.p1", 
        const.CONF_P1_L2: "sensor.p2", 
        const.CONF_P1_L3: "sensor.p3",
        const.CONF_MAX_FUSE: 20, 
        const.CONF_CHARGER_LOSS: 10,
        const.CONF_CAR_CAPACITY: 60,
        const.CONF_CAR_SOC_SENSOR: "sensor.soc"
    }
    entry.options = {}

    with patch.object(coordinator_mod, "async_track_state_change_event"):
        coordinator = coordinator_mod.EVSmartChargerCoordinator(mock_hass, entry)
        coordinator.async_request_refresh = MagicMock()
        
        # First call
        coordinator._async_p1_update_callback(None)
        coordinator.async_request_refresh.assert_called_once()
        coordinator.async_request_refresh.reset_mock()
        
        # Second call - debounced
        # Need to patch datetime inside coordinator mod
        with patch("custom_components.ev_optimizer.coordinator.datetime") as mock_dt:
            # Note: since coordinator IS loaded from that path by pkg_loader, this patch *should* work
            # provided sys.modules is set up correctly by pkg_loader.
            # pkg_loader uses _load_pkg_module which puts it in sys.modules.
            
            mock_dt.now.return_value = coordinator._last_p1_update + timedelta(seconds=0.1)
            
            coordinator._async_p1_update_callback(None)
            
            coordinator.async_request_refresh.assert_not_called()
            mock_hass.loop.call_later.assert_called_once()
            
            # Fire timer
            args, _ = mock_hass.loop.call_later.call_args
            callback = args[1]
            callback()
            
            coordinator.async_request_refresh.assert_called_once()
