# Testing EV Smart Charger

This directory contains tests for the EV Smart Charger integration.

## Test Files

### 1. `verify_debug_dump.py`
Static analysis that verifies all constants are properly defined and imported.

**Run:**
```bash
python3 verify_debug_dump.py
```

**What it checks:**
- ✅ All constants exist in `const.py`
- ✅ All constants are imported in `coordinator.py`
- ✅ `dump_debug_state` method exists and uses the constants

### 2. `test_dump_simple.py`
Integration test that actually executes `dump_debug_state` with mocked HA.

**Run:**
```bash
PYTHONPATH=/workspaces/ev_smart_charger python3 tests/test_dump_simple.py
```

**What it tests:**
- ✅ All constants can be imported
- ✅ `dump_debug_state` executes without errors
- ✅ Returns correct structure
- ✅ No `NameError` or `AttributeError`
- ✅ Session state is correctly determined

### 3. `test_imports_simple.py`
Checks import statements and usage in coordinator.

### 4. Legacy Tests
- `test_coordinator.py` - Main coordinator tests
- `test_planner.py` - Planner logic tests  
- `test_session.py` - Session manager tests
- `test_integration.py` - Full integration tests

## Running All Tests

```bash
# Verification only (fastest)
python3 verify_debug_dump.py

# Integration test with mocking
PYTHONPATH=/workspaces/ev_smart_charger python3 tests/test_dump_simple.py

# All pytest tests (requires homeassistant package)
pytest tests/
```

## CI/CD Integration

Add to your CI pipeline:

```yaml
- name: Verify debug dump
  run: python3 verify_debug_dump.py

- name: Test debug dump
  run: PYTHONPATH=$PWD python3 tests/test_dump_simple.py
```

## Test Results

Both tests should output:
```
🎉 ALL CHECKS PASSED!
```

If they fail, check:
1. Are all constants in `const.py`?
2. Are they imported in `coordinator.py`?
3. Does `session_manager` have `current_session` attribute?
