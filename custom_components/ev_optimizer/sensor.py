"""Sensor platform for EV Smart Charger."""
import math
from datetime import datetime
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EVSmartChargerCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: EVSmartChargerCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        EVSmartChargerStatusSensor(coordinator),
        EVMaxAvailableCurrentSensor(coordinator),
        EVPriceStatusSensor(coordinator),
        EVChargingPlanSensor(coordinator),
        EVSmartChargerLastSessionSensor(coordinator), # New Sensor
        EVDebugDumpPathSensor(coordinator), # Debug dump file path
    ])

class EVSmartChargerBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for EV Smart Charger sensors."""

    def __init__(self, coordinator):
        """Initialize."""
        super().__init__(coordinator)
        self._attr_has_entity_name = True

class EVSmartChargerStatusSensor(EVSmartChargerBaseSensor):
    """Sensor showing the overall status."""
    _attr_name = "Charger Logic Status"
    _attr_unique_id = "ev_optimizer_status"
    _attr_icon = "mdi:ev-station"

    @property
    def state(self):
        data = self.coordinator.data
        if not data["car_plugged"]:
            return "Disconnected"
        if data.get("should_charge_now"):
            return "Charging"
        return "Waiting for Schedule"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        return {
            "car_soc": data.get("car_soc"),
            "plugged": data.get("car_plugged"),
            "target_soc": data.get("planned_target_soc"),
            "action_log": data.get("action_log", []),
            "latency_ms": data.get("latency_ms"),
        }

class EVMaxAvailableCurrentSensor(EVSmartChargerBaseSensor):
    """Sensor showing max safe current."""
    _attr_name = "Max Safe Current"
    _attr_unique_id = "ev_optimizer_safe_current"
    _attr_icon = "mdi:current-ac"
    _attr_native_unit_of_measurement = "A"

    @property
    def state(self):
        raw_current = self.coordinator.data["max_available_current"]
        return math.floor(raw_current)

class EVPriceStatusSensor(EVSmartChargerBaseSensor):
    """Sensor showing price logic status."""
    _attr_name = "Price Logic"
    _attr_unique_id = "ev_optimizer_price_logic"
    _attr_icon = "mdi:cash-clock"

    @property
    def state(self):
        return self.coordinator.data["current_price_status"]

class EVChargingPlanSensor(EVSmartChargerBaseSensor):
    """Sensor containing the calculated schedule."""
    _attr_name = "Charging Schedule"
    _attr_unique_id = "ev_optimizer_plan"
    _attr_icon = "mdi:calendar-clock"

    @property
    def state(self):
        """Show next start time or status."""
        data = self.coordinator.data
        if not data.get("car_plugged"):
            return "Car Disconnected"
        
        if data.get("should_charge_now"):
            return "Active Now"
            
        start = data.get("scheduled_start")
        if start:
            try:
                dt = datetime.fromisoformat(start)
                return f"Next: {dt.strftime('%H:%M')}"
            except:
                return start
        
        return "No Charging Needed"

    @property
    def extra_state_attributes(self):
        """Return the schedule for graphing."""
        return {
            "planned_target": self.coordinator.data.get("planned_target_soc"),
            "charging_summary": self.coordinator.data.get("charging_summary"),
            "schedule": self.coordinator.data.get("charging_schedule", [])
        }

class EVSmartChargerLastSessionSensor(EVSmartChargerBaseSensor):
    """Sensor containing the report for the last finished session."""
    _attr_name = "Last Charging Session"
    _attr_unique_id = "ev_optimizer_last_session"
    _attr_icon = "mdi:history"

    @property
    def state(self):
        """Return the timestamp of the last session end."""
        report = self.coordinator.session_manager.last_session_data
        if report:
            return report.get("end_time")
        return "No Data"

    @property
    def extra_state_attributes(self):
        """Return the full report data."""
        report = self.coordinator.session_manager.last_session_data
        if not report:
            return {}
            
        return {
            "start_time": report.get("start_time"),
            "added_kwh": report.get("added_kwh"),
            "cost": report.get("total_cost"),
            "currency": report.get("currency"),
            "start_soc": int(report.get("start_soc", 0)),
            "end_soc": int(report.get("end_soc", 0)),
            "graph_data": report.get("graph_data", []),
            "session_log": report.get("session_log", [])
        }


class EVDebugDumpPathSensor(EVSmartChargerBaseSensor):
    """Sensor showing the path to the debug dump file."""
    _attr_name = "Debug Dump File"
    _attr_unique_id = "ev_optimizer_debug_dump_file"
    _attr_icon = "mdi:file-document"

    @property
    def state(self):
        """Return the URL to download the debug dump."""
        return "/local/ev_optimizer_debug_dump.json"

    @property
    def extra_state_attributes(self):
        """Return additional info."""
        import os
        file_path = self.coordinator.hass.config.path("www", "ev_optimizer_debug_dump.json")
        file_exists = os.path.exists(file_path)
        
        attrs = {
            "file_path": file_path,
            "file_exists": file_exists,
            "download_url": "/local/ev_optimizer_debug_dump.json",
        }
        
        if file_exists:
            try:
                stat = os.stat(file_path)
                attrs["file_size_bytes"] = stat.st_size
                attrs["last_modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
            except:
                pass
        
        return attrs

