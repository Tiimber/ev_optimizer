"""Planning logic for EV Optimizer."""

import logging
import math
import re
from datetime import timedelta, datetime, time, timezone

from .const import (
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
    LEARNING_CHARGER_LOSS,
    LEARNING_CONFIDENCE,
    LEARNING_SESSIONS,
    LEARNING_MEASUREMENTS,
    LEARNING_LOCKED,
    LEARNING_CHARGE_POWER,
    LEARNING_POWER_SAMPLES,
    MIN_POWER_SAMPLES,
    NOMINAL_VOLTAGE,
    DEFAULT_LOSS,
)

_LOGGER = logging.getLogger(__name__)

# Optional: only used to make price-slot arithmetic and calendar offsets
# timezone-correct. The module stays importable and fully functional without
# Home Assistant, falling back to naive local arithmetic.
try:  # pragma: no cover - trivially environment-dependent
    from homeassistant.util import dt as dt_util
except ImportError:  # pragma: no cover
    dt_util = None


def _local_timezone():
    """The configured local timezone, or None if we cannot determine it."""
    if dt_util is None:
        return None
    return getattr(dt_util, "DEFAULT_TIME_ZONE", None)


def slot_start_times(date_ref, count: int, interval_min: int) -> list:
    """Wall-clock start times for a day's price slots.

    Naive arithmetic — midnight plus i x interval — silently assumes every day
    is 24 hours long. On the two days a year that are not, the price array is
    short or long to match (Nordpool publishes 92 or 100 quarters), and the
    naive sequence walks an hour out of step with it for the rest of the day,
    so every slot is priced from the wrong hour.

    Stepping through absolute time instead and converting back to local gets
    this right for free: the same 92 entries land on the same 23 wall-clock
    hours the market actually traded.

    Returns naive local datetimes, because that is what the rest of this module
    compares against. On the autumn fold two slots therefore share a wall-clock
    label for one hour a year; that ambiguity is bounded and far smaller than
    the misalignment it replaces.
    """
    midnight = datetime.combine(date_ref, time(0, 0))
    tz = _local_timezone()

    if tz is None:
        # No timezone available (module used standalone, or HA not configured
        # yet): behave exactly as before.
        return [midnight + timedelta(minutes=i * interval_min) for i in range(count)]

    try:
        aware_midnight = midnight.replace(tzinfo=tz)
        utc_midnight = aware_midnight.astimezone(timezone.utc)
        return [
            (utc_midnight + timedelta(minutes=i * interval_min))
            .astimezone(tz)
            .replace(tzinfo=None)
            for i in range(count)
        ]
    except Exception:  # pragma: no cover - defensive
        _LOGGER.warning(
            "Could not build timezone-aware price slots; falling back to naive "
            "arithmetic", exc_info=True,
        )
        return [midnight + timedelta(minutes=i * interval_min) for i in range(count)]


def get_effective_charger_loss(config_settings: dict, learning_state: dict) -> tuple[float, bool]:
    """Get the charger loss percentage to use in calculations.
    
    Returns:
        Tuple of (effective_loss_percentage, is_learning_phase)
    """
    configured_loss = config_settings.get("charger_loss", DEFAULT_LOSS)
    sessions = learning_state.get(LEARNING_SESSIONS, 0)
    measurements = learning_state.get(LEARNING_MEASUREMENTS, 0)

    # The learned value is only meaningful once something has actually been
    # measured. Completed sessions do not imply a measurement — a session can
    # end without ever producing a usable window — so this gates on the
    # measurement count, not the session count.
    if measurements > 0:
        effective_loss = learning_state.get(LEARNING_CHARGER_LOSS, configured_loss)
    else:
        effective_loss = configured_loss

    # Check if in learning phase
    locked = learning_state.get(LEARNING_LOCKED, False)
    is_learning = (sessions < 10 and not locked)

    confidence = learning_state.get(LEARNING_CONFIDENCE, 0)
    _LOGGER.debug(
        "🔧 Charger efficiency: %.1f%% loss (sessions=%d, measurements=%d, "
        "confidence=%d/10, %s)",
        effective_loss, sessions, measurements, confidence,
        "Learning" if is_learning else "Locked"
    )

    return effective_loss, is_learning


def get_charging_power_estimate(
    config_settings: dict, learning_state: dict
) -> tuple[float, str]:
    """How much power to assume a charging slot will deliver.

    Returns (kW, source) so callers can say which rung of the chain answered.

    The fallback is the original behaviour — derived from the house fuse — so an
    installation with no charger current or power sensors plans exactly as it
    did before. Where those sensors exist, the observed figure is better on two
    counts the fuse cannot capture: the car's onboard charger is usually the
    real ceiling, and load balancing throttles the charger whenever the house
    draws current.
    """
    fuse_estimate = min(
        (3 * NOMINAL_VOLTAGE * config_settings.get("max_fuse", 16.0)) / 1000.0, 11.0
    )

    observed = (learning_state or {}).get(LEARNING_CHARGE_POWER)
    samples = (learning_state or {}).get(LEARNING_POWER_SAMPLES, 0)

    if not observed or samples < MIN_POWER_SAMPLES:
        return fuse_estimate, "fuse"

    # Never plan on more than the fuse would physically allow, however
    # enthusiastic the sensor. Undershooting is safe — it buys extra slots —
    # so only the upper side is clamped.
    if observed > fuse_estimate:
        return fuse_estimate, "fuse (observed higher)"

    return observed, f"observed over {samples} samples"


def cost_of_slots(
    slots: list,
    kwh_to_pull: float,
    kwh_per_slot_max: float,
    extra_fee: float = 0.0,
    vat_pct: float = 0.0,
) -> float:
    """Cost of drawing `kwh_to_pull` through `slots`, cheapest-relevant first.

    Slots are consumed in chronological order until the energy is covered, the
    same way the real schedule fills up, so plan and baseline are costed on
    identical terms.
    """
    if kwh_per_slot_max <= 0:
        return 0.0

    remaining = kwh_to_pull
    total = 0.0
    for slot in sorted(slots, key=lambda s: s["start"]):
        if remaining <= 0.001:
            break
        kwh = min(kwh_per_slot_max, remaining)
        remaining -= kwh
        total += (slot["price"] + extra_fee) * (1 + vat_pct / 100.0) * kwh
    return total


def select_charging_slots(
    calc_window: list,
    slots_needed: int,
    contiguous: bool = False,
    prefer_late: bool = False,
) -> list:
    """Choose which slots to charge in.

    Default (both flags off) is unchanged: the `slots_needed` cheapest slots
    before departure, wherever in the window they fall.

    `prefer_late` only changes the answer among slots that cost the *same*.
    Quarter-hourly data splits each hourly price into four identical values, so
    ties are the common case rather than the exception, and breaking them
    towards the end of the window finishes the charge nearer departure without
    paying anything for it.

    `contiguous` changes the answer more substantially: it picks the cheapest
    unbroken run of `slots_needed` slots, so the session is one stretch instead
    of several. That generally costs more than cherry-picking, and is a
    trade against wear rather than against price.
    """
    if slots_needed <= 0 or not calc_window:
        return []

    ordered = sorted(calc_window, key=lambda s: s["start"])

    if contiguous:
        if slots_needed >= len(ordered):
            return list(ordered)
        best_start, best_cost = 0, None
        for i in range(len(ordered) - slots_needed + 1):
            run = ordered[i:i + slots_needed]
            cost = sum(s["price"] for s in run)
            # `<` rather than `<=` keeps the earliest of equally-priced runs by
            # default; prefer_late flips that to the latest.
            if best_cost is None or cost < best_cost or (prefer_late and cost == best_cost):
                best_start, best_cost = i, cost
        return list(ordered[best_start:best_start + slots_needed])

    # Cheapest-first. The secondary key breaks ties by time — later slots first
    # when prefer_late is set, earliest otherwise (which is the original order).
    return sorted(
        calc_window,
        key=lambda s: (s["price"], -s["start"].timestamp() if prefer_late else s["start"].timestamp()),
    )[:slots_needed]


def calculate_load_balancing(data: dict, max_fuse: float) -> float:
    """Calculate the safe current available for the charger."""
    p1_l1 = data.get("p1_l1", 0.0)
    p1_l2 = data.get("p1_l2", 0.0)
    p1_l3 = data.get("p1_l3", 0.0)
    ch_l1 = data.get("ch_l1", 0.0)
    ch_l2 = data.get("ch_l2", 0.0)
    ch_l3 = data.get("ch_l3", 0.0)

    # With no charger current sensors configured, fall back to the commanded
    # limiter value so load balancing still subtracts the charger's own draw.
    #
    # The limiter is amps *per phase*, not a total to be divided between them.
    # Spreading it as zap_limit/3 under-subtracted the charger by a factor of
    # three, which inflated apparent house load by the difference and drove a
    # stable start/stop oscillation: the charger would be allowed on, the next
    # reading would look like an overload, it would be paused, the reading
    # would recover, and so on every cycle — on a house with plenty to spare.
    if ch_l1 == 0.0 and ch_l2 == 0.0 and ch_l3 == 0.0:
        zap_limit = data.get("zap_limit_value", 0.0)
        if zap_limit > 0:
            ch_l1 = ch_l2 = ch_l3 = zap_limit

    house_l1 = max(0.0, p1_l1 - ch_l1)
    house_l2 = max(0.0, p1_l2 - ch_l2)
    house_l3 = max(0.0, p1_l3 - ch_l3)

    max_house_current = max(house_l1, house_l2, house_l3)
    buffer = max(1.0, max_fuse * 0.05)
    available = max_fuse - max_house_current - buffer
    
    # Cap at max_fuse to prevent trying to set values above the limit
    available = min(available, max_fuse)
    
    _LOGGER.debug(
        "⚡ Load Balancing: P1=[%.1fA, %.1fA, %.1fA] Charger=[%.1fA, %.1fA, %.1fA] "
        "House=[%.1fA, %.1fA, %.1fA] Max=%.1fA Buffer=%.1fA → Available=%.1fA (capped at %.1fA)",
        p1_l1, p1_l2, p1_l3, ch_l1, ch_l2, ch_l3,
        house_l1, house_l2, house_l3, max_house_current, buffer, available, max_fuse
    )

    return max(0.0, available)


def analyze_prices(raw_prices) -> str:
    """Quick status for UI.

    Accepts the day's price list. A mapping is rejected outright rather than
    silently producing "Unknown": the caller used to hand over the whole price
    sensor attribute dict, and the resulting KeyError was swallowed by the
    except below, so the sensor read "Unknown" permanently.
    """
    if isinstance(raw_prices, dict):
        _LOGGER.error(
            "analyze_prices expects the price list, got a mapping with keys %s",
            sorted(raw_prices)[:8],
        )
        return "Unknown"
    if isinstance(raw_prices, str):
        try:
            raw_prices = [float(x) for x in raw_prices.split(",") if x.strip()]
        except ValueError:
            return "Unknown"
    if not raw_prices:
        return "No Data"
    try:
        raw_prices = [float(p) for p in raw_prices]
        count = len(raw_prices)
        now_dt = datetime.now()
        idx = (now_dt.hour * 4) + (now_dt.minute // 15) if count > 25 else now_dt.hour
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


def get_calendar_data(
    events: list, now: datetime
) -> tuple[datetime | None, float | None]:
    """Check for relevant calendar event.
    
    In the early morning (midnight to 08:00), only look for events TODAY.
    This prevents using tomorrow's calendar events when we should be charging
    for today's manual departure time.
    
    After 08:00, can look ahead to tomorrow as usual.
    """
    if not events:
        return None, None
    
    # Before 08:00: only look at today's events to prevent using tomorrow's
    # calendar events at midnight (which causes waiting for tomorrow's prices)
    if now.hour < 8:
        limit = datetime.combine(now.date(), time.max)
    else:
        # After 08:00: can look ahead to tomorrow
        limit = datetime.combine(now.date() + timedelta(days=1), time.max)
    
    sorted_events = sorted(events, key=lambda x: x.get("start"))
    for event in sorted_events:
        start_str = event.get("start")
        if isinstance(start_str, dict):
            start_str = start_str.get("dateTime", start_str.get("date"))
        if not start_str:
            continue
        try:
            if len(start_str) == 10:
                evt_start = datetime.fromisoformat(start_str)
            else:
                evt_start = datetime.fromisoformat(start_str)
                if evt_start.tzinfo:
                    # Convert into local time before dropping the offset.
                    # replace(tzinfo=None) alone *discards* it, which is only
                    # correct while the calendar happens to emit local-offset
                    # timestamps — a UTC one would land hours out.
                    tz = _local_timezone()
                    if tz is not None:
                        evt_start = evt_start.astimezone(tz)
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
            _LOGGER.warning(
                "Skipping calendar event with unparseable start %r", start_str
            )
            continue
    return None, None


def get_departure_time(
    data: dict, now: datetime, calendar_events: list = None
) -> datetime:
    """Determine the target departure datetime."""
    if calendar_events:
        cal_time, _ = get_calendar_data(calendar_events, now)
        if cal_time:
            return cal_time

    time_input = data.get(ENTITY_DEPARTURE_OVERRIDE) or data.get(
        ENTITY_DEPARTURE_TIME, time(7, 0)
    )
    dept_dt = datetime.combine(now.date(), time_input)
    if dept_dt < now:
        dept_dt = dept_dt + timedelta(days=1)
    return dept_dt


def generate_charging_plan(
    data: dict, config_settings: dict, manual_override: bool, learning_state: dict = None, now: datetime | None = None, overload_prevention_minutes: float = 0.0, expected_price_time: str | None = None
) -> dict:
    """Core Logic.
    
    Args:
        data: Sensor data and settings
        config_settings: Configuration parameters
        manual_override: Whether manual override is active
        learning_state: Dictionary with learning state (efficiency learning)
        now: Current datetime (for testing)
        overload_prevention_minutes: Minutes of accumulated charging time lost due to overload prevention
        expected_price_time: Expected time when tomorrow's prices typically arrive (HH:MM format)
    """
    now = now or datetime.now()
    learning_state = learning_state or {}  # Default to empty dict if not provided
    _LOGGER.debug(
        "🔍 ===== CHARGING PLAN GENERATION START ===== Time: %s",
        now.strftime("%Y-%m-%d %H:%M:%S")
    )
    _LOGGER.debug("📊 Input data: car_plugged=%s, car_soc=%s, smart_switch=%s, manual_override=%s",
                  data.get("car_plugged"), data.get("car_soc"), 
                  data.get(ENTITY_SMART_SWITCH, True), manual_override)
    
    plan = {
        "should_charge_now": False,
        "scheduled_start": None,
        "planned_target_soc": data.get(ENTITY_TARGET_SOC, 80),
        "charging_schedule": [],
        "charging_summary": "Not calculated",
        "overload_prevention_minutes": overload_prevention_minutes,
    }

    if not data.get(ENTITY_SMART_SWITCH, True):
        plan["should_charge_now"] = True
        plan["charging_summary"] = "Smart charging disabled. Charging immediately."
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
        _LOGGER.debug("⚡ DECISION: Smart charging DISABLED → should_charge=%s (plugged=%s)",
                      plan["should_charge_now"], data.get("car_plugged"))
        return plan

    prices = []
    raw_today = data["price_data"].get("today", [])

    if not raw_today:
        if not config_settings.get("has_price_sensor"):
            plan["charging_summary"] = "No Price Sensor. Load Balancing Mode."
            _LOGGER.debug("📍 No price sensor configured - load balancing mode")
        else:
            plan["charging_summary"] = (
                "Error: Price sensor configured but no data received."
            )
            _LOGGER.warning("⚠️ Price sensor configured but NO DATA received!")
        plan["should_charge_now"] = True
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
        _LOGGER.debug("⚡ DECISION: No price data → should_charge=%s", plan["should_charge_now"])
        return plan

    raw_tomorrow = data["price_data"].get("tomorrow", [])

    def parse_price_list(price_list, date_ref):
        parsed = []
        if not price_list:
            return []
        interval_min = 60 if len(price_list) <= 25 else 15
        starts = slot_start_times(date_ref, len(price_list), interval_min)
        for i, price in enumerate(price_list):
            start_dt = starts[i]
            # The slot ends where the next one begins. Deriving it from the
            # neighbour rather than adding the nominal interval keeps the DST
            # hour from leaving a gap or an overlap.
            if i + 1 < len(starts):
                end_dt = starts[i + 1]
            else:
                end_dt = start_dt + timedelta(minutes=interval_min)
            if end_dt < now:
                continue
            parsed.append(
                {
                    "start": start_dt,
                    "end": end_dt,
                    "price": float(price),
                }
            )
        return parsed

    if isinstance(raw_today, str):
        raw_today = [float(x) for x in raw_today.split(",")]
    if isinstance(raw_tomorrow, str):
        raw_tomorrow = [float(x) for x in raw_tomorrow.split(",")]
    
    _LOGGER.debug("💰 Price data: today=%d slots, tomorrow=%d slots, tomorrow_valid=%s",
                  len(raw_today) if raw_today else 0,
                  len(raw_tomorrow) if raw_tomorrow else 0,
                  data["price_data"].get("tomorrow_valid", False))
    
    prices.extend(parse_price_list(raw_today, now.date()))
    if data["price_data"].get("tomorrow_valid", False) or raw_tomorrow:
        prices.extend(parse_price_list(raw_tomorrow, now.date() + timedelta(days=1)))

    if not prices:
        plan["should_charge_now"] = True
        plan["charging_summary"] = "No future price data found."
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
        return plan

    dept_dt = get_departure_time(data, now, data.get("calendar_events"))
    # Deliberately NOT "departure_time": the coordinator merges this plan into
    # the same dict that carries the user's settings, where "departure_time" is
    # the configured time-of-day. Reusing the name overwrote the setting with an
    # ISO datetime, which is why the Standard Departure Time entity always read
    # back 07:00 no matter what was stored.
    plan["plan_departure_time"] = dept_dt.isoformat()

    # State where the time came from as data. Consumers used to infer it by
    # looking for "(Calendar)" inside the human-readable summary, which is only
    # present on the detailed scheduling summary — so a maintenance-mode or
    # early-return plan reported the wrong source.
    _cal_time, _ = get_calendar_data(data.get("calendar_events") or [], now)
    _override = data.get(ENTITY_DEPARTURE_OVERRIDE)
    _standard = data.get(ENTITY_DEPARTURE_TIME)
    if _cal_time and _cal_time == dept_dt:
        plan["departure_source"] = "calendar"
    elif _override and _standard and _override != _standard:
        plan["departure_source"] = "next session override"
    else:
        plan["departure_source"] = "standard departure time"
    calc_window = [p for p in prices if p["start"] < dept_dt]
    
    _LOGGER.debug("🕐 Departure time: %s (from %s)",
                  dept_dt.strftime("%Y-%m-%d %H:%M"),
                  "calendar" if data.get("calendar_events") else "manual setting")
    _LOGGER.debug("📈 Price window: %d slots available until departure", len(calc_window))

    # If the departure is beyond our available price horizon, we can end up making
    # a suboptimal decision (e.g., charging immediately on expensive afternoon
    # prices while cheaper night prices haven't been published yet).
    # In that situation, wait for more price data *only if* there's still enough
    # time left to reach the target SoC.
    last_price_end = max((p["end"] for p in prices), default=None)
    price_horizon_covers_departure = bool(last_price_end and last_price_end >= dept_dt)
    
    _LOGGER.debug("🌅 Price horizon: last_price_end=%s, covers_departure=%s",
                  last_price_end.strftime("%Y-%m-%d %H:%M") if last_price_end else "None",
                  price_horizon_covers_departure)

    if not calc_window:
        plan["should_charge_now"] = True
        plan["charging_summary"] = "Departure passed. Charging."
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
        return plan

    cal_time, cal_soc = get_calendar_data(data.get("calendar_events", []), now)
    time_source = "(Calendar)" if cal_time and cal_time == dept_dt else "(Manual)"
    min_guaranteed = data.get(ENTITY_MIN_SOC, 20)
    status_note = ""

    if manual_override:
        final_target = data.get(ENTITY_TARGET_OVERRIDE, 80)
        status_note = "(Manual Override)"
        _LOGGER.debug("🎯 Target SOC: %d%% from MANUAL OVERRIDE", final_target)
    elif cal_soc is not None:
        final_target = cal_soc
        status_note = "(Calendar Event)"
        _LOGGER.debug("🎯 Target SOC: %d%% from CALENDAR EVENT", final_target)
    else:
        final_target = data.get(ENTITY_TARGET_SOC, 80)
        if price_horizon_covers_departure:
            status_note = "(Smart)"
            min_price_in_window = min(slot["price"] for slot in calc_window)
            limit_1 = data.get(ENTITY_PRICE_LIMIT_1, 0.5)
            target_1 = data.get(ENTITY_TARGET_SOC_1, 100)
            limit_2 = data.get(ENTITY_PRICE_LIMIT_2, 1.5)
            target_2 = data.get(ENTITY_TARGET_SOC_2, 80)
            _LOGGER.debug("🎯 Target SOC: base=%d%%, min_price=%.2f, limit_1=%.2f→%d%%, limit_2=%.2f→%d%%",
                          final_target, min_price_in_window, limit_1, target_1, limit_2, target_2)
            if min_price_in_window <= limit_1:
                final_target = max(final_target, target_1)
                _LOGGER.debug("   → Opportunistic Level 1 triggered: target=%d%%", final_target)
            elif min_price_in_window <= limit_2:
                final_target = max(final_target, target_2)
                _LOGGER.debug("   → Opportunistic Level 2 triggered: target=%d%%", final_target)
        else:
            status_note = "(Smart - Waiting for prices)"
            _LOGGER.debug("🎯 Target SOC: %d%% (waiting for more price data)", final_target)

    final_target = max(final_target, min_guaranteed)
    plan["planned_target_soc"] = final_target
    current_soc = data.get("car_soc", 0) or 0.0
    
    _LOGGER.debug("🔋 Battery: current_soc=%.1f%%, target=%.1f%%, need=%.1f%%",
                  current_soc, final_target, max(0, final_target - current_soc))

    selected_slots = []
    selected_start_times = set()
    price_limit_high = data.get(ENTITY_PRICE_LIMIT_2, 1.5)

    # Calculate extra slots needed to compensate for overload prevention minutes
    extra_slots_needed = 0
    if overload_prevention_minutes > 0:
        slot_duration_min = (
            15 if len(raw_today) > 25 else 60
        )  # 15 min slots or 60 min slots
        extra_slots_needed = math.ceil(overload_prevention_minutes / slot_duration_min)

    if current_soc >= final_target:
        plan["charging_summary"] = (
            f"Target reached ({int(current_soc)}%). Maintenance mode active."
        )
        _LOGGER.debug("✅ Target ALREADY REACHED - entering maintenance mode (price_limit=%.2f)", price_limit_high)
        for slot in calc_window:
            if slot["price"] <= price_limit_high:
                selected_start_times.add(slot["start"])
                selected_slots.append(slot)
        _LOGGER.debug("   → Selected %d maintenance slots at price <= %.2f", len(selected_slots), price_limit_high)
        for slot in calc_window:
            if (
                slot["start"] in selected_start_times
                and slot["start"] <= now < slot["end"]
            ):
                plan["should_charge_now"] = True
                _LOGGER.debug("   → Current slot qualifies for maintenance charging")
                break
        if not data.get("car_plugged"):
            plan["should_charge_now"] = False
        _LOGGER.debug("⚡ DECISION: Maintenance mode → should_charge=%s", plan["should_charge_now"])
    else:
        # Get effective charger loss (learned or configured)
        effective_loss, is_learning = get_effective_charger_loss(config_settings, learning_state)
        
        soc_needed = final_target - current_soc
        kwh_needed = (soc_needed / 100.0) * config_settings["car_capacity"]
        efficiency = 1.0 - (effective_loss / 100.0)
        kwh_to_pull = kwh_needed / efficiency

        # What a slot will actually deliver — measured where the sensors to
        # measure it exist, otherwise derived from the fuse as before.
        est_power_kw, power_source = get_charging_power_estimate(
            config_settings, learning_state
        )
        plan["charge_power_kw"] = round(est_power_kw, 2)
        plan["charge_power_source"] = power_source
        hours_needed = kwh_to_pull / est_power_kw
        
        _LOGGER.debug("⚡ Energy calculation: kwh_needed=%.2f, efficiency=%.2f (%.1f%% loss), kwh_to_pull=%.2f",
                      kwh_needed, efficiency, effective_loss, kwh_to_pull)
        _LOGGER.debug("⏱️  Timing: est_power=%.2f kW (%s), hours_needed=%.2f, overload_prevention_min=%.1f",
                      est_power_kw, power_source, hours_needed, overload_prevention_minutes)

        # If we don't have price data all the way to departure, prefer waiting for
        # updated price data as long as we can still reach the target before departure.
        # This avoids starting charging on expensive prices when cheaper prices may
        # appear in the yet-unknown portion of the window.
        if not price_horizon_covers_departure:
            latest_start_dt = dept_dt - timedelta(
                hours=hours_needed + (overload_prevention_minutes / 60.0)
            )
            _LOGGER.debug("🕐 Price horizon does NOT cover departure. Latest start: %s (now: %s)",
                          latest_start_dt.strftime("%H:%M"), now.strftime("%H:%M"))
            
            # CRITICAL: If departure is TODAY, NEVER wait for tomorrow's prices.
            # Plan with whatever prices we have available.
            departure_is_today = dept_dt.date() == now.date()
            
            should_wait_for_prices = False
            if departure_is_today:
                _LOGGER.info(
                    "🚗 Departure is TODAY (%s at %s). Will NOT wait for tomorrow's prices. "
                    "Planning with available data for today.",
                    dept_dt.strftime("%Y-%m-%d"),
                    dept_dt.strftime("%H:%M")
                )
                should_wait_for_prices = False
            else:
                # Departure is tomorrow or later
                # If we're early in the day (before 08:00) AND have full prices for today,
                # don't wait - plan with what we have and charge during cheap night hours.
                end_of_today_threshold = datetime.combine(now.date(), time(23, 0, 0))
                departure_is_tomorrow_or_later = dept_dt.date() > now.date()
                have_prices_for_rest_of_today = last_price_end and last_price_end >= end_of_today_threshold
                early_in_day = now.hour < 8  # Before 08:00
                
                if departure_is_tomorrow_or_later and have_prices_for_rest_of_today and early_in_day:
                    _LOGGER.info(
                        "📅 Departure is on %s (tomorrow+), but we're early in the day (hour %d) "
                        "and have full prices for today (%s until %s). "
                        "Planning with available data instead of waiting for tomorrow's prices.",
                        dept_dt.strftime("%Y-%m-%d"),
                        now.hour,
                        now.strftime("%Y-%m-%d"),
                        last_price_end.strftime("%Y-%m-%d %H:%M") if last_price_end else "unknown"
                    )
                    should_wait_for_prices = False
                elif now < latest_start_dt:
                    should_wait_for_prices = True
            
            if should_wait_for_prices:
                plan["should_charge_now"] = False
                horizon_str = last_price_end.strftime("%H:%M") if last_price_end else "unknown"
                
                # Build informative waiting message
                current_soc = data.get("car_soc", 0)
                plugged = data.get("car_plugged", False)
                plugged_str = "plugged in" if plugged else "NOT PLUGGED IN"
                
                summary_parts = [
                    f"Waiting for additional price data before planning.",
                    f"Car: {int(current_soc)}% SoC, {plugged_str}.",
                    f"Known prices until {horizon_str}; departure at {dept_dt.strftime('%H:%M')} {time_source}.",
                ]
                
                # Add expected price arrival time if learned
                if expected_price_time:
                    summary_parts.append(f"Tomorrow's prices typically arrive around {expected_price_time}.")
                
                summary_parts.append(f"Latest start to reach target is ~{latest_start_dt.strftime('%H:%M')}.")
                
                plan["charging_summary"] = " ".join(summary_parts)
                _LOGGER.debug("⏸️  WAITING for more price data (still have time until %s)", latest_start_dt.strftime("%H:%M"))
                # Keep schedule visible (all inactive) but don't select slots yet.
                selected_slots = []
                selected_start_times = set()
                calc_window = [p for p in prices if p["start"] < dept_dt]
                # Skip slot selection logic for now.
                schedule_data = []
                for slot in prices:
                    schedule_data.append(
                        {
                            "start": slot["start"].isoformat(),
                            "end": slot["end"].isoformat(),
                            "price": slot["price"],
                            "active": False,
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

                if not data.get("car_plugged"):
                    plan["should_charge_now"] = False

                return plan

        slot_duration_hours = (
            calc_window[0]["end"] - calc_window[0]["start"]
        ).seconds / 3600.0
        if slot_duration_hours <= 0:
            slot_duration_hours = 1.0
        slots_needed = math.ceil(hours_needed / slot_duration_hours)
        
        _LOGGER.debug("📊 Slot calculation: duration=%.2fh, base_slots=%d, extra_slots=%d (overload compensation)",
                      slot_duration_hours, slots_needed - extra_slots_needed, extra_slots_needed)
        
        # Add extra slots to compensate for overload prevention minutes
        slots_needed += extra_slots_needed
        
        # Add safety buffer during learning phase (30 minutes)
        if is_learning:
            slot_duration_min = 15 if len(raw_today) > 25 else 60
            buffer_slots = 2 if slot_duration_min == 15 else 1  # 30 minutes
            slots_needed += buffer_slots
            _LOGGER.debug("⏰ Learning phase: adding %d slots (30min safety buffer)", buffer_slots)
        
        selected_slots = select_charging_slots(
            calc_window,
            slots_needed,
            contiguous=config_settings.get("contiguous_charging", False),
            prefer_late=config_settings.get("prefer_late_slots", False),
        )
        selected_start_times = {s["start"] for s in selected_slots}
        
        if selected_slots:
            prices_str = ", ".join([f"{s['start'].strftime('%H:%M')}→{s['price']:.2f}" for s in sorted(selected_slots, key=lambda x: x['start'])])
            _LOGGER.debug("✅ Selected %d cheapest slots: %s", len(selected_slots), prices_str)
            # Show what current slot looks like
            current_slot_info = None
            for slot in calc_window:
                if slot["start"] <= now < slot["end"]:
                    current_slot_info = f"{slot['start'].strftime('%H:%M')}→{slot['price']:.2f}"
                    break
            _LOGGER.debug("📍 Current slot: %s (is_selected=%s)", 
                         current_slot_info if current_slot_info else "None",
                         "YES" if any(s["start"] <= now < s["end"] for s in selected_slots) else "NO")

        # Check Buffer Logic in Coordinator side or here?
        # Logic is simpler here:
        session_end_time = (
            max(s["end"] for s in selected_slots) if selected_slots else None
        )

        for slot in calc_window:
            if (
                slot["start"] in selected_start_times
                and slot["start"] <= now < slot["end"]
            ):
                plan["should_charge_now"] = True
                break

        plan["session_end_time"] = (
            session_end_time.isoformat() if session_end_time else None
        )

        summary_lines = []
        total_plan_cost = 0.0
        extra_fee = data.get(ENTITY_PRICE_EXTRA_FEE, 0.0)
        vat_pct = data.get(ENTITY_PRICE_VAT, 0.0)
        currency = config_settings.get("currency", "SEK")
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
                    kwh_batt_this_slot / config_settings["car_capacity"]
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

            # What the same charge would have cost starting right now, taking
            # slots as they come. The difference is what the scheduling bought.
            baseline_cost = cost_of_slots(
                [s for s in calc_window if s["end"] > now],
                kwh_to_pull,
                kwh_grid_per_slot_max,
                extra_fee,
                vat_pct,
            )
            plan["planned_cost"] = round(total_plan_cost, 2)
            plan["baseline_cost"] = round(baseline_cost, 2)
            plan["estimated_savings"] = round(baseline_cost - total_plan_cost, 2)
            plan["currency"] = currency

            summary_lines.append(
                f"**Departure:** {dept_dt.strftime('%H:%M')} {time_source}"
            )
            summary_lines.append(f"**Target:** {int(final_target)}% {status_note}")
            summary_lines.append(
                f"**Total Estimated Cost:** {total_plan_cost:.2f} {currency} {cost_note}"
            )
            if plan.get("estimated_savings", 0) > 0.01:
                summary_lines.append(
                    f"**Saved vs charging now:** {plan['estimated_savings']:.2f} {currency}"
                )
            summary_lines.append("")
            for b in blocks:
                start_s = b["soc_start"]
                end_s = min(100, start_s + b["soc_gain"])
                if end_s > final_target:
                    end_s = final_target
                avg_p = b["avg_price_acc"] / b["count"]
                line = (
                    f"**{b['start'].strftime('%H:%M')} - {b['end'].strftime('%H:%M')}**\n"
                    f"SoC: {int(start_s)}% → {int(end_s)}%\n"
                    f"Cost: {b['cost']:.2f} {currency} (Avg: {avg_p:.2f})"
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
        _LOGGER.debug("🔌 Car NOT plugged - forcing should_charge_now=False")

    if plan["should_charge_now"] and "amps" not in plan:
        plan["amps"] = config_settings.get("max_fuse", 16.0)
    
    _LOGGER.debug("⚡ ===== FINAL DECISION: should_charge_now=%s, target_soc=%d%% =====",
                  plan["should_charge_now"], plan.get("planned_target_soc", 0))

    return plan
