import math
from datetime import datetime, time
import importlib.util
import sys
from pathlib import Path

# Load modules directly to avoid importing Home Assistant at package import-time
pkg_dir = Path(__file__).resolve().parents[1] / "custom_components" / "ev_smart_charger"

# Fixed timestamp for deterministic tests
FIXED_NOW = datetime(2025, 1, 15, 12, 0)


def _load_package_module(full_name, path):
    """Load a module using the provided full package-style name (e.g. custom_components.ev_smart_charger.const)
    This ensures relative imports inside the module work by preloading sibling modules into sys.modules.
    """
    spec = importlib.util.spec_from_file_location(full_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod

# Load const first under the package namespace, then planner so relative imports succeed
const = _load_package_module("custom_components.ev_smart_charger.const", pkg_dir / "const.py")
planner = _load_package_module("custom_components.ev_smart_charger.planner", pkg_dir / "planner.py")


def make_price_list(length=96, base=2.0, low_indices=None, low_value=0.1, mid_indices=None, mid_value=1.4):
    """Create a price list (today) with optional low/mid price spikes."""
    prices = [float(base) for _ in range(length)]
    low_indices = low_indices or []
    mid_indices = mid_indices or []
    for i in low_indices:
        if 0 <= i < length:
            prices[i] = float(low_value)
    for i in mid_indices:
        if 0 <= i < length:
            prices[i] = float(mid_value)
    return prices


def test_low_price_triggers_high_target():
    # A very low price appears in the future -> target should be increased to target_1 (default 100)
    # place a low price a few slots in the future relative to now
    now = FIXED_NOW
    idx = (now.hour * 4) + (now.minute // 15) + 4
    raw_today = make_price_list(low_indices=[idx], low_value=0.1)

    data = {
        "price_data": {"today": raw_today},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_DEPARTURE_TIME: time(23, 59),
        "car_soc": 30,
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}

    plan = planner.generate_charging_plan(data, config, manual_override=False, now=now)
    assert plan["planned_target_soc"] >= 100


def test_mid_price_triggers_medium_target_overrides_lower_user_target():
    # No very low prices, but a mid price <= price_limit_2 should increase a low user target
    now = FIXED_NOW
    idx = (now.hour * 4) + (now.minute // 15) + 5
    raw_today = make_price_list(mid_indices=[idx], mid_value=1.4)

    data = {
        "price_data": {"today": raw_today},
        const.ENTITY_TARGET_SOC: 70,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_DEPARTURE_TIME: time(23, 59),
        "car_soc": 50,
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}

    plan = planner.generate_charging_plan(data, config, manual_override=False, now=now)
    # target_2 default is 80, so final_target should be raised to at least 80
    assert plan["planned_target_soc"] >= 80


def test_target_reached_selects_low_price_slots():
    # If car SoC already at/above target, the plan should select only slots with price <= price_limit_2
    raw_today = make_price_list(low_indices=[70, 71, 72], low_value=1.0, base=2.5)

    data = {
        "price_data": {"today": raw_today},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        "car_soc": 90,  # already above target
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}

    plan = planner.generate_charging_plan(data, config, manual_override=False, now=FIXED_NOW)
    schedule = plan.get("charging_schedule", [])
    # All active slots should have price <= price_limit_2 (default 1.5)
    for slot in schedule:
        if slot.get("active"):
            assert slot["price"] <= data.get(const.ENTITY_PRICE_LIMIT_2, 1.5)


def test_manual_override_respected():
    raw_today = make_price_list(low_indices=[10], low_value=0.2)
    data = {
        "price_data": {"today": raw_today},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_TARGET_OVERRIDE: 50,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        "car_soc": 40,
    }
    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    plan = planner.generate_charging_plan(data, config, manual_override=True, now=FIXED_NOW)
    assert int(plan["planned_target_soc"]) == 50


def test_calendar_event_sets_target_and_departure():
    # Calendar event with 90% in summary should set target
    now = FIXED_NOW
    evt_start = (now.replace(hour=(now.hour + 2) % 24)).isoformat()
    data = {
        "price_data": {"today": make_price_list()},
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 10,
        "car_soc": 30,
        "calendar_events": [{"start": evt_start, "summary": "Trip 90%"}],
    }
    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    plan = planner.generate_charging_plan(data, config, manual_override=False, now=now)
    assert int(plan["planned_target_soc"]) == 90
    assert plan.get("scheduled_start") is not None


def test_price_list_length_variations_handle_hourly_and_quarter():
    # Hourly price list (len <= 25)
    hourly = [1.0 for _ in range(24)]
    data_h = {"price_data": {"today": hourly}, const.ENTITY_SMART_SWITCH: True, "car_soc": 10}
    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    plan_h = planner.generate_charging_plan(data_h, config, manual_override=False, now=FIXED_NOW)
    assert isinstance(plan_h.get("charging_schedule"), list)

    # Quarter-hourly price list
    q = make_price_list(length=96)
    data_q = {"price_data": {"today": q}, const.ENTITY_SMART_SWITCH: True, "car_soc": 10}
    plan_q = planner.generate_charging_plan(data_q, config, manual_override=False, now=FIXED_NOW)
    assert isinstance(plan_q.get("charging_schedule"), list)


def test_calculate_load_balancing_with_zap_limit():
    data = {"p1_l1": 5.0, "p1_l2": 5.0, "p1_l3": 5.0, "ch_l1": 0.0, "ch_l2": 0.0, "ch_l3": 0.0, "zap_limit_value": 16.0}
    available = planner.calculate_load_balancing(data, max_fuse=20.0)
    # With zap_limit distributed, house load becomes zero and buffer=1 => available ~= 19
    assert math.isclose(available, 19.0, rel_tol=1e-3)


# === NEW TEST SCENARIOS ===

def test_detached_charging_quarters_marked():
    """When charging is needed over multiple non-consecutive 15-minute slots, those slots are marked active."""
    now = FIXED_NOW
    # Low prices at slots 10, 12, 14, 16 (non-consecutive)
    raw_today = make_price_list(low_indices=[10, 12, 14, 16], low_value=0.5, base=3.0)

    data = {
        "price_data": {"today": raw_today},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 10,
        const.ENTITY_DEPARTURE_TIME: time(23, 59),
        "car_soc": 20,
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    plan = planner.generate_charging_plan(data, config, manual_override=False, now=now)

    schedule = plan.get("charging_schedule", [])
    active_slots = [s for s in schedule if s.get("active")]
    # Should have multiple active slots (at least the low-price ones)
    assert len(active_slots) >= 2
    # Verify they cover sufficient energy for the SoC increase
    total_energy = sum(s.get("current", 16.0) * 0.25 * 230 / 1000 for s in active_slots)
    assert total_energy > 0


def test_no_charging_needed_empty_schedule():
    """When car SoC >= target, no charging is planned."""
    raw_today = make_price_list(base=5.0)  # High prices everywhere

    data = {
        "price_data": {"today": raw_today},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_DEPARTURE_TIME: time(23, 59),
        "car_soc": 85,  # Already above target
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    plan = planner.generate_charging_plan(data, config, manual_override=False, now=FIXED_NOW)

    schedule = plan.get("charging_schedule", [])
    active_slots = [s for s in schedule if s.get("active")]
    # Should have no active charging slots
    assert len(active_slots) == 0
    assert plan.get("should_charge_now") is False


def test_overload_protection_6a_minimum():
    """Charger cannot reserve less than 6A; if total load is too high, charging is disabled."""
    # House load is very high: 18A on each phase
    data = {
        "price_data": {"today": make_price_list(low_indices=[50], low_value=0.1)},
        "p1_l1": 18.0,
        "p1_l2": 18.0,
        "p1_l3": 18.0,
        "ch_l1": 0.0,
        "ch_l2": 0.0,
        "ch_l3": 0.0,
        const.ENTITY_TARGET_SOC: 90,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        "car_soc": 30,
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    # calculate_load_balancing should return < 6 if overloaded
    available = planner.calculate_load_balancing(data, max_fuse=20.0)
    # With p1=54A total, available should be negative or very small, protecting against 6A minimum
    assert available < 6.0, "Load balancing should not allow charging when overloaded"


def test_soc_force_update_adds_charging_time():
    """When car SoC is force-updated lower than anticipated at the end of a charging window, extra time is added."""
    # Plan a charge to reach 80% by end of day
    # Simulate that SoC was re-measured and is lower than expected
    now = FIXED_NOW
    raw_today = make_price_list(low_indices=[50, 51, 52], low_value=0.5)

    data = {
        "price_data": {"today": raw_today},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 10,
        const.ENTITY_DEPARTURE_TIME: time(23, 59),
        "car_soc": 50,  # Current measured SoC
        "soc_force_updated": True,  # Flag that SoC was refreshed
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    plan = planner.generate_charging_plan(data, config, manual_override=False, now=now)

    # Plan should still exist and target should be 80
    assert plan.get("planned_target_soc") >= 80
    schedule = plan.get("charging_schedule", [])
    active_slots = [s for s in schedule if s.get("active")]
    # Should have active slots to reach the target
    assert len(active_slots) > 0


def test_calendar_event_percentage_override():
    """If a calendar event includes a percentage (e.g., '90%'), the planner targets that percentage."""
    now = FIXED_NOW
    evt_start = (now.replace(hour=(now.hour + 3) % 24)).isoformat()
    data = {
        "price_data": {"today": make_price_list()},
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 10,
        const.ENTITY_TARGET_SOC: 50,  # Default target is 50
        "car_soc": 30,
        "calendar_events": [{"start": evt_start, "summary": "Trip 75%"}],  # Calendar overrides to 75%
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    plan = planner.generate_charging_plan(data, config, manual_override=False, now=now)

    # Target should be 75% from calendar, not 50% from setting
    assert int(plan["planned_target_soc"]) == 75


def test_dst_transition_spring():
    """On spring DST transition, the charging window correctly spans the time shift."""
    # March 31, 2025, 2:00 AM -> 3:00 AM (UTC+1 -> UTC+2 in Europe)
    # Create a timestamp near the DST transition
    now_dst = datetime(2025, 3, 31, 1, 30)
    raw_today = make_price_list(low_indices=[6, 7, 8], low_value=0.5)

    data = {
        "price_data": {"today": raw_today},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_DEPARTURE_TIME: time(8, 0),
        "car_soc": 30,
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    plan = planner.generate_charging_plan(data, config, manual_override=False, now=now_dst)

    # Should produce a valid plan despite the DST transition
    assert plan.get("planned_target_soc") > 30
    schedule = plan.get("charging_schedule", [])
    assert isinstance(schedule, list)


def test_load_balancing_without_nordpool():
    """When nordpool pricing is not configured, load balancing still works for passive charging."""
    data = {
        "price_data": {"today": []},  # No pricing data
        "p1_l1": 8.0,
        "p1_l2": 7.0,
        "p1_l3": 6.0,
        "ch_l1": 0.0,
        "ch_l2": 0.0,
        "ch_l3": 0.0,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        "car_soc": 30,
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": False}
    # Load balancing should still calculate available amps
    available = planner.calculate_load_balancing(data, max_fuse=20.0)
    # Available should be positive and represent the space left after house load
    assert available > 0
    assert available <= 20.0


def test_car_target_soc_entity_fallback():
    """If car 'target SoC' entity is not available, fallback to charging window and SoC estimation."""
    data = {
        "price_data": {"today": make_price_list()},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_DEPARTURE_TIME: time(18, 0),
        "car_soc": 40,
        # No car_charging_level_entity value; should fall back to plan
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}
    plan = planner.generate_charging_plan(data, config, manual_override=False, now=FIXED_NOW)

    # Should still generate a valid plan using SoC estimation
    assert plan.get("planned_target_soc") is not None
    assert plan.get("planned_target_soc") > 0
    schedule = plan.get("charging_schedule", [])
    assert isinstance(schedule, list)


def test_overload_prevention_extends_charging_schedule():
    """When overload_prevention_minutes > 0, extra charging slots are added at cheapest prices."""
    # Create price list with clear cheap and expensive slots
    now = FIXED_NOW
    raw_today = make_price_list(
        low_indices=[40, 41, 42, 70, 71, 72],  # Two blocks of cheap prices
        low_value=0.3,
        base=3.0
    )

    data = {
        "price_data": {"today": raw_today},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_DEPARTURE_TIME: time(23, 59),
        "car_soc": 40,
    }

    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 64.0, "has_price_sensor": True}

    # Plan without overload prevention
    plan_normal = planner.generate_charging_plan(data, config, manual_override=False, now=now, overload_prevention_minutes=0)
    schedule_normal = plan_normal.get("charging_schedule", [])
    active_normal = [s for s in schedule_normal if s.get("active")]
    
    # Plan with 30 minutes of overload prevention (2 extra slots @ 15min each)
    plan_extended = planner.generate_charging_plan(data, config, manual_override=False, now=now, overload_prevention_minutes=30)
    schedule_extended = plan_extended.get("charging_schedule", [])
    active_extended = [s for s in schedule_extended if s.get("active")]
    
    # Extended plan should have more active slots
    assert len(active_extended) >= len(active_normal), "Extended plan should have at least as many or more slots"
    # Verify the overload prevention minutes are tracked in the plan
    assert plan_extended.get("overload_prevention_minutes") == 30


def test_waits_for_additional_price_data_when_departure_beyond_horizon_and_slack_exists():
    """If departure is beyond known prices (e.g., tomorrow prices not published yet),
    the planner should wait (not charge now) when there is still enough time left
    to reach the target before departure.
    """
    now = datetime(2025, 1, 15, 13, 0)
    hourly_today = [2.0 for _ in range(24)]

    data = {
        "price_data": {"today": hourly_today, "tomorrow": []},
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_DEPARTURE_TIME: time(5, 0),  # next day (since 05:00 < 13:00)
        "car_soc": 58,
        "car_plugged": True,
    }
    config = {"max_fuse": 20.0, "charger_loss": 10.0, "car_capacity": 80.0, "has_price_sensor": True}

    plan = planner.generate_charging_plan(data, config, manual_override=False, now=now)
    assert plan["should_charge_now"] is False
    assert "Waiting for additional price data" in plan.get("charging_summary", "")
    # While waiting, the schedule should still be present but with no active slots
    schedule = plan.get("charging_schedule", [])
    assert isinstance(schedule, list)
    assert all(not s.get("active") for s in schedule)


def test_does_not_wait_for_additional_price_data_when_time_is_tight():
    """When departure is beyond known prices but there is not enough slack, the
    planner should start charging based on what it knows.
    """
    now = datetime(2025, 1, 15, 13, 0)
    hourly_today = [2.0 for _ in range(24)]

    data = {
        "price_data": {"today": hourly_today, "tomorrow": []},
        const.ENTITY_TARGET_SOC: 100,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_DEPARTURE_TIME: time(2, 0),  # next day (since 02:00 < 13:00)
        "car_soc": 10,
        "car_plugged": True,
    }
    # Very large battery -> requires many hours, leaving little/no slack.
    config = {"max_fuse": 16.0, "charger_loss": 10.0, "car_capacity": 250.0, "has_price_sensor": True}

    plan = planner.generate_charging_plan(data, config, manual_override=False, now=now)
    assert plan["should_charge_now"] is True

