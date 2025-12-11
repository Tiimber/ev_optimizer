"""DataUpdateCoordinator for EV Smart Charger."""
from __future__ import annotations

import logging
import math
import re
from datetime import timedelta, datetime, time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.storage import Store
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, SERVICE_TURN_ON, SERVICE_TURN_OFF

from .const import (
    DOMAIN,
    CONF_CAR_SOC_SENSOR,
    CONF_CAR_PLUGGED_SENSOR,
    CONF_CAR_CAPACITY,
    CONF_CAR_CHARGING_LEVEL_ENTITY,
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
        self.user_settings = {} # Storage for UI inputs
        self.action_log = []    # Rolling log of actions
        
        # State tracking to prevent API spamming
        self._last_applied_amps = -1
        self._last_applied_state = None # "charging" or "paused"
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
            "car_limit": get_conf(CONF_CAR_CHARGING_LEVEL_ENTITY), 
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
        }

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )

    def _add_log(self, message: str):
        """Add an entry to the action log and prune old entries."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {message}"
        self.action_log.insert(0, entry) # Prepend newest
        
        # Keep only last 50 events
        if len(self.action_log) > 50:
            self.action_log.pop()

    async def _load_data(self):
        """Load persisted settings from disk."""
        if self._data_loaded:
            return
        
        try:
            data = await self.store.async_load()
            if data:
                # Restore Override Flag
                self.manual_override_active = data.get("manual_override_active", False)
                
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
                self._add_log("System started. Settings loaded.")
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
                "user_settings": clean_settings
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
            
            # 3. Handle Plugged-In Event (MOVED TO TOP)
            # This ensures we detect plug-in and sync Virtual SoC BEFORE calculating plans
            await self._handle_plugged_event(data["car_plugged"], data)
            
            # 4. Update Virtual SoC (Handle stale sensors)
            self._update_virtual_soc(data)
            # OVERWRITE the sensor soc with our better estimate if needed
            # This ensures the planning logic runs on the most accurate number we have
            data["car_soc"] = self._virtual_soc
            
            # 5. Fetch Calendar Events
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

            # 6. Logic: Load Balancing
            data["max_available_current"] = self._calculate_load_balancing(data)

            # 7. Logic: Price Analysis (Simple status)
            data["current_price_status"] = self._analyze_prices(data["price_data"])

            # 8. Logic: Smart Charging Plan (The Brain)
            plan = self._generate_charging_plan(data)
            data.update(plan)

            # 9. ACTUATION: Apply logic to physical charger AND car
            await self._apply_charger_control(data, plan)
            
            # Attach log to data so Sensor can read it
            data["action_log"] = self.action_log

            return data

        except Exception as err:
            _LOGGER.error(f"Error in EV Coordinator: {err}")
            raise UpdateFailed(f"Error communicating with API: {err}")

    def _update_virtual_soc(self, data: dict):
        """Update the internal estimated SoC based on charging activity."""
        current_time = datetime.now()
        sensor_soc = data.get("car_soc")
        
        # 1. Sync Logic
        # Sync if sensor is valid AND (Higher than estimate OR we are not charging)
        # This allows the car's real sensor to correct our drift upwards, but prevents it from dragging us down if it's stale.
        if sensor_soc is not None:
            if sensor_soc > self._virtual_soc or self._virtual_soc == 0.0 or self._last_applied_state != "charging":
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
            used_amps = measured_amps if measured_amps > 0.5 else self._last_applied_amps
            
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
                    # This prevents the estimator from drifting past the point where the car stopped itself.
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
                self._add_log(f"Safety Cutoff: Available {safe_amps}A is below minimum 6A. Pausing.")
            should_charge = False
            
        # Determine Target Amps based on State
        # If paused, we force 0A. If charging, we use calculated safe amps.
        target_amps = safe_amps if should_charge else 0
        
        # 2. Control Car Charge Limit
        if self.conf_keys["car_limit"]:
            target_soc = int(plan.get("planned_target_soc", 80))
            if target_soc != self._last_applied_car_limit:
                try:
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": self.conf_keys["car_limit"], "value": target_soc},
                        blocking=True
                    )
                    self._last_applied_car_limit = target_soc
                    self._add_log(f"Set Car Charge Limit to {target_soc}%")
                except Exception as e:
                    _LOGGER.error(f"Failed to set Car Charge Limit: {e}")

        # 3. Control Start/Stop (Switch Logic)
        desired_state = "charging" if should_charge else "paused"
        
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
                            "switch", SERVICE_TURN_ON,
                            {"entity_id": self.conf_keys["zap_switch"]},
                            blocking=True
                        )
                        self._add_log(f"Switched Charging state to: CHARGING")
                    elif self.conf_keys.get("zap_resume"):
                         # Fallback button
                        await self.hass.services.async_call(
                            "button", "press",
                            {"entity_id": self.conf_keys["zap_resume"]},
                            blocking=True
                        )
                        self._add_log("Sent Resume command")
                    
                    self._last_applied_state = "charging"
                except Exception as e:
                    _LOGGER.error(f"Failed to switch Zaptec state to CHARGING: {e}")

            # B. Control Current Limiter
            if target_amps != self._last_applied_amps and self.conf_keys["zap_limit"]:
                try:
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": self.conf_keys["zap_limit"], "value": target_amps},
                        blocking=True
                    )
                    self._last_applied_amps = target_amps
                    self._add_log(f"Load Balancing: Set Zaptec limit to {target_amps}A")
                except Exception as e:
                    _LOGGER.error(f"Failed to set Zaptec limit: {e}")

        else:
            # ---> PAUSING SEQUENCE <---
            
            # A. Set Amps to 0 first (Soft Stop)
            if target_amps != self._last_applied_amps and self.conf_keys["zap_limit"]:
                try:
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": self.conf_keys["zap_limit"], "value": 0},
                        blocking=True
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
                            "switch", SERVICE_TURN_OFF,
                            {"entity_id": self.conf_keys["zap_switch"]},
                            blocking=True
                        )
                        self._add_log(f"Switched Charging state to: PAUSED")
                    elif self.conf_keys.get("zap_stop"):
                        # Fallback button
                        await self.hass.services.async_call(
                            "button", "press",
                            {"entity_id": self.conf_keys["zap_stop"]},
                            blocking=True
                        )
                        self._add_log("Sent Stop command")
                    
                    self._last_applied_state = "paused"
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
        
        # Check SoC Sensor Validity
        soc_entity = self.conf_keys["car_soc"]
        if soc_entity:
            soc_state = self.hass.states.get(soc_entity)
            if soc_state is None or soc_state.state in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
                 data["car_soc"] = None # Mark as unknown
            else:
                try:
                    data["car_soc"] = float(soc_state.state)
                except ValueError:
                    data["car_soc"] = None
        else:
            data["car_soc"] = 0.0 # No sensor configured
        
        # Fetch Charger Current if configured
        data["ch_l1"] = get_float(self.conf_keys.get("ch_l1"))
        data["ch_l2"] = get_float(self.conf_keys.get("ch_l2"))
        data["ch_l3"] = get_float(self.conf_keys.get("ch_l3"))
        
        plugged_state = get_state(self.conf_keys["car_plugged"])
        # Handle state object being None safely
        if plugged_state:
            data["car_plugged"] = plugged_state.state in ["on", "true", "connected", "charging", "full", "plugged_in"]
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
            
            # Sync Virtual SoC to Sensor immediately on plug-in
            if data.get("car_soc") is not None:
                self._virtual_soc = data["car_soc"]
            else:
                self._virtual_soc = 0.0 # Start fresh if unknown
            
            soc_entity = self.conf_keys["car_soc"]
            try:
                await self.hass.services.async_call(
                    "homeassistant", "update_entity", {"entity_id": soc_entity}, blocking=False
                )
            except Exception as e:
                _LOGGER.warning(f"Failed to force update car sensor: {e}")

        # Case B: Just Unplugged -> Reset Overrides to Standards
        if not is_plugged and self.previous_plugged_state:
            self._add_log("Car unplugged. Resetting settings.")
            
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
                        "switch", SERVICE_TURN_OFF,
                        {"entity_id": self.conf_keys["zap_switch"]},
                        blocking=True
                    )
                    self._add_log("Unplugged: Forced Zaptec Switch OFF (Paused).")
                except Exception as e:
                    _LOGGER.error(f"Failed to force Zaptec off: {e}")
            
            # Force State Reset so next plug-in starts fresh logic
            self._last_applied_state = "paused" 
            self._last_applied_car_limit = -1

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

    def _get_calendar_data(self, data: dict, now: datetime) -> tuple[datetime | None, float | None]:
        """Check for relevant calendar event. Returns (departure_time, target_soc)."""
        events = data.get("calendar_events", [])
        if not events:
            return None, None
            
        # Limit window: End of tomorrow
        limit = datetime.combine(now.date() + timedelta(days=1), time.max)
        
        # Sort by start time to find the earliest valid next event
        sorted_events = sorted(events, key=lambda x: x.get("start"))
        
        for event in sorted_events:
            start_str = event.get("start")
            
            # Sometimes it's a dict (Google Cal style), sometimes ISO string (Local Cal style)
            if isinstance(start_str, dict):
                start_str = start_str.get("dateTime", start_str.get("date"))
            
            if not start_str:
                continue
                
            try:
                # Handle YYYY-MM-DD (All day)
                if len(start_str) == 10:
                    evt_start = datetime.fromisoformat(start_str)
                else:
                    evt_start = datetime.fromisoformat(start_str)
                    if evt_start.tzinfo is not None:
                        evt_start = evt_start.replace(tzinfo=None) # Simple naive comparison for now

                if evt_start < now:
                    continue # Skip past events
                if evt_start > limit:
                    break # Passed our window
                
                # Found the next valid event!
                
                # Look for Target SoC in description or summary
                text = f"{event.get('summary', '')} {event.get('description', '')}"
                match = re.search(r"(\d+)\s*%", text)
                
                target_soc = None
                if match:
                    val = int(match.group(1))
                    if 10 <= val <= 100:
                        target_soc = float(val)
                
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
            "charging_summary": "Not calculated"
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
                plan["charging_summary"] = "Load Balancing Mode (No Price Sensor configured)."
            else:
                plan["charging_summary"] = "Error: Price sensor configured but no data received."
            
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
            if not price_list: return []
            
            # Determine if 60 or 15 minute resolution based on list length
            # 24 = Hourly, 96 = 15-Min
            interval_min = 60 if len(price_list) <= 25 else 15
            
            for i, price in enumerate(price_list):
                start_dt = datetime.combine(date_ref, time(0,0)) + timedelta(minutes=i*interval_min)
                if start_dt + timedelta(minutes=interval_min) < now: continue
                parsed.append({"start": start_dt, "end": start_dt + timedelta(minutes=interval_min), "price": float(price)})
            return parsed

        if isinstance(raw_today, str):
            raw_today = [float(x) for x in raw_today.split(",")]
        if isinstance(raw_tomorrow, str):
            raw_tomorrow = [float(x) for x in raw_tomorrow.split(",")]

        prices.extend(parse_price_list(raw_today, now.date()))
        if data["price_data"].get("tomorrow_valid", False) or raw_tomorrow:
             prices.extend(parse_price_list(raw_tomorrow, now.date() + timedelta(days=1)))

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
            plan["charging_summary"] = "Departure time passed. Charging immediately."
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
        if current_soc is None:
             # Assume 0 or fallback? Let's use 0 so it charges.
             current_soc = 0.0
             
        selected_slots = []
        selected_start_times = set()
        price_limit_high = data.get(ENTITY_PRICE_LIMIT_2, 1.5)

        if current_soc >= final_target:
            plan["charging_summary"] = f"Target reached ({int(current_soc)}%). Maintenance mode active (Price <= {price_limit_high} {self.currency})."
            for slot in calc_window:
                if slot["price"] <= price_limit_high:
                    selected_start_times.add(slot["start"])
                    selected_slots.append(slot)
            for slot in calc_window:
                if slot["start"] in selected_start_times and slot["start"] <= now < slot["end"]:
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
            slot_duration_hours = (calc_window[0]["end"] - calc_window[0]["start"]).seconds / 3600.0
            # Guard against zero division if window data bad
            if slot_duration_hours <= 0: slot_duration_hours = 1.0
                
            slots_needed = math.ceil(hours_needed / slot_duration_hours)

            sorted_window = sorted(calc_window, key=lambda x: x["price"])
            selected_slots = sorted_window[:slots_needed]
            selected_start_times = {s["start"] for s in selected_slots}
            
            for slot in calc_window:
                if slot["start"] in selected_start_times and slot["start"] <= now < slot["end"]:
                    plan["should_charge_now"] = True
                    break

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
                    if remaining_kwh_grid <= 0.001: # Small epsilon
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
                    soc_gain_this_slot = (kwh_batt_this_slot / self.car_capacity) * 100.0
                    
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
                            "count": 1
                        }
                if current_block:
                    blocks.append(current_block)

                summary_lines.append(f"**Departure:** {dept_dt.strftime('%H:%M')} {time_source}")
                summary_lines.append(f"**Target:** {int(final_target)}% {status_note}")
                summary_lines.append(f"**Total Estimated Cost:** {total_plan_cost:.2f} {self.currency} {cost_note}")
                summary_lines.append("")
                for b in blocks:
                    start_s = b["soc_start"]
                    end_s = min(100, start_s + b["soc_gain"])
                    # Clamp end_s to final_target if it slightly exceeds due to math
                    if end_s > final_target:
                        end_s = final_target
                    
                    avg_p = b["avg_price_acc"] / b["count"]
                    line = (f"**{b['start'].strftime('%H:%M')} - {b['end'].strftime('%H:%M')}**\n"
                            f"SoC: {int(start_s)}% → {int(end_s)}%\n"
                            f"Cost: {b['cost']:.2f} {self.currency} (Avg: {avg_p:.2f})")
                    summary_lines.append(line)
                plan["charging_summary"] = "\n\n".join(summary_lines)
        
        schedule_data = []
        for slot in prices:
            active = slot["start"] in selected_start_times
            schedule_data.append({
                "start": slot["start"].isoformat(),
                "end": slot["end"].isoformat(),
                "price": slot["price"],
                "active": active
            })

        if schedule_data:
            last_slot = schedule_data[-1]
            schedule_data.append({
                "start": last_slot["end"],
                "end": last_slot["end"],
                "price": last_slot["price"],
                "active": False
            })

        plan["charging_schedule"] = schedule_data
        
        future_starts = [s["start"] for s in calc_window if s["start"] > now and s["start"] in selected_start_times]
        if future_starts:
            plan["scheduled_start"] = min(future_starts).isoformat()

        if not data.get("car_plugged"):
            plan["should_charge_now"] = False

        return plan