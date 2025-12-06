"""Number platform for EV Smart Charger."""
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, 
    ENTITY_TARGET_SOC,
    ENTITY_TARGET_OVERRIDE,
    ENTITY_PRICE_LIMIT_1,
    ENTITY_TARGET_SOC_1,
    ENTITY_PRICE_LIMIT_2,
    ENTITY_TARGET_SOC_2,
    ENTITY_MIN_SOC,
    ENTITY_PRICE_EXTRA_FEE,
    ENTITY_PRICE_VAT
)
from .coordinator import EVSmartChargerCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the number platform."""
    coordinator: EVSmartChargerCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        # Manual Target: 50-100%, step 10%
        EVChargeTargetNumber(coordinator, ENTITY_TARGET_SOC, "Standard Target SoC", 50, 100, 80, step=10),
        
        # Override Target: 50-100%, step 10% (New Entity)
        EVChargeTargetNumber(coordinator, ENTITY_TARGET_OVERRIDE, "Next Session Target SoC", 50, 100, 80, step=10),
        
        # Min Guarantee: 10-50%, default step 1%
        EVChargeTargetNumber(coordinator, ENTITY_MIN_SOC, "Minimum Guaranteed SoC", 10, 50, 20),
        
        # Threshold 1: Cheap
        EVPriceLimitNumber(coordinator, ENTITY_PRICE_LIMIT_1, "Price Limit Low (Cheap)", 0.0, 5.0, 0.5, step=0.01),
        EVChargeTargetNumber(coordinator, ENTITY_TARGET_SOC_1, "Target SoC at Low Price", 50, 100, 100, step=10),
        
        # Threshold 2: Moderate
        EVPriceLimitNumber(coordinator, ENTITY_PRICE_LIMIT_2, "Price Limit High (Acceptable)", 0.0, 10.0, 1.5, step=0.01),
        EVChargeTargetNumber(coordinator, ENTITY_TARGET_SOC_2, "Target SoC at High Price", 50, 100, 80, step=10),

        # Cost Settings
        EVPriceLimitNumber(coordinator, ENTITY_PRICE_EXTRA_FEE, "Extra Cost per kWh (Fees)", 0.0, 5.0, 0.0, step=0.01),
        EVChargeTargetNumber(coordinator, ENTITY_PRICE_VAT, "VAT Percentage", 0, 100, 0, step=1),
    ])

class EVNumberBase(CoordinatorEntity, NumberEntity):
    """Base class for EV numbers."""
    
    def __init__(self, coordinator, key, name, min_val, max_val, default_val, step=1.0):
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"ev_smart_{key}"
        self._attr_native_min_value = min_val
        self._attr_native_max_value = max_val
        self._attr_native_step = step
        self._default_val = default_val

    @property
    def native_value(self) -> float:
        """Return the current value from the coordinator data."""
        return self.coordinator.data.get(self._key, self._default_val)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        # Save to coordinator so logic can use it
        self.coordinator.set_user_input(self._key, value)

class EVChargeTargetNumber(EVNumberBase):
    """Slider for SoC targets."""
    _attr_icon = "mdi:battery-charging-high"
    _attr_native_unit_of_measurement = "%"

class EVPriceLimitNumber(EVNumberBase):
    """Slider for Price limits."""
    _attr_icon = "mdi:currency-usd"
    _attr_mode = NumberMode.BOX