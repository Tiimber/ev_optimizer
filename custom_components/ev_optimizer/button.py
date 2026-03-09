"""Button platform for EV Optimizer."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ENTITY_BUTTON_CLEAR_OVERRIDE, LEARNING_CHARGER_LOSS, LEARNING_CONFIDENCE, LEARNING_SESSIONS, LEARNING_LOCKED, LEARNING_HISTORY, LEARNING_LAST_REFRESH, DEFAULT_LOSS
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
            EVDumpCustomScenarioButton(coordinator),
            ResetEfficiencyLearningButton(coordinator),
            CaptureSnapshotButton(coordinator),
            ExportSnapshotsButton(coordinator),
        ]
    )


class EVRefreshButton(CoordinatorEntity, ButtonEntity):
    """Button to force a plan refresh."""

    _attr_name = "Refresh Charging Plan"
    _attr_unique_id = "ev_optimizer_refresh_plan"
    _attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_refresh()


class EVClearOverrideButton(CoordinatorEntity, ButtonEntity):
    """Button to clear manual overrides and revert to smart logic."""

    _attr_name = "Clear Manual Override"
    _attr_unique_id = "ev_optimizer_clear_override"
    _attr_icon = "mdi:restore-alert"

    async def async_press(self) -> None:
        """Handle the button press."""
        self.coordinator.clear_manual_override()


class EVGenerateReportButton(CoordinatorEntity, ButtonEntity):
    """Button to manually regenerate the last session report image."""

    _attr_name = "Regenerate Session Image"
    _attr_unique_id = "ev_optimizer_regenerate_session"
    _attr_icon = "mdi:printer"

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_trigger_report_generation()


class EVGeneratePlanButton(CoordinatorEntity, ButtonEntity):
    """Button to manually regenerate the future charging plan image."""

    _attr_name = "Regenerate Plan Image"
    _attr_unique_id = "ev_optimizer_regenerate_plan"
    _attr_icon = "mdi:printer-eye"

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_trigger_plan_image_generation()


class EVDumpDebugStateButton(CoordinatorEntity, ButtonEntity):
    """Button to dump complete debug state to logs."""

    _attr_name = "Dump Debug State"
    _attr_unique_id = "ev_optimizer_dump_debug"
    _attr_icon = "mdi:bug-check"

    async def async_press(self) -> None:
        """Handle the button press."""
        self.coordinator.dump_debug_state()


class EVDumpCustomScenarioButton(CoordinatorEntity, ButtonEntity):
    """Button to dump custom debug scenario using debug fields."""

    _attr_name = "Dump Custom Scenario"
    _attr_unique_id = "ev_optimizer_dump_custom"
    _attr_icon = "mdi:bug-play"

    async def async_press(self) -> None:
        """Handle the button press."""
        self.coordinator.dump_custom_scenario()


class ResetEfficiencyLearningButton(CoordinatorEntity, ButtonEntity):
    """Button to reset efficiency learning."""
    
    _attr_has_entity_name = False
    _attr_name = "Reset Efficiency Learning"
    _attr_unique_id = "ev_optimizer_reset_efficiency"
    _attr_icon = "mdi:restore"
    
    async def async_press(self) -> None:
        """Handle the button press."""
        configured = self.coordinator.config_settings.get("charger_loss", DEFAULT_LOSS)
        
        self.coordinator.learning_state.update({
            LEARNING_CHARGER_LOSS: configured,
            LEARNING_CONFIDENCE: 0,
            LEARNING_SESSIONS: 0,
            LEARNING_LOCKED: False,
            LEARNING_HISTORY: [],
            LEARNING_LAST_REFRESH: None,
        })
        
        self.coordinator._save_data()
        self.coordinator._add_log(
            f"Efficiency learning reset to {configured}% (from config)"
        )
        
        await self.coordinator.async_refresh()


class CaptureSnapshotButton(CoordinatorEntity, ButtonEntity):
    """Button to manually capture a snapshot for debugging."""
    
    _attr_has_entity_name = False
    _attr_name = "Capture Debug Snapshot"
    _attr_unique_id = "ev_optimizer_capture_snapshot"
    _attr_icon = "mdi:camera"
    
    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.snapshot_manager.capture_snapshot(
            coordinator_data=self.coordinator.data,
            plan=self.coordinator.data,
            actions_taken=["Manual snapshot capture via button"]
        )
        self.coordinator._add_log("Debug snapshot captured manually")


class ExportSnapshotsButton(CoordinatorEntity, ButtonEntity):
    """Button to export all snapshots to www/ directory."""
    
    _attr_has_entity_name = False
    _attr_name = "Export Debug Snapshots"
    _attr_unique_id = "ev_optimizer_export_snapshots"
    _attr_icon = "mdi:download"
    
    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            file_path = await self.coordinator.snapshot_manager.export_snapshots(
                output_format="json"
            )
            self.coordinator._add_log(f"Snapshots exported to {file_path}")
        except Exception as e:
            self.coordinator._add_log(f"Export failed: {e}")
