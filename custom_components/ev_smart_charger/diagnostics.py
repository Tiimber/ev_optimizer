"""Diagnostics support for EV Smart Charger."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EVSmartChargerCoordinator

TO_REDACT = {"password", "secret", "token", "unique_id"}

async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: EVSmartChargerCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Gather information
    data = {
        "entry_data": async_redact_data(entry.data, TO_REDACT),
        "entry_options": async_redact_data(entry.options, TO_REDACT),
        "coordinator_data": coordinator.data,
        "manual_override_active": coordinator.manual_override_active,
        "user_settings": coordinator.user_settings,
        "last_applied_state": {
            "amps": coordinator._last_applied_amps,
            "state": coordinator._last_applied_state,
            "car_limit": coordinator._last_applied_car_limit,
        },
        "virtual_soc": coordinator._virtual_soc,
        # Add session data to diagnostics
        "current_session": coordinator.session_manager.current_session,
        "last_session": coordinator.session_manager.last_session_data,
    }

    return data