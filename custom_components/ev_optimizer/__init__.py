"""The EV Optimizer integration."""

from __future__ import annotations

import importlib
import logging
from datetime import datetime
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EVSmartChargerCoordinator

_LOGGER = logging.getLogger(__name__)

# List the platforms that we will create entities for
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.TIME,
    Platform.CAMERA,  # Added Camera
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EV Optimizer from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Initialize the Coordinator
    coordinator = EVSmartChargerCoordinator(hass, entry)

    # Fetch initial data so we have data when entities are added
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator reference
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # FIX: Pre-import logbook platform in background to prevent blocking I/O error
    await hass.async_add_executor_job(importlib.import_module, f"{__package__}.logbook")

    # Forward the setup to the platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(update_listener))
    
    # Setup real-time listeners
    coordinator.async_setup_listeners()
    entry.async_on_unload(coordinator.async_shutdown)
    
    # Register services
    async def handle_dump_debug_state(call):
        """Handle the dump_debug_state service call."""
        coordinator.dump_debug_state()
    
    async def handle_capture_snapshot(call):
        """Handle the capture_snapshot service call."""
        await coordinator.snapshot_manager.capture_snapshot(
            coordinator_data=coordinator.data,
            plan=coordinator.data,
            actions_taken=["Manual snapshot capture"]
        )
        _LOGGER.info("Manual snapshot captured")
    
    async def handle_export_snapshots(call):
        """Handle the export_snapshots service call."""
        start_time = call.data.get("start_time")
        end_time = call.data.get("end_time")
        output_format = call.data.get("output_format", "json")
        
        # Parse ISO datetime strings if provided
        if start_time:
            start_time = datetime.fromisoformat(start_time)
        if end_time:
            end_time = datetime.fromisoformat(end_time)
        
        file_path = await coordinator.snapshot_manager.export_snapshots(
            start_time=start_time,
            end_time=end_time,
            output_format=output_format
        )
        _LOGGER.info(f"Snapshots exported to {file_path}")
        
        # Fire event so user can be notified
        hass.bus.async_fire(
            f"{DOMAIN}_snapshots_exported",
            {"file_path": file_path, "format": output_format}
        )
    
    async def handle_replay_scenario(call):
        """Handle the replay_scenario service call."""
        start_time_str = call.data.get("start_time")
        end_time_str = call.data.get("end_time")
        output_format = call.data.get("output_format", "html")
        
        if not start_time_str or not end_time_str:
            _LOGGER.error("replay_scenario requires both start_time and end_time")
            return
        
        start_time = datetime.fromisoformat(start_time_str)
        end_time = datetime.fromisoformat(end_time_str)
        
        # Get snapshots in range
        snapshots = coordinator.snapshot_manager.get_snapshots_in_range(start_time, end_time)
        
        if not snapshots:
            _LOGGER.warning(f"No snapshots found between {start_time} and {end_time}")
            return
        
        # Export as replay report
        file_path = await coordinator.snapshot_manager.export_snapshots(
            start_time=start_time,
            end_time=end_time,
            output_format=output_format
        )
        
        _LOGGER.info(f"Replay report generated: {file_path} ({len(snapshots)} snapshots)")
        
        # Fire event
        hass.bus.async_fire(
            f"{DOMAIN}_replay_complete",
            {
                "file_path": file_path,
                "format": output_format,
                "start_time": start_time_str,
                "end_time": end_time_str,
                "snapshot_count": len(snapshots)
            }
        )
    
    hass.services.async_register(
        DOMAIN,
        "dump_debug_state",
        handle_dump_debug_state,
    )
    
    hass.services.async_register(
        DOMAIN,
        "capture_snapshot",
        handle_capture_snapshot,
    )
    
    hass.services.async_register(
        DOMAIN,
        "export_snapshots",
        handle_export_snapshots,
    )
    
    hass.services.async_register(
        DOMAIN,
        "replay_scenario",
        handle_replay_scenario,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
