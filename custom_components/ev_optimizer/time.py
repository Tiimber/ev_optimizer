"""Time platform for EV Optimizer."""
from datetime import time
from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    ENTITY_DEPARTURE_TIME,
    ENTITY_DEPARTURE_OVERRIDE,
    ENTITY_DEBUG_CURRENT_TIME,
    ENTITY_DEBUG_DEPARTURE_TIME,
)
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
        EVDepartureOverride(coordinator),
        EVDebugCurrentTime(coordinator),
        EVDebugDepartureTime(coordinator),
    ])

class EVDepartureTime(CoordinatorEntity, TimeEntity):
    """Time entity for setting the standard daily departure time."""

    _attr_has_entity_name = False
    _attr_name = "Standard Departure Time"
    _attr_unique_id = "ev_optimizer_departure_time"
    _attr_icon = "mdi:clock-out"

    def __init__(self, coordinator):
        """Initialize the time entity."""
        super().__init__(coordinator)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True

    @property
    def native_value(self) -> time:
        """Return the current value from the coordinator data."""
        if not self.coordinator.data:
            return time(7, 0)
        value = self.coordinator.data.get(ENTITY_DEPARTURE_TIME)
        if value is None:
            return time(7, 0)
        # Convert string to time if needed
        if isinstance(value, str):
            try:
                parts = value.split(":")
                return time(int(parts[0]), int(parts[1]))
            except:
                return time(7, 0)
        return value

    async def async_set_value(self, value: time) -> None:
        """Update the time."""
        self.coordinator.set_user_input(ENTITY_DEPARTURE_TIME, value)
        self.async_write_ha_state()

class EVDepartureOverride(CoordinatorEntity, TimeEntity):
    """Time entity for overriding the next session's departure time."""

    _attr_has_entity_name = False
    _attr_name = "Next Session Departure"
    _attr_unique_id = "ev_optimizer_departure_override"
    _attr_icon = "mdi:clock-fast"

    def __init__(self, coordinator):
        """Initialize the override entity."""
        super().__init__(coordinator)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True

    @property
    def native_value(self) -> time:
        """Return the current value from the coordinator data."""
        if not self.coordinator.data:
            return time(7, 0)
        # Get override value
        override = self.coordinator.data.get(ENTITY_DEPARTURE_OVERRIDE)
        if override is not None:
            # Convert string to time if needed
            if isinstance(override, str):
                try:
                    parts = override.split(":")
                    return time(int(parts[0]), int(parts[1]))
                except:
                    pass
            elif isinstance(override, time):
                return override
        # Fall back to standard departure time
        std_time = self.coordinator.data.get(ENTITY_DEPARTURE_TIME, time(7, 0))
        if isinstance(std_time, str):
            try:
                parts = std_time.split(":")
                return time(int(parts[0]), int(parts[1]))
            except:
                return time(7, 0)
        return std_time if isinstance(std_time, time) else time(7, 0)

    async def async_set_value(self, value: time) -> None:
        """Update the override time."""
        self.coordinator.set_user_input(ENTITY_DEPARTURE_OVERRIDE, value)
        self.async_write_ha_state()

class EVDebugCurrentTime(CoordinatorEntity, TimeEntity):
    """Debug entity for custom simulation - current time."""

    _attr_has_entity_name = False
    _attr_name = "Debug: Current Time"
    _attr_unique_id = "ev_optimizer_debug_current_time"
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator):
        """Initialize the debug time entity."""
        super().__init__(coordinator)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True

    @property
    def native_value(self) -> time | None:
        """Return the current value from the coordinator data."""
        if not self.coordinator.data:
            return time(0, 0)
        value = self.coordinator.data.get(ENTITY_DEBUG_CURRENT_TIME)
        if value is None:
            return time(0, 0)
        # Convert string to time if needed
        if isinstance(value, str):
            try:
                parts = value.split(":")
                return time(int(parts[0]), int(parts[1]))
            except:
                return time(0, 0)
        return value

    async def async_set_value(self, value: time) -> None:
        """Update the time."""
        self.coordinator.set_user_input(ENTITY_DEBUG_CURRENT_TIME, value)
        self.async_write_ha_state()

class EVDebugDepartureTime(CoordinatorEntity, TimeEntity):
    """Debug entity for custom simulation - departure time."""

    _attr_has_entity_name = False
    _attr_name = "Debug: Departure Time"
    _attr_unique_id = "ev_optimizer_debug_departure_time"
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator):
        """Initialize the debug time entity."""
        super().__init__(coordinator)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True

    @property
    def native_value(self) -> time | None:
        """Return the current value from the coordinator data."""
        if not self.coordinator.data:
            return time(7, 0)
        value = self.coordinator.data.get(ENTITY_DEBUG_DEPARTURE_TIME)
        if value is None:
            return time(7, 0)
        # Convert string to time if needed
        if isinstance(value, str):
            try:
                parts = value.split(":")
                return time(int(parts[0]), int(parts[1]))
            except:
                return time(7, 0)
        return value

    async def async_set_value(self, value: time) -> None:
        """Update the time."""
        self.coordinator.set_user_input(ENTITY_DEBUG_DEPARTURE_TIME, value)
        self.async_write_ha_state()