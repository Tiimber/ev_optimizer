"""Session Manager for EV Smart Charger."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .const import (
    DOMAIN,
    ENTITY_PRICE_EXTRA_FEE,
    ENTITY_PRICE_VAT,
)

_LOGGER = logging.getLogger(__name__)

class SessionManager:
    """Manages charging sessions, history, and action logging."""

    def __init__(self, hass):
        """Initialize the session manager."""
        self.hass = hass
        self.action_log = []
        self.current_session = None
        self.last_session_data = None
        self.overload_prevention_minutes = 0.0
        self._was_charging_in_interval = False
    
    def load_from_dict(self, data: dict):
        """Load persisted state."""
        if not data:
            return
        self.action_log = data.get("action_log", [])
        self.last_session_data = data.get("last_session_data")
        self.overload_prevention_minutes = data.get("overload_prevention_minutes", 0.0)

    def to_dict(self) -> dict:
        """Return state for persistence."""
        return {
            "action_log": self.action_log,
            "last_session_data": self.last_session_data,
            "overload_prevention_minutes": self.overload_prevention_minutes,
        }

    def add_log(self, message: str):
        """Add an entry to the action log and prune entries older than 24h."""
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {message}"
        self.action_log.insert(0, entry)

        # Keep only last 24h events
        cutoff = now - timedelta(hours=24)
        while self.action_log:
            try:
                last_entry = self.action_log[-1]
                last_ts_str = last_entry[1:20]
                last_dt = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
                if last_dt < cutoff:
                    self.action_log.pop()
                else:
                    break
            except (ValueError, IndexError):
                self.action_log.pop()

        # Add to current session log if active
        if self.current_session is not None:
            self.current_session["log"].append(entry)

        # Fire event for Logbook
        if self.hass:
            self.hass.bus.async_fire(
                f"{DOMAIN}_log_event", {"message": message, "name": "EV Smart Charger"}
            )

    def start_session(self, initial_soc: float):
        """Start a new charging session."""
        self.add_log("Car plugged in. Session started.")
        self.current_session = {
            "start_time": datetime.now().isoformat(),
            "history": [],
            "log": [],
        }
        # Reset prevention minutes for new session? 
        # Actually logic in coordinator seemed to persist it, let's keep it consistent or reset?
        # Coordinator didn't reset it explicitly on plug-in, but likely should?
        # Actually coordinator loaded it from persistence.
        # Let's assume it resets on new session logic in planner usually handles clean state.
        # But for now, let's just track session start.

    def stop_session(self, user_settings: dict, currency: str, final_soc: float = None):
        """Finalize the current session."""
        self.add_log("Unplugged. Session ended.")
        if not self.current_session:
            return None
            
        report = self._calculate_session_totals(currency, final_soc)
        self.last_session_data = report
        self.current_session = None
        return report

    def calculate_session_totals(self, currency: str, final_soc: float | None = None) -> dict:
        """Calculate current totals for an ACTIVE session without ending it."""
        return self._calculate_session_totals(currency, final_soc)

    def record_data_point(self, data: dict, user_settings: dict, last_applied_amps: float, last_applied_state: str):
        """Record a history data point for the active session."""
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
        except Exception:
            current_price = 0.0

        extra_fee = user_settings.get(ENTITY_PRICE_EXTRA_FEE, 0.0)
        vat_pct = user_settings.get(ENTITY_PRICE_VAT, 0.0)
        adjusted_price = (current_price + extra_fee) * (1 + vat_pct / 100.0)

        is_charging = 1 if (last_applied_state == "charging" or self._was_charging_in_interval) else 0
        point = {
            "time": now_ts.isoformat(),
            "soc": data.get("car_soc", 0),
            "amps": last_applied_amps,
            "charging": is_charging,
            "price": adjusted_price,
        }

        self.current_session["history"].append(point)
        self._was_charging_in_interval = False

    def mark_charging_in_interval(self):
        """Mark that charging occurred during this interval (even if short)."""
        self._was_charging_in_interval = True

    def add_overload_minutes(self, minutes: float):
        """Accumulate overload prevention minutes."""
        self.overload_prevention_minutes += minutes

    def _calculate_session_totals(self, currency: str, final_soc: float = None) -> dict:
        """Calculate totals for the finished session."""
        if not self.current_session:
            return {}
            
        history = self.current_session["history"]
        if not history:
            return {}
            
        start_soc = history[0]["soc"]
        end_soc = final_soc if final_soc is not None else history[-1]["soc"]
        total_kwh = 0.0
        total_cost = 0.0
        
        # Avoid crash if only 1 point
        if len(history) < 2:
            return {
             "start_time": self.current_session["start_time"],
             "end_time": datetime.now().isoformat(),
             "start_soc": start_soc,
             "end_soc": end_soc,
             "added_kwh": 0.0,
             "total_cost": 0.0,
             "currency": currency,
             "graph_data": history,
             "session_log": self.current_session["log"],
             "overload_prevention_minutes": self.overload_prevention_minutes,
            }

        prev = datetime.fromisoformat(history[0]["time"])
        for i in range(1, len(history)):
            curr = datetime.fromisoformat(history[i]["time"])
            delta_h = (curr - prev).total_seconds() / 3600.0
            prev = curr
            amps = history[i - 1]["amps"]
            is_charging = history[i - 1]["charging"]
            
            if is_charging and amps > 0:
                # Standard 3-phase calculation, maybe should be configurable (1 vs 3 phase)
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
            "currency": currency,
            "graph_data": history,
            "session_log": self.current_session["log"],
            "overload_prevention_minutes": self.overload_prevention_minutes,
        }
