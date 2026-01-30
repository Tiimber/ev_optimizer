# Example: Debugging "Car Started Charging Immediately" Issue

This is a walkthrough example of how to use the new debugging features.

## The Problem

**User Report:** "My car started charging immediately when I plugged it in at 3 PM. It shouldn't - I only need 5% charge (~3 kW), and there are several cheaper price slots during the night. Why is it charging now?"

## Step 1: Enable Debug Logging

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.ev_smart_charger: debug
```

Restart Home Assistant.

## Step 2: Reproduce & Capture State

1. Plug in the car (or wait for next plugin)
2. Press the **"Dump Debug State"** button in Home Assistant
3. Open Home Assistant logs

## Step 3: Review Debug Logs

Look for the planner decision logs (with emoji indicators):

```
2026-01-30 15:03:22 DEBUG (MainThread) [custom_components.ev_smart_charger.planner] 
🔍 ===== CHARGING PLAN GENERATION START ===== Time: 2026-01-30 15:03:22
📊 Input data: car_plugged=True, car_soc=75, smart_switch=True, manual_override=False
💰 Price data: today=24 slots, tomorrow=0 slots, tomorrow_valid=False
🕐 Departure time: 2026-01-31 07:00 (from manual setting)
📈 Price window: 16 slots available until departure
🌅 Price horizon: last_price_end=2026-01-30 23:00, covers_departure=False
🎯 Target SOC: 80% (waiting for more price data)
🔋 Battery: current_soc=75.0%, target=80.0%, need=5.0%
⚡ Energy calculation: kwh_needed=2.50, efficiency=0.90, kwh_to_pull=2.78
⏱️  Timing: est_power=11.00 kW, hours_needed=0.25
🕐 Price horizon does NOT cover departure. Latest start: 06:45 (now: 15:03)
⏸️  WAITING for more price data (still have time until 06:45)
⚡ ===== FINAL DECISION: should_charge_now=False, target_soc=80% =====
```

**BUT WAIT** - The logs say `should_charge_now=False`! So why is it charging?

## Step 4: Check Coordinator Logic

Look earlier in the logs for coordinator decisions:

```
2026-01-30 15:03:22 DEBUG (MainThread) [custom_components.ev_smart_charger.coordinator]
Planner returned should_charge_now=False
Checking buffer logic: last_scheduled_end=None
```

Hmm, planner says don't charge. Let's check if there's an override...

```
2026-01-30 15:03:21 INFO (MainThread) [custom_components.ev_smart_charger.coordinator]
Car plugged in! Starting new session.
Manual override active: False
Smart switch: True
```

Still nothing obvious. Let's look at the state dump...

## Step 5: Review State Dump

Find the JSON dump in logs (between the markers):

```json
{
  "timestamp": "2026-01-30T15:03:22.123456",
  "config_settings": {
    "max_fuse": 16.0,
    "has_price_sensor": true
  },
  "user_settings": {
    "smart_switch": false,    <-- AHA!
    "target_soc": 80
  },
  "sensor_data": {
    "car_soc": 75,
    "car_plugged": true
  },
  "price_data": {
    "today": [0.85, 0.82, ...],
    "tomorrow": [],
    "tomorrow_valid": false
  }
}
```

## The Root Cause

**Found it!** `"smart_switch": false` in user_settings!

Looking back at the logs with this knowledge:

```
2026-01-30 15:03:22 DEBUG (MainThread) [custom_components.ev_smart_charger.planner]
⚡ DECISION: Smart charging DISABLED → should_charge=True (plugged=True)
```

## The Solution

The user accidentally toggled the **Smart Charging Switch** to OFF (probably in the UI). 

To fix:
1. Go to Home Assistant dashboard
2. Find `switch.ev_smart_charger_smart_switch`
3. Turn it **ON**
4. Unplug and replug to reset the session

## What We Learned

The debug logs and state dump made it immediately clear:
- ✅ Price sensor working (today has 24 slots)
- ✅ Tomorrow's prices not available yet (published at ~13:00-14:00)
- ✅ Planner logic working correctly (waiting for prices)
- ❌ **Smart switch was disabled** (user error)

Without debug logging, this would have been very hard to diagnose!

## Alternative Scenario: Opportunistic Charging

What if the logs showed this instead:

```
🎯 Target SOC: base=80%, min_price=0.32, limit_1=0.50→100%, limit_2=1.50→80%
   → Opportunistic Level 1 triggered: target=100%
✅ Selected 5 cheapest slots: 15:00→0.32, 16:00→0.33, 17:00→0.35, 23:00→0.42, 00:00→0.43
⚡ ===== FINAL DECISION: should_charge_now=True, target_soc=100% =====
```

**Explanation:** The current price (0.32) is below the opportunistic threshold (0.50), so the system automatically raised the target from 80% to 100% and started charging immediately to take advantage of the very cheap electricity.

**This is working as designed!** If you don't want this behavior, adjust your opportunistic thresholds in the integration settings.

## Key Debugging Indicators

When reviewing logs, look for:

| Indicator | Meaning |
|-----------|---------|
| `smart_switch=False` | Smart charging disabled |
| `covers_departure=False` | Waiting for tomorrow's prices |
| `Opportunistic Level X triggered` | Charging due to cheap prices now |
| `Manual Override` | User set custom target/time |
| `Calendar Event` | Calendar determined target/time |
| `Target ALREADY REACHED` | Maintenance mode |
| `Price horizon does NOT cover departure` | Waiting strategy active |

## Tips for Reporting Issues

When reporting unexpected behavior, include:

1. ✅ The complete JSON debug dump
2. ✅ The decision logs (lines with 🔍 🎯 ⚡ emojis)
3. ✅ What you expected to happen
4. ✅ What actually happened
5. ✅ Screenshots of your settings (if relevant)

This gives developers everything needed to reproduce and fix the issue!
