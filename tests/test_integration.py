
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime
from time import time as timestamp

@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.loop.call_later = MagicMock()
    hass.states.get.return_value = None
    hass.data = {}
    hass.services.async_call = AsyncMock()
    return hass

@pytest.mark.asyncio
async def test_full_charging_cycle(pkg_loader, mock_hass):
    """
    Integration Test: 'The Night Charge'
    
    Scenario:
    1. 18:00 - Plug in. Price is high. Should NOT charge.
    2. 02:00 - Price is low. Should START charging.
    3. 02:15 - Household load high. Should REDUCE current (Load Balancing).
    4. 07:00 - Unplug. Session finalizes.
    
    Note: Requires correct date progression to avoid startup verification buffer.
    """
    
    # Load Real Modules
    const = pkg_loader("const")
    pkg_loader("planner")
    coordinator_mod = pkg_loader("coordinator")
    
    # SETUP
    entry = MagicMock()
    entry.entry_id = "integration_test"
    entry.data = {
        const.CONF_P1_L1: "sensor.p1",
        const.CONF_P1_L2: "sensor.p2", 
        const.CONF_P1_L3: "sensor.p3",
        const.CONF_MAX_FUSE: 16.0,
        const.CONF_CHARGER_LOSS: 0.0,
        const.CONF_CAR_CAPACITY: 80.0, # 80kWh battery
        const.CONF_CAR_SOC_SENSOR: "sensor.soc",
        const.CONF_PRICE_SENSOR: "sensor.price",
        const.CONF_CAR_PLUGGED_SENSOR: "sensor.plugged",
        const.ENTITY_TARGET_SOC: 80, # Target 80%
        const.ENTITY_DEPARTURE_TIME: "07:00",
        const.CONF_ZAPTEC_LIMITER: "number.zap_limit",
    }
    entry.options = {}

    # State Database to mock HASS state machine
    state_db = {}
    def get_state(eid):
        return state_db.get(eid)
    
    def set_state(eid, state, attrs=None):
        state_db[eid] = MagicMock(state=str(state), attributes=attrs or {})
        
    mock_hass.states.get.side_effect = get_state
    
    # Initialize States
    set_state("sensor.soc", "40") # 40% SoC
    set_state("sensor.plugged", "off") # Unplugged
    set_state("sensor.p1", "0")
    set_state("sensor.p2", "0")
    set_state("sensor.p3", "0")
    
    # Prices: Expensive (10 SEK) normally, Cheap (1 SEK) 02:00-05:00
    today_prices = [10.0] * 24
    today_prices[2] = 1.0 # 02:00
    today_prices[3] = 1.0 # 03:00
    today_prices[4] = 1.0 # 04:00
    
    # Nordpool format roughly
    price_attrs = {
        "raw_today": [{"value": p} for p in today_prices], 
        "today": today_prices
    }
    set_state("sensor.price", today_prices[datetime.now().hour], price_attrs)

    # Patch with string paths to capture module-level datetime imports
    with patch("custom_components.ev_smart_charger.coordinator.async_track_state_change_event"), \
         patch("custom_components.ev_smart_charger.coordinator.datetime") as mock_dt_coord, \
         patch("custom_components.ev_smart_charger.planner.datetime") as mock_dt_plan:

        # Sync all mock datetime objects
        for mock_dt in [mock_dt_coord, mock_dt_plan]:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.min = datetime.min
            mock_dt.max = datetime.max
            mock_dt.strptime = datetime.strptime
            mock_dt.time = datetime.time
            mock_dt.combine = datetime.combine
        
        def set_time(dt):
            mock_dt_coord.now.return_value = dt
            mock_dt_plan.now.return_value = dt
        
        # -----------------------------------------------------------------
        # PHASE 1: PLUG IN (18:00)
        # -----------------------------------------------------------------
        start_time = datetime(2023, 1, 1, 18, 0, 0)
        set_time(start_time)
        
        coordinator = coordinator_mod.EVSmartChargerCoordinator(mock_hass, entry)
        # Initialization sets startup_time to now(), so it matches start_time
        coordinator._data_loaded = True 
        
        set_state("sensor.plugged", "on")
        await coordinator._async_update_data()
        
        assert coordinator.session_manager.current_session is not None
        assert coordinator._last_applied_state != "charging"
        
        # -----------------------------------------------------------------
        # PHASE 2: CHARGING START (02:00 - Next Day)
        # -----------------------------------------------------------------
        # Must advance day to ensure time > startup_time + 2 mins
        set_time(datetime(2023, 1, 2, 2, 0, 0))
        set_state("sensor.soc", "40") # Still 40%
        
        data = await coordinator._async_update_data()
        
        assert coordinator._last_applied_state == "charging", "Should be charging at 02:00"
        # Expect 15A because: Max Fuse 16A - Buffer 1A (min buffer) = 15A Available.
        assert coordinator._last_applied_amps == 15.0 
        
        # -----------------------------------------------------------------
        # PHASE 3: LOAD BALANCING (02:15)
        # -----------------------------------------------------------------
        set_time(datetime(2023, 1, 2, 2, 15, 0))
        set_state("sensor.p1", "8.0") # High load on Phase 1
        
        await coordinator._async_update_data()
        
        # 16A Fuse - 8A Load - 1A Buffer = 7A Available
        assert coordinator._last_applied_state == "charging"
        assert coordinator._last_applied_amps <= 7.0 
        assert coordinator._last_applied_amps >= 6.0 
        
        # -----------------------------------------------------------------
        # PHASE 4: UNPLUG (07:00)
        # -----------------------------------------------------------------
        set_time(datetime(2023, 1, 2, 7, 0, 0))
        set_state("sensor.plugged", "off")
        set_state("sensor.soc", "80")
        
        await coordinator._async_update_data()
        
        assert coordinator.session_manager.current_session is None
        report = coordinator.session_manager.last_session_data
        assert report is not None
        assert float(report["end_soc"]) == 80.0
