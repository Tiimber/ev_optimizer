# Enhanced Debugging Features - Summary

## What Was Added

This update adds comprehensive debugging capabilities to help diagnose unexpected charging behavior.

## Changes Made

### 1. Enhanced Debug Logging in Planner (`planner.py`)

Added detailed step-by-step logging throughout the `generate_charging_plan()` function with emoji indicators:

- 🔍 Plan generation start/end
- 📊 Input data summary  
- 💰 Price data availability
- 🕐 Departure time calculation
- 🌅 Price horizon coverage
- 🎯 Target SOC determination (manual/calendar/opportunistic)
- 🔋 Battery state (current vs target)
- ⚡ Energy and timing calculations
- 📊 Slot selection logic
- ✅ Selected charging slots with times and prices
- ⚡ Final decision with reasoning

### 2. Debug State Dump Service (`coordinator.py`)

Added `dump_debug_state()` method that outputs complete JSON snapshot including:

- All configuration settings
- User settings from UI
- Current sensor readings
- Complete price data (today/tomorrow)
- Calendar events
- Session state
- Last plan output
- Entity ID mappings

The output is logged in a clearly marked section for easy copy/paste.

### 3. Service and Button Registration

**New Service:** `ev_smart_charger.dump_debug_state`
- Registered in `__init__.py`
- Documented in `services.yaml`

**New Button:** `button.ev_smart_charger_dump_debug_state`
- Added to `button.py`
- Easy one-click access to dump state

### 4. Documentation

**New Files:**
- `DEBUGGING.md` - Complete debugging guide
- `simulate_from_dump.py` - Script to visualize debug dumps

**Updated:**
- `README.md` - Added debugging section with link to guide

## How to Use (Quick Start)

### For Users Having Issues:

1. **Enable debug logging** in `configuration.yaml`:
   ```yaml
   logger:
     default: info
     logs:
       custom_components.ev_smart_charger: debug
   ```

2. **Reproduce the issue** (e.g., plug in the car)

3. **Press the "Dump Debug State" button** 
   (or call service `ev_smart_charger.dump_debug_state`)

4. **Check Home Assistant logs** and copy:
   - The JSON debug dump (between the markers)
   - The relevant log entries with emoji indicators

5. **Share** both when reporting the issue

### For Developers:

The debug dump can be used to:
- Create exact reproduction test cases
- Simulate scenarios without a running HA instance
- Understand edge cases and user configurations

## Example Output

### Debug Logs
```
🔍 ===== CHARGING PLAN GENERATION START ===== Time: 2026-01-30 15:30:00
📊 Input data: car_plugged=True, car_soc=25, smart_switch=True, manual_override=False
💰 Price data: today=24 slots, tomorrow=24 slots, tomorrow_valid=True
🎯 Target SOC: base=80%, min_price=0.45, limit_1=0.50→100%
   → Opportunistic Level 1 triggered: target=100%
⚡ Energy calculation: kwh_needed=37.50, efficiency=0.90, kwh_to_pull=41.67
✅ Selected 4 cheapest slots: 23:00→0.42, 00:00→0.43, 01:00→0.44, 02:00→0.45
⚡ ===== FINAL DECISION: should_charge_now=False, target_soc=100% =====
```

### Debug Dump
```json
{
  "timestamp": "2026-01-30T15:30:00",
  "config_settings": {
    "max_fuse": 16.0,
    "charger_loss": 10.0,
    "car_capacity": 50.0
  },
  "sensor_data": {
    "car_soc": 25,
    "car_plugged": true
  },
  "price_data": {
    "today": [0.85, 0.82, 0.78, ...],
    "tomorrow": [0.42, 0.43, 0.44, ...]
  }
}
```

## Benefits

1. **User self-diagnosis** - Users can understand why charging decisions were made
2. **Faster support** - Complete context in one JSON dump
3. **Reproducible bugs** - Exact state can be recreated for testing
4. **Transparent logic** - Every decision step is explained in logs
5. **Better issue reports** - Users know exactly what to share

## Files Modified

- `custom_components/ev_smart_charger/planner.py` - Added debug logging
- `custom_components/ev_smart_charger/coordinator.py` - Added dump_debug_state method
- `custom_components/ev_smart_charger/__init__.py` - Registered new service
- `custom_components/ev_smart_charger/button.py` - Added dump button entity
- `custom_components/ev_smart_charger/services.yaml` - Documented new service
- `README.md` - Added debugging section
- `DEBUGGING.md` - New comprehensive debugging guide
- `simulate_from_dump.py` - New utility script

## Testing Recommendations

1. Test with debug logging enabled - verify logs are readable
2. Call the dump service - verify JSON is valid and complete
3. Press the button entity - verify it triggers the dump
4. Test the simulator script with a real dump
5. Verify no performance impact (logging should be minimal when debug disabled)
