"""Binary sensor platform for EV Optimizer.

These expose internal state that previously existed only as a flag on the
coordinator or as a line in the log: whether the manual override is engaged,
and the two ways the integration can silently drift away from what the car and
charger are really doing.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    ENTITY_TARGET_OVERRIDE,
    ENTITY_TARGET_SOC,
)
from .coordinator import EVSmartChargerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""
    coordinator: EVSmartChargerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            EVManualOverrideBinarySensor(coordinator),
            EVCarLimitDriftBinarySensor(coordinator),
            EVChargeEstimateDriftBinarySensor(coordinator),
        ]
    )


class EVManualOverrideBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Whether the next session's target came from the user, not the planner.

    The override lives on the coordinator and has no entity of its own, so
    anything outside the integration had to infer it by looking for
    "(Manual Override)" inside the charging summary text. This reports it
    directly.
    """

    _attr_has_entity_name = False
    _attr_name = "Manual Override Active"
    _attr_unique_id = "ev_optimizer_manual_override_active"
    _attr_icon = "mdi:hand-back-right"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.manual_override_active)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        summary = data.get("charging_summary", "")

        if self.coordinator.manual_override_active:
            source = "manual"
        elif "(Calendar Event)" in summary:
            source = "calendar"
        elif "(Smart - Waiting for prices)" in summary:
            source = "waiting_for_prices"
        elif "(Smart)" in summary:
            source = "smart"
        else:
            source = "unknown"

        return {
            "target_source": source,
            "planned_target_soc": data.get("planned_target_soc"),
            "next_session_target_soc": self.coordinator.user_settings.get(
                ENTITY_TARGET_OVERRIDE
            ),
            "standard_target_soc": self.coordinator.user_settings.get(
                ENTITY_TARGET_SOC
            ),
        }


class EVCarLimitDriftBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """The car's reported charge limit disagrees with the one we applied.

    Setting the limit is fire-and-forget — a successful service call only means
    Home Assistant accepted it, not that the car did — and the virtual SoC is
    clamped to the value we *believe* we set, so a silent disagreement lets the
    estimate run above the car's real ceiling.
    """

    _attr_has_entity_name = False
    _attr_name = "Car Limit Drift"
    _attr_unique_id = "ev_optimizer_car_limit_drift"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:battery-alert-variant"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.car_limit_drift_active)

    @property
    def extra_state_attributes(self) -> dict:
        applied = self.coordinator._last_applied_car_limit
        written_at = self.coordinator._car_limit_write_time
        age_minutes = None
        if written_at is not None:
            age_minutes = round(
                (datetime.now() - written_at).total_seconds() / 60.0, 1
            )

        return {
            "applied_limit": applied if applied > 0 else None,
            "reported_limit": self.coordinator.car_limit_reported,
            "applied_minutes_ago": age_minutes,
            "car_limit_entity": self.coordinator.conf_keys.get("car_limit"),
        }


class EVChargeEstimateDriftBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """The charger reports no current while we believe it is charging.

    The estimator falls back to the *commanded* current when the charger's
    per-phase sensors read zero, because those sensors can lag. If the car has
    simply stopped accepting charge, that fallback keeps adding energy which
    never flowed, and the virtual SoC climbs away from reality.
    """

    _attr_has_entity_name = False
    _attr_name = "Charge Estimate Drift"
    _attr_unique_id = "ev_optimizer_charge_estimate_drift"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chart-line-variant"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.charge_estimate_drift_active)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        since = self.coordinator._amps_fallback_since
        minutes = None
        if since is not None:
            minutes = round((datetime.now() - since).total_seconds() / 60.0, 1)

        return {
            "commanded_amps": self.coordinator._last_applied_amps,
            "measured_amps": [
                data.get("ch_l1"),
                data.get("ch_l2"),
                data.get("ch_l3"),
            ],
            "fallback_minutes": minutes,
            "virtual_soc": self.coordinator._virtual_soc,
            "reported_soc": self.coordinator._last_sensor_soc,
        }
