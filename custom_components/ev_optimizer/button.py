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
    async_add_entities(
        [
            EVRefreshButton(coordinator),
            EVClearOverrideButton(coordinator),
            EVGenerateReportButton(coordinator),
            EVGeneratePlanButton(coordinator),
            EVDumpDebugStateButton(coordinator),
        ]
    )


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
        self.coordinator.clear_manual_override()


class EVGenerateReportButton(CoordinatorEntity, ButtonEntity):
    """Button to manually regenerate the last session report image."""

    _attr_name = "Regenerate Session Image"
    _attr_unique_id = "ev_smart_regenerate_session"
    _attr_icon = "mdi:printer"

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_trigger_report_generation()


class EVGeneratePlanButton(CoordinatorEntity, ButtonEntity):
    """Button to manually regenerate the future charging plan image."""

    _attr_name = "Regenerate Plan Image"
    _attr_unique_id = "ev_smart_regenerate_plan"
    _attr_icon = "mdi:printer-eye"

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_trigger_plan_image_generation()


class EVDumpDebugStateButton(CoordinatorEntity, ButtonEntity):
    """Button to dump complete debug state to logs."""

    _attr_name = "Dump Debug State"
    _attr_unique_id = "ev_smart_dump_debug"
    _attr_icon = "mdi:bug-check"

    async def async_press(self) -> None:
        """Handle the button press."""
        self.coordinator.dump_debug_state()
