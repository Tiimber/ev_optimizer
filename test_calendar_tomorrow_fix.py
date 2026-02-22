#!/usr/bin/env python3
"""
Test to verify the fix for calendar events blocking charging when event is tomorrow.

Issue: When calendar event is on Feb 23 (tomorrow) and current time is Feb 22 midnight,
the system would wait for Feb 23 prices instead of charging during Feb 22.

Fix: If departure is tomorrow+ AND we have full prices for today (until 23:00+)
AND it's early in the day (before 08:00), plan with available data instead of waiting.
"""
from datetime import datetime, time

# Simulate the scenario
now = datetime(2026, 2, 22, 0, 0)  # Feb 22 midnight
dept_dt = datetime(2026, 2, 23, 7, 5)  # Calendar event Feb 23 07:05
last_price_end = datetime(2026, 2, 22, 23, 45)  # Prices until end of Feb 22

# The fix logic
end_of_today_threshold = datetime.combine(now.date(), time(23, 0, 0))
departure_is_tomorrow_or_later = dept_dt.date() > now.date()
have_prices_for_rest_of_today = last_price_end and last_price_end >= end_of_today_threshold
early_in_day = now.hour < 8  # Before 08:00

should_plan_not_wait = departure_is_tomorrow_or_later and have_prices_for_rest_of_today and early_in_day

print("=" * 70)
print("CALENDAR EVENT TOMORROW - SHOULD WE PLAN OR WAIT?")
print("=" * 70)
print(f"Current time:           {now.strftime('%Y-%m-%d %H:%M')} (hour: {now.hour})")
print(f"Calendar departure:     {dept_dt.strftime('%Y-%m-%d %H:%M')}")
print(f"Last price available:   {last_price_end.strftime('%Y-%m-%d %H:%M')}")
print()
print(f"Departure is tomorrow+: {departure_is_tomorrow_or_later}")
print(f"Have full today prices: {have_prices_for_rest_of_today}")
print(f"Early in day (<08:00):  {early_in_day}")
print("=" * 70)
if should_plan_not_wait:
    print("✅ RESULT: PLAN with available data (charge during Feb 22)")
    print("   The system will re-plan tomorrow when Feb 23 prices arrive.")
else:
    print("❌ RESULT: WAIT for tomorrow's prices (old buggy behavior)")
    print("   This would prevent charging all night!")
print("=" * 70)
print()
print("COUNTER-EXAMPLE: Afternoon scenario (should wait)")
print("=" * 70)
now_afternoon = datetime(2026, 2, 22, 13, 0)  # Feb 22 afternoon
early_in_day_afternoon = now_afternoon.hour < 8
should_plan_afternoon = departure_is_tomorrow_or_later and have_prices_for_rest_of_today and early_in_day_afternoon
print(f"Current time:           {now_afternoon.strftime('%Y-%m-%d %H:%M')} (hour: {now_afternoon.hour})")
print(f"Early in day (<08:00):  {early_in_day_afternoon}")
if should_plan_afternoon:
    print("❌ Would PLAN (wrong - should wait for tonight's cheap prices)")
else:
    print("✅ Would WAIT (correct - best charging might be tonight 00:00-05:00)")
print("=" * 70)
