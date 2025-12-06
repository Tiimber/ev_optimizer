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
    _attr_unique_id = "ev_smart_charger_status"
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
        }

class EVMaxAvailableCurrentSensor(EVSmartChargerBaseSensor):
    """Sensor showing max safe current."""
    _attr_name = "Max Safe Current"
    _attr_unique_id = "ev_smart_charger_safe_current"
    _attr_icon = "mdi:current-ac"
    _attr_native_unit_of_measurement = "A"

    @property
    def state(self):
        raw_current = self.coordinator.data["max_available_current"]
        return math.floor(raw_current)

class EVPriceStatusSensor(EVSmartChargerBaseSensor):
    """Sensor showing price logic status."""
    _attr_name = "Price Logic"
    _attr_unique_id = "ev_smart_charger_price_logic"
    _attr_icon = "mdi:cash-clock"

    @property
    def state(self):
        return self.coordinator.data["current_price_status"]

class EVChargingPlanSensor(EVSmartChargerBaseSensor):
    """Sensor containing the calculated schedule."""
    _attr_name = "Charging Schedule"
    _attr_unique_id = "ev_smart_charger_plan"
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
        # Added 'charging_summary' here so you can access it in the UI
        return {
            "planned_target": self.coordinator.data.get("planned_target_soc"),
            "charging_summary": self.coordinator.data.get("charging_summary"),
            "schedule": self.coordinator.data.get("charging_schedule", [])
        }