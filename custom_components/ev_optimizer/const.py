"""Constants for the EV Optimizer integration."""

DOMAIN = "ev_optimizer"

# Configuration Keys
CONF_CAR_SOC_SENSOR = "car_soc_sensor"
CONF_CAR_PLUGGED_SENSOR = "car_plugged_sensor"
CONF_CAR_CAPACITY = "car_capacity"
CONF_CAR_CHARGING_LEVEL_ENTITY = "car_charging_level_entity"

# Car Integration Settings (Merged)
CONF_CAR_ENTITY_ID = "car_entity_id"  # Shared Vehicle Entity/Device
CONF_CAR_LIMIT_SERVICE = (
    "car_limit_service"  # Service to set limit (e.g. set_charge_limits)
)
CONF_CAR_REFRESH_ACTION = (
    "car_refresh_action"  # Service to force refresh (e.g. force_update)
)
CONF_CAR_REFRESH_INTERVAL = "car_refresh_interval"  # Refresh Strategy

CONF_PRICE_SENSOR = "price_sensor"
CONF_P1_L1 = "p1_l1_sensor"
CONF_P1_L2 = "p1_l2_sensor"
CONF_P1_L3 = "p1_l3_sensor"
CONF_ZAPTEC_LIMITER = "zaptec_current_limiter"
CONF_ZAPTEC_STOP = "zaptec_stop_button"
CONF_ZAPTEC_RESUME = "zaptec_resume_button"
CONF_ZAPTEC_SWITCH = "zaptec_switch"
CONF_MAX_FUSE = "max_fuse"
CONF_CHARGER_LOSS = "charger_loss"
CONF_CURRENCY = "currency"
CONF_CALENDAR_ENTITY = "calendar_entity"

# Charger Current Sensors
CONF_CHARGER_CURRENT_L1 = "charger_current_l1"
CONF_CHARGER_CURRENT_L2 = "charger_current_l2"
CONF_CHARGER_CURRENT_L3 = "charger_current_l3"

# Defaults
DEFAULT_NAME = "EV Optimizer"
DEFAULT_CAPACITY = 64.0
DEFAULT_MAX_FUSE = 20.0
DEFAULT_LOSS = 0.0  # Changed to 0% - will be learned
DEFAULT_CURRENCY = "SEK"
DEFAULT_DEPARTURE_TIME = "07:00"
DEFAULT_TARGET_SOC = 80
DEFAULT_PRICE_LIMIT_1 = 0.5
DEFAULT_TARGET_SOC_1 = 100
DEFAULT_PRICE_LIMIT_2 = 1.5
DEFAULT_TARGET_SOC_2 = 70
DEFAULT_MIN_SOC = 20

# Refresh Options
REFRESH_NEVER = "never"
REFRESH_30_MIN = "30_min"
REFRESH_1_HOUR = "1_hour"
REFRESH_2_HOURS = "2_hours"
REFRESH_3_HOURS = "3_hours"
REFRESH_4_HOURS = "4_hours"
REFRESH_AT_TARGET = "at_target"

# --- NEW CONSTANTS FOR ENTITIES ---
ENTITY_TARGET_SOC = "target_soc"
ENTITY_TARGET_OVERRIDE = "target_soc_override"

ENTITY_DEPARTURE_TIME = "departure_time"
ENTITY_DEPARTURE_OVERRIDE = "departure_override"

ENTITY_SMART_SWITCH = "smart_charging_active"
ENTITY_BUTTON_CLEAR_OVERRIDE = "clear_manual_override"

# Price/Cost Settings
ENTITY_PRICE_EXTRA_FEE = "price_extra_fee"
ENTITY_PRICE_VAT = "price_vat"

# Price Thresholds
ENTITY_PRICE_LIMIT_1 = "price_limit_1"
ENTITY_TARGET_SOC_1 = "target_soc_1"

ENTITY_PRICE_LIMIT_2 = "price_limit_2"
ENTITY_TARGET_SOC_2 = "target_soc_2"

ENTITY_MIN_SOC = "min_guaranteed_soc"

# Debug Simulation Fields
ENTITY_DEBUG_CURRENT_TIME = "debug_current_time"
ENTITY_DEBUG_DEPARTURE_TIME = "debug_departure_time"
ENTITY_DEBUG_CURRENT_SOC = "debug_current_soc"
ENTITY_DEBUG_TARGET_SOC = "debug_target_soc"

# Learning State Keys (for persistence)
LEARNING_CHARGER_LOSS = "learned_charger_loss"
LEARNING_CONFIDENCE = "loss_confidence_level"
LEARNING_SESSIONS = "loss_learning_sessions"  # Completed plug-in sessions
LEARNING_MEASUREMENTS = "loss_measurements"  # Efficiency measurements taken
LEARNING_LOCKED = "loss_locked"
LEARNING_HISTORY = "learning_history"
LEARNING_LAST_REFRESH = "last_refresh_time"
LEARNING_PRICE_ARRIVAL = "price_arrival_times"  # Track when tomorrow's prices arrive
LEARNING_MODEL_VERSION = "learning_model_version"

# Bumped whenever the meaning of the learned loss changes. A stored state from
# an older model is discarded rather than carried forward, because the number
# was fitted against a different estimator and is not comparable.
#
#   1 -> 2: the virtual SoC estimator used the loss from entry.data while the
#           planner used the learned value, so the loop adjusted a figure that
#           did not feed the quantity it measured and could never converge.
CURRENT_LEARNING_MODEL = 2

# Bounds for the learned loss. The figure absorbs every source of model error
# (charger loss, a mis-configured pack capacity, mains voltage away from 230 V,
# phase count), so a small negative correction is physically meaningful: it
# means the estimator under-predicts the car's real SoC gain.
MIN_LEARNED_LOSS = -10.0
MAX_LEARNED_LOSS = 30.0

# A measurement window must contain at least this much predicted SoC gain
# before it says anything about efficiency. Below it, sensor quantisation
# (the car reports whole percent) dominates the signal.
MIN_LEARNING_WINDOW_SOC = 3.0
