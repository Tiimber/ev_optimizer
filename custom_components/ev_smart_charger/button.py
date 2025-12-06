"""Button platform for EV Smart Charger."""
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ENTITY_BUTTON_CLEAR_OVERRIDE
from .coordinator import EVSmartChargerCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    coordinator: EVSmartChargerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EVRefreshButton(coordinator),
        EVClearOverrideButton(coordinator)
    ])

class EVRefreshButton(CoordinatorEntity, ButtonEntity):
    """Button to force a plan refresh."""

    _attr_name = "Refresh Charging Plan"
    _attr_unique_id = "ev_smart_refresh_plan"
    _attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_refresh()

class EVClearOverrideButton(CoordinatorEntity, ButtonEntity):
    """Button to clear manual overrides and revert to smart logic."""

    _attr_name = "Clear Manual Override"
    _attr_unique_id = "ev_smart_clear_override"
    _attr_icon = "mdi:restore-alert"

    async def async_press(self) -> None:
        """Handle the button press."""
        # Call coordinator to reset flags
        self.coordinator.clear_manual_override()