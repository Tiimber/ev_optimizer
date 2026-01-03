"""Planning logic for EV Smart Charger."""

import math
import re
from datetime import timedelta, datetime, time

from .const import (
    ENTITY_TARGET_SOC,
    ENTITY_MIN_SOC,
    ENTITY_PRICE_LIMIT_1,
    ENTITY_TARGET_SOC_1,
    ENTITY_PRICE_LIMIT_2,
    ENTITY_TARGET_SOC_2,
    ENTITY_DEPARTURE_TIME,
    ENTITY_DEPARTURE_OVERRIDE,
    ENTITY_SMART_SWITCH,
    ENTITY_TARGET_OVERRIDE,
    ENTITY_PRICE_EXTRA_FEE,
    ENTITY_PRICE_VAT,
)


def calculate_load_balancing(data: dict, max_fuse: float) -> float:
    """Calculate the safe current available for the charger."""
    p1_l1 = data.get("p1_l1", 0.0)
    p1_l2 = data.get("p1_l2", 0.0)
    p1_l3 = data.get("p1_l3", 0.0)
    ch_l1 = data.get("ch_l1", 0.0)
    ch_l2 = data.get("ch_l2", 0.0)
    ch_l3 = data.get("ch_l3", 0.0)

    # If charger current sensors are not configured, use Zaptec limiter value as fallback
    # This ensures the load balancing accounts for the commanded current limit
    if ch_l1 == 0.0 and ch_l2 == 0.0 and ch_l3 == 0.0:
        zap_limit = data.get("zap_limit_value", 0.0)
        if zap_limit > 0:
            # Assume limiter current is distributed (roughly evenly) across phases
            ch_l1 = ch_l2 = ch_l3 = zap_limit / 3.0

    house_l1 = max(0.0, p1_l1 - ch_l1)
    house_l2 = max(0.0, p1_l2 - ch_l2)
    house_l3 = max(0.0, p1_l3 - ch_l3)

    max_house_current = max(house_l1, house_l2, house_l3)
    buffer = max(1.0, max_fuse * 0.05)
    available = max_fuse - max_house_current - buffer

    return max(0.0, available)


def analyze_prices(raw_prices: list) -> str:
    """Quick status for UI."""
    if not raw_prices:
        return "No Data"
    try:
        count = len(raw_prices)
        now_dt = datetime.now()
        idx = (now_dt.hour * 4) + (now_dt.minute // 15) if count > 25 else now_dt.hour
        idx = min(idx, count - 1)
        current = raw_prices[idx]
        avg = sum(raw_prices) / count
        if current < avg * 0.8:
            return "Very Cheap"
        if current < avg:
            return "Cheap"
        return "Expensive"
    except Exception:
        return "Unknown"


def get_calendar_data(
    events: list, now: datetime
) -> tuple[datetime | None, float | None]:
    """Check for relevant calendar event."""
    if not events:
        return None, None
    limit = datetime.combine(now.date() + timedelta(days=1), time.max)
    sorted_events = sorted(events, key=lambda x: x.get("start"))
    for event in sorted_events:
        start_str = event.get("start")
        if isinstance(start_str, dict):
            start_str = start_str.get("dateTime", start_str.get("date"))
        if not start_str:
            continue
        try:
            if len(start_str) == 10:
                evt_start = datetime.fromisoformat(start_str)
            else:
                evt_start = datetime.fromisoformat(start_str)
                if evt_start.tzinfo:
                    evt_start = evt_start.replace(tzinfo=None)
            if evt_start < now:
                continue
            if evt_start > limit:
                break
            text = f"{event.get('summary', '')} {event.get('description', '')}"
            match = re.search(r"(\d+)\s*%", text)
            target_soc = (
                float(match.group(1))
                if match and 10 <= int(match.group(1)) <= 100
                else None
            )
            return evt_start, target_soc
        except ValueError:
            continue
    return None, None


def get_departure_time(
    data: dict, now: datetime, calendar_events: list = None
) -> datetime:
    """Determine the target departure datetime."""
    if calendar_events:
        cal_time, _ = get_calendar_data(calendar_events, now)
        if cal_time:
            return cal_time

    time_input = data.get(ENTITY_DEPARTURE_OVERRIDE) or data.get(
        ENTITY_DEPARTURE_TIME, time(7, 0)
    )
    dept_dt = datetime.combine(now.date(), time_input)
    if dept_dt < now:
        dept_dt = dept_dt + timedelta(days=1)
    return dept_dt


def generate_charging_plan(
    data: dict, config_settings: dict, manual_override: bool, now: datetime | None = None, overload_prevention_minutes: float = 0.0
) -> dict:
    """Core Logic.
    
    Args:
        data: Sensor data and settings
        config_settings: Configuration parameters
        manual_override: Whether manual override is active
        now: Current datetime (for testing)
        overload_prevention_minutes: Minutes of accumulated charging time lost due to overload prevention
    """
    plan = {
        "should_charge_now": False,
        "scheduled_start": None,
        "planned_target_soc": data.get(ENTITY_TARGET_SOC, 80),
        "charging_schedule": [],
        "charging_summary": "Not calculated",
        "overload_prevention_minutes": overload_prevention_minutes,
    }

    if not data.get(ENTITY_SMART_SWITCH, True):
        plan["should_charge_now"] = True
        plan["charging_summary"] = "Smart charging disabled. Charging immediately."
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
        return plan

    now = now or datetime.now()
    prices = []
    raw_today = data["price_data"].get("today", [])

    if not raw_today:
        if not config_settings.get("has_price_sensor"):
            plan["charging_summary"] = "No Price Sensor. Load Balancing Mode."
        else:
            plan["charging_summary"] = (
                "Error: Price sensor configured but no data received."
            )
        plan["should_charge_now"] = True
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
        return plan

    raw_tomorrow = data["price_data"].get("tomorrow", [])

    def parse_price_list(price_list, date_ref):
        parsed = []
        if not price_list:
            return []
        interval_min = 60 if len(price_list) <= 25 else 15
        for i, price in enumerate(price_list):
            start_dt = datetime.combine(date_ref, time(0, 0)) + timedelta(
                minutes=i * interval_min
            )
            if start_dt + timedelta(minutes=interval_min) < now:
                continue
            parsed.append(
                {
                    "start": start_dt,
                    "end": start_dt + timedelta(minutes=interval_min),
                    "price": float(price),
                }
            )
        return parsed

    if isinstance(raw_today, str):
        raw_today = [float(x) for x in raw_today.split(",")]
    if isinstance(raw_tomorrow, str):
        raw_tomorrow = [float(x) for x in raw_tomorrow.split(",")]
    prices.extend(parse_price_list(raw_today, now.date()))
    if data["price_data"].get("tomorrow_valid", False) or raw_tomorrow:
        prices.extend(parse_price_list(raw_tomorrow, now.date() + timedelta(days=1)))

    if not prices:
        plan["should_charge_now"] = True
        plan["charging_summary"] = "No future price data found."
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
        return plan

    dept_dt = get_departure_time(data, now, data.get("calendar_events"))
    calc_window = [p for p in prices if p["start"] < dept_dt]

    if not calc_window:
        plan["should_charge_now"] = True
        plan["charging_summary"] = "Departure passed. Charging."
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
        return plan

    cal_time, cal_soc = get_calendar_data(data.get("calendar_events", []), now)
    time_source = "(Calendar)" if cal_time and cal_time == dept_dt else "(Manual)"
    min_guaranteed = data.get(ENTITY_MIN_SOC, 20)
    status_note = ""

    if manual_override:
        final_target = data.get(ENTITY_TARGET_OVERRIDE, 80)
        status_note = "(Manual Override)"
    elif cal_soc is not None:
        final_target = cal_soc
        status_note = "(Calendar Event)"
    else:
        final_target = data.get(ENTITY_TARGET_SOC, 80)
        status_note = "(Smart)"
        min_price_in_window = min(slot["price"] for slot in calc_window)
        limit_1 = data.get(ENTITY_PRICE_LIMIT_1, 0.5)
        target_1 = data.get(ENTITY_TARGET_SOC_1, 100)
        limit_2 = data.get(ENTITY_PRICE_LIMIT_2, 1.5)
        target_2 = data.get(ENTITY_TARGET_SOC_2, 80)
        if min_price_in_window <= limit_1:
            final_target = max(final_target, target_1)
        elif min_price_in_window <= limit_2:
            final_target = max(final_target, target_2)

    final_target = max(final_target, min_guaranteed)
    plan["planned_target_soc"] = final_target
    current_soc = data.get("car_soc", 0) or 0.0

    selected_slots = []
    selected_start_times = set()
    price_limit_high = data.get(ENTITY_PRICE_LIMIT_2, 1.5)

    # Calculate extra slots needed to compensate for overload prevention minutes
    extra_slots_needed = 0
    if overload_prevention_minutes > 0:
        slot_duration_min = (
            15 if len(raw_today) > 25 else 60
        )  # 15 min slots or 60 min slots
        extra_slots_needed = math.ceil(overload_prevention_minutes / slot_duration_min)

    if current_soc >= final_target:
        plan["charging_summary"] = (
            f"Target reached ({int(current_soc)}%). Maintenance mode active."
        )
        for slot in calc_window:
            if slot["price"] <= price_limit_high:
                selected_start_times.add(slot["start"])
                selected_slots.append(slot)
        for slot in calc_window:
            if (
                slot["start"] in selected_start_times
                and slot["start"] <= now < slot["end"]
            ):
                plan["should_charge_now"] = True
                break
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
    else:
        soc_needed = final_target - current_soc
        kwh_needed = (soc_needed / 100.0) * config_settings["car_capacity"]
        efficiency = 1.0 - (config_settings["charger_loss"] / 100.0)
        kwh_to_pull = kwh_needed / efficiency

        # Estimate power from max fuse (converted to kW)
        # P = 3 * 230 * Amps / 1000
        est_power_kw = min((3 * 230 * config_settings["max_fuse"]) / 1000.0, 11.0)
        hours_needed = kwh_to_pull / est_power_kw

        slot_duration_hours = (
            calc_window[0]["end"] - calc_window[0]["start"]
        ).seconds / 3600.0
        if slot_duration_hours <= 0:
            slot_duration_hours = 1.0
        slots_needed = math.ceil(hours_needed / slot_duration_hours)
        
        # Add extra slots to compensate for overload prevention minutes
        slots_needed += extra_slots_needed

        sorted_window = sorted(calc_window, key=lambda x: x["price"])
        selected_slots = sorted_window[:slots_needed]
        selected_start_times = {s["start"] for s in selected_slots}

        # Check Buffer Logic in Coordinator side or here?
        # Logic is simpler here:
        session_end_time = (
            max(s["end"] for s in selected_slots) if selected_slots else None
        )

        for slot in calc_window:
            if (
                slot["start"] in selected_start_times
                and slot["start"] <= now < slot["end"]
            ):
                plan["should_charge_now"] = True
                break

        plan["session_end_time"] = (
            session_end_time.isoformat() if session_end_time else None
        )

        summary_lines = []
        total_plan_cost = 0.0
        extra_fee = data.get(ENTITY_PRICE_EXTRA_FEE, 0.0)
        vat_pct = data.get(ENTITY_PRICE_VAT, 0.0)
        currency = config_settings.get("currency", "SEK")
        cost_note = ""
        if extra_fee > 0 or vat_pct > 0:
            cost_note = "(incl fees/VAT)"

        if selected_slots:
            chrono_slots = sorted(selected_slots, key=lambda x: x["start"])
            kwh_grid_per_slot_max = est_power_kw * slot_duration_hours
            remaining_kwh_grid = kwh_to_pull
            running_soc = current_soc
            blocks = []
            current_block = None

            for slot in chrono_slots:
                if remaining_kwh_grid <= 0.001:
                    break
                raw_price = slot["price"]
                adjusted_price = (raw_price + extra_fee) * (1 + vat_pct / 100.0)
                kwh_this_slot = min(kwh_grid_per_slot_max, remaining_kwh_grid)
                remaining_kwh_grid -= kwh_this_slot
                slot_cost = adjusted_price * kwh_this_slot
                total_plan_cost += slot_cost
                kwh_batt_this_slot = kwh_this_slot * efficiency
                soc_gain_this_slot = (
                    kwh_batt_this_slot / config_settings["car_capacity"]
                ) * 100.0

                if current_block and slot["start"] == current_block["end"]:
                    current_block["end"] = slot["end"]
                    current_block["cost"] += slot_cost
                    current_block["soc_gain"] += soc_gain_this_slot
                    current_block["avg_price_acc"] += adjusted_price
                    current_block["count"] += 1
                else:
                    if current_block:
                        running_soc += current_block["soc_gain"]
                        blocks.append(current_block)
                    current_block = {
                        "start": slot["start"],
                        "end": slot["end"],
                        "cost": slot_cost,
                        "soc_start": running_soc,
                        "soc_gain": soc_gain_this_slot,
                        "avg_price_acc": adjusted_price,
                        "count": 1,
                    }
            if current_block:
                blocks.append(current_block)

            summary_lines.append(
                f"**Departure:** {dept_dt.strftime('%H:%M')} {time_source}"
            )
            summary_lines.append(f"**Target:** {int(final_target)}% {status_note}")
            summary_lines.append(
                f"**Total Estimated Cost:** {total_plan_cost:.2f} {currency} {cost_note}"
            )
            summary_lines.append("")
            for b in blocks:
                start_s = b["soc_start"]
                end_s = min(100, start_s + b["soc_gain"])
                if end_s > final_target:
                    end_s = final_target
                avg_p = b["avg_price_acc"] / b["count"]
                line = (
                    f"**{b['start'].strftime('%H:%M')} - {b['end'].strftime('%H:%M')}**\n"
                    f"SoC: {int(start_s)}% → {int(end_s)}%\n"
                    f"Cost: {b['cost']:.2f} {currency} (Avg: {avg_p:.2f})"
                )
                summary_lines.append(line)
            plan["charging_summary"] = "\n\n".join(summary_lines)

    schedule_data = []
    for slot in prices:
        active = slot["start"] in selected_start_times
        schedule_data.append(
            {
                "start": slot["start"].isoformat(),
                "end": slot["end"].isoformat(),
                "price": slot["price"],
                "active": active,
            }
        )
    if schedule_data:
        last_slot = schedule_data[-1]
        schedule_data.append(
            {
                "start": last_slot["end"],
                "end": last_slot["end"],
                "price": last_slot["price"],
                "active": False,
            }
        )
    plan["charging_schedule"] = schedule_data

    future_starts = [
        s["start"]
        for s in calc_window
        if s["start"] > now and s["start"] in selected_start_times
    ]
    if future_starts:
        plan["scheduled_start"] = min(future_starts).isoformat()

    if not data.get("car_plugged"):
        plan["should_charge_now"] = False

    if plan["should_charge_now"] and "amps" not in plan:
        plan["amps"] = config_settings.get("max_fuse", 16.0)

    return plan
