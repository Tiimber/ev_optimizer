#!/usr/bin/env python3
"""
Simulator for EV Smart Charger debugging.

This script takes a debug dump JSON (from the dump_debug_state service)
and simulates the charging decision locally, showing the exact logic flow.

Usage:
    python3 simulate_from_dump.py debug_dump.json
    
Or pipe directly:
    cat debug_dump.json | python3 simulate_from_dump.py -
"""

import sys
import json
from datetime import datetime, time, timedelta


def parse_time(time_str):
    """Parse time string to time object."""
    if not time_str:
        return time(7, 0)
    try:
        # Handle HH:MM:SS or HH:MM format
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))
    except:
        return time(7, 0)


def simulate_from_dump(dump_data):
    """Run the planner simulation with dumped data."""
    print("=" * 80)
    print("EV Smart Charger - Simulation from Debug Dump")
    print("=" * 80)
    print(f"Timestamp: {dump_data['timestamp']}")
    print()
    
    # Extract key data
    config = dump_data['config_settings']
    user = dump_data['user_settings']
    sensor = dump_data['sensor_data']
    price_data = dump_data['price_data']
    
    # Display current state
    print("📊 CURRENT STATE:")
    print(f"  Car Plugged: {sensor.get('car_plugged', False)}")
    print(f"  Current SOC: {sensor.get('car_soc', 0)}%")
    print(f"  Target SOC: {user.get('target_soc', 80)}%")
    print(f"  Departure: {user.get('departure_override', '07:00')}")
    print(f"  Smart Switch: {user.get('smart_switch', True)}")
    print(f"  Manual Override: {dump_data.get('manual_override_active', False)}")
    print()
    
    # Show price data
    print("💰 PRICE DATA:")
    today_prices = price_data.get('today', [])
    tomorrow_prices = price_data.get('tomorrow', [])
    print(f"  Today: {len(today_prices)} slots")
    print(f"  Tomorrow: {len(tomorrow_prices)} slots (valid: {price_data.get('tomorrow_valid', False)})")
    
    if today_prices:
        current_hour = datetime.now().hour
        if len(today_prices) > current_hour:
            current_price = today_prices[current_hour]
            print(f"  Current price: {current_price:.2f}")
        
        min_today = min(today_prices) if today_prices else 0
        max_today = max(today_prices) if today_prices else 0
        avg_today = sum(today_prices) / len(today_prices) if today_prices else 0
        print(f"  Today range: {min_today:.2f} - {max_today:.2f} (avg: {avg_today:.2f})")
    
    if tomorrow_prices:
        min_tomorrow = min(tomorrow_prices) if tomorrow_prices else 0
        max_tomorrow = max(tomorrow_prices) if tomorrow_prices else 0
        avg_tomorrow = sum(tomorrow_prices) / len(tomorrow_prices) if tomorrow_prices else 0
        print(f"  Tomorrow range: {min_tomorrow:.2f} - {max_tomorrow:.2f} (avg: {avg_tomorrow:.2f})")
    
    print()
    
    # Show last plan decision
    print("⚡ LAST PLAN DECISION:")
    last_plan = dump_data.get('last_plan', {})
    print(f"  Should Charge Now: {last_plan.get('should_charge_now', False)}")
    print(f"  Planned Target SOC: {last_plan.get('planned_target_soc', 0)}%")
    print(f"  Scheduled Start: {last_plan.get('scheduled_start', 'None')}")
    print(f"  Departure Time: {last_plan.get('departure_time', 'None')}")
    print()
    
    summary = last_plan.get('charging_summary', '')
    if summary:
        print("📝 CHARGING SUMMARY:")
        print(summary)
        print()
    
    # Show opportunistic levels
    print("🎯 OPPORTUNISTIC SETTINGS:")
    print(f"  Level 1: Price ≤ {user.get('price_limit_1', 0.5)} → Target {user.get('target_soc_1', 100)}%")
    print(f"  Level 2: Price ≤ {user.get('price_limit_2', 1.5)} → Target {user.get('target_soc_2', 80)}%")
    print()
    
    # Configuration
    print("⚙️  CONFIGURATION:")
    print(f"  Max Fuse: {config.get('max_fuse', 16)} A")
    print(f"  Car Capacity: {config.get('car_capacity', 50)} kWh")
    print(f"  Charger Loss: {config.get('charger_loss', 10)}%")
    print(f"  Currency: {config.get('currency', 'SEK')}")
    print()
    
    print("=" * 80)
    print("💡 TIP: Check the Home Assistant logs for the detailed decision logic")
    print("    Look for lines starting with 🔍, 🎯, ⚡, etc.")
    print("=" * 80)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 simulate_from_dump.py <debug_dump.json>")
        print("   or: cat debug_dump.json | python3 simulate_from_dump.py -")
        sys.exit(1)
    
    # Read input
    if sys.argv[1] == '-':
        # Read from stdin
        data = sys.stdin.read()
    else:
        # Read from file
        with open(sys.argv[1], 'r') as f:
            data = f.read()
    
    # Parse JSON
    try:
        dump_data = json.loads(data)
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing JSON: {e}")
        sys.exit(1)
    
    # Run simulation
    simulate_from_dump(dump_data)


if __name__ == "__main__":
    main()
