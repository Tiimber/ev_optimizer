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
    CONF_CAR_ENTITY_ID,
    CONF_CAR_REFRESH_ACTION,
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

# Imports for Real-time Safety safety
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.core import callback
from time import perf_counter

# Imports from helper modules
from .image_generator import generate_report_image, generate_plan_image
from .planner import generate_charging_plan, calculate_load_balancing, analyze_prices
from .session_manager import SessionManager

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
        
        # Session & Logging Management
        self.session_manager = SessionManager(hass)

        # Scheduling state

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

        # New Flag: Tracks if user explicitly moved the Next Session slider
        self.manual_override_active = False

        # Real-time Safety Listeners
        self._safety_listeners = []
        self._debounce_unsub = None
        self._last_p1_update = datetime.min


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
            ),  # Shared Entity for Limit AND Refresh
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
            "refresh_int": get_conf(CONF_CAR_REFRESH_INTERVAL),
        }

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )

    def async_setup_listeners(self):
        """Set up event listeners for real-time safety."""
        # Listen to P1 sensor changes used for load balancing
        p1_sensors = [
            self.conf_keys["p1_l1"],
            self.conf_keys["p1_l2"],
            self.conf_keys["p1_l3"],
        ]
        # Filter out None values
        p1_sensors = [s for s in p1_sensors if s]

        if p1_sensors:
            _LOGGER.debug(f"Setting up real-time safety listeners for: {p1_sensors}")
            self._safety_listeners.append(
                async_track_state_change_event(
                    self.hass, p1_sensors, self._async_p1_update_callback
                )
            )

    def async_shutdown(self):
        """Cancel listeners and timers to clean up."""
        for unsub in self._safety_listeners:
            unsub()
        self._safety_listeners = []

        if self._debounce_unsub:
            self._debounce_unsub()
            self._debounce_unsub = None

    @callback
    def _async_p1_update_callback(self, event):
        """Handle P1 meter state changes with debouncing."""
        now = datetime.now()
        
        # Debounce: Ensure we don't update more than once every 2 seconds
        # unless an update is already scheduled.
        time_since = (now - self._last_p1_update).total_seconds()
        
        if time_since < 2.0:
            # If we recently updated, schedule a delayed update if not already scheduled
            if not self._debounce_unsub:
                delay = 2.0 - time_since
                self._debounce_unsub = self.hass.loop.call_later(
                    delay, self._async_scheduled_refresh
                )
            return

        # If enough time passed, update immediately
        self._async_scheduled_refresh()

    @callback
    def _async_scheduled_refresh(self):
        """Trigger the actual refresh."""
        self._debounce_unsub = None
        self._last_p1_update = datetime.now()
        # Request refresh (schedule it since we can't await in a callback)
        self.hass.async_create_task(self.async_request_refresh())

    def _add_log(self, message: str):
        """Add an entry to the action log."""
        self.session_manager.add_log(message)

    async def _load_data(self):
        """Load persisted settings from disk with robust error handling."""
        if self._data_loaded:
            return

        try:
            data = await self.store.async_load()
            if data:
                self.manual_override_active = data.get("manual_override_active", False)
                self.session_manager.load_from_dict(data)

                settings = data.get("user_settings", {})

                # Robustly parse time objects (Fix for str vs time error)
                for key in [ENTITY_DEPARTURE_TIME, ENTITY_DEPARTURE_OVERRIDE]:
                    if key in settings and settings[key]:
                        try:
                            val = settings[key]
                            # If it's a string from JSON, parse it
                            if isinstance(val, str):
                                parts = val.split(":")
                                settings[key] = time(int(parts[0]), int(parts[1]))
                        except Exception:
                            _LOGGER.warning(
                                f"Failed to parse saved time for {key}, resetting to default."
                            )
                            settings.pop(key, None)

                self.user_settings.update(settings)
                self._add_log("System started. Settings and Log loaded.")
        except Exception as e:
            _LOGGER.error(f"Failed to load EV settings: {e}")

        self._data_loaded = True

    def _save_data(self):
        """Schedule save of settings to disk."""

        def data_to_save():
            clean_settings = self.user_settings.copy()
            for key, val in clean_settings.items():
                if isinstance(val, time):
                    clean_settings[key] = val.strftime("%H:%M")

            data = {
                "manual_override_active": self.manual_override_active,
                "user_settings": clean_settings,
            }
            data.update(self.session_manager.to_dict())
            return data

        self.store.async_delay_save(data_to_save, 1.0)

    def set_user_input(self, key: str, value, internal: bool = False):
        """Update a user setting from the UI."""
        _LOGGER.debug(f"Setting user input: {key} = {value}")
        self.user_settings[key] = value

        if not internal:
            self._add_log(f"User setting changed: {key} -> {value}")

        if key == ENTITY_TARGET_OVERRIDE and not internal:
            self.manual_override_active = True
            self._add_log("Manual Override Mode Activated.")

        self._save_data()

        if self.data:
            self.hass.async_create_task(self.async_refresh())

    def clear_manual_override(self):
        """Called by the Clear Override button."""
        _LOGGER.info("Manual override cleared by user.")
        self._add_log("Manual override cleared. Reverting to Smart Logic.")
        self.manual_override_active = False

        std_target = self.user_settings.get(ENTITY_TARGET_SOC, 80)
        self.user_settings[ENTITY_TARGET_OVERRIDE] = std_target

        self._save_data()

        if self.data:
            self.hass.async_create_task(self.async_refresh())

    async def async_trigger_report_generation(self):
        """Manually trigger image generation for the current or last session."""
        report = None
        if self.current_session:
            _LOGGER.info("Generating report for ACTIVE session.")
            report = self._calculate_session_totals()
            report["end_time"] = datetime.now().isoformat()
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

        # Inject fee settings into data passed to image gen
        data_with_fees = self.data.copy()
        data_with_fees[ENTITY_PRICE_EXTRA_FEE] = self.user_settings.get(
            ENTITY_PRICE_EXTRA_FEE, 0.0
        )
        data_with_fees[ENTITY_PRICE_VAT] = self.user_settings.get(ENTITY_PRICE_VAT, 0.0)

        save_path = self.hass.config.path("www", "ev_smart_charger_plan.png")
        await self.hass.async_add_executor_job(
            generate_plan_image, data_with_fees, save_path
        )
        self._add_log("Plan Image Generated")

    async def _async_update_data(self):
        """Update data via library."""
        start_time = perf_counter()
        
        if not self._data_loaded:
            await self._load_data()

        try:
            data = self._fetch_sensor_data()
            data.update(self.user_settings)

            cal_entity = self.conf_keys.get("calendar")
            data["calendar_events"] = []
            if cal_entity:
                try:
                    now = datetime.now()
                    resp = await self.hass.services.async_call(
                        "calendar",
                        "get_events",
                        {
                            "entity_id": cal_entity,
                            "start_date_time": now.isoformat(),
                            "end_date_time": (now + timedelta(hours=48)).isoformat(),
                        },
                        blocking=True,
                        return_response=True,
                    )
                    if resp and cal_entity in resp:
                        data["calendar_events"] = resp[cal_entity].get("events", [])
                except Exception as e:
                    _LOGGER.warning(f"Failed to fetch calendar events: {e}")

            await self._handle_plugged_event(data["car_plugged"], data)
            self._update_virtual_soc(data)
            data["car_soc"] = self._virtual_soc

            # Delegate Logic to Planner
            data["max_available_current"] = calculate_load_balancing(
                data, self.config_settings["max_fuse"]
            )
            data["current_price_status"] = analyze_prices(data["price_data"])

            plan = generate_charging_plan(
                data, self.config_settings, self.manual_override_active, overload_prevention_minutes=self.session_manager.overload_prevention_minutes
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
                    plan["charging_summary"] = "Charging Buffer Active."

            data.update(plan)

            await self._manage_car_refresh(data, plan)
            await self._apply_charger_control(data, plan)
            self._record_session_data(data)
            data["action_log"] = self.session_manager.action_log
            data["last_session_data"] = self.session_manager.last_session_data

            # Performance Logging
            duration = perf_counter() - start_time
            data["latency_ms"] = round(duration * 1000, 2)
            _LOGGER.debug(f"Data Update & Logic completed in {duration:.4f}s")

            return data

        except Exception as err:
            _LOGGER.error(f"Error in EV Coordinator: {err}")
            raise UpdateFailed(f"Error communicating with API: {err}")

    async def _manage_car_refresh(self, data: dict, plan: dict):
        if not data.get("car_plugged"):
            return

        svc = self.conf_keys.get("refresh_svc")
        # Use shared entity
        ent = self.conf_keys.get("car_target_ent")
        interval_mode = self.conf_keys.get("refresh_int", REFRESH_NEVER)

        if not svc or not ent or interval_mode == REFRESH_NEVER:
            return

        now = datetime.now()

        if self._last_car_refresh_time:
            delta = now - self._last_car_refresh_time
        else:
            delta = timedelta(days=365)

        should_refresh = False

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
        elif interval_mode == REFRESH_AT_TARGET and delta > timedelta(hours=12):
            if self._virtual_soc >= float(plan.get("planned_target_soc", 80)):
                should_refresh = True

        if should_refresh:
            await self._trigger_car_refresh(svc, ent)

    async def _trigger_car_refresh(self, service: str, entity_id: str):
        try:
            state = self.hass.states.get(self.conf_keys["car_soc"])
            val = (
                float(state.state)
                if state and state.state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]
                else 0.0
            )
            self._soc_before_refresh = val

            self._add_log(f"Forcing Car Refresh (Current: {val}%)")
            domain, name = service.split(".", 1)

            payload = {}
            if "." in entity_id:
                payload["entity_id"] = entity_id
            else:
                payload["device_id"] = entity_id

            await self.hass.services.async_call(domain, name, payload, blocking=True)
            self._last_car_refresh_time = datetime.now()
            self._refresh_trigger_timestamp = datetime.now()
        except Exception as e:
            _LOGGER.error(f"Failed to force refresh car: {e}")

    def _update_virtual_soc(self, data: dict):
        current_time = datetime.now()
        sensor_soc = data.get("car_soc")

        trust_sensor_period = False
        if self._refresh_trigger_timestamp:
            if (current_time - self._refresh_trigger_timestamp) < timedelta(minutes=5):
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

        if self._last_applied_state == "charging":
            ch_l1 = data.get("ch_l1", 0.0)
            ch_l2 = data.get("ch_l2", 0.0)
            ch_l3 = data.get("ch_l3", 0.0)
            measured_amps = max(ch_l1, ch_l2, ch_l3)
            used_amps = (
                measured_amps if measured_amps > 0.5 else self._last_applied_amps
            )

            if used_amps > 0:
                seconds_passed = (current_time - self._last_update_time).total_seconds()
                hours_passed = seconds_passed / 3600.0
                estimated_power_kw = (3 * 230 * used_amps) / 1000.0
                efficiency_pct = self.entry.data.get(CONF_CHARGER_LOSS, 10.0)
                efficiency_factor = 1.0 - (efficiency_pct / 100.0)
                added_kwh = estimated_power_kw * hours_passed * efficiency_factor

                if self.car_capacity > 0:
                    added_percent = (added_kwh / self.car_capacity) * 100.0
                    self._virtual_soc += added_percent
                    if self._last_applied_car_limit > 0:
                        if self._virtual_soc > self._last_applied_car_limit:
                            self._virtual_soc = float(self._last_applied_car_limit)
                    if self._virtual_soc > 100.0:
                        self._virtual_soc = 100.0

        self._last_update_time = current_time

    async def _apply_charger_control(self, data: dict, plan: dict):
        if datetime.now() - self._startup_time < timedelta(minutes=2):
            return

        should_charge = data.get("should_charge_now", False)
        safe_amps = math.floor(data.get("max_available_current", 0))
        if safe_amps < 6:
            if should_charge:
                self._add_log(
                    f"Safety Cutoff: Available {safe_amps}A is below minimum 6A. Pausing."
                )
                # Track minutes lost to overload prevention (30 second update interval)
                self.overload_prevention_minutes += 0.5
            should_charge = False

        target_amps = safe_amps if should_charge else 0
        desired_state = "charging" if should_charge else "paused"

        if "Maintenance mode active" in plan.get("charging_summary", ""):
            should_charge = True
            target_amps = 0
            desired_state = "maintenance"

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
                    self._add_log(f"Set Car Limit: {target_soc}%")
                except Exception:
                    pass
            elif self.conf_keys.get("car_svc") and self.conf_keys.get("car_target_ent"):
                try:
                    full = self.conf_keys["car_svc"]
                    dom, svc = full.split(".", 1)
                    tid = self.conf_keys["car_target_ent"]
                    pl = {"ac_limit": target_soc, "dc_limit": target_soc}
                    if "." in tid:
                        pl["entity_id"] = tid
                    else:
                        pl["device_id"] = tid
                    await self.hass.services.async_call(dom, svc, pl, blocking=True)
                    self._last_applied_car_limit = target_soc
                    self._add_log(f"Service Call: Set Car Limit to {target_soc}%")
                except Exception as e:
                    _LOGGER.error(f"Car Limit Service Failed: {e}")

        if should_charge:
            # Mark that we charged in this interval
            self.session_manager.mark_charging_in_interval()
            if self._last_applied_state != "charging" or self._last_applied_car_limit != target_amps:
                self._add_log(f"Setting charger to {target_amps}A")
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
                    if target_amps > 0:
                        self._add_log(
                            f"Load Balancing: Set Zaptec limit to {target_amps}A"
                        )
                except Exception as e:
                    _LOGGER.error(f"Failed to set Zaptec limit: {e}")

        else:
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
            return self.hass.states.get(entity_id) if entity_id else None

        data["p1_l1"] = get_float(self.conf_keys["p1_l1"])
        data["p1_l2"] = get_float(self.conf_keys["p1_l2"])
        data["p1_l3"] = get_float(self.conf_keys["p1_l3"])
        data["car_soc"] = get_float(self.conf_keys["car_soc"])
        data["ch_l1"] = get_float(self.conf_keys.get("ch_l1"))
        data["ch_l2"] = get_float(self.conf_keys.get("ch_l2"))
        data["ch_l3"] = get_float(self.conf_keys.get("ch_l3"))
        # Read Zaptec limiter value for load balancing fallback
        data["zap_limit_value"] = get_float(self.conf_keys.get("zap_limit"))

        plugged_state = get_state(self.conf_keys["car_plugged"])
        data["car_plugged"] = (
            plugged_state.state
            in ["on", "true", "connected", "charging", "full", "plugged_in"]
            if plugged_state
            else False
        )
        price_entity = self.conf_keys.get("price")
        data["price_data"] = (
            self.hass.states.get(price_entity).attributes
            if price_entity and self.hass.states.get(price_entity)
            else {}
        )
        return data

    async def _handle_plugged_event(self, is_plugged, data):
        """Handle logic when car is plugged/unplugged."""
        if is_plugged and not self.previous_plugged_state:
            self.session_manager.start_session(data.get("car_soc", 0))
            self.manual_override_active = False
            # If we missed a session finalization, it's too late now, start fresh.
            try:
                # Reset inputs to defaults
                std_time = self.user_settings.get(ENTITY_DEPARTURE_TIME, time(7, 0))
                self.set_user_input(ENTITY_DEPARTURE_OVERRIDE, std_time, internal=True)
                self.user_settings[ENTITY_DEPARTURE_OVERRIDE] = std_time
                
                std_target = self.user_settings.get(ENTITY_TARGET_SOC, 80)
                self.set_user_input(ENTITY_TARGET_OVERRIDE, std_target, internal=True)
                self.user_settings[ENTITY_TARGET_OVERRIDE] = std_target

                # Potentially zaptec specific resume
                if self.conf_keys.get("zap_switch") and self.conf_keys.get("zap_resume"):
                    pass # Keep existing logic logic if present
            except Exception:
                pass

        if not is_plugged and self.previous_plugged_state:
            self._finalize_session(final_soc=data.get("car_soc"))
            self.manual_override_active = False

            std_time = self.user_settings.get(ENTITY_DEPARTURE_TIME, time(7, 0))
            self.set_user_input(ENTITY_DEPARTURE_OVERRIDE, std_time, internal=True)
            self.user_settings[ENTITY_DEPARTURE_OVERRIDE] = std_time

            std_target = self.user_settings.get(ENTITY_TARGET_SOC, 80)
            self.set_user_input(ENTITY_TARGET_OVERRIDE, std_target, internal=True)
            self.user_settings[ENTITY_TARGET_OVERRIDE] = std_target

            self._save_data()

            if self.conf_keys.get("zap_switch"):
                try:
                    await self.hass.services.async_call(
                        "switch",
                        SERVICE_TURN_OFF,
                        {"entity_id": self.conf_keys["zap_switch"]},
                        blocking=True,
                    )
                except:
                    pass
            self._last_applied_state = "paused"
            self._last_applied_car_limit = -1
            self._last_scheduled_end = None

        self.previous_plugged_state = is_plugged

    def _record_session_data(self, data):
        self.session_manager.record_data_point(
            data, self.user_settings, self._last_applied_amps, self._last_applied_state
        )

    def _finalize_session(self, final_soc=None):
        report = self.session_manager.stop_session(self.user_settings, self.currency, final_soc=final_soc)
        if report:
            try:
                save_path = self.hass.config.path(
                    "www", "ev_smart_charger_last_session.png"
                )
                self.hass.async_add_executor_job(generate_report_image, report, save_path)
            except Exception as e:
                _LOGGER.warning(f"Could not trigger image generation: {e}")
