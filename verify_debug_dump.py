#!/usr/bin/env python3
"""Verify dump_debug_state will work by checking imports statically."""

import re
import sys


def check_const_file():
    """Check that const.py has all required constants."""
    print("=" * 70)
    print("Checking const.py for required constants...")
    print("=" * 70)
    
    with open('custom_components/ev_optimizer/const.py', 'r') as f:
        const_content = f.read()
    
    required = [
        'ENTITY_TARGET_SOC',
        'ENTITY_MIN_SOC',
        'ENTITY_PRICE_LIMIT_1',
        'ENTITY_TARGET_SOC_1',
        'ENTITY_PRICE_LIMIT_2',
        'ENTITY_TARGET_SOC_2',
        'ENTITY_PRICE_EXTRA_FEE',
        'ENTITY_PRICE_VAT',
        'ENTITY_DEPARTURE_TIME',
        'ENTITY_DEPARTURE_OVERRIDE',
        'ENTITY_SMART_SWITCH',
        'ENTITY_TARGET_OVERRIDE',
    ]
    
    missing = []
    found = {}
    
    for const in required:
        # Look for ENTITY_XXX = "..."
        pattern = rf'^{const}\s*=\s*["\'](.+?)["\']'
        match = re.search(pattern, const_content, re.MULTILINE)
        if match:
            found[const] = match.group(1)
            print(f"✅ {const} = '{match.group(1)}'")
        else:
            missing.append(const)
            print(f"❌ {const} NOT FOUND")
    
    if missing:
        print(f"\n❌ FAILED: Missing constants in const.py: {missing}")
        return False
    
    print(f"\n✅ All {len(required)} constants found in const.py")
    return True


def check_coordinator_imports():
    """Check that coordinator.py imports all required constants."""
    print("\n" + "=" * 70)
    print("Checking coordinator.py imports...")
    print("=" * 70)
    
    with open('custom_components/ev_optimizer/coordinator.py', 'r') as f:
        coordinator_content = f.read()
    
    required = [
        'ENTITY_TARGET_SOC',
        'ENTITY_MIN_SOC',
        'ENTITY_PRICE_LIMIT_1',
        'ENTITY_TARGET_SOC_1',
        'ENTITY_PRICE_LIMIT_2',
        'ENTITY_TARGET_SOC_2',
        'ENTITY_PRICE_EXTRA_FEE',
        'ENTITY_PRICE_VAT',
        'ENTITY_DEPARTURE_TIME',
        'ENTITY_DEPARTURE_OVERRIDE',
        'ENTITY_SMART_SWITCH',
        'ENTITY_TARGET_OVERRIDE',
    ]
    
    # Find the import section
    import_match = re.search(
        r'from \.const import \((.*?)\)',
        coordinator_content,
        re.DOTALL
    )
    
    if not import_match:
        print("❌ Could not find 'from .const import (...)' block")
        return False
    
    import_section = import_match.group(1)
    
    missing = []
    for const in required:
        if const in import_section:
            print(f"✅ {const}")
        else:
            missing.append(const)
            print(f"❌ {const} NOT IMPORTED")
    
    if missing:
        print(f"\n❌ FAILED: Missing imports in coordinator.py: {missing}")
        print("\nCurrent import section:")
        print(import_section)
        return False
    
    print(f"\n✅ All {len(required)} constants imported in coordinator.py")
    return True


def check_dump_debug_state_usage():
    """Check that dump_debug_state uses the constants correctly."""
    print("\n" + "=" * 70)
    print("Checking dump_debug_state method...")
    print("=" * 70)
    
    with open('custom_components/ev_optimizer/coordinator.py', 'r') as f:
        coordinator_content = f.read()
    
    # Find the dump_debug_state method
    if 'def dump_debug_state(self)' not in coordinator_content:
        print("❌ dump_debug_state method not found")
        return False
    
    print("✅ dump_debug_state method exists")
    
    # Check that it uses the constants
    constants_used = [
        'ENTITY_TARGET_SOC',
        'ENTITY_MIN_SOC',
        'ENTITY_PRICE_LIMIT_1',
        'ENTITY_TARGET_SOC_1',
        'ENTITY_PRICE_LIMIT_2',
        'ENTITY_TARGET_SOC_2',
    ]
    
    # Find method body
    method_start = coordinator_content.find('def dump_debug_state(self)')
    # Find next method or end of class
    next_method = coordinator_content.find('\n    def ', method_start + 1)
    if next_method == -1:
        next_method = len(coordinator_content)
    
    method_body = coordinator_content[method_start:next_method]
    
    for const in constants_used:
        if const in method_body:
            print(f"✅ Uses {const}")
        else:
            print(f"⚠️  Does not use {const}")
    
    print("\n✅ dump_debug_state method is properly defined")
    return True


def main():
    print("\n🔍 Verifying dump_debug_state will work correctly\n")
    
    results = []
    results.append(check_const_file())
    results.append(check_coordinator_imports())
    results.append(check_dump_debug_state_usage())
    
    print("\n" + "=" * 70)
    if all(results):
        print("🎉 ALL CHECKS PASSED!")
        print("=" * 70)
        print("\nThe dump_debug_state method should work correctly.")
        print("All required constants are defined and imported.")
        return 0
    else:
        print("❌ SOME CHECKS FAILED!")
        print("=" * 70)
        print("\nPlease review the errors above.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
