"""Test that all constants required by dump_debug_state are importable."""

def test_all_debug_dump_constants_importable():
    """Verify all constants used in dump_debug_state can be imported."""
    try:
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
        
        # Verify they're all strings
        constants = [
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
        ]
        
        for const in constants:
            assert isinstance(const, str), f"Constant should be a string: {const}"
            assert len(const) > 0, f"Constant should not be empty: {const}"
        
        print("✅ All constants imported successfully:")
        print(f"   ENTITY_TARGET_SOC = {ENTITY_TARGET_SOC}")
        print(f"   ENTITY_PRICE_LIMIT_1 = {ENTITY_PRICE_LIMIT_1}")
        print(f"   ENTITY_TARGET_SOC_1 = {ENTITY_TARGET_SOC_1}")
        print(f"   ENTITY_PRICE_LIMIT_2 = {ENTITY_PRICE_LIMIT_2}")
        print(f"   ENTITY_TARGET_SOC_2 = {ENTITY_TARGET_SOC_2}")
        
        return True
        
    except ImportError as e:
        print(f"❌ Import failed: {e}")
        raise
    except NameError as e:
        print(f"❌ NameError: {e}")
        raise


def test_coordinator_imports_constants():
    """Verify coordinator.py has all the constants imported."""
    import os
    
    coordinator_path = "custom_components/ev_smart_charger/coordinator.py"
    assert os.path.exists(coordinator_path), f"coordinator.py not found at {coordinator_path}"
    
    with open(coordinator_path, 'r') as f:
        content = f.read()
    
    # Check that all constants are in the import section
    required_imports = [
        "ENTITY_TARGET_SOC",
        "ENTITY_MIN_SOC",
        "ENTITY_PRICE_LIMIT_1",
        "ENTITY_TARGET_SOC_1",
        "ENTITY_PRICE_LIMIT_2",
        "ENTITY_TARGET_SOC_2",
        "ENTITY_PRICE_EXTRA_FEE",
        "ENTITY_PRICE_VAT",
        "ENTITY_DEPARTURE_TIME",
        "ENTITY_DEPARTURE_OVERRIDE",
        "ENTITY_SMART_SWITCH",
        "ENTITY_TARGET_OVERRIDE",
    ]
    
    # Find the import section
    import_section_start = content.find("from .const import (")
    import_section_end = content.find(")", import_section_start)
    
    if import_section_start == -1:
        raise AssertionError("Could not find 'from .const import (' in coordinator.py")
    
    import_section = content[import_section_start:import_section_end]
    
    missing = []
    for imp in required_imports:
        if imp not in import_section:
            missing.append(imp)
    
    if missing:
        print(f"❌ Missing imports in coordinator.py: {missing}")
        print("\nImport section found:")
        print(import_section)
        raise AssertionError(f"Missing imports: {missing}")
    
    print("✅ All required constants are imported in coordinator.py")
    return True


if __name__ == "__main__":
    test_all_debug_dump_constants_importable()
    test_coordinator_imports_constants()
    print("\n🎉 All tests passed!")
