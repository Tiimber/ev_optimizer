"""Time platform for EV Smart Charger."""
from datetime import time
from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ENTITY_DEPARTURE_TIME, ENTITY_DEPARTURE_OVERRIDE
from .coordinator import EVSmartChargerCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the time platform."""
    coordinator: EVSmartChargerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EVDepartureTime(coordinator),
        EVDepartureOverride(coordinator)
    ])

class EVDepartureTime(CoordinatorEntity, TimeEntity):
    """Time entity for setting the standard daily departure time."""

    _attr_name = "Standard Departure Time"
    _attr_unique_id = "ev_smart_departure_time"
    _attr_icon = "mdi:clock-out"

    def __init__(self, coordinator):
        """Initialize the time entity."""
        super().__init__(coordinator)
        # Load the saved time, or default to 07:00
        self._attr_native_value = self.coordinator.data.get(ENTITY_DEPARTURE_TIME, time(7, 0))

    async def async_set_value(self, value: time) -> None:
        """Update the time."""
        self._attr_native_value = value
        self.coordinator.set_user_input(ENTITY_DEPARTURE_TIME, value)
        self.async_write_ha_state()

class EVDepartureOverride(CoordinatorEntity, TimeEntity):
    """Time entity for overriding the next session's departure time."""

    _attr_name = "Next Session Departure"
    _attr_unique_id = "ev_smart_departure_override"
    _attr_icon = "mdi:clock-fast"

    def __init__(self, coordinator):
        """Initialize the override entity."""
        super().__init__(coordinator)
        # Default to the Standard time if no override is currently active
        std_time = self.coordinator.data.get(ENTITY_DEPARTURE_TIME, time(7, 0))
        self._attr_native_value = self.coordinator.data.get(ENTITY_DEPARTURE_OVERRIDE, std_time)

    async def async_set_value(self, value: time) -> None:
        """Update the override time."""
        self._attr_native_value = value
        self.coordinator.set_user_input(ENTITY_DEPARTURE_OVERRIDE, value)
        self.async_write_ha_state()