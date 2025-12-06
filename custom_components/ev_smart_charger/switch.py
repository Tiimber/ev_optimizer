"""Switch platform for EV Smart Charger."""
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ENTITY_SMART_SWITCH
from .coordinator import EVSmartChargerCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform."""
    coordinator: EVSmartChargerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EVSmartChargingSwitch(coordinator)])

class EVSmartChargingSwitch(CoordinatorEntity, SwitchEntity):
    """Master switch for Smart Charging logic."""

    _attr_name = "Smart Charging Enabled"
    _attr_unique_id = "ev_smart_charging_active"
    _attr_icon = "mdi:auto-fix"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        # Default to True
        self._attr_is_on = self.coordinator.data.get(ENTITY_SMART_SWITCH, True)

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        self._attr_is_on = True
        self.coordinator.set_user_input(ENTITY_SMART_SWITCH, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        self._attr_is_on = False
        self.coordinator.set_user_input(ENTITY_SMART_SWITCH, False)
        self.async_write_ha_state()