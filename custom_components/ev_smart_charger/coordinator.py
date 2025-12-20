"""DataUpdateCoordinator for EV Smart Charger."""

from __future__ import annotations

import logging
import math
import re
import os
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
    # Entity Keys
    ENTITY_TARGET_SOC,
    ENTITY_MIN_SOC,
    ENTITY_PRICE_LIMIT_1,
    ENTITY_TARGET_SOC_1,
    ENTITY_PRICE_LIMIT_2,
    ENTITY_TARGET_SOC_2,
    ENTITY_DEPARTURE_TIME,
    ENTITY_DEPARTURE_OVERRIDE,
    ENTITY_SMART_SWITCH,
    ENTITY_TARGET_OVERRIDE,
    ENTITY_BUTTON_CLEAR_OVERRIDE,
    ENTITY_PRICE_EXTRA_FEE,
    ENTITY_PRICE_VAT,
)

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

        # Configuration variables
        self.max_fuse = float(get_conf(CONF_MAX_FUSE))
        self.charger_loss = float(get_conf(CONF_CHARGER_LOSS))
        self.car_capacity = float(get_conf(CONF_CAR_CAPACITY))
        self.currency = get_conf(CONF_CURRENCY, DEFAULT_CURRENCY)

        # Store key mappings for retrieval in fetch loop
        self.conf_keys = {
            "p1_l1": get_conf(CONF_P1_L1),
            "p1_l2": get_conf(CONF_P1_L2),
            "p1_l3": get_conf(CONF_P1_L3),
            "car_soc": get_conf(CONF_CAR_SOC_SENSOR),
            "car_plugged": get_conf(CONF_CAR_PLUGGED_SENSOR),
            "car_limit": get_conf(CONF_CAR_CHARGING_LEVEL_ENTITY),  # Option A
            "car_svc": get_conf(CONF_CAR_LIMIT_SERVICE),  # Option B
            "car_svc_ent": get_conf(CONF_CAR_LIMIT_ENTITY_ID),  # Option B
            "price": get_conf(CONF_PRICE_SENSOR),
            "calendar": get_conf(CONF_CALENDAR_ENTITY),
            # Control Entities
            "zap_limit": get_conf(CONF_ZAPTEC_LIMITER),
            "zap_switch": get_conf(CONF_ZAPTEC_SWITCH),
            "zap_resume": get_conf(CONF_ZAPTEC_RESUME),
            "zap_stop": get_conf(CONF_ZAPTEC_STOP),
            # Charger Readings
            "ch_l1": get_conf(CONF_CHARGER_CURRENT_L1),
            "ch_l2": get_conf(CONF_CHARGER_CURRENT_L2),
            "ch_l3": get_conf(CONF_CHARGER_CURRENT_L3),
            # Refresh
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
                last_entry = self.action_log[-1]
                # Extract timestamp: "[YYYY-MM-DD HH:MM:SS] Message"
                # Timestamp is between index 1 and 20
                last_ts_str = last_entry[1:20]
                last_dt = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
                if last_dt < cutoff:
                    self.action_log.pop()
                else:
                    break
            except (ValueError, IndexError):
                # Remove malformed entries if any
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
                            _LOGGER.warning(f"Failed to parse saved time for {key}")

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
                self._generate_report_image, report, save_path
            )
            self._add_log("Manually triggered report image generation.")
        else:
            _LOGGER.warning("No session data available to generate report.")

    async def async_trigger_plan_image_generation(self):
        """Manually trigger image generation for the current charging plan."""
        if not self.data or "charging_schedule" not in self.data:
            _LOGGER.warning("No charging plan data available to generate image.")
            return

        save_path = self.hass.config.path("www", "ev_smart_charger_plan.png")
        await self.hass.async_add_executor_job(
            self._generate_plan_image, self.data, save_path
        )
        self._add_log("Manually triggered plan image generation.")

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

            # 6. Logic: Load Balancing
            data["max_available_current"] = self._calculate_load_balancing(data)

            # 7. Logic: Price Analysis (Simple status)
            data["current_price_status"] = self._analyze_prices(data["price_data"])

            # 8. Logic: Smart Charging Plan (The Brain)
            plan = self._generate_charging_plan(data)
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
        ent = self.conf_keys.get("refresh_ent")
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
            elif self.conf_keys.get("car_svc") and self.conf_keys.get("car_svc_ent"):
                try:
                    full_service = self.conf_keys["car_svc"]
                    if "." in full_service:
                        domain, service_name = full_service.split(".", 1)
                        payload = {"ac_limit": target_soc, "dc_limit": target_soc}
                        target_id = self.conf_keys["car_svc_ent"]
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
            data["car_plugged"] = plugged_state.state in [
                "on",
                "true",
                "connected",
                "charging",
                "full",
                "plugged_in",
            ]
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
            self._last_scheduled_end = None

        self.previous_plugged_state = is_plugged

    def _calculate_load_balancing(self, data: dict) -> float:
        """Calculate the safe current available for the charger."""
        # Get raw grid readings (includes house + charger)
        p1_l1 = data.get("p1_l1", 0.0)
        p1_l2 = data.get("p1_l2", 0.0)
        p1_l3 = data.get("p1_l3", 0.0)

        # Get charger readings (if sensors configured)
        ch_l1 = data.get("ch_l1", 0.0)
        ch_l2 = data.get("ch_l2", 0.0)
        ch_l3 = data.get("ch_l3", 0.0)

        # Calculate House Base Load (Total - Charger)
        # We ensure it doesn't go below 0 (sensor timing issues)
        house_l1 = max(0.0, p1_l1 - ch_l1)
        house_l2 = max(0.0, p1_l2 - ch_l2)
        house_l3 = max(0.0, p1_l3 - ch_l3)

        # Determine highest base load among phases
        max_house_current = max(house_l1, house_l2, house_l3)

        # Calculate Remaining Capacity for EV
        buffer = max(1.0, self.max_fuse * 0.05)
        available = self.max_fuse - max_house_current - buffer

        # Ensure non-negative
        return max(0.0, available)

    def _analyze_prices(self, attributes: dict) -> str:
        """Quick status for UI."""
        raw_prices = attributes.get("today", [])
        if not raw_prices:
            return "No Data"
        try:
            if isinstance(raw_prices, str):
                return "Error"

            # Determine interval based on count
            count = len(raw_prices)
            now_dt = datetime.now()

            # Support for 15-min intervals (96/day) vs Hourly (24/day)
            if count > 25:
                idx = (now_dt.hour * 4) + (now_dt.minute // 15)
            else:
                idx = now_dt.hour

            # Safety clamp index
            idx = min(idx, count - 1)

            current = raw_prices[idx]
            avg = sum(raw_prices) / count

            if current < avg * 0.8:
                return "Very Cheap"
            if current < avg:
                return "Cheap"
            return "Expensive"
        except Exception:
            return "Unknown"

    def _get_calendar_data(
        self, data: dict, now: datetime
    ) -> tuple[datetime | None, float | None]:
        """Check for relevant calendar event. Returns (departure_time, target_soc)."""
        events = data.get("calendar_events", [])
        if not events:
            return None, None
        limit = datetime.combine(now.date() + timedelta(days=1), time.max)
        sorted_events = sorted(events, key=lambda x: x.get("start"))
        for event in sorted_events:
            start_str = event.get("start")
            if isinstance(start_str, dict):
                start_str = start_str.get("dateTime", start_str.get("date"))
            if not start_str:
                continue
            try:
                evt_start = datetime.fromisoformat(start_str)
                if evt_start.tzinfo:
                    evt_start = evt_start.replace(tzinfo=None)
                if evt_start < now:
                    continue
                if evt_start > limit:
                    break
                text = f"{event.get('summary', '')} {event.get('description', '')}"
                match = re.search(r"(\d+)\s*%", text)
                target_soc = (
                    float(match.group(1))
                    if match and 10 <= int(match.group(1)) <= 100
                    else None
                )
                return evt_start, target_soc
            except ValueError:
                continue
        return None, None

    def _get_departure_time(self, data: dict, now: datetime) -> datetime:
        """Determine the target departure datetime."""
        # Check Calendar First (Only if we didn't just manually override everything)
        # But wait, logic says Calendar should set time AND maybe target.
        # This method ONLY returns time.

        cal_time, _ = self._get_calendar_data(data, now)
        if cal_time:
            # Sync the UI override time to match calendar so user sees it
            # But only if we haven't manually touched it recently?
            # User requirement: "if the logic finds it... have target level... and time"
            return cal_time

        time_input = data.get(ENTITY_DEPARTURE_OVERRIDE)
        if not time_input:
            time_input = data.get(ENTITY_DEPARTURE_TIME, time(7, 0))

        dept_dt = datetime.combine(now.date(), time_input)
        if dept_dt < now:
            dept_dt = dept_dt + timedelta(days=1)

        return dept_dt

    def _generate_charging_plan(self, data: dict) -> dict:
        """Core Logic: Determine WHEN to charge and to WHAT level."""
        plan = {
            "should_charge_now": False,
            "scheduled_start": None,
            "planned_target_soc": data.get(ENTITY_TARGET_SOC, 80),
            "charging_schedule": [],
            "charging_summary": "Not calculated",
        }

        # Safety Check for Unplugged happens at end to allow visualization calculation if needed
        # But we force False if unplugged anyway.

        if not data.get(ENTITY_SMART_SWITCH, True):
            plan["should_charge_now"] = True
            plan["charging_summary"] = "Smart charging disabled. Charging immediately."
            if not data.get("car_plugged"):
                plan["should_charge_now"] = False
            return plan

        now = datetime.now()
        prices = []

        # --- Handle Missing Price Sensor ---
        # If no prices, we cannot optimize. Default to Charge Now (Load Balanced).
        raw_today = data["price_data"].get("today", [])

        if not raw_today:
            # Check if sensor was configured at all
            if not self.conf_keys.get("price"):
                plan["charging_summary"] = (
                    "Load Balancing Mode (No Price Sensor configured)."
                )
            else:
                plan["charging_summary"] = (
                    "Error: Price sensor configured but no data received."
                )

            # Default behavior: Charge Immediately
            plan["should_charge_now"] = True

            # If unplugged, force off
            if not data.get("car_plugged"):
                plan["should_charge_now"] = False
            return plan

        # --- Standard Price Parsing ---
        raw_tomorrow = data["price_data"].get("tomorrow", [])

        def parse_price_list(price_list, date_ref):
            parsed = []
            if not price_list:
                return []

            # Determine if 60 or 15 minute resolution based on list length
            # 24 = Hourly, 96 = 15-Min
            interval_min = 60 if len(price_list) <= 25 else 15

            for i, price in enumerate(price_list):
                start_dt = datetime.combine(date_ref, time(0, 0)) + timedelta(
                    minutes=i * interval_min
                )
                if start_dt + timedelta(minutes=interval_min) < now:
                    continue
                parsed.append(
                    {
                        "start": start_dt,
                        "end": start_dt + timedelta(minutes=interval_min),
                        "price": float(price),
                    }
                )
            return parsed

        if isinstance(raw_today, str):
            raw_today = [float(x) for x in raw_today.split(",")]
        if isinstance(raw_tomorrow, str):
            raw_tomorrow = [float(x) for x in raw_tomorrow.split(",")]

        prices.extend(parse_price_list(raw_today, now.date()))
        if data["price_data"].get("tomorrow_valid", False) or raw_tomorrow:
            prices.extend(
                parse_price_list(raw_tomorrow, now.date() + timedelta(days=1))
            )

        if not prices:
            # Fallback if parsing failed
            plan["should_charge_now"] = True
            plan["charging_summary"] = "No future price data found."
            if not data.get("car_plugged"):
                plan["should_charge_now"] = False
            return plan

        # 2. Define Time Window
        dept_dt = self._get_departure_time(data, now)
        calc_window = [p for p in prices if p["start"] < dept_dt]

        if not calc_window:
            plan["should_charge_now"] = True
            plan["charging_summary"] = "Departure passed. Charging."
            if not data.get("car_plugged"):
                plan["should_charge_now"] = False
            return plan

        # Determine departure source for summary
        cal_time, cal_soc = self._get_calendar_data(data, now)
        time_source = "(Calendar)" if cal_time and cal_time == dept_dt else "(Manual)"

        # 3. Determine Target SoC
        min_guaranteed = data.get(ENTITY_MIN_SOC, 20)
        status_note = ""

        if self.manual_override_active:
            final_target = data.get(ENTITY_TARGET_OVERRIDE, 80)
            status_note = "(Manual Override)"
        elif cal_soc is not None:
            final_target = cal_soc
            status_note = "(Calendar Event)"
        else:
            final_target = data.get(ENTITY_TARGET_SOC, 80)
            status_note = "(Smart)"

            min_price_in_window = min(slot["price"] for slot in calc_window)
            limit_1 = data.get(ENTITY_PRICE_LIMIT_1, 0.5)
            target_1 = data.get(ENTITY_TARGET_SOC_1, 100)
            limit_2 = data.get(ENTITY_PRICE_LIMIT_2, 1.5)
            target_2 = data.get(ENTITY_TARGET_SOC_2, 80)

            if min_price_in_window <= limit_1:
                final_target = max(final_target, target_1)
            elif min_price_in_window <= limit_2:
                final_target = max(final_target, target_2)

        final_target = max(final_target, min_guaranteed)
        plan["planned_target_soc"] = final_target

        # 4. Calculate Energy Needed
        # Use our Virtual SoC (synced/estimated) instead of potentially stale sensor
        # FIX: Check if car_soc is None (unavailable) first
        current_soc = data.get("car_soc")
        if current_soc is None or current_soc <= 0.0:
            plan["should_charge_now"] = False
            plan["charging_summary"] = (
                "Waiting for valid Car SoC (Current: 0% or Unknown)."
            )
            if not data.get("car_plugged"):
                plan["should_charge_now"] = False
            return plan

        # Ensure we work with float for calculations
        current_soc = float(current_soc)

        selected_slots = []
        selected_start_times = set()
        price_limit_high = data.get(ENTITY_PRICE_LIMIT_2, 1.5)

        if current_soc >= final_target:
            plan["charging_summary"] = (
                f"Target reached ({int(current_soc)}%). Maintenance mode active."
            )
            for slot in calc_window:
                if slot["price"] <= price_limit_high:
                    selected_start_times.add(slot["start"])
                    selected_slots.append(slot)
            for slot in calc_window:
                if (
                    slot["start"] in selected_start_times
                    and slot["start"] <= now < slot["end"]
                ):
                    plan["should_charge_now"] = True
                    break
        else:
            soc_needed = final_target - current_soc
            kwh_needed = (soc_needed / 100.0) * self.car_capacity
            efficiency = 1.0 - (self.charger_loss / 100.0)
            kwh_to_pull = kwh_needed / efficiency

            est_power_kw = min((3 * 230 * self.max_fuse) / 1000, 11.0)
            hours_needed = kwh_to_pull / est_power_kw

            # Dynamic slot duration calculation (supports 15-min or 60-min slots)
            slot_duration_hours = (
                calc_window[0]["end"] - calc_window[0]["start"]
            ).seconds / 3600.0
            # Guard against zero division if window data bad
            if slot_duration_hours <= 0:
                slot_duration_hours = 1.0

            slots_needed = math.ceil(hours_needed / slot_duration_hours)

            sorted_window = sorted(calc_window, key=lambda x: x["price"])
            selected_slots = sorted_window[:slots_needed]
            selected_start_times = {s["start"] for s in selected_slots}

            # --- BUFFER LOGIC (15 Min Overrun) ---
            if selected_slots:
                session_end_time = max(s["end"] for s in selected_slots)
                self._last_scheduled_end = session_end_time

            for slot in calc_window:
                if (
                    slot["start"] in selected_start_times
                    and slot["start"] <= now < slot["end"]
                ):
                    plan["should_charge_now"] = True
                    break

            if not plan["should_charge_now"] and self._last_scheduled_end:
                if (
                    self._last_scheduled_end
                    <= now
                    < self._last_scheduled_end + timedelta(minutes=15)
                ):
                    plan["should_charge_now"] = True
                    plan["charging_summary"] = (
                        "Charging Buffer Active (15 min overrun)."
                    )

            summary_lines = []
            total_plan_cost = 0.0

            # --- CALCULATE COSTS WITH FEES AND VAT ---
            extra_fee = data.get(ENTITY_PRICE_EXTRA_FEE, 0.0)
            vat_pct = data.get(ENTITY_PRICE_VAT, 0.0)

            # Include text note if adjustments exist
            cost_note = ""
            if extra_fee > 0 or vat_pct > 0:
                cost_note = "(incl fees/VAT)"

            if selected_slots:
                chrono_slots = sorted(selected_slots, key=lambda x: x["start"])
                kwh_grid_per_slot_max = est_power_kw * slot_duration_hours
                remaining_kwh_grid = kwh_to_pull

                running_soc = current_soc
                blocks = []
                current_block = None

                for slot in chrono_slots:
                    # Stop calculating if we've reached the energy target
                    if remaining_kwh_grid <= 0.001:  # Small epsilon
                        break

                    # Calculate Adjusted Price per kWh for this slot
                    raw_price = slot["price"]
                    adjusted_price = (raw_price + extra_fee) * (1 + vat_pct / 100.0)

                    # Determine energy used in this slot (capped by need)
                    kwh_this_slot = min(kwh_grid_per_slot_max, remaining_kwh_grid)
                    remaining_kwh_grid -= kwh_this_slot

                    slot_cost = adjusted_price * kwh_this_slot
                    total_plan_cost += slot_cost

                    # Calculate SoC gain for this specific energy amount
                    kwh_batt_this_slot = kwh_this_slot * efficiency
                    soc_gain_this_slot = (
                        kwh_batt_this_slot / self.car_capacity
                    ) * 100.0

                    if current_block and slot["start"] == current_block["end"]:
                        current_block["end"] = slot["end"]
                        current_block["cost"] += slot_cost
                        current_block["soc_gain"] += soc_gain_this_slot
                        current_block["avg_price_acc"] += adjusted_price
                        current_block["count"] += 1
                    else:
                        if current_block:
                            running_soc += current_block["soc_gain"]
                            blocks.append(current_block)
                        current_block = {
                            "start": slot["start"],
                            "end": slot["end"],
                            "cost": slot_cost,
                            "soc_start": running_soc,
                            "soc_gain": soc_gain_this_slot,
                            "avg_price_acc": adjusted_price,
                            "count": 1,
                        }
                if current_block:
                    blocks.append(current_block)

                summary_lines.append(
                    f"**Departure:** {dept_dt.strftime('%H:%M')} {time_source}"
                )
                summary_lines.append(f"**Target:** {int(final_target)}% {status_note}")
                summary_lines.append(
                    f"**Total Estimated Cost:** {total_plan_cost:.2f} {self.currency} {cost_note}"
                )
                summary_lines.append("")
                for b in blocks:
                    start_s = b["soc_start"]
                    end_s = min(100, start_s + b["soc_gain"])
                    # Clamp end_s to final_target if it slightly exceeds due to math
                    if end_s > final_target:
                        end_s = final_target

                    avg_p = b["avg_price_acc"] / b["count"]
                    line = (
                        f"**{b['start'].strftime('%H:%M')} - {b['end'].strftime('%H:%M')}**\n"
                        f"SoC: {int(start_s)}% → {int(end_s)}%\n"
                        f"Cost: {b['cost']:.2f} {self.currency} (Avg: {avg_p:.2f})"
                    )
                    summary_lines.append(line)
                plan["charging_summary"] = "\n\n".join(summary_lines)

        schedule_data = []
        for slot in prices:
            active = slot["start"] in selected_start_times
            schedule_data.append(
                {
                    "start": slot["start"].isoformat(),
                    "end": slot["end"].isoformat(),
                    "price": slot["price"],
                    "active": active,
                }
            )
        if schedule_data:
            last_slot = schedule_data[-1]
            schedule_data.append(
                {
                    "start": last_slot["end"],
                    "end": last_slot["end"],
                    "price": last_slot["price"],
                    "active": False,
                }
            )
        plan["charging_schedule"] = schedule_data

        future_starts = [
            s["start"]
            for s in calc_window
            if s["start"] > now and s["start"] in selected_start_times
        ]
        if future_starts:
            plan["scheduled_start"] = min(future_starts).isoformat()

        if not data.get("car_plugged"):
            plan["should_charge_now"] = False

        return plan

    def _generate_report_image(self, report: dict, file_path: str):
        """Generate a PNG image for thermal printers."""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            _LOGGER.warning("PIL (Pillow) not found. Cannot generate image.")
            return

        width = 576
        bg_color = "white"

        # Load fonts via helper
        font_header, font_text, font_small = self._load_fonts()

        # Calculate Text Summary Section Height
        history = report.get("graph_data", [])
        charging_blocks = []
        if history:
            current_block = None
            for i, point in enumerate(history):
                if point["charging"] == 1:
                    if current_block is None:
                        current_block = {
                            "start": point["time"],
                            "soc_start": point["soc"],
                            "soc_end": point["soc"],
                        }
                    current_block["soc_end"] = point["soc"]
                    current_block["end"] = point["time"]
                else:
                    if current_block:
                        charging_blocks.append(current_block)
                        current_block = None
            if current_block:
                charging_blocks.append(current_block)

        # FIX: Increased text section base height substantially for larger fonts
        text_section_height = 600 + (len(charging_blocks) * 35)
        height = text_section_height + 400

        img = Image.new("RGB", (width, height), bg_color)
        draw = ImageDraw.Draw(img)

        # --- DRAW TEXT HEADER ---
        y = 30
        draw.text(
            (width // 2, y),
            "EV Charging Report",
            font=font_header,
            fill="black",
            anchor="mt",
        )
        y += 70

        lines = [
            f"Start: {report['start_time'][:16].replace('T', ' ')}",
            f"End:   {report['end_time'][:16].replace('T', ' ')}",
            f"Power: {report['added_kwh']} kWh",
            f"Cost:  {report['total_cost']} {report['currency']}",
            f"SoC:   {int(report['start_soc'])}% -> {int(report['end_soc'])}%",
        ]

        for line in lines:
            draw.text((30, y), line, font=font_text, fill="black")
            y += 35

        y += 15
        draw.line([(10, y), (width - 10, y)], fill="black", width=3)
        y += 30

        # --- DRAW CHARGING LOG ---
        if charging_blocks:
            draw.text((30, y), "Charging Activity:", font=font_text, fill="black")
            y += 40
            for block in charging_blocks:
                start_dt = datetime.fromisoformat(block["start"])
                end_dt = datetime.fromisoformat(block["end"])
                start_str = start_dt.strftime("%H:%M")
                end_str = end_dt.strftime("%H:%M")
                line = f"- {start_str} to {end_str} ({int(block['soc_start'])}% -> {int(block['soc_end'])}%)"
                draw.text((40, y), line, font=font_small, fill="black")
                y += 30
        else:
            draw.text((30, y), "No charging recorded.", font=font_text, fill="black")
            y += 40

        y += 20  # Spacing before graph

        # --- DRAW GRAPH ---
        if history:
            graph_top = y
            graph_height = 250
            graph_bottom = graph_top + graph_height

            margin_left = 60
            margin_right = 60
            graph_draw_width = width - margin_left - margin_right

            prices = [p["price"] for p in history]
            min_p = min(prices) if prices else 0
            max_p = max(prices) if prices else 1
            axis_min_p = math.floor(min_p * 2) / 2
            axis_max_p = math.ceil(max_p * 2) / 2
            if axis_max_p == axis_min_p:
                axis_max_p += 0.5
            price_range = axis_max_p - axis_min_p

            count = len(history)
            bar_w_float = graph_draw_width / max(1, count)

            for i, point in enumerate(history):
                x0 = margin_left + (i * bar_w_float)
                x1 = margin_left + ((i + 1) * bar_w_float)

                # Price Bar (Gray)
                p_norm = (point["price"] - axis_min_p) / price_range
                p_h = p_norm * graph_height
                draw.rectangle(
                    [x0, graph_bottom - p_h, x1, graph_bottom],
                    fill="#e0e0e0",
                    outline=None,
                )

                # Active Charging Indicator
                if point["charging"] == 1:
                    draw.rectangle(
                        [x0, graph_bottom - 20, x1, graph_bottom],
                        fill="black",
                        outline=None,
                    )

            # Left Axis (Price)
            draw.line(
                [(margin_left, graph_top), (margin_left, graph_bottom)],
                fill="black",
                width=2,
            )
            curr_mark = axis_min_p
            while curr_mark <= axis_max_p + 0.01:
                norm = (curr_mark - axis_min_p) / price_range
                mark_y = graph_bottom - (norm * graph_height)
                draw.line(
                    [(margin_left - 5, mark_y), (margin_left, mark_y)],
                    fill="black",
                    width=1,
                )
                label = f"{curr_mark:.1f}"
                draw.text(
                    (margin_left - 45, mark_y - 7), label, font=font_small, fill="black"
                )
                curr_mark += 0.5

            # Right Axis (SoC)
            draw.line(
                [
                    (width - margin_right, graph_top),
                    (width - margin_right, graph_bottom),
                ],
                fill="black",
                width=2,
            )
            for soc_mark in [0, 20, 40, 60, 80, 100]:
                norm = soc_mark / 100.0
                mark_y = graph_bottom - (norm * graph_height)
                draw.line(
                    [
                        (width - margin_right, mark_y),
                        (width - margin_right + 5, mark_y),
                    ],
                    fill="black",
                    width=1,
                )
                label = f"{soc_mark}%"
                draw.text(
                    (width - margin_right + 8, mark_y - 7),
                    label,
                    font=font_small,
                    fill="black",
                )

            # SoC Line (Black)
            points = []
            for i, point in enumerate(history):
                x = margin_left + (i * bar_w_float) + (bar_w_float / 2)
                soc_norm = point["soc"] / 100.0
                y = graph_bottom - (soc_norm * graph_height)
                points.append((x, y))

            if len(points) > 1:
                draw.line(points, fill="black", width=2)

            # X-Axis Timestamps
            try:
                start_dt = datetime.fromisoformat(history[0]["time"])
                start_str = start_dt.strftime("%H:%M")
                draw.text(
                    (margin_left, graph_bottom + 15),
                    start_str,
                    font=font_small,
                    fill="black",
                )

                end_dt = datetime.fromisoformat(history[-1]["time"])
                end_str = end_dt.strftime("%H:%M")
                try:
                    w = draw.textlength(end_str, font=font_small)
                    draw.text(
                        (width - margin_right - w, graph_bottom + 15),
                        end_str,
                        font=font_small,
                        fill="black",
                    )
                except AttributeError:
                    draw.text(
                        (width - margin_right - 50, graph_bottom + 15),
                        end_str,
                        font=font_small,
                        fill="black",
                    )
            except Exception:
                pass

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        img.save(file_path)
        _LOGGER.info(f"Saved session image to {file_path}")

    def _generate_plan_image(self, data: dict, file_path: str):
        """Generate a PNG image for the future charging plan."""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            _LOGGER.warning("PIL (Pillow) not found. Cannot generate image.")
            return

        width = 576
        bg_color = "white"

        # Load Fonts
        font_header, font_text, font_small = self._load_fonts()

        schedule = data.get("charging_schedule", [])
        if not schedule:
            return

        valid_slots = [s for s in schedule if s["price"] is not None]
        if not valid_slots:
            return

        height = 650  # Adjusted height
        img = Image.new("RGB", (width, height), bg_color)
        draw = ImageDraw.Draw(img)

        # Header
        y = 30
        draw.text(
            (width // 2, y),
            "Charging Plan",
            font=font_header,
            fill="black",
            anchor="mt",
        )
        y += 80

        # Extract Summary info
        summary_text = data.get("charging_summary", "")
        cost_match = re.search(
            r"Total Estimated Cost:\*\* ([\d\.]+) (\w+)", summary_text
        )
        cost_str = (
            f"{cost_match.group(1)} {cost_match.group(2)}" if cost_match else "N/A"
        )

        start_time = valid_slots[0]["start"]
        end_time = valid_slots[-1]["end"]
        start_dt = datetime.fromisoformat(start_time)
        end_dt = datetime.fromisoformat(end_time)

        current_soc = data.get("car_soc", 0)
        target_soc = data.get("planned_target_soc", 0)

        # Logic to hide "range" if target already reached
        if int(current_soc) >= int(target_soc):
            soc_line = f"SoC:   {int(current_soc)}% (Target Reached)"
        else:
            soc_line = f"SoC:   {int(current_soc)}% -> {int(target_soc)}%"

        # Format: DD/MM HH:MM
        s_fmt = start_dt.strftime("%d/%m %H:%M")
        e_fmt = end_dt.strftime("%d/%m %H:%M")

        lines = [
            f"Plan:  {s_fmt} -> {e_fmt}",
            soc_line,
            f"Est Cost: {cost_str}",
            f"State: {data.get('current_price_status', 'Unknown')}",
        ]

        for line in lines:
            draw.text((30, y), line, font=font_text, fill="black")
            y += 35

        y += 20
        draw.line([(10, y), (width - 10, y)], fill="black", width=3)
        y += 30

        # Graph
        graph_top = y
        graph_height = 250
        graph_bottom = graph_top + graph_height
        margin_left = 60
        margin_right = 20
        graph_draw_width = width - margin_left - margin_right

        prices = [s["price"] for s in valid_slots]
        min_p = min(prices)
        max_p = max(prices)
        axis_min_p = math.floor(min_p * 2) / 2
        axis_max_p = math.ceil(max_p * 2) / 2
        if axis_max_p == axis_min_p:
            axis_max_p += 0.5
        price_range = axis_max_p - axis_min_p

        count = len(valid_slots)
        bar_w_float = graph_draw_width / max(1, count)

        for i, slot in enumerate(valid_slots):
            x0 = margin_left + (i * bar_w_float)
            x1 = margin_left + ((i + 1) * bar_w_float)

            p_norm = (slot["price"] - axis_min_p) / price_range
            p_h = p_norm * graph_height
            draw.rectangle(
                [x0, graph_bottom - p_h, x1, graph_bottom], fill="#e0e0e0", outline=None
            )

            if slot["active"]:
                draw.rectangle(
                    [x0, graph_bottom - 20, x1, graph_bottom],
                    fill="black",
                    outline=None,
                )

        # Left Axis
        draw.line(
            [(margin_left, graph_top), (margin_left, graph_bottom)],
            fill="black",
            width=2,
        )
        curr_mark = axis_min_p
        while curr_mark <= axis_max_p + 0.01:
            norm = (curr_mark - axis_min_p) / price_range
            mark_y = graph_bottom - (norm * graph_height)
            draw.line(
                [(margin_left - 5, mark_y), (margin_left, mark_y)],
                fill="black",
                width=1,
            )
            label = f"{curr_mark:.1f}"
            draw.text(
                (margin_left - 55, mark_y - 10), label, font=font_small, fill="black"
            )
            curr_mark += 0.5

        # X-Axis Labels
        draw.text(
            (margin_left, graph_bottom + 15),
            start_dt.strftime("%H:%M"),
            font=font_small,
            fill="black",
        )

        end_str = end_dt.strftime("%H:%M")
        try:
            w = draw.textlength(end_str, font=font_small)
            draw.text(
                (width - margin_right - w, graph_bottom + 15),
                end_str,
                font=font_small,
                fill="black",
            )
        except AttributeError:
            draw.text(
                (width - margin_right - 50, graph_bottom + 15),
                end_str,
                font=font_small,
                fill="black",
            )

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        img.save(file_path)
        _LOGGER.info(f"Saved plan image to {file_path}")
