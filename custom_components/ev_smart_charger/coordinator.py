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
        self.user_settings = {}  # Storage for UI inputs
        self.action_log = []  # Rolling log of actions

        # Session Tracking
        self.current_session = None  # Active recording
        self.last_session_data = None  # Finished report
        # Flag to capture short charging bursts between ticks
        self._was_charging_in_interval = False

        # Scheduling state
        self._last_scheduled_end = None  # Track end of planned charging for buffer

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
        self.action_log.insert(0, entry)  # Prepend newest

        # Keep only last 50 events
        if len(self.action_log) > 50:
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

            # 9. ACTUATION: Apply logic to physical charger AND car
            await self._apply_charger_control(data, plan)

            # 10. SESSION RECORDING: Record current status
            self._record_session_data(data)

            # Attach log to data so Sensor can read it
            data["action_log"] = self.action_log

            return data

        except Exception as err:
            _LOGGER.error(f"Error in EV Coordinator: {err}")
            raise UpdateFailed(f"Error communicating with API: {err}")

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
            self.hass.async_add_executor_job(
                self._generate_report_image, report, save_path
            )
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

    def _load_fonts(self):
        """Helper to load standard fonts with fallbacks."""
        from PIL import ImageFont

        # Get component directory for bundled fonts
        component_dir = os.path.dirname(__file__)

        # Paths to try for TrueType fonts
        font_candidates = [
            # 1. Bundled Font (Best for Nabu Casa / Docker)
            os.path.join(component_dir, "DejaVuSans.ttf"),
            # 2. System Paths
            "DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/ttf-dejavu/DejaVuSans-Bold.ttf",  # Alpine default
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/freefont/FreeSansBold.ttf",
            "arial.ttf",
        ]

        font_header = None
        font_text = None
        font_small = None

        # Desired Sizes (Updated: ~20% increase from original tiny sizes)
        # Original: 22, 16, 12
        # Increased: 26, 19, 14
        s_header = 26
        s_text = 19
        s_small = 14

        found_path = None

        # 1. Try specific candidates
        for path in font_candidates:
            if os.path.exists(path):
                found_path = path
                break
            # Also try checking via PIL if name is resolved (for system fonts not using full path)
            try:
                ImageFont.truetype(path, s_header)
                found_path = path
                break
            except OSError:
                continue

        # 2. Fallback: Search common directories if nothing found
        if not found_path:
            search_dirs = [
                "/usr/share/fonts",
                "/usr/local/share/fonts",
                "/root/.local/share/fonts",
            ]
            for search_dir in search_dirs:
                if not os.path.isdir(search_dir):
                    continue
                for root, _, files in os.walk(search_dir):
                    for file in files:
                        if file.lower().endswith(".ttf"):
                            # Prefer bold sans
                            if "bold" in file.lower() and "sans" in file.lower():
                                found_path = os.path.join(root, file)
                                break
                            if "bold" in file.lower() and not found_path:
                                found_path = os.path.join(root, file)
                    if found_path:
                        break
                if found_path:
                    break

        # Load if found
        if found_path:
            try:
                _LOGGER.debug(f"Loading font from: {found_path}")
                font_header = ImageFont.truetype(found_path, s_header)

                # Try to find regular version for text
                reg_path = found_path
                # Simple heuristic to find non-bold
                if "Bold" in found_path:
                    try_reg = found_path.replace("Bold", "").replace("bold", "")
                    if os.path.exists(try_reg):
                        reg_path = try_reg
                    elif os.path.exists(try_reg.replace("..", ".")):
                        reg_path = try_reg.replace("..", ".")

                try:
                    font_text = ImageFont.truetype(reg_path, s_text)
                    font_small = ImageFont.truetype(reg_path, s_small)
                except OSError:
                    font_text = ImageFont.truetype(found_path, s_text)
                    font_small = ImageFont.truetype(found_path, s_small)

            except OSError as e:
                _LOGGER.warning(f"Error loading font {found_path}: {e}")
                font_header = None

        if not font_header:
            _LOGGER.warning(
                f"Could not load TrueType fonts. Checked paths and search. Using Pillow default (tiny)."
            )
            font_header = ImageFont.load_default()
            font_text = ImageFont.load_default()
            font_small = ImageFont.load_default()

        return font_header, font_text, font_small

    def _generate_report_image(self, report: dict, file_path: str):
        """Generate a PNG image for thermal printers."""
        try:
            from PIL import Image, ImageDraw
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

        # FIX: Tighter height calculation to remove excessive whitespace
        # Header (110) + Info (180) + Divider (20) + LogHeader (40) = ~350 base
        # Blocks: N * 35
        # Graph area (Graph 250 + Labels 15 + Pad 20) = ~285
        # Pre-graph gap = 40
        # Total = 350 + N*35 + 40 + 285 = 675 + N*35

        base_height = 360  # Header + Info section
        log_height = (
            (len(charging_blocks) * 40) if charging_blocks else 40
        )  # Log lines or "No charging" message
        graph_area_height = 330  # Gap + Graph + Labels

        height = base_height + log_height + graph_area_height

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

        y += 10
        draw.line([(10, y), (width - 10, y)], fill="black", width=3)
        y += 20

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
                y += 40
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
                    (margin_left, graph_bottom + 5),
                    start_str,
                    font=font_small,
                    fill="black",
                )

                end_dt = datetime.fromisoformat(history[-1]["time"])
                end_str = end_dt.strftime("%H:%M")
                try:
                    w = draw.textlength(end_str, font=font_small)
                    draw.text(
                        (width - margin_right - w, graph_bottom + 5),
                        end_str,
                        font=font_small,
                        fill="black",
                    )
                except AttributeError:
                    draw.text(
                        (width - margin_right - 50, graph_bottom + 5),
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

    def _update_virtual_soc(self, data: dict):
        """Update the internal estimated SoC based on charging activity."""
        current_time = datetime.now()
        sensor_soc = data.get("car_soc")

        # 1. Sync Logic
        if sensor_soc is not None:
            if sensor_soc > self._virtual_soc or self._virtual_soc == 0.0:
                self._virtual_soc = float(sensor_soc)

        # 2. Estimate Logic
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
        """Send commands to the Zaptec entities and Car."""

        if datetime.now() - self._startup_time < timedelta(minutes=2):
            return

        should_charge = data.get("should_charge_now", False)
        safe_amps = math.floor(data.get("max_available_current", 0))

        if safe_amps < 6:
            if should_charge:
                self._add_log(
                    f"Safety Cutoff: Available {safe_amps}A is below minimum 6A. Pausing."
                )
            should_charge = False

        target_amps = safe_amps if should_charge else 0
        desired_state = "charging" if should_charge else "paused"

        maintenance_active = "Maintenance mode active" in plan.get(
            "charging_summary", ""
        )

        if maintenance_active:
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

        # 3. Control Start/Stop
        if should_charge:
            if target_amps > 0:
                self._was_charging_in_interval = True

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

    async def _handle_plugged_event(self, is_plugged: bool, data: dict):
        if is_plugged and not self.previous_plugged_state:
            self._add_log("Car plugged in.")
            self.current_session = {
                "start_time": datetime.now().isoformat(),
                "history": [],
                "log": [],
            }
            if data.get("car_soc") is not None:
                self._virtual_soc = data["car_soc"]
            else:
                self._virtual_soc = 0.0
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
            self._add_log("Car unplugged. Resetting settings.")
            if self.current_session:
                self._finalize_session()
                self.current_session = None
            self.manual_override_active = False
            std_time = data.get(ENTITY_DEPARTURE_TIME, time(7, 0))
            self.set_user_input(ENTITY_DEPARTURE_OVERRIDE, std_time, internal=True)
            data[ENTITY_DEPARTURE_OVERRIDE] = std_time
            std_target = data.get(ENTITY_TARGET_SOC, 80)
            self.set_user_input(ENTITY_TARGET_OVERRIDE, std_target, internal=True)
            data[ENTITY_TARGET_OVERRIDE] = std_target
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
                    pass
            self._last_applied_state = "paused"
            self._last_applied_car_limit = -1
            self._last_scheduled_end = None

        self.previous_plugged_state = is_plugged

    def _calculate_load_balancing(self, data: dict) -> float:
        p1_l1 = data.get("p1_l1", 0.0)
        p1_l2 = data.get("p1_l2", 0.0)
        p1_l3 = data.get("p1_l3", 0.0)
        ch_l1 = data.get("ch_l1", 0.0)
        ch_l2 = data.get("ch_l2", 0.0)
        ch_l3 = data.get("ch_l3", 0.0)
        house_l1 = max(0.0, p1_l1 - ch_l1)
        house_l2 = max(0.0, p1_l2 - ch_l2)
        house_l3 = max(0.0, p1_l3 - ch_l3)
        max_house_current = max(house_l1, house_l2, house_l3)
        buffer = max(1.0, self.max_fuse * 0.05)
        return max(0.0, self.max_fuse - max_house_current - buffer)

    def _analyze_prices(self, attributes: dict) -> str:
        raw_prices = attributes.get("today", [])
        if not raw_prices:
            return "No Data"
        try:
            count = len(raw_prices)
            now_dt = datetime.now()
            idx = (
                (now_dt.hour * 4) + (now_dt.minute // 15) if count > 25 else now_dt.hour
            )
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
        cal_time, _ = self._get_calendar_data(data, now)
        if cal_time:
            return cal_time
        time_input = data.get(ENTITY_DEPARTURE_OVERRIDE) or data.get(
            ENTITY_DEPARTURE_TIME, time(7, 0)
        )
        dept_dt = datetime.combine(now.date(), time_input)
        if dept_dt < now:
            dept_dt = dept_dt + timedelta(days=1)
        return dept_dt

    def _generate_charging_plan(self, data: dict) -> dict:
        """Core Logic."""
        plan = {
            "should_charge_now": False,
            "scheduled_start": None,
            "planned_target_soc": data.get(ENTITY_TARGET_SOC, 80),
            "charging_schedule": [],
            "charging_summary": "Not calculated",
        }
        if not data.get(ENTITY_SMART_SWITCH, True):
            plan["should_charge_now"] = True
            plan["charging_summary"] = "Smart charging disabled. Charging immediately."
            if not data.get("car_plugged"):
                plan["should_charge_now"] = False
            return plan

        now = datetime.now()
        prices = []
        raw_today = data["price_data"].get("today", [])
        if not raw_today:
            plan["should_charge_now"] = True
            plan["charging_summary"] = "No Price Sensor. Load Balancing Mode."
            if not data.get("car_plugged"):
                plan["should_charge_now"] = False
            return plan

        raw_tomorrow = data["price_data"].get("tomorrow", [])

        def parse_price_list(price_list, date_ref):
            parsed = []
            if not price_list:
                return []
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
            plan["should_charge_now"] = True
            plan["charging_summary"] = "No future price data found."
            if not data.get("car_plugged"):
                plan["should_charge_now"] = False
            return plan

        dept_dt = self._get_departure_time(data, now)
        calc_window = [p for p in prices if p["start"] < dept_dt]
        if not calc_window:
            plan["should_charge_now"] = True
            plan["charging_summary"] = "Departure passed. Charging."
            if not data.get("car_plugged"):
                plan["should_charge_now"] = False
            return plan

        cal_time, cal_soc = self._get_calendar_data(data, now)
        time_source = "(Calendar)" if cal_time and cal_time == dept_dt else "(Manual)"
        min_guaranteed = data.get(ENTITY_MIN_SOC, 20)

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
        current_soc = data.get("car_soc", 0) or 0.0

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
            # Calculation
            soc_needed = final_target - current_soc
            kwh_needed = (soc_needed / 100.0) * self.car_capacity
            efficiency = 1.0 - (self.charger_loss / 100.0)
            kwh_to_pull = kwh_needed / efficiency
            est_power_kw = min((3 * 230 * self.max_fuse) / 1000, 11.0)
            hours_needed = kwh_to_pull / est_power_kw
            slot_duration_hours = (
                calc_window[0]["end"] - calc_window[0]["start"]
            ).seconds / 3600.0
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
            extra_fee = data.get(ENTITY_PRICE_EXTRA_FEE, 0.0)
            vat_pct = data.get(ENTITY_PRICE_VAT, 0.0)
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
                    if remaining_kwh_grid <= 0.001:
                        break
                    raw_price = slot["price"]
                    adjusted_price = (raw_price + extra_fee) * (1 + vat_pct / 100.0)
                    kwh_this_slot = min(kwh_grid_per_slot_max, remaining_kwh_grid)
                    remaining_kwh_grid -= kwh_this_slot
                    slot_cost = adjusted_price * kwh_this_slot
                    total_plan_cost += slot_cost
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
