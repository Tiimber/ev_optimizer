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
            ),  # Shared Entity for Refresh & Limit
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
            hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=30)
        )

    def _add_log(self, message: str):
        """Add log entry."""
        now = datetime.now()
        entry = f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        self.action_log.insert(0, entry)
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
        if self.current_session is not None:
            self.current_session["log"].append(entry)
        self.hass.bus.async_fire(
            f"{DOMAIN}_log_event", {"message": message, "name": "EV Smart Charger"}
        )

    async def _load_data(self):
        """Load settings."""
        if self._data_loaded:
            return
        try:
            data = await self.store.async_load()
            if data:
                self.manual_override_active = data.get("manual_override_active", False)
                self.action_log = data.get("action_log", [])
                self.last_session_data = data.get("last_session_data")
                self.user_settings.update(data.get("user_settings", {}))
                self._add_log("System started. Settings loaded.")
        except Exception as e:
            _LOGGER.error(f"Failed load: {e}")
        self._data_loaded = True

    def _save_data(self):
        """Save settings."""

        def data_to_save():
            clean = self.user_settings.copy()
            for k, v in clean.items():
                if isinstance(v, time):
                    clean[k] = v.strftime("%H:%M")
            return {
                "manual_override_active": self.manual_override_active,
                "user_settings": clean,
                "action_log": self.action_log,
                "last_session_data": self.last_session_data,
            }

        self.store.async_delay_save(data_to_save, 1.0)

    def set_user_input(self, key, value, internal=False):
        self.user_settings[key] = value
        if not internal:
            self._add_log(f"Setting {key} -> {value}")
        if key == ENTITY_TARGET_OVERRIDE and not internal:
            self.manual_override_active = True
            self._add_log("Manual Override Active")
        self._save_data()
        if self.data:
            self.hass.async_create_task(self.async_refresh())

    def clear_manual_override(self):
        self.manual_override_active = False
        self.user_settings[ENTITY_TARGET_OVERRIDE] = self.user_settings.get(
            ENTITY_TARGET_SOC, 80
        )
        self._add_log("Override Cleared")
        self._save_data()
        if self.data:
            self.hass.async_create_task(self.async_refresh())

    async def async_trigger_report_generation(self):
        report = (
            self._calculate_session_totals()
            if self.current_session
            else self.last_session_data
        )
        if report:
            if self.current_session:
                report["end_time"] = datetime.now().isoformat()
            path = self.hass.config.path("www", "ev_smart_charger_last_session.png")
            await self.hass.async_add_executor_job(generate_report_image, report, path)
            self._add_log("Report Image Generated")

    async def async_trigger_plan_image_generation(self):
        if self.data:
            path = self.hass.config.path("www", "ev_smart_charger_plan.png")
            await self.hass.async_add_executor_job(generate_plan_image, self.data, path)
            self._add_log("Plan Image Generated")

    async def _async_update_data(self):
        if not self._data_loaded:
            await self._load_data()

        try:
            data = self._fetch_sensor_data()
            data.update(self.user_settings)

            cal = self.conf_keys.get("calendar")
            data["calendar_events"] = []
            if cal:
                try:
                    now = datetime.now()
                    resp = await self.hass.services.async_call(
                        "calendar",
                        "get_events",
                        {
                            "entity_id": cal,
                            "start_date_time": now.isoformat(),
                            "end_date_time": (now + timedelta(hours=48)).isoformat(),
                        },
                        blocking=True,
                        return_response=True,
                    )
                    if resp and cal in resp:
                        data["calendar_events"] = resp[cal].get("events", [])
                except Exception:
                    pass

            await self._handle_plugged_event(data["car_plugged"], data)
            self._update_virtual_soc(data)
            data["car_soc"] = self._virtual_soc

            data["max_available_current"] = calculate_load_balancing(
                data, self.config_settings["max_fuse"]
            )
            data["current_price_status"] = analyze_prices(data["price_data"])

            plan = generate_charging_plan(
                data, self.config_settings, self.manual_override_active
            )

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
            data["action_log"] = self.action_log

            return data
        except Exception as e:
            _LOGGER.error(f"Coordinator Error: {e}")
            raise UpdateFailed(e)

    async def _manage_car_refresh(self, data: dict, plan: dict):
        if not data.get("car_plugged"):
            return
        svc = self.conf_keys.get("refresh_svc")
        ent = self.conf_keys.get("car_target_ent")  # Use shared entity
        mode = self.conf_keys.get("refresh_int", REFRESH_NEVER)

        if not svc or not ent or mode == REFRESH_NEVER:
            return

        now = datetime.now()
        delta = (
            (now - self._last_car_refresh_time)
            if self._last_car_refresh_time
            else timedelta(days=365)
        )
        do_refresh = False

        if mode == REFRESH_30_MIN and delta > timedelta(minutes=30):
            do_refresh = True
        elif mode == REFRESH_1_HOUR and delta > timedelta(hours=1):
            do_refresh = True
        elif mode == REFRESH_2_HOURS and delta > timedelta(hours=2):
            do_refresh = True
        elif mode == REFRESH_3_HOURS and delta > timedelta(hours=3):
            do_refresh = True
        elif mode == REFRESH_4_HOURS and delta > timedelta(hours=4):
            do_refresh = True
        elif mode == REFRESH_AT_TARGET and delta > timedelta(hours=12):
            if self._virtual_soc >= float(plan.get("planned_target_soc", 80)):
                do_refresh = True

        if do_refresh:
            await self._trigger_car_refresh(svc, ent)

    async def _trigger_car_refresh(self, service: str, entity_id: str):
        try:
            state = self.hass.states.get(self.conf_keys["car_soc"])
            val = (
                float(state.state)
                if state and state.state not in [STATE_UNKNOWN, STATE_UNAVAILABLE]
                else 0.0
            )
            self._soc_before_refresh = val

            self._add_log(f"Refreshing Car Data via {service} (Current: {val}%)")
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
            _LOGGER.error(f"Refresh failed: {e}")

    async def _apply_charger_control(self, data: dict, plan: dict):
        if datetime.now() - self._startup_time < timedelta(minutes=2):
            return

        should_charge = data.get("should_charge_now", False)
        safe_amps = math.floor(data.get("max_available_current", 0))
        if safe_amps < 6:
            if should_charge:
                self._add_log(f"Safety Cutoff: {safe_amps}A < 6A. Pausing.")
            should_charge = False

        target_amps = safe_amps if should_charge else 0
        desired_state = "charging" if should_charge else "paused"

        if "Maintenance mode active" in plan.get("charging_summary", ""):
            should_charge = True
            target_amps = 0
            desired_state = "maintenance"

        # Car Limit
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
                    self._add_log(f"Set Car Limit Service: {target_soc}%")
                except Exception as e:
                    _LOGGER.error(f"Car Limit Service Failed: {e}")

        # Zaptec Control
        if should_charge:
            if target_amps > 0:
                self._was_charging_in_interval = True

            if desired_state != self._last_applied_state:
                if self.conf_keys.get("zap_switch"):
                    try:
                        await self.hass.services.async_call(
                            "switch",
                            SERVICE_TURN_ON,
                            {"entity_id": self.conf_keys["zap_switch"]},
                            blocking=True,
                        )
                    except Exception:
                        pass
                    self._add_log(f"State: {desired_state.upper()}")
                elif self.conf_keys.get("zap_resume"):
                    try:
                        await self.hass.services.async_call(
                            "button",
                            "press",
                            {"entity_id": self.conf_keys["zap_resume"]},
                            blocking=True,
                        )
                    except Exception:
                        pass
                self._last_applied_state = desired_state

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
                        self._add_log(f"Limit set: {target_amps}A")
                except Exception:
                    pass
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
                except Exception:
                    pass

            if desired_state != self._last_applied_state:
                if self.conf_keys.get("zap_switch"):
                    try:
                        await self.hass.services.async_call(
                            "switch",
                            SERVICE_TURN_OFF,
                            {"entity_id": self.conf_keys["zap_switch"]},
                            blocking=True,
                        )
                    except Exception:
                        pass
                    self._add_log(f"State: {desired_state.upper()}")
                elif self.conf_keys.get("zap_stop"):
                    try:
                        await self.hass.services.async_call(
                            "button",
                            "press",
                            {"entity_id": self.conf_keys["zap_stop"]},
                            blocking=True,
                        )
                    except Exception:
                        pass
                self._last_applied_state = desired_state

    def _fetch_sensor_data(self):
        data = {}

        def gf(k):
            s = self.hass.states.get(self.conf_keys.get(k))
            return (
                float(s.state)
                if s and s.state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]
                else 0.0
            )

        data["p1_l1"] = gf("p1_l1")
        data["p1_l2"] = gf("p1_l2")
        data["p1_l3"] = gf("p1_l3")
        data["ch_l1"] = gf("ch_l1")
        data["ch_l2"] = gf("ch_l2")
        data["ch_l3"] = gf("ch_l3")

        s = self.hass.states.get(self.conf_keys["car_soc"])
        data["car_soc"] = (
            float(s.state)
            if s and s.state not in [STATE_UNAVAILABLE, STATE_UNKNOWN]
            else None
        )

        p = self.hass.states.get(self.conf_keys["car_plugged"])
        data["car_plugged"] = (
            p.state in ["on", "true", "connected", "charging", "full", "plugged_in"]
            if p
            else False
        )

        pe = self.conf_keys.get("price")
        data["price_data"] = (
            self.hass.states.get(pe).attributes
            if pe and self.hass.states.get(pe)
            else {}
        )

        return data

    async def _handle_plugged_event(self, is_plugged, data):
        if is_plugged and not self.previous_plugged_state:
            self._add_log("Car plugged in.")
            self.current_session = {
                "start_time": datetime.now().isoformat(),
                "history": [],
                "log": [],
            }
            self._virtual_soc = data["car_soc"] if data["car_soc"] is not None else 0.0

            soc_entity = self.conf_keys["car_soc"]
            try:
                await self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": soc_entity},
                    blocking=False,
                )
            except Exception:
                pass

        if not is_plugged and self.previous_plugged_state:
            self._add_log("Unplugged.")
            self._finalize_session()
            self.current_session = None
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

        self.previous_plugged_state = is_plugged

    def _update_virtual_soc(self, data):
        now = datetime.now()
        s = data.get("car_soc")

        trust = False
        if self._refresh_trigger_timestamp and (
            now - self._refresh_trigger_timestamp
        ) < timedelta(minutes=5):
            if s is not None and float(s) != self._soc_before_refresh:
                trust = True

        if s is not None:
            if s > self._virtual_soc or self._virtual_soc == 0.0 or trust:
                self._virtual_soc = float(s)

        if self._last_applied_state == "charging" and self._last_applied_amps > 0:
            ch_l1 = data.get("ch_l1", 0.0)
            ch_l2 = data.get("ch_l2", 0.0)
            ch_l3 = data.get("ch_l3", 0.0)
            meas = max(ch_l1, ch_l2, ch_l3)
            used = meas if meas > 0.5 else self._last_applied_amps

            secs = (now - self._last_update_time).total_seconds()
            p_kw = (3 * 230 * used) / 1000.0
            eff = self.entry.data.get(CONF_CHARGER_LOSS, 10.0)
            added = p_kw * (secs / 3600.0) * (1 - eff / 100.0)

            if self.car_capacity > 0:
                self._virtual_soc += (added / self.car_capacity) * 100.0
                if (
                    self._last_applied_car_limit > 0
                    and self._virtual_soc > self._last_applied_car_limit
                ):
                    self._virtual_soc = float(self._last_applied_car_limit)
                if self._virtual_soc > 100.0:
                    self._virtual_soc = 100.0

        self._last_update_time = now

    def _record_session_data(self, data):
        if not self.current_session:
            return
        now_ts = datetime.now()
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

        extra_fee = data.get(ENTITY_PRICE_EXTRA_FEE, 0.0)
        vat_pct = data.get(ENTITY_PRICE_VAT, 0.0)
        adjusted_price = (current_price + extra_fee) * (1 + vat_pct / 100.0)

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
        self._was_charging_in_interval = False

    def _finalize_session(self):
        if not self.current_session:
            return
        report = self._calculate_session_totals()
        self.last_session_data = report
        self._save_data()
        self.hass.create_task(self.async_trigger_report_generation())

    def _calculate_session_totals(self):
        hist = self.current_session["history"]
        if not hist:
            return {}

        start_soc = hist[0]["soc"]
        end_soc = hist[-1]["soc"]
        total_kwh = 0.0
        total_cost = 0.0
        prev = datetime.fromisoformat(hist[0]["time"])

        for i in range(1, len(hist)):
            curr = datetime.fromisoformat(hist[i]["time"])
            h = (curr - prev).total_seconds() / 3600.0
            prev = curr
            if hist[i - 1]["charging"] and hist[i - 1]["amps"] > 0:
                p = (3 * 230 * hist[i - 1]["amps"]) / 1000.0
                k = p * h
                total_kwh += k
                total_cost += k * hist[i - 1]["price"]

        return {
            "start_time": self.current_session["start_time"],
            "end_time": datetime.now().isoformat(),
            "start_soc": start_soc,
            "end_soc": end_soc,
            "added_kwh": round(total_kwh, 2),
            "total_cost": round(total_cost, 2),
            "currency": self.currency,
            "graph_data": hist,
            "session_log": self.current_session["log"],
        }
