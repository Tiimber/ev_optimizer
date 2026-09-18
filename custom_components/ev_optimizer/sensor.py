"""Sensor platform for EV Optimizer."""
import math
from datetime import datetime
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

try:  # Only needed to make the departure timestamp timezone-aware.
    from homeassistant.util import dt as dt_util
except ImportError:  # pragma: no cover
    dt_util = None

from .const import (
    DOMAIN,
    LEARNING_CHARGER_LOSS,
    LEARNING_CONFIDENCE,
    LEARNING_SESSIONS,
    LEARNING_MEASUREMENTS,
    LEARNING_LOCKED,
    LEARNING_HISTORY,
    LEARNING_CHARGE_POWER,
    LEARNING_POWER_SAMPLES,
    MIN_POWER_SAMPLES,
)
from .coordinator import EVSmartChargerCoordinator

_LOGGER = logging.getLogger(__name__)


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
        LearnedEfficiencySensor(coordinator), # Learning efficiency
        EfficiencyConfidenceSensor(coordinator), # Learning confidence
        EVVirtualSoCSensor(coordinator), # The SoC every decision actually runs on
        EVChargePowerSensor(coordinator), # What plans are sized on
        EVSavingsSensor(coordinator), # What the scheduling bought
        EVPlannedDepartureSensor(coordinator), # Hook for preconditioning etc.
    ])

class EVSmartChargerBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for EV Optimizer sensors."""

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
        if data.get("idle_assist_active"):
            return "Assisting Car Request"
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
            "charger_status": data.get("charger_status"),
            "idle_assist_active": bool(data.get("idle_assist_active")),
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
        """Return additional info.

        Reported from what the coordinator recorded when it last wrote a dump.
        This used to stat the filesystem, which is blocking I/O on the event
        loop and ran every time the state was written — i.e. every update — to
        answer a question that only changes when the button is pressed.
        """
        coordinator = self.coordinator
        written = coordinator._last_debug_dump_time

        return {
            "file_path": coordinator._last_debug_dump_path
            or coordinator.hass.config.path("www", "ev_optimizer_debug_dump.json"),
            "written_this_session": written is not None,
            "download_url": "/local/ev_optimizer_debug_dump.json",
            "file_size_bytes": coordinator._last_debug_dump_size,
            "last_written": written.isoformat() if written else None,
        }


class LearnedEfficiencySensor(CoordinatorEntity, SensorEntity):
    """Shows current learned charger efficiency."""
    
    _attr_has_entity_name = False
    _attr_name = "Learned Charger Efficiency"
    _attr_unique_id = "ev_optimizer_learned_efficiency"
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:school"
    _attr_state_class = "measurement"
    
    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
    
    @property
    def native_value(self) -> float:
        return round(
            self.coordinator.learning_state.get(LEARNING_CHARGER_LOSS, 0.0), 1
        )
    
    @property
    def extra_state_attributes(self) -> dict:
        learning = self.coordinator.learning_state
        confidence = learning.get(LEARNING_CONFIDENCE, 0)
        locked = learning.get(LEARNING_LOCKED, False)
        sessions = learning.get(LEARNING_SESSIONS, 0)
        
        if locked:
            status_text = "Locked - Using learned value"
        elif sessions == 0:
            status_text = "Not started - Awaiting first session"
        elif sessions < 10:
            status_text = f"Learning - {sessions}/10 sessions complete"
        else:
            status_text = "Converged"
        
        return {
            "confidence": f"{confidence}/10",
            "confidence_level": confidence,
            "locked": locked,
            "sessions_completed": sessions,
            "measurements_taken": learning.get(LEARNING_MEASUREMENTS, 0),
            "status": status_text,
            "last_3_measurements": learning.get(LEARNING_HISTORY, [])[-3:],
        }


class EfficiencyConfidenceSensor(CoordinatorEntity, SensorEntity):
    """Shows confidence level in learned efficiency."""
    
    _attr_has_entity_name = False
    _attr_name = "Efficiency Learning Confidence"
    _attr_unique_id = "ev_optimizer_efficiency_confidence"
    _attr_icon = "mdi:target"
    
    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
    
    @property
    def native_value(self) -> str:
        confidence = self.coordinator.learning_state.get(LEARNING_CONFIDENCE, 0)
        return f"{confidence}/10"
    
    @property
    def extra_state_attributes(self) -> dict:
        learning = self.coordinator.learning_state
        locked = learning.get(LEARNING_LOCKED, False)
        sessions = learning.get(LEARNING_SESSIONS, 0)
        
        if locked:
            status_text = "Locked - Using learned value"
        elif sessions == 0:
            status_text = "Not started - Awaiting first session"
        elif sessions < 10:
            status_text = f"Learning - {sessions}/10 sessions complete"
        else:
            status_text = "Converged"
        
        return {
            "status": status_text,
            "locked": locked,
            "sessions": sessions,
            "efficiency_loss_pct": round(learning.get(LEARNING_CHARGER_LOSS, 0.0), 1),
        }


class EVVirtualSoCSensor(CoordinatorEntity, SensorEntity):
    """The estimated state of charge that every decision is actually made on.

    The car reports its SoC only when it next phones home, so the integration
    walks an estimate forward between those reports and plans against it. That
    estimate was previously visible only as an attribute of the status sensor,
    which made it awkward to graph or alert on.
    """

    _attr_has_entity_name = False
    _attr_name = "Virtual State of Charge"
    _attr_unique_id = "ev_optimizer_virtual_soc"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        value = self.coordinator._virtual_soc
        if not value:
            return None
        return round(value, 1)

    @property
    def extra_state_attributes(self) -> dict:
        coordinator = self.coordinator
        reported = coordinator._last_sensor_soc
        drift = None
        if reported is not None and coordinator._virtual_soc:
            drift = round(coordinator._virtual_soc - reported, 1)

        anchor_time = coordinator._anchor_time
        return {
            "reported_soc": reported,
            "drift_from_reported": drift,
            "car_limit_applied": (
                coordinator._last_applied_car_limit
                if coordinator._last_applied_car_limit > 0
                else None
            ),
            "charger_state": coordinator._last_applied_state,
            "energy_source": coordinator.energy_source,
            "measurement_window_open": coordinator._anchor_sensor_soc is not None,
            "measurement_window_since": (
                anchor_time.isoformat() if anchor_time else None
            ),
        }


class EVChargePowerSensor(CoordinatorEntity, SensorEntity):
    """The power figure charging plans are sized on.

    Sizing a session from the house fuse is optimistic twice over: the car's
    onboard charger is usually the lower limit, and load balancing throttles the
    charger whenever the house draws. This reports what the plan actually
    assumed, and where that number came from, so a session that comes up short
    can be explained rather than guessed at.
    """

    _attr_has_entity_name = False
    _attr_name = "Planned Charge Power"
    _attr_unique_id = "ev_optimizer_charge_power"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_icon = "mdi:speedometer"
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        from .planner import get_charging_power_estimate

        value, _ = get_charging_power_estimate(
            self.coordinator.config_settings, self.coordinator.learning_state
        )
        return round(value, 2)

    @property
    def extra_state_attributes(self) -> dict:
        from .planner import get_charging_power_estimate

        learning = self.coordinator.learning_state
        samples = learning.get(LEARNING_POWER_SAMPLES, 0)
        observed = learning.get(LEARNING_CHARGE_POWER)
        _, source = get_charging_power_estimate(
            self.coordinator.config_settings, learning
        )

        if not self.coordinator.conf_keys.get("charger_power") and not any(
            self.coordinator.conf_keys.get(k) for k in ("ch_l1", "ch_l2", "ch_l3")
        ):
            observation = "unavailable - no charger power or current sensors configured"
        elif samples < MIN_POWER_SAMPLES:
            observation = f"warming up - {samples}/{MIN_POWER_SAMPLES} samples"
        else:
            observation = "measured"

        return {
            "source": source,
            "observed_kw": round(observed, 2) if observed else None,
            "samples": samples,
            "observation_status": observation,
        }


class EVSavingsSensor(CoordinatorEntity, SensorEntity):
    """What scheduling saved against simply charging on plug-in.

    Reports the figure for the session as a whole, held steady while it runs.
    The planner re-costs only the energy still to be pulled, so its own number
    falls towards zero as the session delivers — an honest forecast of what is
    left, but read as "savings" it looks like the benefit is evaporating at
    exactly the moment it is being collected. The live remainder is still
    available as an attribute for anyone who wants it.

    This is savings from *scheduling* — cheap hours instead of whatever hour the
    cable went in. It is not the saving against running a petrol or diesel car,
    which is a different comparison entirely.
    """

    _attr_has_entity_name = False
    _attr_name = "Charging Savings"
    _attr_unique_id = "ev_optimizer_savings"
    _attr_icon = "mdi:piggy-bank"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    @property
    def native_unit_of_measurement(self):
        return self.coordinator.currency

    @property
    def native_value(self) -> float | None:
        savings = self.coordinator.session_savings
        if savings is None:
            # Nothing latched yet — before the first plan of a session, and on
            # a restart mid-session. The live figure is the best answer there.
            savings = (self.coordinator.data or {}).get("estimated_savings")
        return round(savings, 2) if savings is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        planned = self.coordinator.session_savings_planned
        baseline = self.coordinator.session_savings_baseline
        if planned is None and baseline is None:
            planned = data.get("planned_cost")
            baseline = data.get("baseline_cost")

        share = None
        if planned is not None and baseline:
            share = round((1 - planned / baseline) * 100.0, 1)

        return {
            "planned_cost": planned,
            "cost_if_charging_now": baseline,
            "saved_percent": share,
            # The remaining-energy view the planner produces each tick. Falls
            # towards zero as the session delivers; that is delivery, not loss.
            "remaining_savings": data.get("estimated_savings"),
            "remaining_planned_cost": data.get("planned_cost"),
            "planned_grid_kwh": data.get("planned_grid_kwh"),
            "basis": "scheduling vs charging on plug-in",
            "currency": self.coordinator.currency,
        }


class EVPlannedDepartureSensor(CoordinatorEntity, SensorEntity):
    """When the car is expected to be needed.

    Exposed as a timestamp so automations can key off it — preconditioning,
    climate, notifications — without having to re-derive the calendar and
    override logic that produced it.
    """

    _attr_has_entity_name = False
    _attr_name = "Planned Departure"
    _attr_unique_id = "ev_optimizer_planned_departure"
    _attr_icon = "mdi:clock-out"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        raw = (self.coordinator.data or {}).get("plan_departure_time")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        # HA requires an aware datetime for a timestamp sensor; the planner
        # works in naive local time.
        if parsed.tzinfo is None:
            tz = getattr(dt_util, "DEFAULT_TIME_ZONE", None) if dt_util else None
            if tz is None:
                return None
            parsed = parsed.replace(tzinfo=tz)
        return parsed

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}

        return {
            "source": data.get("departure_source", "unknown"),
            "target_soc": data.get("planned_target_soc"),
            "scheduled_start": data.get("scheduled_start"),
            "session_end_time": data.get("session_end_time"),
        }
