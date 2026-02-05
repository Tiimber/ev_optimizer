"""DataUpdateCoordinator for EV Optimizer."""

from __future__ import annotations

import asyncio
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
    DEFAULT_LOSS,
    DEFAULT_DEPARTURE_TIME,
    DEFAULT_TARGET_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_PRICE_LIMIT_1,
    DEFAULT_TARGET_SOC_1,
    DEFAULT_PRICE_LIMIT_2,
    DEFAULT_TARGET_SOC_2,
    REFRESH_NEVER,
    REFRESH_30_MIN,
    REFRESH_AT_TARGET,
    LEARNING_CHARGER_LOSS,
    LEARNING_CONFIDENCE,
    LEARNING_SESSIONS,
    LEARNING_LOCKED,
    LEARNING_HISTORY,
    LEARNING_LAST_REFRESH,
    LEARNING_PRICE_ARRIVAL,
    REFRESH_1_HOUR,
    REFRESH_2_HOURS,
    REFRESH_3_HOURS,
    REFRESH_4_HOURS,
    REFRESH_AT_TARGET,
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
    ENTITY_PRICE_EXTRA_FEE,
    ENTITY_PRICE_VAT,
    ENTITY_DEBUG_CURRENT_TIME,
    ENTITY_DEBUG_DEPARTURE_TIME,
    ENTITY_DEBUG_CURRENT_SOC,
    ENTITY_DEBUG_TARGET_SOC,
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
        self._last_unknown_plugged_state: str | None = None
        self.user_settings = {}  # Storage for UI inputs
        self.learning_state = {}  # Learning state (initialized after config loaded)
        
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
        self._last_sensor_soc = None  # Track last raw sensor value to detect real updates

        # Locked Plan: Lock plan once charging starts to prevent recalculation with stale SoC
        self._locked_plan = None
        self._locked_plan_soc = None  # SoC when plan was locked

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

        # Initialize user_settings with default values from configuration
        # These will be overwritten by persisted values in _load_data if they exist
        departure_time_str = get_conf(ENTITY_DEPARTURE_TIME, DEFAULT_DEPARTURE_TIME)
        try:
            parts = departure_time_str.split(":")
            self.user_settings[ENTITY_DEPARTURE_TIME] = time(int(parts[0]), int(parts[1]))
        except:
            self.user_settings[ENTITY_DEPARTURE_TIME] = time(7, 0)
        
        self.user_settings[ENTITY_TARGET_SOC] = get_conf(ENTITY_TARGET_SOC, DEFAULT_TARGET_SOC)
        self.user_settings[ENTITY_MIN_SOC] = get_conf(ENTITY_MIN_SOC, DEFAULT_MIN_SOC)
        self.user_settings[ENTITY_PRICE_LIMIT_1] = get_conf(ENTITY_PRICE_LIMIT_1, DEFAULT_PRICE_LIMIT_1)
        self.user_settings[ENTITY_TARGET_SOC_1] = get_conf(ENTITY_TARGET_SOC_1, DEFAULT_TARGET_SOC_1)
        self.user_settings[ENTITY_PRICE_LIMIT_2] = get_conf(ENTITY_PRICE_LIMIT_2, DEFAULT_PRICE_LIMIT_2)
        self.user_settings[ENTITY_TARGET_SOC_2] = get_conf(ENTITY_TARGET_SOC_2, DEFAULT_TARGET_SOC_2)

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

        # Initialize learning state with config defaults
        configured_loss = float(get_conf(CONF_CHARGER_LOSS, DEFAULT_LOSS))
        self.learning_state = {
            LEARNING_CHARGER_LOSS: configured_loss,
            LEARNING_CONFIDENCE: 0,
            LEARNING_SESSIONS: 0,
            LEARNING_LOCKED: False,
            LEARNING_HISTORY: [],
            LEARNING_LAST_REFRESH: None,
            LEARNING_PRICE_ARRIVAL: [],  # Track when tomorrow's prices arrive
        }
        
        # Track when we last saw tomorrow's prices
        self._last_tomorrow_valid = False

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

                # Load learning state
                learning_data = data.get("learning_state", {})
                if learning_data:
                    self.learning_state.update(learning_data)
                    _LOGGER.info(
                        "Loaded learning state: loss=%.1f%%, confidence=%d/10, sessions=%d, locked=%s",
                        self.learning_state.get(LEARNING_CHARGER_LOSS, 0),
                        self.learning_state.get(LEARNING_CONFIDENCE, 0),
                        self.learning_state.get(LEARNING_SESSIONS, 0),
                        self.learning_state.get(LEARNING_LOCKED, False),
                    )

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

            # Prepare learning state for saving
            learning_to_save = self.learning_state.copy()
            if LEARNING_LAST_REFRESH in learning_to_save and learning_to_save[LEARNING_LAST_REFRESH]:
                # Convert datetime to ISO string
                if isinstance(learning_to_save[LEARNING_LAST_REFRESH], datetime):
                    learning_to_save[LEARNING_LAST_REFRESH] = learning_to_save[LEARNING_LAST_REFRESH].isoformat()

            data = {
                "manual_override_active": self.manual_override_active,
                "user_settings": clean_settings,
                "learning_state": learning_to_save,
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
            # Clear locked plan when user manually changes target
            self._locked_plan = None
            self._locked_plan_soc = None

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
        if self.session_manager.current_session:
            _LOGGER.info("Generating report for ACTIVE session.")
            report = self.session_manager.calculate_session_totals(self.currency)
            if report:
                report["end_time"] = datetime.now().isoformat()
        elif self.session_manager.last_session_data:
            _LOGGER.info("Regenerating report for LAST FINISHED session.")
            report = self.session_manager.last_session_data

        if report:
            save_path = self.hass.config.path(
                "www", "ev_optimizer_last_session.png"
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

        save_path = self.hass.config.path("www", "ev_optimizer_plan.png")
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

            # Track when tomorrow's prices become available
            self._track_price_arrival(data.get("price_data", {}))

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
            trust_sensor_period = self._update_virtual_soc(data)
            data["car_soc"] = self._virtual_soc
            data["soc_sensor_refresh"] = trust_sensor_period

            # Delegate Logic to Planner
            data["max_available_current"] = calculate_load_balancing(
                data, self.config_settings["max_fuse"]
            )
            data["current_price_status"] = analyze_prices(data["price_data"])

            # Get expected price arrival time from learning
            expected_price_time = self._get_expected_price_arrival_time()

            # Check if sensor SoC has actually updated (unlock plan if it has)
            sensor_soc_updated = False
            if self._last_sensor_soc is not None:
                current_sensor = data.get("car_soc", 0.0)
                if abs(current_sensor - self._last_sensor_soc) > 0.5:  # >0.5% change
                    sensor_soc_updated = True
                    _LOGGER.info(
                        "🔓 SoC sensor updated (%.1f%% → %.1f%%), unlocking plan",
                        self._last_sensor_soc, current_sensor
                    )
                    self._locked_plan = None
                    self._locked_plan_soc = None

            # Use locked plan if available and sensor hasn't updated
            if self._locked_plan is not None and not sensor_soc_updated:
                _LOGGER.debug(
                    "🔒 Using locked plan (locked at %.1f%% SoC, current virtual: %.1f%%)",
                    self._locked_plan_soc, self._virtual_soc
                )
                plan = self._locked_plan.copy()
                # Update should_charge_now based on current time and schedule
                plan = self._update_locked_plan_status(plan)
            else:
                # Generate new plan
                plan = generate_charging_plan(
                    data, self.config_settings, self.manual_override_active, 
                    learning_state=self.learning_state, 
                    overload_prevention_minutes=self.session_manager.overload_prevention_minutes,
                    expected_price_time=expected_price_time
                )
                
                # Lock the plan when charging starts for the first time
                if plan["should_charge_now"] and self._locked_plan is None:
                    self._locked_plan = plan.copy()
                    self._locked_plan_soc = self._virtual_soc
                    _LOGGER.info(
                        "🔒 Locking charging plan at %.1f%% SoC (plan will remain stable until SoC updates)",
                        self._locked_plan_soc
                    )

            # Handle Buffer Logic (stateful, so stays in coordinator)
            # Update scheduled end when we're waiting to charge
            if not plan["should_charge_now"] and plan.get("session_end_time"):
                self._last_scheduled_end = datetime.fromisoformat(
                    plan["session_end_time"]
                )
            
            # Clear scheduled end when charging starts (otherwise it blocks future charges)
            if plan["should_charge_now"] and self._last_scheduled_end:
                _LOGGER.debug(
                    "📍 Clearing scheduled_end %s because charging is now active",
                    self._last_scheduled_end.strftime("%H:%M:%S")
                )
                self._last_scheduled_end = None

            # Buffer logic: keep charging for 15min after scheduled end
            if not plan["should_charge_now"] and self._last_scheduled_end:
                now_dt = datetime.now()
                buffer_end = self._last_scheduled_end + timedelta(minutes=15)
                if self._last_scheduled_end <= now_dt < buffer_end:
                    _LOGGER.warning(
                        "🚨 Buffer Logic Override: Forcing charge because current time %s is "
                        "within 15min buffer after last_scheduled_end %s",
                        now_dt.strftime("%H:%M:%S"),
                        self._last_scheduled_end.strftime("%H:%M:%S")
                    )
                    plan["should_charge_now"] = True
                    plan["charging_summary"] = "Charging Buffer Active."
                elif now_dt >= buffer_end:
                    # Clear old scheduled end if we're past the buffer
                    _LOGGER.debug(
                        "📍 Clearing old scheduled_end %s (now %s, buffer expired)",
                        self._last_scheduled_end.strftime("%H:%M:%S"),
                        now_dt.strftime("%H:%M:%S")
                    )
                    self._last_scheduled_end = None

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
        elif interval_mode == REFRESH_AT_TARGET:
            # Smart refresh mode with learning
            should_refresh = self._should_trigger_smart_refresh(plan, delta)

        if should_refresh:
            await self._trigger_car_refresh(svc, ent)

    def _should_trigger_smart_refresh(self, plan: dict, time_since_last: timedelta) -> bool:
        """Determine if smart refresh should be triggered for efficiency learning."""
        # Only for smart refresh mode
        if not self.session_manager.current_session:
            return False  # Not charging
        
        planned_end = plan.get("session_end_time")
        if not planned_end:
            # Fallback: refresh if at target and time has passed
            if self._virtual_soc >= float(plan.get("planned_target_soc", 80)):
                return time_since_last > timedelta(hours=12)
            return False
        
        # Parse session_end_time if it's a string
        if isinstance(planned_end, str):
            try:
                planned_end = datetime.fromisoformat(planned_end)
            except Exception:
                return False
        
        now = datetime.now()
        time_to_end_minutes = (planned_end - now).total_seconds() / 60
        
        # Don't refresh if session hasn't started or already ended
        if time_to_end_minutes < 0 or time_to_end_minutes > 300:  # More than 5 hours
            return False
        
        sessions = self.learning_state.get(LEARNING_SESSIONS, 0)
        locked = self.learning_state.get(LEARNING_LOCKED, False)
        last_refresh_str = self.learning_state.get(LEARNING_LAST_REFRESH)
        
        # Convert last_refresh from ISO string if needed
        last_refresh = None
        if last_refresh_str:
            if isinstance(last_refresh_str, str):
                try:
                    last_refresh = datetime.fromisoformat(last_refresh_str)
                except Exception:
                    pass
            elif isinstance(last_refresh_str, datetime):
                last_refresh = last_refresh_str
        
        minutes_since_last = 999
        if last_refresh:
            minutes_since_last = (now - last_refresh).total_seconds() / 60
        
        if locked or sessions >= 10:
            # Locked phase: single refresh near completion
            if 0 < time_to_end_minutes <= 5 and minutes_since_last > 5:
                _LOGGER.debug("📊 Locked: Final refresh at completion")
                return True
        else:
            # Learning phase: multiple measurement points
            if time_to_end_minutes > 55 and time_to_end_minutes <= 65 and minutes_since_last > 30:
                _LOGGER.debug("📊 Learning: Measurement #1 (60min before end)")
                return True
            elif 25 < time_to_end_minutes <= 35 and minutes_since_last > 15:
                _LOGGER.debug("📊 Learning: Measurement #2 (30min before end)")
                return True
            elif 0 < time_to_end_minutes <= 5 and minutes_since_last > 5:
                _LOGGER.debug("📊 Learning: Final measurement")
                return True
        
        return False

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
            refresh_time = datetime.now()
            self._last_car_refresh_time = refresh_time
            self._refresh_trigger_timestamp = refresh_time
            
            # Record refresh time for learning and schedule evaluation
            interval_mode = self.conf_keys.get("refresh_int", REFRESH_NEVER)
            if interval_mode == REFRESH_AT_TARGET and self.session_manager.current_session:
                self.learning_state[LEARNING_LAST_REFRESH] = refresh_time.isoformat()
                # Schedule evaluation after a short delay to allow SoC to update
                self.hass.async_create_task(self._evaluate_efficiency_after_delay(refresh_time))
        except Exception as e:
            _LOGGER.error(f"Failed to force refresh car: {e}")

    async def _evaluate_efficiency_after_delay(self, refresh_time: datetime):
        """Wait for SoC to update, then evaluate efficiency learning."""
        # Wait 10 seconds for car SoC to update after refresh
        await asyncio.sleep(10)
        await self._evaluate_efficiency_learning(refresh_time)

    async def _evaluate_efficiency_learning(self, refresh_time: datetime):
        """Evaluate and adjust learned efficiency after a measurement point."""
        
        # Get current state
        actual_soc = self.data.get("car_soc", 0)
        if actual_soc == 0:
            _LOGGER.debug("📊 Skipping learning evaluation: SoC is 0 (sensor unavailable)")
            return
        
        planned_target = self.data.get("planned_target_soc", 80)
        
        # Get session data
        session = self.session_manager.current_session
        if not session:
            _LOGGER.debug("📊 Skipping learning evaluation: No active session")
            return
        
        charge_start_soc = session.get("start_soc", actual_soc)
        charge_start_time_str = session.get("start_time")
        if not charge_start_time_str:
            _LOGGER.debug("📊 Skipping learning evaluation: No session start time")
            return
        
        # Parse start time
        if isinstance(charge_start_time_str, str):
            charge_start_time = datetime.fromisoformat(charge_start_time_str)
        else:
            charge_start_time = charge_start_time_str
        
        time_elapsed_hours = (refresh_time - charge_start_time).total_seconds() / 3600
        
        # Get overload prevention minutes
        overload_minutes = self.session_manager.overload_prevention_minutes
        effective_charge_hours = time_elapsed_hours - (overload_minutes / 60.0)
        
        if effective_charge_hours <= 0:
            _LOGGER.debug("📊 Skipping learning evaluation: No effective charge time yet")
            return
        
        # Calculate expected SoC based on energy delivered
        battery_kwh = self.config_settings.get("car_capacity", 64.0)
        max_fuse = self.config_settings.get("max_fuse", 20.0)
        
        # Estimate charging power (simplified - assumes 3-phase at 230V)
        charging_power_kw = min((3 * 230 * max_fuse) / 1000.0, 11.0)
        
        current_loss = self.learning_state.get(LEARNING_CHARGER_LOSS, 0.0)
        efficiency = 1.0 - (current_loss / 100.0)
        
        # Expected energy into battery
        expected_energy_kwh = charging_power_kw * effective_charge_hours * efficiency
        expected_soc = charge_start_soc + (expected_energy_kwh / battery_kwh * 100)
        
        # Calculate error
        soc_error = actual_soc - expected_soc
        
        _LOGGER.info(
            "📊 Efficiency measurement: expected %.1f%%, actual %.1f%%, error %.1f%% (charged %.1fh)",
            expected_soc, actual_soc, soc_error, effective_charge_hours
        )
        
        # Determine adjustment
        sessions = self.learning_state.get(LEARNING_SESSIONS, 0)
        confidence = self.learning_state.get(LEARNING_CONFIDENCE, 0)
        
        # Margin of error (tighter as we learn)
        if confidence < 3:
            margin = 3.0  # Allow 3% error initially
        elif confidence < 6:
            margin = 2.0
        else:
            margin = 1.0
        
        adjustment = 0.0
        reason = ""
        
        if abs(soc_error) <= margin:
            # Within acceptable range
            confidence += 1
            reason = f"Within {margin}% margin, confidence increased"
        elif soc_error < -margin:
            # Actual LOWER than expected - we're losing more energy than thought
            # Need to INCREASE loss percentage
            if sessions < 5:
                adjustment = min(3.0, abs(soc_error) * 0.5)  # Aggressive early
            else:
                adjustment = min(1.5, abs(soc_error) * 0.3)  # More conservative
            
            current_loss += adjustment
            confidence = max(0, confidence - 1)
            reason = f"Underperforming by {abs(soc_error):.1f}%, increasing loss"
            
        elif soc_error > margin:
            # Actual HIGHER than expected - we're losing less than thought
            # Need to DECREASE loss percentage (careful!)
            if sessions < 5:
                adjustment = -min(2.0, soc_error * 0.4)
            else:
                adjustment = -min(1.0, soc_error * 0.25)
            
            current_loss += adjustment
            confidence = max(0, confidence - 1)
            reason = f"Outperforming by {soc_error:.1f}%, decreasing loss"
        
        # Apply bounds
        current_loss = max(0.0, min(20.0, current_loss))
        
        # Lock if high confidence
        locked = confidence >= 8
        if locked and not self.learning_state.get(LEARNING_LOCKED, False):
            reason += " - LOCKED IN"
        
        # Update state
        self.learning_state.update({
            LEARNING_CHARGER_LOSS: current_loss,
            LEARNING_CONFIDENCE: min(10, confidence),
            LEARNING_LOCKED: locked,
        })
        
        # Add to history
        history = self.learning_state.get(LEARNING_HISTORY, [])
        history.append({
            "timestamp": refresh_time.isoformat(),
            "expected_soc": round(expected_soc, 1),
            "actual_soc": round(actual_soc, 1),
            "error": round(soc_error, 1),
            "adjustment": round(adjustment, 1),
            "new_loss_pct": round(current_loss, 1),
            "confidence": confidence,
        })
        self.learning_state[LEARNING_HISTORY] = history[-10:]  # Keep last 10
        
        # LOG TO ACTION LOG
        self._add_log(
            f"Efficiency Learning: {reason}. "
            f"Loss now {current_loss:.1f}% (confidence {confidence}/10). "
            f"Expected {expected_soc:.1f}%, got {actual_soc:.1f}%"
        )
        
        _LOGGER.info(
            "🎓 Learning result: loss=%.1f%%, confidence=%d/10, locked=%s - %s",
            current_loss, confidence, locked, reason
        )
        
        # Save state
        self._save_data()

    def _update_locked_plan_status(self, locked_plan: dict) -> dict:
        """Update should_charge_now in locked plan based on current time and schedule.
        
        This allows the locked plan to continue executing its charging schedule
        without recalculating the entire plan.
        """
        now = datetime.now()
        schedule = locked_plan.get("charging_schedule", [])
        
        # Check if current time is within any active charging slot
        should_charge = False
        for slot in schedule:
            if not slot.get("active", False):
                continue
            slot_start = datetime.fromisoformat(slot["start"])
            slot_end = datetime.fromisoformat(slot["end"])
            if slot_start <= now < slot_end:
                should_charge = True
                break
        
        locked_plan["should_charge_now"] = should_charge
        return locked_plan

    def _update_virtual_soc(self, data: dict):
        current_time = datetime.now()
        sensor_soc = data.get("car_soc")

        # Validate the underlying HA state so we don't treat unavailable/unknown as a real 0.0
        # (our _fetch_sensor_data() uses 0.0 as a fallback for invalid states).
        sensor_state_valid = True
        car_soc_entity = self.conf_keys.get("car_soc")
        if car_soc_entity:
            state_obj = self.hass.states.get(car_soc_entity)
            if state_obj is None or state_obj.state in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
                sensor_state_valid = False

        trust_sensor_period = False
        if self._refresh_trigger_timestamp:
            if (current_time - self._refresh_trigger_timestamp) < timedelta(minutes=5):
                if (
                    sensor_soc is not None
                    and float(sensor_soc) != self._soc_before_refresh
                ):
                    trust_sensor_period = True

        sensor_soc_f = None
        if sensor_state_valid and sensor_soc is not None:
            try:
                sensor_soc_f = float(sensor_soc)
            except (TypeError, ValueError):
                sensor_soc_f = None

        actively_charging = self._last_applied_state == "charging"
        
        # Check if sensor value actually changed (indicating a real update from the car)
        sensor_value_changed = (
            sensor_soc_f is not None 
            and self._last_sensor_soc is not None 
            and abs(sensor_soc_f - self._last_sensor_soc) > 0.1  # More than 0.1% difference
        )
        
        if sensor_soc_f is not None:
            # During active charging: only trust sensor when:
            # 1. It's a forced refresh window, OR
            # 2. The sensor value actually changed (real update from car), OR
            # 3. Virtual SoC is uninitialized
            # This prevents wobbling from re-reading the same stale sensor value
            if actively_charging:
                if trust_sensor_period or sensor_value_changed or self._virtual_soc == 0.0:
                    self._virtual_soc = sensor_soc_f
                    _LOGGER.debug(
                        f"Virtual SoC updated to {sensor_soc_f:.1f}% during charging "
                        f"(forced_refresh={trust_sensor_period}, value_changed={sensor_value_changed})"
                    )
                # else: ignore unchanged sensor value during charging; rely purely on virtual estimator
            else:
                # When not charging: always trust sensor (could be driven, charged elsewhere, etc.)
                # Follow sensor upwards, downwards, or on first reading
                if sensor_soc_f > self._virtual_soc or self._virtual_soc == 0.0:
                    self._virtual_soc = sensor_soc_f
                else:
                    # Sensor reports lower value while not charging - trust it (vehicle may have been driven)
                    self._virtual_soc = sensor_soc_f
            
            # Update last sensor value for next comparison
            self._last_sensor_soc = sensor_soc_f

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
        return trust_sensor_period

    async def _apply_charger_control(self, data: dict, plan: dict):
        if datetime.now() - self._startup_time < timedelta(minutes=2):
            return

        if not data.get("car_plugged", False):
            return

        should_charge = data.get("should_charge_now", False)
        safe_amps = math.floor(data.get("max_available_current", 0))

        maintenance_now = (
            "Maintenance mode active" in plan.get("charging_summary", "") and should_charge
        )

        # Maintenance mode is intentionally 0A, so it must not be blocked by the
        # minimum 6A safety cutoff and must not count as overload prevention.
        if maintenance_now:
            target_amps = 0
            desired_state = "maintenance"
        else:
            if safe_amps < 6:
                if should_charge:
                    self._add_log(
                        f"Safety Cutoff: Available {safe_amps}A is below minimum 6A. Pausing."
                    )
                    # Track minutes lost to overload prevention (30 second update interval)
                    self.session_manager.add_overload_minutes(0.5)
                should_charge = False

            target_amps = safe_amps if should_charge else 0
            desired_state = "charging" if should_charge else "paused"

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
            # Mark that we charged in this interval (only if we actually drew current)
            if target_amps > 0:
                self.session_manager.mark_charging_in_interval()

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
                        self._add_log(f"Setting charger to {target_amps}A")
                    elif self.conf_keys.get("zap_resume"):
                        await self.hass.services.async_call(
                            "button",
                            "press",
                            {"entity_id": self.conf_keys["zap_resume"]},
                            blocking=True,
                        )
                        self._add_log("Sent Resume command")
                        self._add_log(f"Setting charger to {target_amps}A")
                    self._last_applied_state = desired_state
                except Exception as e:
                    _LOGGER.error(f"Failed to switch Zaptec state to CHARGING: {e}")

            if target_amps != self._last_applied_amps and self.conf_keys["zap_limit"]:
                try:
                    # Cap at Zaptec entity's max value to prevent out-of-range errors
                    zap_entity = self.hass.states.get(self.conf_keys["zap_limit"])
                    if zap_entity and hasattr(zap_entity.attributes, 'get'):
                        entity_max = zap_entity.attributes.get("max")
                        if entity_max is not None:
                            if target_amps > entity_max:
                                _LOGGER.warning(
                                    f"⚠️ Capping Zaptec limit from {target_amps}A to entity max {entity_max}A"
                                )
                                target_amps = entity_max
                    
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
                            f"Adjusted charger to {target_amps}A (Load Balancing)"
                        )
                except Exception as e:
                    _LOGGER.error(f"Failed to set Zaptec limit: {e}")

        else:
            is_stopping = self._last_applied_state in ("charging", "maintenance")

            # Only touch the Zaptec limiter when we are actively stopping charging.
            # Outside planned charging windows we keep the limiter unchanged to avoid
            # unnecessary toggling/spam.
            if is_stopping and self.conf_keys["zap_limit"]:
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
                        # If the car is plugged in, keep the charger enabled so the pilot
                        # signal stays present. Some vehicle integrations report the
                        # car as "unplugged" if the EVSE is fully disabled.
                        if data.get("car_plugged", False):
                            self._add_log("Paused (plugged): Keeping charger enabled")
                        else:
                            await self.hass.services.async_call(
                                "switch",
                                SERVICE_TURN_OFF,
                                {"entity_id": self.conf_keys["zap_switch"]},
                                blocking=True,
                            )
                            self._add_log("Switched Charging state to: PAUSED")
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
        if plugged_state:
            raw_state = str(plugged_state.state)
            normalized = raw_state.strip().lower()

            truthy_states = {
                "on",
                "true",
                "connected",
                "charging",
                "full",
                "plugged_in",
                "plugged",
                "yes",
                "y",
                "1",
            }
            falsy_states = {
                "off",
                "false",
                "disconnected",
                "unplugged",
                "no",
                "n",
                "0",
                STATE_UNKNOWN,
                STATE_UNAVAILABLE,
            }

            if normalized in truthy_states:
                data["car_plugged"] = True
            elif normalized in falsy_states:
                data["car_plugged"] = False
            else:
                # Fallback: numeric parsing (e.g. 0/1, 0.0/1.0)
                try:
                    data["car_plugged"] = float(normalized) > 0
                except ValueError:
                    data["car_plugged"] = False
                    if self._last_unknown_plugged_state != normalized:
                        self._last_unknown_plugged_state = normalized
                        _LOGGER.warning(
                            "Unexpected plugged sensor state for %s: '%s' (treating as unplugged)",
                            self.conf_keys.get("car_plugged"),
                            raw_state,
                        )
        else:
            data["car_plugged"] = False
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
            
            # CRITICAL FIX: Clear old session state to prevent buffer logic from interfering
            # When car plugs in, we MUST clear any buffered state from previous session
            self._last_scheduled_end = None
            self._last_applied_state = None
            self._last_applied_amps = -1
            self._last_applied_car_limit = -1
            
            # Clear locked plan for new session
            self._locked_plan = None
            self._locked_plan_soc = None
            
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
            
            # Clear locked plan when car unplugs
            self._locked_plan = None
            self._locked_plan_soc = None

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
        
        # Increment learning session counter
        interval_mode = self.conf_keys.get("refresh_int", REFRESH_NEVER)
        if interval_mode == REFRESH_AT_TARGET and report:
            sessions = self.learning_state.get(LEARNING_SESSIONS, 0) + 1
            self.learning_state[LEARNING_SESSIONS] = sessions
            _LOGGER.info("📚 Learning session %d completed", sessions)
            self._save_data()
        
        if report:
            try:
                save_path = self.hass.config.path(
                    "www", "ev_optimizer_last_session.png"
                )
                self.hass.async_add_executor_job(generate_report_image, report, save_path)
            except Exception as e:
                _LOGGER.warning(f"Could not trigger image generation: {e}")
    
    def dump_debug_state(self) -> dict:
        """Dump complete state for debugging/simulation purposes.
        
        Returns a comprehensive snapshot of all inputs to the planner that can be
        used to reproduce the exact charging decision in isolation.
        """
        import json
        from datetime import datetime
        
        _LOGGER.debug("🔍 Starting debug state dump...")
        
        # Get current data snapshot
        data = self.data if self.data else {}
        
        # Build comprehensive debug dump
        debug_dump = {
            "timestamp": datetime.now().isoformat(),
            "description": "Complete state dump for ev_optimizer debugging/simulation",
            
            # Configuration
            "config_settings": self.config_settings.copy(),
            
            # User settings from UI
            "user_settings": {
                ENTITY_TARGET_SOC: self.user_settings.get(ENTITY_TARGET_SOC, 80),
                ENTITY_MIN_SOC: self.user_settings.get(ENTITY_MIN_SOC, 20),
                ENTITY_DEPARTURE_TIME: str(self.user_settings.get(ENTITY_DEPARTURE_TIME, time(7, 0))),
                ENTITY_DEPARTURE_OVERRIDE: str(self.user_settings.get(ENTITY_DEPARTURE_OVERRIDE, time(7, 0))),
                ENTITY_SMART_SWITCH: self.user_settings.get(ENTITY_SMART_SWITCH, True),
                ENTITY_TARGET_OVERRIDE: self.user_settings.get(ENTITY_TARGET_OVERRIDE, 80),
                ENTITY_PRICE_LIMIT_1: self.user_settings.get(ENTITY_PRICE_LIMIT_1, 0.5),
                ENTITY_TARGET_SOC_1: self.user_settings.get(ENTITY_TARGET_SOC_1, 100),
                ENTITY_PRICE_LIMIT_2: self.user_settings.get(ENTITY_PRICE_LIMIT_2, 1.5),
                ENTITY_TARGET_SOC_2: self.user_settings.get(ENTITY_TARGET_SOC_2, 80),
                ENTITY_PRICE_EXTRA_FEE: self.user_settings.get(ENTITY_PRICE_EXTRA_FEE, 0.0),
                ENTITY_PRICE_VAT: self.user_settings.get(ENTITY_PRICE_VAT, 0.0),
            },
            
            # State flags
            "manual_override_active": self.manual_override_active,
            "previous_plugged_state": self.previous_plugged_state,
            
            # Current sensor data
            "sensor_data": {
                "car_soc": data.get("car_soc"),
                "car_plugged": data.get("car_plugged"),
                "car_limit": data.get("car_limit"),
                "p1_l1": data.get("p1_l1"),
                "p1_l2": data.get("p1_l2"),
                "p1_l3": data.get("p1_l3"),
                "ch_l1": data.get("ch_l1"),
                "ch_l2": data.get("ch_l2"),
                "ch_l3": data.get("ch_l3"),
                "zap_limit_value": data.get("zap_limit_value"),
            },
            
            # Price data (critical for reproducing decisions)
            "price_data": {
                "today": data.get("price_data", {}).get("today", []),
                "tomorrow": data.get("price_data", {}).get("tomorrow", []),
                "tomorrow_valid": data.get("price_data", {}).get("tomorrow_valid", False),
                "expected_arrival_time": self._get_expected_price_arrival_time(),
                "arrival_history": self.learning_state.get(LEARNING_PRICE_ARRIVAL, []),
            },
            
            # Calendar events if any
            "calendar_events": data.get("calendar_events", []),
            
            # Session state
            "session_info": {
                "overload_prevention_minutes": self.session_manager.overload_prevention_minutes,
                "session_active": self.session_manager.current_session is not None,
            },
            
            # Efficiency learning state
            "efficiency_learning": {
                "enabled": self.entry.options.get(CONF_CAR_REFRESH_INTERVAL, self.entry.data.get(CONF_CAR_REFRESH_INTERVAL)) in [
                    REFRESH_AT_TARGET, REFRESH_1_HOUR, REFRESH_2_HOURS, REFRESH_3_HOURS, REFRESH_4_HOURS
                ],
                "learned_loss_pct": self.learning_state.get(LEARNING_CHARGER_LOSS, 0.0),
                "confidence": self.learning_state.get(LEARNING_CONFIDENCE, 0),
                "sessions_completed": self.learning_state.get(LEARNING_SESSIONS, 0),
                "locked": self.learning_state.get(LEARNING_LOCKED, False),
                "configured_loss_pct": float(self.entry.options.get(CONF_CHARGER_LOSS, self.entry.data.get(CONF_CHARGER_LOSS, DEFAULT_LOSS))),
                "last_refresh": self.learning_state.get(LEARNING_LAST_REFRESH),
                "measurement_history": self.learning_state.get(LEARNING_HISTORY, []),
                "explanation": self._get_learning_explanation(),
            },
            
            # Latest plan output (for comparison)
            "last_plan": {
                "should_charge_now": data.get("should_charge_now", False),
                "planned_target_soc": data.get("planned_target_soc"),
                "scheduled_start": data.get("scheduled_start"),
                "departure_time": data.get("departure_time"),
                "charging_summary": data.get("charging_summary", ""),
            },
            
            # Entity configurations (for reference)
            "entity_ids": {k: v for k, v in self.conf_keys.items() if v},
        }
        
        # Format as JSON
        json_dump = json.dumps(debug_dump, indent=2, default=str)
        
        # Save to file
        file_path = self.hass.config.path("www", "ev_optimizer_debug_dump.json")
        _LOGGER.debug(f"💾 Saving debug dump to: {file_path}")
        
        try:
            import os
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w') as f:
                f.write(json_dump)
            _LOGGER.info(f"✅ Debug dump saved to: {file_path}")
            _LOGGER.info(f"📥 Download at: /local/ev_optimizer_debug_dump.json")
            
            # Also log to console for easy copy/paste
            _LOGGER.info("=" * 80)
            _LOGGER.info("DEBUG STATE DUMP - Also saved to file above")
            _LOGGER.info("=" * 80)
            _LOGGER.info(json_dump)
            _LOGGER.info("=" * 80)
            
        except Exception as e:
            _LOGGER.error(f"❌ Failed to save debug dump to file: {e}")
            # Still log it even if file save fails
            _LOGGER.info("=" * 80)
            _LOGGER.info("DEBUG STATE DUMP (file save failed, logging only)")
            _LOGGER.info("=" * 80)
            _LOGGER.info(json_dump)
            _LOGGER.info("=" * 80)
        
        return debug_dump

    def dump_custom_scenario(self) -> dict:
        """Dump custom debug scenario using debug input fields.
        
        Uses the 4 debug entities (current_time, departure_time, current_soc, target_soc)
        along with all other current configuration and sensor values.
        If tomorrow's prices are missing, copies today's prices.
        """
        import json
        from datetime import datetime, timedelta
        
        _LOGGER.debug("🔍 Starting custom debug scenario dump...")
        
        # Get debug field values
        debug_current_time = self.user_settings.get(ENTITY_DEBUG_CURRENT_TIME, time(0, 0))
        debug_departure_time = self.user_settings.get(ENTITY_DEBUG_DEPARTURE_TIME, time(7, 0))
        debug_current_soc = self.user_settings.get(ENTITY_DEBUG_CURRENT_SOC, 50.0)
        debug_target_soc = self.user_settings.get(ENTITY_DEBUG_TARGET_SOC, 80.0)
        
        # Build timestamp from debug current time (use today's date)
        now = datetime.now()
        custom_timestamp = now.replace(
            hour=debug_current_time.hour,
            minute=debug_current_time.minute,
            second=0,
            microsecond=0
        )
        
        # Get current data snapshot
        data = self.data if self.data else {}
        
        # Handle price data with fallback
        price_data = data.get("price_data", {})
        today_prices = price_data.get("today", [])
        tomorrow_prices = price_data.get("tomorrow", [])
        tomorrow_valid = price_data.get("tomorrow_valid", False)
        
        # If tomorrow prices missing, fake them by copying today's
        if not tomorrow_prices or not tomorrow_valid:
            _LOGGER.info("⚠️ Tomorrow prices not available, faking with today's prices")
            tomorrow_prices = today_prices.copy() if today_prices else []
            tomorrow_valid = True
        
        # Build comprehensive debug dump
        custom_dump = {
            "timestamp": custom_timestamp.isoformat(),
            "description": "Custom debug scenario using debug input fields",
            "debug_fields_used": {
                "current_time": str(debug_current_time),
                "departure_time": str(debug_departure_time),
                "current_soc": debug_current_soc,
                "target_soc": debug_target_soc,
            },
            
            # Configuration
            "config_settings": self.config_settings.copy(),
            
            # User settings from UI (use debug target, but keep other settings)
            "user_settings": {
                ENTITY_TARGET_SOC: debug_target_soc,  # Use debug target
                ENTITY_MIN_SOC: self.user_settings.get(ENTITY_MIN_SOC, 20),
                ENTITY_DEPARTURE_TIME: str(debug_departure_time),  # Use debug departure
                ENTITY_DEPARTURE_OVERRIDE: str(debug_departure_time),  # Use debug departure
                ENTITY_SMART_SWITCH: self.user_settings.get(ENTITY_SMART_SWITCH, True),
                ENTITY_TARGET_OVERRIDE: debug_target_soc,  # Use debug target
                ENTITY_PRICE_LIMIT_1: self.user_settings.get(ENTITY_PRICE_LIMIT_1, 0.5),
                ENTITY_TARGET_SOC_1: self.user_settings.get(ENTITY_TARGET_SOC_1, 100),
                ENTITY_PRICE_LIMIT_2: self.user_settings.get(ENTITY_PRICE_LIMIT_2, 1.5),
                ENTITY_TARGET_SOC_2: self.user_settings.get(ENTITY_TARGET_SOC_2, 80),
                ENTITY_PRICE_EXTRA_FEE: self.user_settings.get(ENTITY_PRICE_EXTRA_FEE, 0.0),
                ENTITY_PRICE_VAT: self.user_settings.get(ENTITY_PRICE_VAT, 0.0),
            },
            
            # State flags
            "manual_override_active": self.manual_override_active,
            "previous_plugged_state": True,  # Assume plugged for scenario
            
            # Current sensor data (with custom SoC)
            "sensor_data": {
                "car_soc": debug_current_soc,  # Use debug SoC
                "car_plugged": True,  # Assume plugged for scenario
                "car_limit": data.get("car_limit"),
                "p1_l1": data.get("p1_l1"),
                "p1_l2": data.get("p1_l2"),
                "p1_l3": data.get("p1_l3"),
                "ch_l1": data.get("ch_l1"),
                "ch_l2": data.get("ch_l2"),
                "ch_l3": data.get("ch_l3"),
                "zap_limit_value": data.get("zap_limit_value"),
            },
            
            # Price data (with fallback logic applied)
            "price_data": {
                "today": today_prices,
                "tomorrow": tomorrow_prices,
                "tomorrow_valid": tomorrow_valid,
                "prices_faked": not price_data.get("tomorrow_valid", False),
            },
            
            # Calendar events if any
            "calendar_events": data.get("calendar_events", []),
            
            # Efficiency learning state
            "efficiency_learning": {
                "learned_loss_pct": self.learning_state.get(LEARNING_CHARGER_LOSS, 0.0),
                "confidence": self.learning_state.get(LEARNING_CONFIDENCE, 0),
                "locked": self.learning_state.get(LEARNING_LOCKED, False),
                "configured_loss_pct": float(self.entry.options.get(CONF_CHARGER_LOSS, self.entry.data.get(CONF_CHARGER_LOSS, DEFAULT_LOSS))),
            },
        }
        
        # Format as JSON
        json_dump = json.dumps(custom_dump, indent=2, default=str)
        
        # Save to file
        file_path = self.hass.config.path("www", "ev_optimizer_custom_scenario.json")
        _LOGGER.debug(f"💾 Saving custom scenario to: {file_path}")
        
        try:
            import os
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w') as f:
                f.write(json_dump)
            _LOGGER.info(f"✅ Custom scenario saved to: {file_path}")
            _LOGGER.info(f"📥 Download at: /local/ev_optimizer_custom_scenario.json")
            
            # Also log summary
            _LOGGER.info("=" * 80)
            _LOGGER.info("CUSTOM DEBUG SCENARIO SAVED")
            _LOGGER.info("=" * 80)
            _LOGGER.info(f"Current Time: {debug_current_time}")
            _LOGGER.info(f"Departure: {debug_departure_time}")
            _LOGGER.info(f"Current SoC: {debug_current_soc}%")
            _LOGGER.info(f"Target SoC: {debug_target_soc}%")
            _LOGGER.info(f"Tomorrow prices: {'faked (copied from today)' if not price_data.get('tomorrow_valid') else 'real'}")
            _LOGGER.info(f"Download: /local/ev_optimizer_custom_scenario.json")
            _LOGGER.info("=" * 80)
            
        except Exception as e:
            _LOGGER.error(f"❌ Failed to save custom scenario to file: {e}")
        
        return custom_dump

    def _track_price_arrival(self, price_data: dict):
        """Track when tomorrow's prices become available to learn the pattern."""
        tomorrow_valid = price_data.get("tomorrow_valid", False) or bool(price_data.get("tomorrow"))
        
        # Detect transition from no tomorrow prices to having tomorrow prices
        if tomorrow_valid and not self._last_tomorrow_valid:
            now = datetime.now()
            arrival_time = now.strftime("%H:%M")
            
            # Add to history (keep last 14 days)
            price_arrivals = self.learning_state.get(LEARNING_PRICE_ARRIVAL, [])
            price_arrivals.append({
                "date": now.date().isoformat(),
                "time": arrival_time,
                "timestamp": now.isoformat(),
            })
            
            # Keep only last 14 entries
            price_arrivals = price_arrivals[-14:]
            self.learning_state[LEARNING_PRICE_ARRIVAL] = price_arrivals
            
            _LOGGER.info(f"📅 Tomorrow's prices detected at {arrival_time}. Learning pattern ({len(price_arrivals)} samples).")
            
            # Save learning state
            self._save_data()
        
        self._last_tomorrow_valid = tomorrow_valid
    
    def _get_expected_price_arrival_time(self) -> str | None:
        """Calculate expected time when tomorrow's prices typically arrive."""
        price_arrivals = self.learning_state.get(LEARNING_PRICE_ARRIVAL, [])
        
        if len(price_arrivals) < 3:
            return None  # Need at least 3 samples to be confident
        
        # Parse times and calculate average
        from datetime import datetime as dt
        times_in_minutes = []
        
        for entry in price_arrivals:
            try:
                time_str = entry.get("time", "")
                parts = time_str.split(":")
                if len(parts) == 2:
                    hours = int(parts[0])
                    minutes = int(parts[1])
                    total_minutes = hours * 60 + minutes
                    times_in_minutes.append(total_minutes)
            except (ValueError, AttributeError):
                continue
        
        if not times_in_minutes:
            return None
        
        # Calculate average time
        avg_minutes = sum(times_in_minutes) // len(times_in_minutes)
        avg_hours = avg_minutes // 60
        avg_mins = avg_minutes % 60
        
        return f"{avg_hours:02d}:{avg_mins:02d}"

    def _get_learning_explanation(self) -> str:
        """Generate a human-readable explanation of the learning state."""
        # Helper to get config from Options (new) or Data (initial)
        def get_conf(key, default=None):
            return self.entry.options.get(key, self.entry.data.get(key, default))
        
        refresh_mode = get_conf(CONF_CAR_REFRESH_INTERVAL)
        
        # Check if learning is enabled
        if refresh_mode not in [REFRESH_AT_TARGET, REFRESH_1_HOUR, REFRESH_2_HOURS, REFRESH_3_HOURS, REFRESH_4_HOURS]:
            return "Adaptive efficiency learning is DISABLED. Car refresh mode is set to 'Never' or not configured. The system uses the fixed configured loss percentage."
        
        # Learning is enabled
        configured_loss = float(get_conf(CONF_CHARGER_LOSS, DEFAULT_LOSS))
        learned_loss = self.learning_state.get(LEARNING_CHARGER_LOSS, configured_loss)
        confidence = self.learning_state.get(LEARNING_CONFIDENCE, 0)
        sessions = self.learning_state.get(LEARNING_SESSIONS, 0)
        locked = self.learning_state.get(LEARNING_LOCKED, False)
        history = self.learning_state.get(LEARNING_HISTORY, [])
        
        lines = ["Adaptive efficiency learning is ENABLED."]
        lines.append(f"Refresh mode: {refresh_mode}")
        lines.append("")
        
        # Status
        if locked:
            lines.append("Status: LOCKED ✓")
            lines.append(f"The system has converged on {learned_loss:.1f}% efficiency loss with high confidence (8+/10).")
            lines.append(f"Completed {sessions} learning sessions. No longer applying safety buffer or frequent refreshes.")
        elif sessions >= 10:
            lines.append("Status: LEARNING COMPLETE (unlocked)")
            lines.append(f"Completed {sessions} learning sessions but confidence is {confidence}/10 (below lock threshold of 8).")
            lines.append(f"Current efficiency loss: {learned_loss:.1f}% (configured baseline: {configured_loss:.1f}%)")
        elif sessions > 0:
            lines.append(f"Status: ACTIVELY LEARNING ({sessions}/10 sessions)")
            lines.append(f"Current efficiency loss: {learned_loss:.1f}% (started from: {configured_loss:.1f}%)")
            lines.append(f"Confidence: {confidence}/10 (needs 8+ to lock)")
            lines.append("The system adds a 30-minute safety buffer to charging plans during learning.")
        else:
            lines.append("Status: NOT STARTED")
            lines.append(f"Using configured loss: {configured_loss:.1f}%")
            lines.append("Learning will begin on the first charging session with Smart Refresh enabled.")
        
        lines.append("")
        
        # Learning progress
        if sessions > 0:
            lines.append("Learning Progress:")
            lines.append(f"  • Sessions completed: {sessions}")
            lines.append(f"  • Confidence level: {confidence}/10")
            lines.append(f"  • Measurements taken: {len(history)}")
            
            if history:
                lines.append(f"  • Last measurement: {history[-1].get('timestamp', 'unknown')}")
                last_error = history[-1].get('error', 0)
                lines.append(f"  • Last SoC error: {last_error:+.2f}% ({'overperforming' if last_error > 0 else 'underperforming'})")
            
            lines.append("")
            lines.append("How it works:")
            lines.append("  • The system measures actual vs expected SoC during charging")
            lines.append("  • Adjusts loss percentage up (if underperforming) or down (if overperforming)")
            lines.append("  • Confidence increases when measurements are within tolerance")
            lines.append("  • Locks at confidence 8/10 for stable long-term use")
        
        return "\n".join(lines)
