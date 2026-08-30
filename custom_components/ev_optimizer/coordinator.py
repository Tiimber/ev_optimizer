"""DataUpdateCoordinator for EV Optimizer."""

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
    LEARNING_MEASUREMENTS,
    LEARNING_LOCKED,
    LEARNING_HISTORY,
    LEARNING_LAST_REFRESH,
    LEARNING_PRICE_ARRIVAL,
    LEARNING_MODEL_VERSION,
    CURRENT_LEARNING_MODEL,
    MIN_LEARNED_LOSS,
    MAX_LEARNED_LOSS,
    MIN_LEARNING_WINDOW_SOC,
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
from .planner import (
    generate_charging_plan,
    calculate_load_balancing,
    analyze_prices,
    get_effective_charger_loss,
)
from .session_manager import SessionManager
from .snapshot_manager import SnapshotManager

_LOGGER = logging.getLogger(__name__)

# How long to let a car charge-limit write settle before treating a
# disagreement as real. The car reports its limit only when it next phones
# home; a 41-minute delay was observed on a confirmed-good write.
CAR_LIMIT_GRACE = timedelta(minutes=45)


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
        
        # Snapshot Management for debugging
        self.snapshot_manager = SnapshotManager(hass, entry.entry_id)
        self._actions_this_update = []  # Track actions taken in current update

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
        # Diagnostics for the two ways this integration can silently drift
        # from reality: a car limit we believe we set but the car did not
        # take, and an amp reading we substitute when the car stops drawing.
        self._car_limit_drift_logged = False
        self._car_limit_write_time = None
        self._amps_fallback_since = None
        self._amps_fallback_logged = False
        # Public mirrors of the two drift detectors, so they can be surfaced as
        # entities instead of living only in the log.
        self.car_limit_drift_active = False
        self.car_limit_reported = None
        self.charge_estimate_drift_active = False

        # Virtual SoC Estimator
        self._virtual_soc = 0.0
        self._last_update_time = datetime.now()
        self._last_sensor_soc = None  # Track last raw sensor value to detect real updates

        # Efficiency measurement anchors.
        #
        # Learning compares the SoC the model predicted against the SoC the car
        # actually reports, but the car reports only when it next phones home —
        # measured at 41 minutes on a confirmed-good write. Comparing a live
        # estimate against a reading of unknown age is meaningless, so both ends
        # of a measurement are pinned to *real* sensor readings instead: the
        # window runs from one reported SoC to the next, and any constant
        # reporting lag cancels between them.
        self._anchor_sensor_soc = None  # Reported SoC that opened the window
        self._anchor_time = None
        self._anchor_loss_used = None  # Effective loss the estimator ran with
        self._anchor_suspect = False  # Window contains data we should not learn from

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

        # Overload prevention tracking
        self._last_overload_check_time = None

        # Automatic image generation tracking
        self._last_plan_image_signature = None
        self._last_report_generated = False

        # Persistence
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._data_loaded = False

        # Helper to get config from Options (new) or Data (initial)
        def get_conf(key, default=None):
            return entry.options.get(key, entry.data.get(key, default))

        # Initialize user_settings with default values
        # These will be overwritten by persisted values in _load_data if they exist
        # NOTE: Do NOT read these from entry.data/options - they are user settings, not config!
        self.user_settings[ENTITY_DEPARTURE_TIME] = time(7, 0)
        self.user_settings[ENTITY_TARGET_SOC] = DEFAULT_TARGET_SOC
        self.user_settings[ENTITY_MIN_SOC] = DEFAULT_MIN_SOC
        self.user_settings[ENTITY_PRICE_LIMIT_1] = DEFAULT_PRICE_LIMIT_1
        self.user_settings[ENTITY_TARGET_SOC_1] = DEFAULT_TARGET_SOC_1
        self.user_settings[ENTITY_PRICE_LIMIT_2] = DEFAULT_PRICE_LIMIT_2
        self.user_settings[ENTITY_TARGET_SOC_2] = DEFAULT_TARGET_SOC_2

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
            LEARNING_MEASUREMENTS: 0,
            LEARNING_LOCKED: False,
            LEARNING_HISTORY: [],
            LEARNING_LAST_REFRESH: None,
            LEARNING_PRICE_ARRIVAL: [],  # Track when tomorrow's prices arrive
            LEARNING_MODEL_VERSION: CURRENT_LEARNING_MODEL,
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
        # Also track for this update's snapshot
        self._actions_this_update.append(message)

    async def _load_data(self):
        """Load persisted settings from disk with robust error handling."""
        if self._data_loaded:
            return

        try:
            # Load snapshots for debugging
            await self.snapshot_manager.async_load()
            
            data = await self.store.async_load()
            if data:
                self.manual_override_active = data.get("manual_override_active", False)
                self.session_manager.load_from_dict(data)

                # Load learning state
                learning_data = data.get("learning_state", {})
                if learning_data:
                    stored_model = learning_data.get(LEARNING_MODEL_VERSION, 1)
                    if stored_model < CURRENT_LEARNING_MODEL:
                        # Keep the price-arrival history — that one is a plain
                        # observation and stays valid across model changes — but
                        # drop everything fitted by the old estimator.
                        self._migrate_learning_state(learning_data, stored_model)
                    else:
                        self.learning_state.update(learning_data)
                    _LOGGER.info(
                        "Loaded learning state: loss=%.1f%%, confidence=%d/10, "
                        "sessions=%d, measurements=%d, locked=%s",
                        self.learning_state.get(LEARNING_CHARGER_LOSS, 0),
                        self.learning_state.get(LEARNING_CONFIDENCE, 0),
                        self.learning_state.get(LEARNING_SESSIONS, 0),
                        self.learning_state.get(LEARNING_MEASUREMENTS, 0),
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

    def _migrate_learning_state(self, learning_data: dict, stored_model: int):
        """Discard learned values fitted by an older estimator.

        The learned loss is only meaningful relative to the estimator that
        produced the error signal. Carrying it across a change to that
        estimator would keep a number that was never measuring what its name
        claims, and — worse — a stored `locked` flag would stop the corrected
        loop from ever re-deriving it.
        """
        configured_loss = self.learning_state[LEARNING_CHARGER_LOSS]
        preserved = learning_data.get(LEARNING_PRICE_ARRIVAL, [])

        _LOGGER.warning(
            "Efficiency learning state was fitted by model v%d (current v%d) and "
            "has been reset: loss %.1f%% / confidence %s / sessions %s discarded, "
            "restarting from the configured %.1f%%. Price-arrival history kept.",
            stored_model,
            CURRENT_LEARNING_MODEL,
            learning_data.get(LEARNING_CHARGER_LOSS, 0.0),
            learning_data.get(LEARNING_CONFIDENCE, 0),
            learning_data.get(LEARNING_SESSIONS, 0),
            configured_loss,
        )

        self.learning_state.update({
            LEARNING_CHARGER_LOSS: configured_loss,
            LEARNING_CONFIDENCE: 0,
            LEARNING_SESSIONS: 0,
            LEARNING_MEASUREMENTS: 0,
            LEARNING_LOCKED: False,
            LEARNING_HISTORY: [],
            LEARNING_LAST_REFRESH: None,
            LEARNING_PRICE_ARRIVAL: preserved,
            LEARNING_MODEL_VERSION: CURRENT_LEARNING_MODEL,
        })
        self._add_log(
            "Efficiency learning reset: stored values came from an older "
            f"estimator. Restarting from {configured_loss:.1f}% loss."
        )

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
        _LOGGER.debug(f"Setting user input: {key} = {value} (type: {type(value)})")
        self.user_settings[key] = value

        if not internal:
            # Format time objects properly for logging
            if isinstance(value, time):
                value_str = value.strftime("%H:%M")
            else:
                value_str = str(value)
            
            # Use friendly names in log
            friendly_names = {
                ENTITY_DEPARTURE_TIME: "Standard Departure Time",
                ENTITY_DEPARTURE_OVERRIDE: "Next Session Departure",
                ENTITY_TARGET_SOC: "Target SoC",
                ENTITY_MIN_SOC: "Minimum SoC",
                ENTITY_PRICE_LIMIT_1: "Price Limit 1",
                ENTITY_TARGET_SOC_1: "Target SoC 1",
                ENTITY_PRICE_LIMIT_2: "Price Limit 2",
                ENTITY_TARGET_SOC_2: "Target SoC 2",
                ENTITY_TARGET_OVERRIDE: "Next Session Target SoC",
            }
            friendly_key = friendly_names.get(key, key)
            self._add_log(f"{friendly_key} changed to {value_str}")

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

    def _get_plan_signature(self, plan: dict) -> str:
        """Generate a signature from a plan to detect meaningful changes.
        
        This helps avoid regenerating the same plan image multiple times.
        Signature includes: departure time, target SoC, charging schedule slots, summary.
        """
        import hashlib
        import json
        
        # Extract key fields that define a unique plan
        signature_data = {
            "departure_time": plan.get("plan_departure_time", ""),
            "planned_target_soc": plan.get("planned_target_soc", 0),
            "charging_summary": plan.get("charging_summary", ""),
            "should_charge_now": plan.get("should_charge_now", False),
            # Include charging schedule (simplified to start times and active status)
            "schedule": [
                {"start": slot.get("start", ""), "active": slot.get("active", False)}
                for slot in plan.get("charging_schedule", [])
            ]
        }
        
        # Create hash of the signature data
        signature_str = json.dumps(signature_data, sort_keys=True)
        return hashlib.md5(signature_str.encode()).hexdigest()

    async def _auto_generate_plan_image_if_needed(self, plan: dict, is_locked: bool):
        """Automatically generate plan image when appropriate.
        
        Generates image when:
        - Car is plugged in
        - Plan is newly generated (not from locked state)
        - Plan is not waiting for prices
        - Plan has actually changed (different signature)
        """
        # Don't generate for locked plans (charging already started, no changes)
        if is_locked:
            return
        
        # Don't generate if waiting for prices (incomplete plan)
        is_waiting = "Waiting for additional price data" in plan.get("charging_summary", "")
        if is_waiting:
            _LOGGER.debug("⏭️  Skipping plan image: waiting for prices")
            return
        
        # Don't generate if no charging schedule (nothing to show)
        if "charging_schedule" not in plan or not plan["charging_schedule"]:
            return
        
        # Check if plan has changed
        current_signature = self._get_plan_signature(plan)
        if current_signature == self._last_plan_image_signature:
            _LOGGER.debug("⏭️  Skipping plan image: plan unchanged")
            return
        
        # Generate the image
        try:
            _LOGGER.info("📸 Auto-generating plan image (plan changed)")
            
            # Inject fee settings
            data_with_fees = self.data.copy() if self.data else plan.copy()
            data_with_fees.update(plan)
            data_with_fees[ENTITY_PRICE_EXTRA_FEE] = self.user_settings.get(
                ENTITY_PRICE_EXTRA_FEE, 0.0
            )
            data_with_fees[ENTITY_PRICE_VAT] = self.user_settings.get(ENTITY_PRICE_VAT, 0.0)

            save_path = self.hass.config.path("www", "ev_optimizer_plan.png")
            await self.hass.async_add_executor_job(
                generate_plan_image, data_with_fees, save_path
            )
            
            # Update signature to avoid regenerating
            self._last_plan_image_signature = current_signature
            self._add_log("Plan Image Auto-Generated")
            
        except Exception as e:
            _LOGGER.warning(f"Could not auto-generate plan image: {e}")

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
            # Save last_sensor_soc BEFORE _update_virtual_soc modifies it, so we can detect
            # real sensor changes after the fact (the function updates _last_sensor_soc internally).
            _prev_sensor_soc = self._last_sensor_soc
            self._check_car_limit_drift()
            trust_sensor_period = self._update_virtual_soc(data)
            data["car_soc"] = self._virtual_soc
            data["virtual_soc"] = self._virtual_soc
            data["reported_soc"] = self._last_sensor_soc
            data["soc_sensor_refresh"] = trust_sensor_period

            # Delegate Logic to Planner
            data["max_available_current"] = calculate_load_balancing(
                data, self.config_settings["max_fuse"]
            )
            data["current_price_status"] = analyze_prices(
                data["price_data"].get("today", [])
            )

            # Get expected price arrival time from learning
            expected_price_time = self._get_expected_price_arrival_time()

            # Check if sensor SoC has actually updated (unlock plan if it has).
            # Compare the sensor value BEFORE _update_virtual_soc ran (_prev_sensor_soc)
            # against the sensor value AFTER (_last_sensor_soc). Both are set by
            # _update_virtual_soc, so comparing them correctly detects a real sensor change.
            # (The old approach compared data["car_soc"] == _virtual_soc against _last_sensor_soc,
            # both of which were already updated to the same value, so the diff was always 0.)
            sensor_soc_updated = False
            if _prev_sensor_soc is not None and self._last_sensor_soc is not None:
                if abs(self._last_sensor_soc - _prev_sensor_soc) > 0.5:  # >0.5% change
                    sensor_soc_updated = True
                    _LOGGER.info(
                        "🔓 SoC sensor updated (%.1f%% → %.1f%%), unlocking plan",
                        _prev_sensor_soc, self._last_sensor_soc
                    )
                    self._locked_plan = None
                    self._locked_plan_soc = None

            # Use locked plan if available and sensor hasn't updated
            if self._locked_plan is not None and not sensor_soc_updated:
                # Unlock if the locked plan is waiting for prices (should regenerate)
                locked_is_waiting = "Waiting for additional price data" in self._locked_plan.get("charging_summary", "")
                if locked_is_waiting:
                    _LOGGER.info("🔓 Locked plan is waiting for prices - unlocking to regenerate")
                    self._locked_plan = None
                    self._locked_plan_soc = None

            # Safety: if locked plan says "Target reached" but virtual_soc is significantly
            # below the target, the SoC sensor must have corrected downward (BMS recalibration
            # or a stale sensor). Clear the lock so a proper charge plan is generated.
            if self._locked_plan is not None:
                locked_target = float(self._locked_plan.get("planned_target_soc", 0))
                locked_summary = self._locked_plan.get("charging_summary", "")
                if "Target reached" in locked_summary and self._virtual_soc < locked_target - 5:
                    _LOGGER.warning(
                        "⚠️ Locked plan says 'Target reached' but virtual_soc %.1f%% is "
                        "%.1f%% below target %.1f%% — clearing stale maintenance lock",
                        self._virtual_soc, locked_target - self._virtual_soc, locked_target,
                    )
                    self._locked_plan = None
                    self._locked_plan_soc = None
                    sensor_soc_updated = True

            if self._locked_plan is not None and not sensor_soc_updated:
                _LOGGER.debug(
                    "🔒 Using locked plan (locked at %.1f%% SoC, current virtual: %.1f%%)",
                    self._locked_plan_soc, self._virtual_soc
                )
                plan = self._locked_plan.copy()
                # Update should_charge_now based on current time and schedule
                plan = self._update_locked_plan_status(plan)
                plan_is_locked = True
            else:
                # Generate new plan
                plan = generate_charging_plan(
                    data, self.config_settings, self.manual_override_active, 
                    learning_state=self.learning_state, 
                    overload_prevention_minutes=self.session_manager.overload_prevention_minutes,
                    expected_price_time=expected_price_time
                )
                plan_is_locked = False
                
                # Lock the plan when charging starts for the first time
                # BUT: Don't lock "waiting for prices" plans - they need to be regenerated when prices arrive
                is_waiting_for_prices = "Waiting for additional price data" in plan.get("charging_summary", "")
                if plan["should_charge_now"] and self._locked_plan is None and not is_waiting_for_prices:
                    self._locked_plan = plan.copy()
                    self._locked_plan_soc = self._virtual_soc
                    _LOGGER.info(
                        "🔒 Locking charging plan at %.1f%% SoC (plan will remain stable until SoC updates)",
                        self._locked_plan_soc
                    )
                
                # Auto-generate plan image when car is plugged and plan is ready
                if data.get("car_plugged", False):
                    await self._auto_generate_plan_image_if_needed(plan, is_locked=False)

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
            
            # Capture snapshot for debugging (async, non-blocking)
            await self.snapshot_manager.capture_snapshot(
                coordinator_data=data,
                plan=plan,
                actions_taken=self._actions_this_update.copy() if self._actions_this_update else None
            )
            self._actions_this_update = []  # Reset for next update

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
        
        # CRITICAL: Force refresh when first entering maintenance mode to get accurate final SoC
        # This prevents graph dips from stale sensor values
        maintenance_now = "Maintenance mode active" in plan.get("charging_summary", "")
        if maintenance_now and self._last_applied_state == "charging":
            _LOGGER.debug("📊 Entering maintenance mode - triggering immediate refresh for final SoC")
            await self._trigger_car_refresh(svc, ent, trigger_learning=False)
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
            should_refresh, trigger_learning = self._should_trigger_smart_refresh(plan, delta)
            if should_refresh:
                await self._trigger_car_refresh(svc, ent, trigger_learning=trigger_learning)
            return

        if should_refresh:
            await self._trigger_car_refresh(svc, ent, trigger_learning=False)

    def _should_trigger_smart_refresh(self, plan: dict, time_since_last: timedelta) -> tuple[bool, bool]:
        """Determine if smart refresh should be triggered for efficiency learning.
        
        Returns:
            tuple[bool, bool]: (should_refresh, trigger_learning)
        """
        # Only for smart refresh mode
        if not self.session_manager.current_session:
            return (False, False)  # Not charging
        
        planned_end = plan.get("session_end_time")
        if not planned_end:
            # Fallback: refresh if at target and time has passed (but no learning)
            if self._virtual_soc >= float(plan.get("planned_target_soc", 80)):
                if time_since_last > timedelta(hours=12):
                    return (True, False)  # Refresh but no learning
            return (False, False)
        
        # Parse session_end_time if it's a string
        if isinstance(planned_end, str):
            try:
                planned_end = datetime.fromisoformat(planned_end)
            except Exception:
                return (False, False)
        
        now = datetime.now()
        session_start_str = self.session_manager.current_session.get("start_time")
        if session_start_str:
            session_start = datetime.fromisoformat(session_start_str) if isinstance(session_start_str, str) else session_start_str
            session_duration_minutes = (planned_end - session_start).total_seconds() / 60
        else:
            session_duration_minutes = 0
        
        time_to_end_minutes = (planned_end - now).total_seconds() / 60
        
        # Don't refresh if session hasn't started or already ended
        if time_to_end_minutes < 0 or time_to_end_minutes > 300:  # More than 5 hours
            return (False, False)
        
        # Only do learning if session is at least 60 minutes
        if session_duration_minutes < 60:
            _LOGGER.debug("📊 Skipping learning: session too short (%.0f min)", session_duration_minutes)
            # Still refresh at target if needed, but no learning
            if self._virtual_soc >= float(plan.get("planned_target_soc", 80)):
                if time_since_last > timedelta(hours=1):
                    return (True, False)
            return (False, False)
        
        sessions = self.learning_state.get(LEARNING_SESSIONS, 0)
        locked = self.learning_state.get(LEARNING_LOCKED, False)
        last_refresh_str = self.learning_state.get(LEARNING_LAST_REFRESH)
        
        # Convert last_refresh from ISO string if needed
        last_refresh = None
        if last_refresh_str:
            if isinstance(last_refresh_str, str):
                try:
                    last_refresh = datetime.fromisoformat(last_refresh_str)
                except ValueError:
                    _LOGGER.error(
                        "Could not parse stored last_refresh %r; treating the "
                        "plan as stale", last_refresh_str,
                    )
            elif isinstance(last_refresh_str, datetime):
                last_refresh = last_refresh_str
        
        minutes_since_last = 999
        if last_refresh:
            minutes_since_last = (now - last_refresh).total_seconds() / 60
        
        if locked or sessions >= 10:
            # Locked phase: single refresh 30min before completion for verification
            if 25 < time_to_end_minutes <= 35 and minutes_since_last > 30:
                _LOGGER.debug("📊 Locked: Refresh 30min before end for verification")
                return (True, True)  # Refresh WITH learning
        else:
            # Learning phase: refresh 30min before end WITH learning
            if 25 < time_to_end_minutes <= 35 and minutes_since_last > 30:
                _LOGGER.debug("📊 Learning: Refresh 30min before end")
                return (True, True)  # Refresh WITH learning
        
        # If we're at target SoC and haven't refreshed recently, do it (but no learning)
        if self._virtual_soc >= float(plan.get("planned_target_soc", 80)):
            if time_since_last > timedelta(hours=1):
                _LOGGER.debug("📊 Target reached: Refresh for confirmation (no learning)")
                return (True, False)  # Refresh but NO learning
        
        return (False, False)

    async def _trigger_car_refresh(self, service: str, entity_id: str, trigger_learning: bool = False):
        try:
            # Record the estimate at every refresh, not only the learning ones.
            # _update_virtual_soc compares the incoming reading against this to
            # decide whether the refresh actually produced a new value, and a
            # baseline left over from some earlier refresh made that verdict
            # arbitrary.
            self._soc_before_refresh = self._virtual_soc
            if trigger_learning:
                self._add_log(f"Forcing Car Refresh for Learning (Virtual SoC: {self._virtual_soc:.1f}%)")
            else:
                self._add_log(f"Forcing Car Refresh (Virtual SoC: {self._virtual_soc:.1f}%)")
            
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
            
            # A forced refresh no longer schedules its own evaluation. Its job
            # is to produce a fresh reading; the measurement is taken when that
            # reading lands, in _update_virtual_soc, where the interval it
            # covers is actually known.
            if trigger_learning:
                interval_mode = self.conf_keys.get("refresh_int", REFRESH_NEVER)
                if interval_mode == REFRESH_AT_TARGET and self.session_manager.current_session:
                    self.learning_state[LEARNING_LAST_REFRESH] = refresh_time.isoformat()
        except Exception as e:
            _LOGGER.error(f"Failed to force refresh car: {e}")

    def _reset_learning_window(self):
        """Drop the open measurement window without taking a measurement."""
        self._anchor_sensor_soc = None
        self._anchor_time = None
        self._anchor_loss_used = None
        self._anchor_suspect = False

    def _open_learning_window(self, sensor_soc: float, when: datetime):
        """Anchor a new measurement window at a real sensor reading."""
        self._anchor_sensor_soc = sensor_soc
        self._anchor_time = when
        self._anchor_loss_used, _ = get_effective_charger_loss(
            self.config_settings, self.learning_state
        )
        self._anchor_suspect = False

    def _close_learning_window(self, sensor_soc: float, when: datetime):
        """Compare the model's prediction against the car's own answer.

        Both ends of the window are real reported readings, so the car's
        reporting lag cancels out and no assumption about when the reading was
        taken is needed. `self._virtual_soc` still holds the prediction for
        this instant — the caller snaps it to the sensor immediately after.
        """
        if self._anchor_sensor_soc is None or self._anchor_loss_used is None:
            return

        predicted_gain = self._virtual_soc - self._anchor_sensor_soc
        observed_gain = sensor_soc - self._anchor_sensor_soc

        if self._anchor_suspect:
            _LOGGER.debug(
                "📊 Discarding measurement window: it contains unmeasured or "
                "clamped charge (predicted %.1f%%, observed %.1f%%)",
                predicted_gain, observed_gain,
            )
            return

        if predicted_gain < MIN_LEARNING_WINDOW_SOC:
            _LOGGER.debug(
                "📊 Skipping measurement: window too small (%.1f%% predicted, "
                "need %.1f%%)", predicted_gain, MIN_LEARNING_WINDOW_SOC,
            )
            return

        if observed_gain < 0:
            # The car was driven, or the BMS recalibrated downward. Either way
            # this window did not measure charging.
            _LOGGER.debug(
                "📊 Skipping measurement: SoC fell %.1f%% during the window",
                observed_gain,
            )
            return

        self._apply_efficiency_measurement(
            predicted_gain=predicted_gain,
            observed_gain=observed_gain,
            loss_used=self._anchor_loss_used,
            when=when,
        )

    def _apply_efficiency_measurement(
        self,
        predicted_gain: float,
        observed_gain: float,
        loss_used: float,
        when: datetime,
    ):
        """Fold one measurement into the learned loss.

        The estimator adds `raw_gain * (1 - loss/100)` per cycle, so over a
        window `predicted = raw * (1 - loss_used/100)` while the truth is
        `observed = raw * (1 - loss_true/100)`. Eliminating the unobservable
        raw gain gives a closed-form estimate of the real loss:

            loss_true = 100 * (1 - (observed / predicted) * (1 - loss_used/100))

        That single-shot estimate is noisy, so it is blended into the running
        value with a step that shrinks as confidence grows, rather than nudged
        by a fixed ladder.
        """
        efficiency_used = 1.0 - (loss_used / 100.0)
        implied_loss = 100.0 * (1.0 - (observed_gain / predicted_gain) * efficiency_used)
        implied_loss = max(MIN_LEARNED_LOSS, min(MAX_LEARNED_LOSS, implied_loss))

        measurements = self.learning_state.get(LEARNING_MEASUREMENTS, 0)
        confidence = self.learning_state.get(LEARNING_CONFIDENCE, 0)
        current_loss = self.learning_state.get(LEARNING_CHARGER_LOSS, loss_used)

        residual = implied_loss - current_loss

        # Move fast while the estimate is still unformed, slowly once it is.
        alpha = 0.5 if measurements < 5 else (0.3 if confidence < 6 else 0.15)
        new_loss = current_loss + alpha * residual
        new_loss = max(MIN_LEARNED_LOSS, min(MAX_LEARNED_LOSS, new_loss))

        # Margin tightens as the estimate settles.
        if confidence < 3:
            margin = 3.0
        elif confidence < 6:
            margin = 2.0
        else:
            margin = 1.0

        if abs(residual) <= margin:
            confidence += 1
            reason = f"Within {margin:.1f}% margin, confidence increased"
        elif residual > 0:
            confidence = max(0, confidence - 1)
            reason = f"Under-delivering: implied loss {implied_loss:.1f}%, raising estimate"
        else:
            confidence = max(0, confidence - 1)
            reason = f"Over-delivering: implied loss {implied_loss:.1f}%, lowering estimate"

        confidence = min(10, confidence)
        measurements += 1
        locked = confidence >= 8
        if locked and not self.learning_state.get(LEARNING_LOCKED, False):
            reason += " - LOCKED IN"

        self.learning_state.update({
            LEARNING_CHARGER_LOSS: new_loss,
            LEARNING_CONFIDENCE: confidence,
            LEARNING_MEASUREMENTS: measurements,
            LEARNING_LOCKED: locked,
            LEARNING_MODEL_VERSION: CURRENT_LEARNING_MODEL,
        })

        history = self.learning_state.get(LEARNING_HISTORY, [])
        history.append({
            "timestamp": when.isoformat(),
            "predicted_gain": round(predicted_gain, 2),
            "observed_gain": round(observed_gain, 2),
            "loss_used_pct": round(loss_used, 1),
            "implied_loss_pct": round(implied_loss, 1),
            "new_loss_pct": round(new_loss, 1),
            "confidence": confidence,
        })
        self.learning_state[LEARNING_HISTORY] = history[-10:]

        self._add_log(
            f"Efficiency Learning: {reason}. "
            f"Loss now {new_loss:.1f}% (confidence {confidence}/10). "
            f"Predicted +{predicted_gain:.1f}%, measured +{observed_gain:.1f}%"
        )

        _LOGGER.info(
            "🎓 Learning: predicted +%.2f%% vs measured +%.2f%% over the window "
            "(ran at %.1f%% loss) → implied %.1f%%, blended to %.1f%% "
            "(confidence %d/10, measurements %d, locked=%s) - %s",
            predicted_gain, observed_gain, loss_used, implied_loss,
            new_loss, confidence, measurements, locked, reason,
        )

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

    def _check_car_limit_drift(self):
        """Warn when the car's reported charge limit disagrees with the one
        we believe we applied.

        Setting the limit is fire-and-forget: a successful service call only
        means Home Assistant accepted it, not that the car did. The virtual
        SoC clamp uses _last_applied_car_limit, so a silent disagreement lets
        the estimate run above the car's real ceiling.
        """
        entity_id = self.conf_keys.get("car_limit")
        if not entity_id or self._last_applied_car_limit <= 0:
            # Nothing applied, or it was reset when charging paused. Forget
            # any earlier mismatch, otherwise the stale flag suppresses the
            # next real one (this check only logs on transitions).
            self._car_limit_drift_logged = False
            self.car_limit_drift_active = False
            return

        # The car only reports its limit when it next phones home. Measured
        # at ~41 minutes in practice, so a fresh disagreement means nothing
        # — it is the ones that persist past that which matter.
        if self._car_limit_write_time is None:
            return
        if datetime.now() - self._car_limit_write_time < CAR_LIMIT_GRACE:
            return
        state_obj = self.hass.states.get(entity_id)
        if state_obj is None or state_obj.state in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
            return
        try:
            actual = float(state_obj.state)
        except (TypeError, ValueError):
            return

        drifted = abs(actual - self._last_applied_car_limit) > 0.5
        self.car_limit_reported = actual
        self.car_limit_drift_active = drifted
        if drifted == self._car_limit_drift_logged:
            return  # only log the transitions, not every cycle
        self._car_limit_drift_logged = drifted
        if drifted:
            _LOGGER.error(
                "Car charge limit mismatch: we applied %s%% %s ago but %s "
                "still reports %s%%. The virtual SoC clamp uses the applied "
                "value, so the estimate may run above the car's real limit.",
                self._last_applied_car_limit,
                datetime.now() - self._car_limit_write_time,
                entity_id, actual,
            )
        else:
            _LOGGER.info(
                "Car charge limit now agrees with %s at %s%%", entity_id, actual
            )

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

        # Treat both "charging" and "maintenance" as actively connected (car plugged in)
        # Don't trust stale downward sensor movements in either state
        actively_charging = self._last_applied_state in ("charging", "maintenance")
        
        # Check if sensor value actually changed (indicating a real update from the car)
        sensor_value_changed = (
            sensor_soc_f is not None 
            and self._last_sensor_soc is not None 
            and abs(sensor_soc_f - self._last_sensor_soc) > 0.1  # More than 0.1% difference
        )
        
        if sensor_soc_f is not None:
            # During active charging/maintenance: only trust sensor when:
            # 1. It's a forced refresh window, OR
            # 2. The sensor value actually changed (real update from car), OR
            # 3. Virtual SoC is uninitialized
            # This prevents wobbling from re-reading the same stale sensor value
            if actively_charging:
                if trust_sensor_period or sensor_value_changed or self._virtual_soc == 0.0:
                    # A fresh reading closes the current measurement window: the
                    # model's prediction and the car's own answer now describe
                    # the same interval, so this is the one moment where the two
                    # can be compared without guessing at the reporting lag.
                    if sensor_value_changed:
                        self._close_learning_window(sensor_soc_f, current_time)
                    self._virtual_soc = sensor_soc_f
                    self._open_learning_window(sensor_soc_f, current_time)
                    _LOGGER.debug(
                        f"Virtual SoC updated to {sensor_soc_f:.1f}% while plugged in "
                        f"(forced_refresh={trust_sensor_period}, value_changed={sensor_value_changed})"
                    )
                # else: ignore unchanged sensor value while plugged in; rely purely on virtual estimator
            else:
                # When not charging the sensor is always the better source: the
                # car may have been driven or charged elsewhere. Follow it in
                # both directions, and start a fresh window — energy that did
                # not come through this charger tells us nothing about it.
                self._virtual_soc = sensor_soc_f
                self._open_learning_window(sensor_soc_f, current_time)

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

            # The fallback to the *commanded* current exists because the
            # charger's per-phase sensors can lag. But if the car has simply
            # stopped accepting charge (it hit its own limit, or ended the
            # session) the measured current stays near zero indefinitely,
            # and every cycle from then on adds energy that never flowed.
            if measured_amps > 0.5:
                self._amps_fallback_since = None
                self._amps_fallback_logged = False
                self.charge_estimate_drift_active = False
            elif self._last_applied_amps > 0:
                # Energy we believe flowed but did not measure must not teach
                # the model anything.
                self._anchor_suspect = True
                if self._amps_fallback_since is None:
                    self._amps_fallback_since = current_time
                elif (
                    not self._amps_fallback_logged
                    and current_time - self._amps_fallback_since > timedelta(minutes=5)
                ):
                    self._amps_fallback_logged = True
                    self.charge_estimate_drift_active = True
                    _LOGGER.error(
                        "Charger has measured <0.5A for over 5 minutes while %sA "
                        "was commanded; the virtual SoC is still being advanced "
                        "using the commanded value and may now be too high. "
                        "Measured phases: %.2f/%.2f/%.2f A",
                        self._last_applied_amps, ch_l1, ch_l2, ch_l3,
                    )

            if used_amps > 0:
                seconds_passed = (current_time - self._last_update_time).total_seconds()
                hours_passed = seconds_passed / 3600.0
                estimated_power_kw = (3 * 230 * used_amps) / 1000.0
                # The estimator and the planner must run on the same efficiency
                # figure. They used to disagree — this read the raw value from
                # entry.data, ignoring both the options flow and everything
                # learning had derived — which left the learning loop adjusting
                # a number that never fed the quantity it was measuring.
                efficiency_pct, _ = get_effective_charger_loss(
                    self.config_settings, self.learning_state
                )
                efficiency_factor = 1.0 - (efficiency_pct / 100.0)
                added_kwh = estimated_power_kw * hours_passed * efficiency_factor

                if self.car_capacity > 0:
                    added_percent = (added_kwh / self.car_capacity) * 100.0
                    self._virtual_soc += added_percent
                    # A clamp truncates the prediction, so the window would look
                    # like an over-prediction that never happened.
                    if self._last_applied_car_limit > 0:
                        if self._virtual_soc > self._last_applied_car_limit:
                            self._virtual_soc = float(self._last_applied_car_limit)
                            self._anchor_suspect = True
                    if self._virtual_soc > 100.0:
                        self._virtual_soc = 100.0
                        self._anchor_suspect = True

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
                    # Track actual minutes lost to overload prevention
                    # (handles variable update intervals from P1 listener triggers)
                    now = datetime.now()
                    if self._last_overload_check_time is not None:
                        elapsed_seconds = (now - self._last_overload_check_time).total_seconds()
                        elapsed_minutes = elapsed_seconds / 60.0
                        self.session_manager.add_overload_minutes(elapsed_minutes)
                    self._last_overload_check_time = now
                should_charge = False
            else:
                # Reset the overload check timer when not in overload state
                self._last_overload_check_time = None

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
                    self._car_limit_write_time = datetime.now()
                    self._add_log(f"Set Car Limit: {target_soc}%")
                except Exception:
                    # Non-blocking: the charge plan is still valid without the
                    # car limit. But it must not fail silently — the virtual
                    # SoC clamp trusts _last_applied_car_limit.
                    _LOGGER.error(
                        "Failed to set car charge limit to %s%% via %s",
                        target_soc, self.conf_keys["car_limit"], exc_info=True,
                    )
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
                        self._add_log(f"Setting charger to {int(target_amps)}A")
                    elif self.conf_keys.get("zap_resume"):
                        await self.hass.services.async_call(
                            "button",
                            "press",
                            {"entity_id": self.conf_keys["zap_resume"]},
                            blocking=True,
                        )
                        self._add_log("Sent Resume command")
                        self._add_log(f"Setting charger to {int(target_amps)}A")
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
                            f"Adjusted charger to {int(target_amps)}A (Load Balancing)"
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

            # Start a fresh measurement window; whatever happened to the pack
            # while it was away from this charger tells us nothing about it.
            self._reset_learning_window()

            # Reset overload prevention timer for new session
            self._last_overload_check_time = None
            
            # Reset automatic image generation tracking for new session
            self._last_plan_image_signature = None
            
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
                _LOGGER.error("Failed to reset session state on plug-in", exc_info=True)

        if not is_plugged and self.previous_plugged_state:
            self._finalize_session(final_soc=data.get("car_soc"))
            self.manual_override_active = False
            
            # Clear image generation tracking for next session
            self._last_plan_image_signature = None
            
            # Clear locked plan when car unplugs
            self._locked_plan = None
            self._locked_plan_soc = None

            self._reset_learning_window()

            # Reset overload prevention timer
            self._last_overload_check_time = None

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
                except Exception:
                    _LOGGER.error(
                        "Failed to turn off charger switch %s",
                        self.conf_keys["zap_switch"], exc_info=True,
                    )
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
        
        # The single place a completed session is counted. The efficiency
        # evaluator used to increment this too, so one plug-in could add two or
        # more, which ran the count past the 10-session threshold — and dropped
        # the learning-phase safety buffer — after a handful of real sessions.
        # Measurements are counted separately, as LEARNING_MEASUREMENTS.
        interval_mode = self.conf_keys.get("refresh_int", REFRESH_NEVER)
        if interval_mode == REFRESH_AT_TARGET and report:
            sessions = self.learning_state.get(LEARNING_SESSIONS, 0) + 1
            self.learning_state[LEARNING_SESSIONS] = sessions
            _LOGGER.info("📚 Learning session %d completed", sessions)
            self._save_data()
        
        if report:
            try:
                _LOGGER.info("📸 Auto-generating session report image (car unplugged)")
                save_path = self.hass.config.path(
                    "www", "ev_optimizer_last_session.png"
                )
                self.hass.async_add_executor_job(generate_report_image, report, save_path)
                self._add_log("Session Report Image Auto-Generated")
            except Exception as e:
                _LOGGER.warning(f"Could not auto-generate report image: {e}")
    
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
                "measurements_taken": self.learning_state.get(LEARNING_MEASUREMENTS, 0),
                "model_version": self.learning_state.get(LEARNING_MODEL_VERSION),
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
                "departure_time": data.get("plan_departure_time"),
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
        measurements = self.learning_state.get(LEARNING_MEASUREMENTS, 0)
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
        if sessions > 0 or measurements > 0:
            lines.append("Learning Progress:")
            lines.append(f"  • Sessions completed: {sessions}")
            lines.append(f"  • Measurements taken: {measurements}")
            lines.append(f"  • Confidence level: {confidence}/10")

            if history:
                last = history[-1]
                lines.append(f"  • Last measurement: {last.get('timestamp', 'unknown')}")
                predicted = last.get("predicted_gain")
                observed = last.get("observed_gain")
                if predicted is not None and observed is not None:
                    lines.append(
                        f"  • Last window: predicted +{predicted:.1f}%, "
                        f"measured +{observed:.1f}% "
                        f"(implied loss {last.get('implied_loss_pct', 0):.1f}%)"
                    )

            lines.append("")
            lines.append("How it works:")
            lines.append("  • Each measurement runs between two SoC readings the car actually reported,")
            lines.append("    so the car's reporting delay cancels out instead of skewing the result")
            lines.append("  • The predicted gain over that window is compared with the measured gain,")
            lines.append("    which gives a direct estimate of the real loss")
            lines.append("  • That estimate is blended into the running value, in smaller steps as")
            lines.append("    confidence grows; confidence rises while measurements agree")
            lines.append("  • Windows containing unmeasured or clamped charge are discarded")
            lines.append("  • Locks at confidence 8/10 for stable long-term use")

        return "\n".join(lines)
