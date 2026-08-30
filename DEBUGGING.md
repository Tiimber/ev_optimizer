# Debugging Guide for EV Optimizer

This guide explains how to use the enhanced debugging features to diagnose issues with charging decisions.

## Check the diagnostic entities first

Three binary sensors report conditions that are otherwise invisible, and one of
them explains most "the state of charge looks wrong" reports:

* **`binary_sensor.charge_estimate_drift`** — the charger reports no output while
  the integration believes it is charging, for more than five minutes. This
  happens when nothing measures the charger's real output, so the estimate is
  advancing from the current we *commanded*. Configure a **Charger Energy Meter**
  or **Charger Power** sensor and it cannot occur.
* **`binary_sensor.car_limit_drift`** — the car's reported charge limit disagrees
  with the one we applied, more than 45 minutes after we applied it. Setting the
  limit is fire-and-forget, so this is the only sign it did not take.
* **`binary_sensor.manual_override_active`** — whether the next session's target
  came from you or from the planner.

Also useful: `sensor.ev_optimizer_planned_charge_power` reports the power figure
plans are sized on and whether it is measured or derived from your fuse rating,
and `sensor.ev_optimizer_virtual_state_of_charge` shows the estimate, the car's
last reported value, the drift between them, and which measurement source is in
use.

## Overview

The integration now includes:
1. **Comprehensive debug logging** - Step-by-step explanation of every charging decision
2. **State dump service/button** - Complete snapshot of all data for simulation

## 1. Enable Debug Logging

Add this to your `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.ev_optimizer: debug
    custom_components.ev_optimizer.planner: debug
    custom_components.ev_optimizer.coordinator: debug
```

Restart Home Assistant. Now all charging decisions will be logged with detailed explanations.

## 2. Understanding Debug Log Output

When the planner runs, you'll see logs like this:

```
🔍 ===== CHARGING PLAN GENERATION START ===== Time: 2026-01-30 15:30:00
📊 Input data: car_plugged=True, car_soc=25, smart_switch=True, manual_override=False
💰 Price data: today=24 slots, tomorrow=24 slots, tomorrow_valid=True
🕐 Departure time: 2026-01-31 07:00 (from manual setting)
📈 Price window: 37 slots available until departure
🌅 Price horizon: last_price_end=2026-01-31 23:00, covers_departure=True
🎯 Target SOC: base=80%, min_price=0.45, limit_1=0.50→100%, limit_2=1.50→80%
🔋 Battery: current_soc=25.0%, target=80.0%, need=55.0%
⚡ Energy calculation: kwh_needed=27.50, efficiency=0.90, kwh_to_pull=30.56
⏱️  Timing: est_power=11.00 kW, hours_needed=2.78, overload_prevention_min=0.0
📊 Slot calculation: duration=1.00h, base_slots=3, extra_slots=0 (overload compensation)
✅ Selected 3 cheapest slots: 23:00→0.42, 00:00→0.43, 01:00→0.44
⚡ ===== FINAL DECISION: should_charge_now=False, target_soc=80% =====
```

### Key Indicators:

- **🔍** Start of plan generation
- **💰** Price data availability
- **🎯** How target SOC was determined (manual/calendar/opportunistic)
- **⚡** Energy calculations and timing
- **✅** Which slots were selected and why
- **⚡ FINAL DECISION** - The actual outcome

## 3. Dump Complete State for Debugging

When you need to share your exact situation for debugging:

### Method 1: Use the Button Entity

1. Go to your Home Assistant dashboard
2. Find: `button.ev_optimizer_dump_debug_state`
3. Press the button
4. Check your Home Assistant logs

### Method 2: Use the Service

1. Go to Developer Tools → Services
2. Select service: `ev_optimizer.dump_debug_state`
3. Call the service
4. Check your Home Assistant logs

### Method 3: Via Automation/Script

```yaml
service: ev_optimizer.dump_debug_state
```

## 4. Reading the Debug Dump

After calling the dump service, your logs will contain:

```
================================================================================
DEBUG STATE DUMP - Copy everything between the markers:
================================================================================
{
  "timestamp": "2026-01-30T15:30:00",
  "description": "Complete state dump for ev_optimizer debugging/simulation",
  "config_settings": {
    "max_fuse": 16.0,
    "charger_loss": 10.0,
    "car_capacity": 50.0,
    "currency": "SEK",
    "has_price_sensor": true,
    "contiguous_charging": false,
    "prefer_late_slots": false
  },
  "user_settings": {
    "target_soc": 80,
    "min_soc": 20,
    "departure_time": "07:00:00",
    "smart_switch": true,
    ...
  },
  "sensor_data": {
    "car_soc": 25,
    "car_plugged": true,
    ...
  },
  "price_data": {
    "today": [0.85, 0.82, 0.78, ...],
    "tomorrow": [0.42, 0.43, 0.44, ...],
    "tomorrow_valid": true
  },
  ...
}
================================================================================
```

**Copy the entire JSON** (between the markers) and share it when asking for help. This contains everything needed to reproduce your exact situation.

## 5. Common Issues & What to Look For

### Issue: "Started charging immediately on plugin"

**Look for in logs:**
- What does `should_charge_now` show? 
- Check `price_horizon_covers_departure` - if `False`, integration may be waiting for tomorrow's prices
- Check `current_soc` vs `planned_target_soc` - already at target?
- Look for "opportunistic level" triggers - cheap prices now?

### Issue: "Not charging when prices are cheap"

**Look for in logs:**
- Is `car_plugged=True`?
- Is `smart_switch=True`?
- Check if current time falls within selected slots
- Look for "Waiting for more price data" message

### Issue: "Wrong target SOC"

**Look for in logs:**
- Search for "🎯 Target SOC:" to see how it was determined
- Check for calendar events
- Check manual override state
- Look at opportunistic level triggers

## 6. Simulating Issues Locally

If you share the debug dump JSON, developers can:

1. Create a test case using the exact data
2. Run the planner with your inputs
3. See exactly what decision was made and why

This makes it much easier to fix edge cases and unexpected behaviors!

## 7. Getting Help

When reporting an issue:

1. ✅ Enable debug logging
2. ✅ Reproduce the issue
3. ✅ Press "Dump Debug State" button
4. ✅ Copy the JSON from logs
5. ✅ Copy relevant log entries showing the decision
6. ✅ Share both in your issue report

With this information, we can typically diagnose and fix issues quickly!
