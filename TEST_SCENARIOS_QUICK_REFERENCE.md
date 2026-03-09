# Test Scenarios - Quick Reference

## Test Files Overview

### 1. `test_week_scenarios.json` - Comprehensive Week
**53 snapshots** covering **8 major scenarios** from March 2-9, 2026

| Scenario | Date/Time | Description | Validates |
|----------|-----------|-------------|-----------|
| **1. Normal Overnight** | Mar 2, 00:00-08:00 | Calendar event same day at 07:30, charge 02:00-05:00 | ✅ Calendar detection, price optimization |
| **2. Default Time** | Mar 2, 17:00 → Mar 3, 02:00 | No calendar, uses default 07:00 | ✅ Default fallback, evening plugin |
| **3. Daytime Urgent** | Mar 3, 13:00-18:00 | Only 5hrs to reach 85%, must charge now | ✅ Time-constrained, custom target |
| **4. Load Balancing** | Mar 4, 22:00 → Mar 5, 04:00 | Available current varies 9A→22A | ✅ Load balancing, minimum threshold |
| **5. Manual Override** | Mar 5, 16:00-19:00 | User forces OFF, too late to charge | ✅ Manual control, warnings |
| **6. Low SoC Emergency** | Mar 6, 08:00-11:00 | 28% triggers emergency charge | ✅ Low SoC detection |
| **7. Multiple Events** | Mar 6, 19:00 → Mar 7, 03:00 | Two events, chooses earliest | ✅ Event selection logic |
| **8. Critical SoC** | Mar 8, 14:00-02:00 | 18% critical, two-stage charging | ✅ Emergency + resume logic |

**Test**: `python3 simulate_from_dump.py test_week_scenarios.json`

---

### 2. `test_calendar_bug_fix_scenario.json` - The Bug Fix ⭐
**13 snapshots** demonstrating the **midnight calendar bug fix** from March 10, 00:00-12:00

| Time | SoC | Status | Key Point |
|------|-----|--------|-----------|
| 00:00 | 42% | 🟢 CHARGING | **THE FIX**: Detects early morning (< 08:00) + tomorrow's event → charges NOW |
| 01:00 | 55% | 🟢 CHARGING | Continuing during cheap night rates |
| 02:00 | 68% | 🟢 CHARGING | Approaching target |
| 03:00 | 80% | 🔴 PAUSED | Target reached - **27 hours early!** |
| 04:00-12:00 | 80% | 🔴 PAUSED | Ready and waiting for tomorrow's departure |

**The Bug That Was Fixed**:
- **Before**: Car plugged at midnight with tomorrow's calendar event → system waited all night for tomorrow's prices → car didn't charge
- **After**: System detects `early_in_day` (hour < 8) + `has_full_today_prices` → charges immediately during cheap hours

**Code Fix**: [planner.py lines 407-420](../custom_components/ev_optimizer/planner.py#L407-L420)

**Test**: `python3 simulate_from_dump.py test_calendar_bug_fix_scenario.json`

**Expected Output**:
```
[1/13] 2026-03-10 00:00 🔌 🟢 CHARGING
     SoC: 42% → 80%  |  Available: 22A
     💡 Actions:
        - Calendar event for tomorrow detected: 07:00
        - Early morning check: hour=0 < 8, have today's prices
        - Planning with today's prices instead of waiting
        - Starting charging session ✅
```

---

## Quick Validation Commands

### Run Both Tests
```bash
# Full week
python3 simulate_from_dump.py test_week_scenarios.json | grep -E "(CHARGING|PAUSED|SoC Change)"

# Bug fix
python3 simulate_from_dump.py test_calendar_bug_fix_scenario.json | grep -E "Early morning"
```

### Validate JSON Structure
```bash
# Check valid JSON
jq . test_week_scenarios.json > /dev/null && echo "✅ Week scenarios valid"
jq . test_calendar_bug_fix_scenario.json > /dev/null && echo "✅ Bug fix scenario valid"

# Check required keys
jq 'has("export_info") and has("snapshots") and has("prices")' test_*.json
```

### Check Snapshot Counts
```bash
echo "Week scenarios: $(jq '.snapshots | length' test_week_scenarios.json) snapshots"
echo "Bug fix scenario: $(jq '.snapshots | length' test_calendar_bug_fix_scenario.json) snapshots"
```

---

## What Each Test Validates

### Coverage Matrix

| Feature | Week Test | Bug Test |
|---------|-----------|----------|
| Calendar same day | ✅ | |
| Calendar tomorrow | | ✅ |
| No calendar (default) | ✅ | |
| Multiple events | ✅ | |
| Price optimization | ✅ | ✅ |
| Time constraints | ✅ | ✅ |
| Load balancing | ✅ | |
| Low SoC emergency | ✅ | |
| Critical SoC | ✅ | |
| Manual overrides | ✅ | |
| Early morning logic | | ✅ |
| Tomorrow event handling | | ✅ |

---

## Expected Outputs

### test_week_scenarios.json
```
📈 SUMMARY:
  Charging: 29/53 hours (54.7%)
  Paused: 24/53 hours (45.3%)
  SoC Change: 45% → 80% (+35.0%)
  Calendar Events: 34 snapshots had events
```

### test_calendar_bug_fix_scenario.json
```
📈 SUMMARY:
  Charging: 3/13 hours (23%)
  Paused: 10/13 hours (77%)
  SoC Change: 42% → 80% (+38.0%)
  Calendar Events: 13 snapshots had events
```

---

## Integration with CI/CD (Future)

Add to `.github/workflows/test.yml`:

```yaml
- name: Validate test scenarios
  run: |
    python3 simulate_from_dump.py test_week_scenarios.json > /dev/null
    python3 simulate_from_dump.py test_calendar_bug_fix_scenario.json > /dev/null
    echo "✅ All scenarios validated"

- name: Check bug fix scenario
  run: |
    OUTPUT=$(python3 simulate_from_dump.py test_calendar_bug_fix_scenario.json)
    if echo "$OUTPUT" | grep -q "Early morning check"; then
      echo "✅ Bug fix scenario working"
    else
      echo "❌ Bug fix validation failed"
      exit 1
    fi
```

---

## Creating Your Own Scenarios

Use these as templates:

1. **Copy structure** from `test_calendar_bug_fix_scenario.json` (simpler)
2. **Modify snapshots** with your specific case
3. **Update prices** to match your situation
4. **Run simulation** to validate
5. **Document** what it tests

Example for a new scenario:
```bash
cp test_calendar_bug_fix_scenario.json test_my_scenario.json
# Edit test_my_scenario.json
python3 simulate_from_dump.py test_my_scenario.json
```

---

## Files Reference

- **test_week_scenarios.json** - 8 scenarios, 53 snapshots, full week
- **test_calendar_bug_fix_scenario.json** - Bug fix validation, 13 snapshots
- **TEST_SCENARIOS_DOCUMENTATION.md** - Detailed documentation of all scenarios
- **simulate_from_dump.py** - Replay tool that processes both formats
- **SNAPSHOT_SYSTEM_SUMMARY.md** - Complete snapshot system documentation

---

## Status

✅ **All 9 scenarios validated**  
✅ **Simulation tool working correctly**  
✅ **Bug fix scenario specifically tested**  
✅ **Ready for production deployment**

The snapshot debugging system is now thoroughly tested with both:
- **Comprehensive real-world scenarios** (week-long test)
- **Specific regression test** (the midnight bug that was fixed)

When you deploy to production, this snapshot system will automatically capture similar data, and you can use `simulate_from_dump.py` to analyze any future issues!
