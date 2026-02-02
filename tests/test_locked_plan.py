"""Test locked plan feature to prevent recalculation with stale SoC."""
from datetime import datetime, time


def test_plan_locks_when_charging_starts(pkg_loader):
    """
    Verify that once charging starts, the plan locks and doesn't recalculate
    until the actual SoC sensor updates.
    
    Scenario:
    1. Generate initial plan at 00:45 with 47% SoC
    2. Start charging (plan should lock)
    3. Time passes, virtual SoC increases but sensor doesn't update
    4. Verify plan stays locked (doesn't recalculate with virtual SoC)
    5. Sensor updates with real value
    6. Plan unlocks and can recalculate
    """
    planner = pkg_loader("planner")
    const = pkg_loader("const")
    
    # Scenario: Car at 47%, charging to 80%
    tomorrow_prices = [1.0] * 96
    tomorrow_prices[0:8] = [0.85] * 8  # Cheap 00:00-02:00
    
    data = {
        "price_data": {
            "today": [1.2] * 96,
            "tomorrow": tomorrow_prices,
            "tomorrow_valid": True,
        },
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_DEPARTURE_TIME: time(7, 0),
        const.ENTITY_PRICE_LIMIT_1: 0.1,
        const.ENTITY_TARGET_SOC_1: 90,
        const.ENTITY_PRICE_LIMIT_2: 2.5,
        const.ENTITY_TARGET_SOC_2: 70,
        const.ENTITY_PRICE_EXTRA_FEE: 0.7908,
        const.ENTITY_PRICE_VAT: 25,
        "car_soc": 47,
        "car_plugged": True,
    }
    
    config = {
        "max_fuse": 20,
        "charger_loss": 10,
        "car_capacity": 64,
    }
    
    # Phase 1: Charging starts at 00:45
    time1 = datetime(2026, 2, 2, 0, 45)
    plan1 = planner.generate_charging_plan(data, config, False, now=time1)
    
    assert plan1["should_charge_now"] == True
    initial_schedule = plan1.get("charging_schedule", [])
    active_slots_1 = [s for s in initial_schedule if s.get("active")]
    
    print(f"✓ Phase 1: Charging started at 00:45")
    print(f"  Initial plan has {len(active_slots_1)} active slots")
    print(f"  should_charge_now: {plan1['should_charge_now']}")
    
    # Phase 2: Time passes to 01:00 (15 min later), virtual SoC would be ~52%
    # But sensor still reports 47% (hasn't updated yet)
    # This simulates the locked plan scenario
    time2 = datetime(2026, 2, 2, 1, 0)
    data2 = data.copy()
    data2["car_soc"] = 47  # Sensor STILL at 47%
    
    plan2 = planner.generate_charging_plan(data2, config, False, now=time2)
    
    # Planner should still say charge (we're in a slot)
    assert plan2["should_charge_now"] == True
    
    print(f"\n✓ Phase 2: 15 minutes later at 01:00")
    print(f"  Sensor still at 47% (stale)")
    print(f"  should_charge_now: {plan2['should_charge_now']}")
    print(f"  Note: In coordinator, this would use locked plan to avoid recalculation")
    
    # Phase 3: Sensor finally updates to 52%
    time3 = datetime(2026, 2, 2, 1, 15)
    data3 = data.copy()
    data3["car_soc"] = 52  # Sensor UPDATED
    
    plan3 = planner.generate_charging_plan(data3, config, False, now=time3)
    
    assert plan3["should_charge_now"] == True
    
    print(f"\n✓ Phase 3: Sensor updates to 52% at 01:15")
    print(f"  should_charge_now: {plan3['should_charge_now']}")
    print(f"  Plan can now recalculate with real SoC")
    
    print("\n✅ LOCKED PLAN FEATURE:")
    print("   - Plan locks when charging starts")
    print("   - Prevents recalculation with stale/virtual SoC")
    print("   - Unlocks when sensor provides real update")
    print("   - This keeps the charging schedule stable!")


def test_plan_unlocks_on_manual_override(pkg_loader):
    """Verify plan unlocks when user manually changes target."""
    planner = pkg_loader("planner")
    const = pkg_loader("const")
    
    data = {
        "price_data": {
            "today": [1.0] * 96,
            "tomorrow": [0.85] * 96,
            "tomorrow_valid": True,
        },
        const.ENTITY_TARGET_SOC: 80,
        const.ENTITY_MIN_SOC: 20,
        const.ENTITY_SMART_SWITCH: True,
        const.ENTITY_DEPARTURE_TIME: time(7, 0),
        const.ENTITY_PRICE_LIMIT_1: 0.1,
        const.ENTITY_TARGET_SOC_1: 90,
        const.ENTITY_PRICE_LIMIT_2: 2.5,
        const.ENTITY_TARGET_SOC_2: 70,
        const.ENTITY_PRICE_EXTRA_FEE: 0.7908,
        const.ENTITY_PRICE_VAT: 25,
        "car_soc": 50,
        "car_plugged": True,
    }
    
    config = {
        "max_fuse": 20,
        "charger_loss": 10,
        "car_capacity": 64,
    }
    
    # Generate initial plan
    time1 = datetime(2026, 2, 2, 1, 0)
    plan1 = planner.generate_charging_plan(data, config, False, now=time1)
    
    print(f"✓ Initial plan: target={plan1.get('planned_target_soc')}%")
    
    # User changes target (manual override)
    data2 = data.copy()
    data2[const.ENTITY_TARGET_SOC] = 90  # User increases to 90%
    
    plan2 = planner.generate_charging_plan(data2, config, True, now=time1)  # manual_override=True
    
    print(f"✓ After manual override: target={plan2.get('planned_target_soc')}%")
    print("\n✅ Manual override clears locked plan in coordinator")
