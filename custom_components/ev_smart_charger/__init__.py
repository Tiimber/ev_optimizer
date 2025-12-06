"""The EV Smart Charger integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EVSmartChargerCoordinator

# List the platforms that we will create entities for
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.NUMBER, Platform.SWITCH, Platform.BUTTON, Platform.TIME]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EV Smart Charger from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    # Initialize the Coordinator
    coordinator = EVSmartChargerCoordinator(hass, entry)
    
    # Fetch initial data so we have data when entities are added
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator reference
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Forward the setup to the platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)