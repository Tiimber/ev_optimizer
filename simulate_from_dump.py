#!/usr/bin/env python3
"""
Simulator for EV Optimizer debugging.

This script takes a debug dump JSON (from the dump_debug_state service)
or a snapshot export (from export_snapshots service) and simulates the 
charging decision locally, showing the exact logic flow.

Usage:
    # Single debug dump:    python3 simulate_from_dump.py debug_dump.json
    
    # Snapshot export (timeline):
    python3 simulate_from_dump.py ev_optimizer_debug_20260309_100000.json
    
    # From stdin:
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
    print("EV Optimizer - Simulation from Debug Dump")
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


def simulate_from_snapshots(export_data):
    """Replay a scenario from snapshot export."""
    print("=" * 80)
    print("EV Optimizer - Snapshot Replay")
    print("=" * 80)
    
    export_info = export_data['export_info']
    snapshots = export_data['snapshots']
    prices_by_date = export_data.get('prices', {})
    
    print(f"Export Created: {export_info['created_at']}")
    print(f"Time Range: {export_info['start_time']} → {export_info['end_time']}")
    print(f"Snapshots: {export_info['snapshot_count']}")
    print()
    
    if not snapshots:
        print("❌ No snapshots found in export!")
        return
    
    print("📊 TIMELINE:")
    print("-" * 80)
    
    for i, snapshot in enumerate(snapshots):
        timestamp = snapshot['timestamp']
        dt = datetime.fromisoformat(timestamp)
        time_str = dt.strftime("%Y-%m-%d %H:%M")
        
        # Status indicators
        charging = "🟢 CHARGING" if snapshot.get('should_charge_now') else "🔴 PAUSED"
        plugged = "🔌" if snapshot.get('car_plugged') else "⚡"
        
        # Key metrics
        soc = snapshot.get('car_soc', 0)
        target = snapshot.get('planned_target_soc', 0)
        available = snapshot.get('max_available_current', 0)
        
        print(f"\n[{i+1}/{len(snapshots)}] {time_str} {plugged} {charging}")
        print(f"     SoC: {soc}% → {target}%  |  Available: {available}A")
        
        # Show summary (first 100 chars)
        summary = snapshot.get('charging_summary', '')
        if summary:
            summary_short = summary[:100] + "..." if len(summary) > 100 else summary
            print(f"     {summary_short}")
        
        # Show changes during this hour
        changes = snapshot.get('changes_this_hour', [])
        if changes:
            print(f"     📝 {len(changes)} updates this hour")
            
            # Show actions if any
            all_actions = []
            for change in changes:
                actions = change.get('actions', [])
                all_actions.extend(actions)
            
            if all_actions:
                print(f"     💡 Actions:")
                for action in all_actions[:3]:  # Show first 3 actions
                    print(f"        - {action}")
                if len(all_actions) > 3:
                    print(f"        ... and {len(all_actions) - 3} more")
    
    print()
    print("-" * 80)
    
    # Summary statistics
    charging_snapshots = sum(1 for s in snapshots if s.get('should_charge_now'))
    paused_snapshots = len(snapshots) - charging_snapshots
    
    print(f"\n📈 SUMMARY:")
    print(f"  Charging: {charging_snapshots}/{len(snapshots)} hours")
    print(f"  Paused: {paused_snapshots}/{len(snapshots)} hours")
    
    # SoC progression
    first_soc = snapshots[0].get('car_soc', 0)
    last_soc = snapshots[-1].get('car_soc', 0)
    soc_change = last_soc - first_soc
    print(f"  SoC Change: {first_soc}% → {last_soc}% ({soc_change:+.1f}%)")
    
    # Calendar events
    calendar_count = sum(1 for s in snapshots if s.get('calendar_events'))
    if calendar_count > 0:
        print(f"  Calendar Events: {calendar_count} snapshots had events")
    
    print()
    print("=" * 80)
    print("💡 TIP: Look for patterns in when charging was PAUSED")
    print("    - Was it waiting for prices?")
    print("    - Was available current too low?")
    print("    - Was it outside the planned schedule?")
    print("=" * 80)


def detect_format(data):
    """Detect if this is a snapshot export or a single debug dump."""
    if 'export_info' in data and 'snapshots' in data:
        return 'snapshot_export'
    elif 'timestamp' in data and 'config_settings' in data:
        return 'debug_dump'
    else:
        return 'unknown'


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 simulate_from_dump.py <file.json>")
        print("   or: cat file.json | python3 simulate_from_dump.py -")
        print()
        print("Supported formats:")
        print("  - Debug dump (from dump_debug_state service)")
        print("  - Snapshot export (from export_snapshots service)")
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
        parsed_data = json.loads(data)
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing JSON: {e}")
        sys.exit(1)
    
    # Detect format and run appropriate simulation
    format_type = detect_format(parsed_data)
    
    if format_type == 'snapshot_export':
        print("Detected: Snapshot Export")
        print()
        simulate_from_snapshots(parsed_data)
    elif format_type == 'debug_dump':
        print("Detected: Debug Dump")
        print()
        simulate_from_dump(parsed_data)
    else:
        print(f"❌ Unknown file format!")
        print("Expected either:")
        print("  - Debug dump from dump_debug_state service")
        print("  - Snapshot export from export_snapshots service")
        sys.exit(1)


if __name__ == "__main__":
    main()
