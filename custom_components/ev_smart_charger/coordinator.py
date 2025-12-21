"""DataUpdateCoordinator for EV Smart Charger."""

from __future__ import annotations

import logging
import math
from datetime import timedelta, datetime, time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.storage import Store
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    SERVICE_TURN_ON,
    SERVICE_TURN_OFF,
)

from .const import (
    DOMAIN,
    CONF_CAR_SOC_SENSOR,
    CONF_CAR_PLUGGED_SENSOR,
    CONF_CAR_CAPACITY,
    CONF_CAR_CHARGING_LEVEL_ENTITY,
    CONF_CAR_LIMIT_SERVICE,
    CONF_CAR_LIMIT_ENTITY_ID,
    CONF_CAR_REFRESH_ACTION,
    CONF_CAR_REFRESH_ENTITY,
    CONF_CAR_REFRESH_INTERVAL,
    CONF_PRICE_SENSOR,
    CONF_P1_L1,
    CONF_P1_L2,
    CONF_P1_L3,
    CONF_MAX_FUSE,
    CONF_CHARGER_LOSS,
    CONF_CURRENCY,
    CONF_CALENDAR_ENTITY,
    CONF_ZAPTEC_LIMITER,
    CONF_ZAPTEC_RESUME,
    CONF_ZAPTEC_STOP,
    CONF_ZAPTEC_SWITCH,
    CONF_CHARGER_CURRENT_L1,
    CONF_CHARGER_CURRENT_L2,
    CONF_CHARGER_CURRENT_L3,
    DEFAULT_CURRENCY,
    REFRESH_NEVER,
    REFRESH_30_MIN,
    REFRESH_1_HOUR,
    REFRESH_2_HOURS,
    REFRESH_3_HOURS,
    REFRESH_4_HOURS,
    REFRESH_AT_TARGET,
    ENTITY_TARGET_SOC,
    ENTITY_DEPARTURE_TIME,
    ENTITY_DEPARTURE_OVERRIDE,
    ENTITY_SMART_SWITCH,
    ENTITY_TARGET_OVERRIDE,
    ENTITY_PRICE_EXTRA_FEE,
    ENTITY_PRICE_VAT,
)

# Imports from helper modules
from .image_generator import generate_report_image, generate_plan_image
from .planner import generate_charging_plan, calculate_load_balancing, analyze_prices

_LOGGER = logging.getLogger(__name__)


class EVSmartChargerCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API and calculating charging logic."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.entry = entry
        self.hass = hass

        # Track startup time for grace period
        self._startup_time = datetime.now()

        # Internal state
        self.previous_plugged_state = False
        self.user_settings = {}  # Storage for UI inputs
        self.action_log = []  # Rolling log of actions

        # Session Tracking
        self.current_session = None  # Active recording
        self.last_session_data = None  # Finished report
        # Flag to capture short charging bursts between ticks
        self._was_charging_in_interval = False

        # Scheduling state
        self._last_scheduled_end = None  # Track end of planned charging for buffer

        # Refresh Logic
        self._last_car_refresh_time = None
        self._refresh_trigger_timestamp = None
        self._soc_before_refresh = None

        # State tracking to prevent API spamming
        self._last_applied_amps = -1
        self._last_applied_state = None  # "charging" or "paused"
        self._last_applied_car_limit = -1

        # Virtual SoC Estimator
        self._virtual_soc = 0.0
        self._last_update_time = datetime.now()

        # New Flag: Tracks if user explicitly moved the Next Session slider
        self.manual_override_active = False

        # Persistence
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._data_loaded = False

        # Helper to get config from Options (new) or Data (initial)
        def get_conf(key, default=None):
            return entry.options.get(key, entry.data.get(key, default))

        # Config Variables passed to planner
        self.config_settings = {
            "max_fuse": float(get_conf(CONF_MAX_FUSE)),
            "charger_loss": float(get_conf(CONF_CHARGER_LOSS)),
            "car_capacity": float(get_conf(CONF_CAR_CAPACITY)),
            "currency": get_conf(CONF_CURRENCY, DEFAULT_CURRENCY),
            "has_price_sensor": bool(get_conf(CONF_PRICE_SENSOR)),
        }

        self.car_capacity = self.config_settings["car_capacity"]
        self.currency = self.config_settings["currency"]

        # Key Mappings
        self.conf_keys = {
            "p1_l1": get_conf(CONF_P1_L1),
            "p1_l2": get_conf(CONF_P1_L2),
            "p1_l3": get_conf(CONF_P1_L3),
            "car_soc": get_conf(CONF_CAR_SOC_SENSOR),
            "car_plugged": get_conf(CONF_CAR_PLUGGED_SENSOR),
            "car_limit": get_conf(CONF_CAR_CHARGING_LEVEL_ENTITY),
            "car_svc": get_conf(CONF_CAR_LIMIT_SERVICE),
            "car_target_ent": get_conf(
                CONF_CAR_ENTITY_ID
            ),  # Shared Entity for B and Refresh
            "price": get_conf(CONF_PRICE_SENSOR),
            "calendar": get_conf(CONF_CALENDAR_ENTITY),
            "zap_limit": get_conf(CONF_ZAPTEC_LIMITER),
            "zap_switch": get_conf(CONF_ZAPTEC_SWITCH),
            "zap_resume": get_conf(CONF_ZAPTEC_RESUME),
            "zap_stop": get_conf(CONF_ZAPTEC_STOP),
            "ch_l1": get_conf(CONF_CHARGER_CURRENT_L1),
            "ch_l2": get_conf(CONF_CHARGER_CURRENT_L2),
            "ch_l3": get_conf(CONF_CHARGER_CURRENT_L3),
            "refresh_svc": get_conf(CONF_CAR_REFRESH_ACTION),
            "refresh_ent": get_conf(CONF_CAR_REFRESH_ENTITY),
            "refresh_int": get_conf(CONF_CAR_REFRESH_INTERVAL),
        }

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )

    def _add_log(self, message: str):
        """Add an entry to the action log and prune entries older than 24h."""
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {message}"
        self.action_log.insert(0, entry)  # Prepend newest

        # Keep only last 24h events
        cutoff = now - timedelta(hours=24)
        while self.action_log:
            try:
                if (
                    datetime.strptime(self.action_log[-1][1:20], "%Y-%m-%d %H:%M:%S")
                    < cutoff
                ):
                    self.action_log.pop()
                else:
                    break
            except:
                self.action_log.pop()

        # Add to current session log if active
        if self.current_session is not None:
            self.current_session["log"].append(entry)

        # Fire event for Logbook
        self.hass.bus.async_fire(
            f"{DOMAIN}_log_event", {"message": message, "name": "EV Smart Charger"}
        )

    async def _load_data(self):
        """Load persisted settings from disk."""
        if self._data_loaded:
            return

        try:
            data = await self.store.async_load()
            if data:
                # Restore Override Flag
                self.manual_override_active = data.get("manual_override_active", False)

                # Restore Action Log
                self.action_log = data.get("action_log", [])

                # Restore Last Session Report
                self.last_session_data = data.get("last_session_data")

                # Restore Settings
                settings = data.get("user_settings", {})

                # Convert Time strings back to objects (JSON doesn't support time objects)
                for key in [ENTITY_DEPARTURE_TIME, ENTITY_DEPARTURE_OVERRIDE]:
                    if key in settings and settings[key]:
                        try:
                            parts = settings[key].split(":")
                            settings[key] = time(int(parts[0]), int(parts[1]))
                        except Exception:
                            pass

                self.user_settings.update(settings)
                self._add_log("System started. Settings and Log loaded.")
        except Exception as e:
            _LOGGER.error(f"Failed to load EV settings: {e}")

        self._data_loaded = True

    def _save_data(self):
        """Schedule save of settings to disk."""

        def data_to_save():
            # Create a serializable copy of settings
            clean_settings = self.user_settings.copy()
            for key, val in clean_settings.items():
                if isinstance(val, time):
                    clean_settings[key] = val.strftime("%H:%M")

            return {
                "manual_override_active": self.manual_override_active,
                "user_settings": clean_settings,
                "action_log": self.action_log,
                "last_session_data": self.last_session_data,
            }

        self.store.async_delay_save(data_to_save, 1.0)

    def set_user_input(self, key: str, value, internal: bool = False):
        """Update a user setting from the UI (Slider/Switch/Time)."""
        _LOGGER.debug(f"Setting user input: {key} = {value}")
        self.user_settings[key] = value

        if not internal:
            self._add_log(f"User setting changed: {key} -> {value}")

        # If user touches the Next Session slider, enable Strict Manual Mode
        if key == ENTITY_TARGET_OVERRIDE and not internal:
            self.manual_override_active = True
            self._add_log("Manual Override Mode Activated.")

        self._save_data()

        if self.data:
            self.hass.async_create_task(self.async_refresh())

    def clear_manual_override(self):
        """Called by the Clear Override button."""
        _LOGGER.info("Manual override cleared by user. Reverting to Smart Logic.")
        self._add_log("Manual override cleared. Reverting to Smart Logic.")
        self.manual_override_active = False

        # Reset the Next Session slider to match Standard Target visually
        std_target = self.user_settings.get(ENTITY_TARGET_SOC, 80)
        self.user_settings[ENTITY_TARGET_OVERRIDE] = std_target

        self._save_data()

        if self.data:
            self.hass.async_create_task(self.async_refresh())

    async def async_trigger_report_generation(self):
        """Manually trigger image generation for the current or last session."""
        report = None
        # Priority: Current session (snapshot) > Last session (finalized)
        if self.current_session:
            _LOGGER.info("Generating report for ACTIVE session.")
            report = self._calculate_session_totals()  # Uses current live history
            # We assume current session logs + history are up to date
            report["end_time"] = (
                datetime.now().isoformat()
            )  # Mark current time as end for snapshot
        elif self.last_session_data:
            _LOGGER.info("Regenerating report for LAST FINISHED session.")
            report = self.last_session_data

        if report:
            save_path = self.hass.config.path(
                "www", "ev_smart_charger_last_session.png"
            )
            await self.hass.async_add_executor_job(
                generate_report_image, report, save_path
            )
            self._add_log("Report Image Generated")
        else:
            _LOGGER.warning("No session data available to generate report.")

    async def async_trigger_plan_image_generation(self):
        """Manually trigger image generation for the current charging plan."""
        if not self.data or "charging_schedule" not in self.data:
            _LOGGER.warning("No charging plan data available to generate image.")
            return

        save_path = self.hass.config.path("www", "ev_smart_charger_plan.png")
        await self.hass.async_add_executor_job(
            generate_plan_image, self.data, save_path
        )
        self._add_log("Plan Image Generated")

    async def _async_update_data(self):
        """Update data via library."""
        # Ensure persisted settings are loaded on first run
        if not self._data_loaded:
            await self._load_data()

        try:
            # 1. Fetch Sensor Data
            data = self._fetch_sensor_data()

            # 2. Merge User Settings (Persistence)
            data.update(self.user_settings)

            # 3. Fetch Calendar Events (Async Service Call)
            cal_entity = self.conf_keys.get("calendar")
            data["calendar_events"] = []

            if cal_entity:
                try:
                    now = datetime.now()
                    # Ask for next 48 hours to be safe, filter later
                    start_date = now
                    end_date = now + timedelta(hours=48)

                    response = await self.hass.services.async_call(
                        "calendar",
                        "get_events",
                        {
                            "entity_id": cal_entity,
                            "start_date_time": start_date.isoformat(),
                            "end_date_time": end_date.isoformat(),
                        },
                        blocking=True,
                        return_response=True,
                    )

                    if response and cal_entity in response:
                        data["calendar_events"] = response[cal_entity].get("events", [])

                except Exception as e:
                    _LOGGER.warning(f"Failed to fetch calendar events: {e}")

            # 4. Handle Plugged-In Event (MOVED TO TOP)
            await self._handle_plugged_event(data["car_plugged"], data)

            # 5. Update Virtual SoC (Handle stale sensors)
            self._update_virtual_soc(data)
            data["car_soc"] = self._virtual_soc

            # 6. Logic: Load Balancing (Delegated)
            data["max_available_current"] = calculate_load_balancing(
                data, self.config_settings["max_fuse"]
            )

            # 7. Logic: Price Analysis (Delegated)
            data["current_price_status"] = analyze_prices(data["price_data"])

            # 8. Logic: Smart Charging Plan (Delegated)
            plan = generate_charging_plan(
                data, self.config_settings, self.manual_override_active
            )

            # Handle Buffer Logic (stateful, so stays in coordinator)
            if not plan["should_charge_now"] and plan.get("session_end_time"):
                self._last_scheduled_end = datetime.fromisoformat(
                    plan["session_end_time"]
                )

            if not plan["should_charge_now"] and self._last_scheduled_end:
                if (
                    self._last_scheduled_end
                    <= datetime.now()
                    < self._last_scheduled_end + timedelta(minutes=15)
                ):
                    plan["should_charge_now"] = True
                    plan["charging_summary"] = (
                        "Charging Buffer Active (15 min overrun)."
                    )

            data.update(plan)

            # 9. Manage Car Refresh
            await self._manage_car_refresh(data, plan)

            # 10. ACTUATION: Apply logic to physical charger AND car
            await self._apply_charger_control(data, plan)

            # 11. SESSION RECORDING: Record current status
            self._record_session_data(data)

            # Attach log to data so Sensor can read it
            data["action_log"] = self.action_log

            return data

        except Exception as err:
            _LOGGER.error(f"Error in EV Coordinator: {err}")
            raise UpdateFailed(f"Error communicating with API: {err}")

    async def _manage_car_refresh(self, data: dict, plan: dict):
        """Handle force refreshing car sensors."""
        if not data.get("car_plugged"):
            return  # Only refresh when plugged in

        svc = self.conf_keys.get("refresh_svc")
        ent = self.conf_keys.get("car_target_ent")
        interval_mode = self.conf_keys.get("refresh_int", REFRESH_NEVER)

        if not svc or not ent or interval_mode == REFRESH_NEVER:
            return

        now = datetime.now()

        # Determine duration since last refresh
        if self._last_car_refresh_time:
            delta = now - self._last_car_refresh_time
        else:
            delta = timedelta(days=365)  # Needs refresh

        should_refresh = False

        # Check Intervals
        if interval_mode == REFRESH_30_MIN and delta > timedelta(minutes=30):
            should_refresh = True
        elif interval_mode == REFRESH_1_HOUR and delta > timedelta(hours=1):
            should_refresh = True
        elif interval_mode == REFRESH_2_HOURS and delta > timedelta(hours=2):
            should_refresh = True
        elif interval_mode == REFRESH_3_HOURS and delta > timedelta(hours=3):
            should_refresh = True
        elif interval_mode == REFRESH_4_HOURS and delta > timedelta(hours=4):
            should_refresh = True

        # Check Target Logic
        if interval_mode == REFRESH_AT_TARGET:
            # Refresh if we think we hit target, to confirm.
            # Limit to once every 12 hours.
            if delta > timedelta(hours=12):
                current_soc = self._virtual_soc
                target_soc = float(plan.get("planned_target_soc", 80))
                # If we are close or above target
                if current_soc >= target_soc:
                    should_refresh = True

        if should_refresh:
            await self._trigger_car_refresh(svc, ent)

    async def _trigger_car_refresh(self, service: str, entity_id: str):
        """Call the refresh service."""
        try:
            # Note the current (potentially stale) value before refreshing
            current_soc_state = self.hass.states.get(self.conf_keys["car_soc"])
            current_val = (
                float(current_soc_state.state)
                if current_soc_state
                and current_soc_state.state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]
                else 0.0
            )

            self._soc_before_refresh = current_val

            self._add_log(
                f"Forcing Car Sensor Refresh via {service} (Current: {current_val}%)"
            )
            domain, name = service.split(".", 1)

            payload = {}
            if "." in entity_id:
                payload["entity_id"] = entity_id
            else:
                payload["device_id"] = entity_id

            await self.hass.services.async_call(domain, name, payload, blocking=True)

            self._last_car_refresh_time = datetime.now()
            self._refresh_trigger_timestamp = datetime.now()  # Mark for trust logic

        except Exception as e:
            _LOGGER.error(f"Failed to force refresh car: {e}")

    def _update_virtual_soc(self, data: dict):
        """Update the internal estimated SoC based on charging activity."""
        current_time = datetime.now()
        sensor_soc = data.get("car_soc")

        # 1. Sync Logic
        # Sync if sensor is valid AND:
        #  - Higher than estimate (drift correction upwards)
        #  - OR we are uninitialized (0.0)
        #  - OR we triggered a refresh recently (trust sensor for 5 mins, even if lower, BUT only if it changed)
        trust_sensor_period = False
        if self._refresh_trigger_timestamp:
            if (current_time - self._refresh_trigger_timestamp) < timedelta(minutes=5):
                # Trust the sensor if it has updated to a NEW value
                if (
                    sensor_soc is not None
                    and float(sensor_soc) != self._soc_before_refresh
                ):
                    trust_sensor_period = True

        if sensor_soc is not None:
            if (
                sensor_soc > self._virtual_soc
                or self._virtual_soc == 0.0
                or trust_sensor_period
            ):
                self._virtual_soc = float(sensor_soc)

        # 2. Estimate Logic
        # Only estimate if we are ACTIVELY charging
        if self._last_applied_state == "charging":
            # Use Real Charger Current if available (More accurate than Target Amps)
            ch_l1 = data.get("ch_l1", 0.0)
            ch_l2 = data.get("ch_l2", 0.0)
            ch_l3 = data.get("ch_l3", 0.0)
            measured_amps = max(ch_l1, ch_l2, ch_l3)

            # Fallback to Target Amps if no sensor or sensor reads 0 while active
            used_amps = (
                measured_amps if measured_amps > 0.5 else self._last_applied_amps
            )

            if used_amps > 0:
                # Calculate time delta in hours
                seconds_passed = (current_time - self._last_update_time).total_seconds()
                hours_passed = seconds_passed / 3600.0

                # Estimate Power (3-phase 230V standard)
                # P (kW) = 3 * 230V * Amps / 1000
                estimated_power_kw = (3 * 230 * used_amps) / 1000.0

                # Efficiency Factor
                efficiency_pct = self.entry.data.get(CONF_CHARGER_LOSS, 10.0)
                efficiency_factor = 1.0 - (efficiency_pct / 100.0)

                # Energy to Battery
                added_kwh = estimated_power_kw * hours_passed * efficiency_factor

                # Convert to % SoC
                if self.car_capacity > 0:
                    added_percent = (added_kwh / self.car_capacity) * 100.0
                    self._virtual_soc += added_percent

                    # Cap at Physical Car Limit (if we know it)
                    if self._last_applied_car_limit > 0:
                        if self._virtual_soc > self._last_applied_car_limit:
                            self._virtual_soc = float(self._last_applied_car_limit)

                    # Absolute Cap at 100
                    if self._virtual_soc > 100.0:
                        self._virtual_soc = 100.0

        self._last_update_time = current_time

    async def _apply_charger_control(self, data: dict, plan: dict):
        """Send commands to the Zaptec entities and Car."""

        # 0. Startup Grace Period Check
        if datetime.now() - self._startup_time < timedelta(minutes=2):
            return

        # 1. Determine Desired State
        should_charge = data.get("should_charge_now", False)
        safe_amps = math.floor(data.get("max_available_current", 0))

        if safe_amps < 6:
            if should_charge:
                self._add_log(
                    f"Safety Cutoff: Available {safe_amps}A is below minimum 6A. Pausing."
                )
            should_charge = False

        # Determine Target Amps based on State
        # If paused, we force 0A. If charging, we use calculated safe amps.
        target_amps = safe_amps if should_charge else 0

        desired_state = "charging" if should_charge else "paused"

        # --- MAINTENANCE MODE / PAUSED OVERRIDE ---
        maintenance_active = "Maintenance mode active" in plan.get(
            "charging_summary", ""
        )

        if maintenance_active:
            # FORCE: Switch ON, Amps 0
            should_charge = True
            target_amps = 0
            desired_state = "maintenance"

        # 2. Control Car Charge Limit
        target_soc = int(plan.get("planned_target_soc", 80))
        is_starting = (
            desired_state == "charging" and self._last_applied_state != "charging"
        )

        if target_soc != self._last_applied_car_limit or is_starting:
            if self.conf_keys["car_limit"]:
                try:
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": self.conf_keys["car_limit"], "value": target_soc},
                        blocking=True,
                    )
                    self._last_applied_car_limit = target_soc
                    self._add_log(f"Set Car Charge Limit to {target_soc}%")
                except Exception as e:
                    _LOGGER.error(f"Failed to set Car Charge Limit: {e}")
            elif self.conf_keys.get("car_svc") and self.conf_keys.get("car_target_ent"):
                try:
                    full_service = self.conf_keys["car_svc"]
                    if "." in full_service:
                        domain, service_name = full_service.split(".", 1)
                        payload = {"ac_limit": target_soc, "dc_limit": target_soc}
                        target_id = self.conf_keys["car_target_ent"]
                        if "." in target_id:
                            payload["entity_id"] = target_id
                        else:
                            payload["device_id"] = target_id
                        await self.hass.services.async_call(
                            domain, service_name, payload, blocking=True
                        )
                        self._last_applied_car_limit = target_soc
                        self._add_log(f"Service Call: Set Car Limit to {target_soc}%")
                except Exception as e:
                    _LOGGER.error(f"Failed to call Car Limit Service: {e}")

        # 3. Control Start/Stop (Switch Logic)

        # Order of Operations:
        # - If Charging: Turn Switch ON, then Set Amps.
        # - If Pausing: Set Amps 0, then Turn Switch OFF.

        if should_charge:
            # ---> CHARGING SEQUENCE <---

            # A. Ensure Switch is ON
            if desired_state != self._last_applied_state:
                try:
                    if self.conf_keys.get("zap_switch"):
                        await self.hass.services.async_call(
                            "switch",
                            SERVICE_TURN_ON,
                            {"entity_id": self.conf_keys["zap_switch"]},
                            blocking=True,
                        )
                        state_msg = (
                            "CHARGING" if target_amps > 0 else "MAINTENANCE (0A)"
                        )
                        self._add_log(f"Switched Charging state to: {state_msg}")
                    elif self.conf_keys.get("zap_resume"):
                        # Fallback button
                        await self.hass.services.async_call(
                            "button",
                            "press",
                            {"entity_id": self.conf_keys["zap_resume"]},
                            blocking=True,
                        )
                        self._add_log("Sent Resume command")

                    self._last_applied_state = desired_state
                except Exception as e:
                    _LOGGER.error(f"Failed to switch Zaptec state to CHARGING: {e}")

            # B. Control Current Limiter
            if target_amps != self._last_applied_amps and self.conf_keys["zap_limit"]:
                try:
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": self.conf_keys["zap_limit"],
                            "value": target_amps,
                        },
                        blocking=True,
                    )
                    self._last_applied_amps = target_amps
                    # Only log amp changes if actually charging (>0)
                    if target_amps > 0:
                        self._add_log(
                            f"Load Balancing: Set Zaptec limit to {target_amps}A"
                        )
                except Exception as e:
                    _LOGGER.error(f"Failed to set Zaptec limit: {e}")

        else:
            # ---> PAUSING SEQUENCE <---

            # A. Set Amps to 0 first (Soft Stop)
            if self._last_applied_amps != 0 and self.conf_keys["zap_limit"]:
                try:
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": self.conf_keys["zap_limit"], "value": 0},
                        blocking=True,
                    )
                    self._last_applied_amps = 0
                    self._add_log(f"Pausing: Set Zaptec limit to 0A")
                except Exception as e:
                    _LOGGER.error(f"Failed to set Zaptec limit to 0: {e}")

            # B. Turn Switch OFF
            if desired_state != self._last_applied_state:
                try:
                    if self.conf_keys.get("zap_switch"):
                        await self.hass.services.async_call(
                            "switch",
                            SERVICE_TURN_OFF,
                            {"entity_id": self.conf_keys["zap_switch"]},
                            blocking=True,
                        )
                        self._add_log(f"Switched Charging state to: PAUSED")
                    elif self.conf_keys.get("zap_stop"):
                        # Fallback button
                        await self.hass.services.async_call(
                            "button",
                            "press",
                            {"entity_id": self.conf_keys["zap_stop"]},
                            blocking=True,
                        )
                        self._add_log("Sent Stop command")

                    self._last_applied_state = desired_state
                except Exception as e:
                    _LOGGER.error(f"Failed to switch Zaptec state to PAUSED: {e}")

    def _fetch_sensor_data(self) -> dict:
        """Read all configured sensors from Home Assistant state machine."""
        data = {}

        def get_float(entity_id):
            if not entity_id:
                return 0.0
            state = self.hass.states.get(entity_id)
            if state is None or state.state in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
                return 0.0
            try:
                return float(state.state)
            except ValueError:
                return 0.0

        def get_state(entity_id):
            if not entity_id:
                return None
            state = self.hass.states.get(entity_id)
            return state

        data["p1_l1"] = get_float(self.conf_keys["p1_l1"])
        data["p1_l2"] = get_float(self.conf_keys["p1_l2"])
        data["p1_l3"] = get_float(self.conf_keys["p1_l3"])
        data["car_soc"] = get_float(self.conf_keys["car_soc"])

        # Fetch Charger Current if configured
        data["ch_l1"] = get_float(self.conf_keys.get("ch_l1"))
        data["ch_l2"] = get_float(self.conf_keys.get("ch_l2"))
        data["ch_l3"] = get_float(self.conf_keys.get("ch_l3"))

        plugged_state = get_state(self.conf_keys["car_plugged"])
        # Handle state object being None safely
        if plugged_state:
            data["car_plugged"] = (
                plugged_state.state
                in ["on", "true", "connected", "charging", "full", "plugged_in"]
                if plugged_state
                else False
            )
        else:
            data["car_plugged"] = False

        # Handle Optional Price Sensor
        price_entity = self.conf_keys.get("price")
        if price_entity:
            price_state = self.hass.states.get(price_entity)
            data["price_data"] = price_state.attributes if price_state else {}
        else:
            data["price_data"] = {}

        return data

    async def _handle_plugged_event(self, is_plugged: bool, data: dict):
        """Check for plug events."""
        # Case A: Just Plugged In -> Force SoC Update
        if is_plugged and not self.previous_plugged_state:
            self._add_log("Car plugged in.")

            # Start new Session
            self.current_session = {
                "start_time": datetime.now().isoformat(),
                "history": [],
                "log": [],
            }

            # Sync Virtual SoC to Sensor immediately on plug-in
            if data.get("car_soc") is not None:
                self._virtual_soc = data["car_soc"]
            else:
                self._virtual_soc = 0.0  # Start fresh if unknown

            soc_entity = self.conf_keys["car_soc"]
            try:
                await self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": soc_entity},
                    blocking=False,
                )
            except Exception as e:
                _LOGGER.warning(f"Failed to force update car sensor: {e}")

        # Case B: Just Unplugged -> Reset Overrides to Standards
        if not is_plugged and self.previous_plugged_state:
            self._add_log("Car unplugged. Resetting settings.")

            # Finalize Session Report
            if self.current_session:
                self._finalize_session()
                self.current_session = None

            # Reset Override Flag
            self.manual_override_active = False

            # 1. Reset Time Override (Internal update)
            std_time = data.get(ENTITY_DEPARTURE_TIME, time(7, 0))
            self.set_user_input(ENTITY_DEPARTURE_OVERRIDE, std_time, internal=True)
            data[ENTITY_DEPARTURE_OVERRIDE] = std_time

            # 2. Reset Target SoC Override (Internal update)
            std_target = data.get(ENTITY_TARGET_SOC, 80)
            self.set_user_input(ENTITY_TARGET_OVERRIDE, std_target, internal=True)
            data[ENTITY_TARGET_OVERRIDE] = std_target

            # Save the cleared state
            self._save_data()

            # IMMEDIATE OFF: Force the switch off right now
            if self.conf_keys.get("zap_switch"):
                try:
                    await self.hass.services.async_call(
                        "switch",
                        SERVICE_TURN_OFF,
                        {"entity_id": self.conf_keys["zap_switch"]},
                        blocking=True,
                    )
                    self._add_log("Unplugged: Forced Zaptec Switch OFF (Paused).")
                except Exception as e:
                    _LOGGER.error(f"Failed to force Zaptec off: {e}")

            # Force State Reset so next plug-in starts fresh logic
            self._last_applied_state = "paused"
            self._last_applied_car_limit = -1

        self.previous_plugged_state = is_plugged

    def _record_session_data(self, data: dict):
        """Record data points for the current session report."""
        if not self.current_session:
            return

        now_ts = datetime.now()

        # Calculate current cost
        current_price = 0.0
        try:
            raw_prices = data["price_data"].get("today", [])
            if raw_prices:
                count = len(raw_prices)
                idx = (
                    (now_ts.hour * 4) + (now_ts.minute // 15)
                    if count > 25
                    else now_ts.hour
                )
                idx = min(idx, count - 1)
                current_price = float(raw_prices[idx])
        except:
            current_price = 0.0

        # Fees/VAT
        extra_fee = data.get(ENTITY_PRICE_EXTRA_FEE, 0.0)
        vat_pct = data.get(ENTITY_PRICE_VAT, 0.0)
        adjusted_price = (current_price + extra_fee) * (1 + vat_pct / 100.0)

        # Detect charging status (capture slivers)
        # Charging is considered TRUE if state is charging OR if we saw it active since last tick
        is_charging = (
            1
            if (
                self._last_applied_state == "charging" or self._was_charging_in_interval
            )
            else 0
        )

        point = {
            "time": now_ts.isoformat(),
            "soc": data.get("car_soc", 0),
            "amps": self._last_applied_amps,
            "charging": is_charging,
            "price": adjusted_price,
        }

        self.current_session["history"].append(point)
        # Reset the inter-tick memory
        self._was_charging_in_interval = False

    def _finalize_session(self):
        """Generate the final report for the ended session."""
        if not self.current_session:
            return

        report = self._calculate_session_totals()

        self.last_session_data = report
        self._save_data()

        # GENERATE IMAGE FOR THERMAL PRINTER
        try:
            save_path = self.hass.config.path(
                "www", "ev_smart_charger_last_session.png"
            )
            self.hass.async_add_executor_job(generate_report_image, report, save_path)
        except Exception as e:
            _LOGGER.warning(f"Could not trigger image generation: {e}")

    def _calculate_session_totals(self):
        """Calculate totals for the current session."""
        history = self.current_session["history"]
        if not history:
            return {}

        start_soc = history[0]["soc"]
        end_soc = history[-1]["soc"]

        total_kwh = 0.0
        total_cost = 0.0

        prev_time = datetime.fromisoformat(history[0]["time"])

        for i in range(1, len(history)):
            curr = history[i]
            curr_time = datetime.fromisoformat(curr["time"])
            delta_h = (curr_time - prev_time).total_seconds() / 3600.0
            prev_time = curr_time

            amps = history[i - 1]["amps"]
            is_charging = history[i - 1]["charging"]

            if is_charging and amps > 0:
                power = (3 * 230 * amps) / 1000.0
                kwh = power * delta_h
                cost = kwh * history[i - 1]["price"]

                total_kwh += kwh
                total_cost += cost

        return {
            "start_time": self.current_session["start_time"],
            "end_time": datetime.now().isoformat(),
            "start_soc": start_soc,
            "end_soc": end_soc,
            "added_kwh": round(total_kwh, 2),
            "total_cost": round(total_cost, 2),
            "currency": self.currency,
            "graph_data": history,
            "session_log": self.current_session["log"],
        }
