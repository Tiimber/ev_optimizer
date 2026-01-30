#!/usr/bin/env python3
"""
Test the exact scenario from user's dump to understand why it's charging now.
"""
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

# Mock homeassistant before importing ANYTHING
from unittest.mock import MagicMock
sys.modules['homeassistant'] = MagicMock()
sys.modules['homeassistant.core'] = MagicMock()
sys.modules['homeassistant.config_entries'] = MagicMock()
sys.modules['homeassistant.helpers'] = MagicMock()
sys.modules['homeassistant.helpers.update_coordinator'] = MagicMock()
sys.modules['homeassistant.helpers.storage'] = MagicMock()
sys.modules['homeassistant.helpers.event'] = MagicMock()
sys.modules['homeassistant.const'] = MagicMock()
sys.modules['homeassistant.components'] = MagicMock()
sys.modules['homeassistant.components.persistent_notification'] = MagicMock()

# Add the custom_components directory to path
sys.path.insert(0, str(Path(__file__).parent))

from custom_components.ev_smart_charger import planner
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

# Load the user's dump
dump_data = {
    "timestamp": "2026-01-30T17:14:33.655132",
    "config_settings": {
        "max_fuse": 20,
        "charger_loss": 10,
        "car_capacity": 64,
        "currency": "SEK",
        "has_price_sensor": True
    },
    "user_settings": {
        "target_soc": 80,
        "min_guaranteed_soc": 20,
        "departure_time": "07:00:00",
        "departure_override": "07:00:00",
        "smart_charging_active": True,
        "target_soc_override": 80,
        "price_limit_1": 0.1,
        "target_soc_1": 90,
        "price_limit_2": 2.5,
        "target_soc_2": 70,
        "price_extra_fee": 0.7908,
        "price_vat": 25
    },
    "sensor_data": {
        "car_soc": 75,
        "car_plugged": True,
        "car_limit": None,
        "p1_l1": 7.9,
        "p1_l2": 2.5,
        "p1_l3": 0.4,
        "ch_l1": 0,
        "ch_l2": 0,
        "ch_l3": 0,
        "zap_limit_value": 0
    },
    "price_data": {
        "today": [1.42, 1.43, 1.36, 1.33, 1.36, 1.35, 1.33, 1.3, 1.33, 1.31, 1.29, 1.29, 1.29, 1.28, 1.27, 1.27, 1.27, 1.26, 1.27, 1.3, 1.24, 1.23, 1.24, 1.38, 1.3, 1.4, 1.51, 1.55, 1.53, 1.66, 1.73, 1.83, 1.83, 1.94, 1.88, 1.76, 2.17, 1.99, 1.96, 1.93, 1.87, 1.82, 1.69, 1.63, 1.74, 1.64, 1.64, 1.58, 1.66, 1.56, 1.53, 1.49, 1.51, 1.47, 1.43, 1.43, 1.44, 1.43, 1.45, 1.45, 1.41, 1.42, 1.44, 1.5, 1.44, 1.47, 1.53, 1.56, 1.54, 1.62, 1.69, 1.66, 1.69, 1.6, 1.61, 1.52, 1.51, 1.45, 1.45, 1.4, 1.45, 1.38, 1.34, 1.32, 1.37, 1.32, 1.27, 1.23, 1.33, 1.28, 1.24, 1.22, 1.23, 1.2, 1.12, 0.96],
        "tomorrow": [1.18, 1.22, 1.06, 1.04, 1.08, 1.08, 1.08, 1.02, 0.99, 0.98, 1, 0.99, 1, 0.98, 0.97, 0.96, 0.97, 0.97, 0.96, 0.94, 0.96, 0.97, 0.96, 0.96, 0.91, 0.96, 0.97, 0.98, 0.96, 0.98, 0.98, 1.03, 0.99, 1.14, 1.29, 1.3, 1.32, 1.33, 1.42, 1.42, 1.44, 1.38, 1.41, 1.34, 1.37, 1.35, 1.36, 1.34, 1.33, 1.26, 1.34, 1.34, 1.35, 1.33, 1.31, 1.31, 1.15, 1.28, 1.35, 1.39, 1.33, 1.4, 1.47, 1.53, 1.35, 1.54, 1.58, 1.58, 1.59, 1.56, 1.54, 1.46, 1.5, 1.47, 1.41, 1.35, 1.48, 1.4, 1.35, 1.29, 1.38, 1.32, 1.29, 1.21, 1.28, 1.29, 1.21, 1.16, 1.23, 1.19, 1.13, 1.08, 1.1, 1.06, 1.1, 1.02],
        "tomorrow_valid": True
    },
}

# Build data dict in the format planner expects
now = datetime.fromisoformat(dump_data["timestamp"])

# Parse departure time to time object
dept_time_str = dump_data["user_settings"]["departure_time"]
dept_time_parts = dept_time_str.split(":")
dept_time_obj = time(int(dept_time_parts[0]), int(dept_time_parts[1]), int(dept_time_parts[2]) if len(dept_time_parts) > 2 else 0)

data = {
    "car_plugged": dump_data["sensor_data"]["car_plugged"],
    "car_soc": dump_data["sensor_data"]["car_soc"],
    ENTITY_SMART_SWITCH: dump_data["user_settings"]["smart_charging_active"],
    ENTITY_TARGET_SOC: dump_data["user_settings"]["target_soc"],
    ENTITY_MIN_SOC: dump_data["user_settings"]["min_guaranteed_soc"],
    ENTITY_DEPARTURE_TIME: dept_time_obj,
    ENTITY_DEPARTURE_OVERRIDE: dept_time_obj,
    ENTITY_TARGET_OVERRIDE: dump_data["user_settings"]["target_soc_override"],
    ENTITY_PRICE_LIMIT_1: dump_data["user_settings"]["price_limit_1"],
    ENTITY_TARGET_SOC_1: dump_data["user_settings"]["target_soc_1"],
    ENTITY_PRICE_LIMIT_2: dump_data["user_settings"]["price_limit_2"],
    ENTITY_TARGET_SOC_2: dump_data["user_settings"]["target_soc_2"],
    ENTITY_PRICE_EXTRA_FEE: dump_data["user_settings"]["price_extra_fee"],
    ENTITY_PRICE_VAT: dump_data["user_settings"]["price_vat"],
    "price_data": dump_data["price_data"],
    "calendar_events": [],
}

config_settings = dump_data["config_settings"]

print("=" * 80)
print("SIMULATING USER SCENARIO")
print("=" * 80)
print(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"Current SOC: {data['car_soc']}%")
print(f"Target SOC: {data[ENTITY_TARGET_SOC]}%")
print(f"Departure: {data[ENTITY_DEPARTURE_TIME]}")
print(f"Smart charging: {data[ENTITY_SMART_SWITCH]}")
print(f"Price slots today: {len(dump_data['price_data']['today'])} (15-min intervals)")
print(f"Price slots tomorrow: {len(dump_data['price_data']['tomorrow'])}")
print()

# Show current and some future prices
today_prices = dump_data['price_data']['today']
tomorrow_prices = dump_data['price_data']['tomorrow']

# Current slot (17:14 = slot 68-69, which is 17:00-17:15)
current_slot_idx = (17 * 4) + 0  # 17:00-17:15
print(f"Current slot index: {current_slot_idx}")
print(f"Current price (17:00-17:15): {today_prices[current_slot_idx]:.2f} SEK")
print()

# Show tonight's cheap prices
print("Tonight's cheapest prices (raw spot):")
for i in range(88, 96):  # 22:00-00:00
    hour = i // 4
    minute = (i % 4) * 15
    print(f"  {hour:02d}:{minute:02d} - {today_prices[i]:.3f} SEK")

print("\nTomorrow early morning (raw spot):")
for i in range(0, 28, 4):  # 00:00-07:00, show hourly
    hour = i // 4
    print(f"  {hour:02d}:00 - {tomorrow_prices[i]:.3f} SEK")

print("\n" + "=" * 80)
print("CALLING PLANNER...")
print("=" * 80)

# Call planner
result = planner.generate_charging_plan(
    data=data,
    config_settings=config_settings,
    manual_override=False,
    overload_prevention_minutes=dump_data.get("session_info", {}).get("overload_prevention_minutes", 0),
    now=now
)

print()
print("=" * 80)
print("PLANNER RESULT")
print("=" * 80)
print(f"Should charge now: {result['should_charge_now']}")
print(f"Planned target SOC: {result['planned_target_soc']}%")
print(f"Scheduled start: {result.get('scheduled_start', 'None')}")
print(f"Departure time: {result.get('departure_time', 'None')}")
print()
print("Charging Summary:")
print(result['charging_summary'])
