"""Config flow for EV Smart Charger integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    DeviceSelector,
    DeviceSelectorConfig,
)

from .const import (
    DOMAIN,
    CONF_CAR_SOC_SENSOR,
    CONF_CAR_PLUGGED_SENSOR,
    CONF_CAR_CAPACITY,
    CONF_CAR_CHARGING_LEVEL_ENTITY,
    CONF_CAR_ENTITY_ID,
    CONF_CAR_LIMIT_SERVICE,
    CONF_CAR_REFRESH_ACTION,
    CONF_CAR_REFRESH_INTERVAL,
    CONF_PRICE_SENSOR,
    CONF_P1_L1,
    CONF_P1_L2,
    CONF_P1_L3,
    CONF_ZAPTEC_LIMITER,
    CONF_ZAPTEC_STOP,
    CONF_ZAPTEC_RESUME,
    CONF_ZAPTEC_SWITCH,
    CONF_MAX_FUSE,
    CONF_CHARGER_LOSS,
    CONF_CURRENCY,
    CONF_CALENDAR_ENTITY,
    CONF_CHARGER_CURRENT_L1,
    CONF_CHARGER_CURRENT_L2,
    CONF_CHARGER_CURRENT_L3,
    REFRESH_NEVER,
    REFRESH_30_MIN,
    REFRESH_1_HOUR,
    REFRESH_2_HOURS,
    REFRESH_3_HOURS,
    REFRESH_4_HOURS,
    REFRESH_AT_TARGET,
    DEFAULT_CAPACITY,
    DEFAULT_MAX_FUSE,
    DEFAULT_LOSS,
    DEFAULT_CURRENCY,
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EV Smart Charger."""

    VERSION = 1

    def __init__(self):
        """Initialize the config flow."""
        self._data = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        return await self.async_step_charger()

    async def async_step_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Charger configuration step."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_car()

        return self.async_show_form(
            step_id="charger", data_schema=self._get_charger_schema()
        )

    async def async_step_car(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Car configuration step."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_meter()

        return self.async_show_form(step_id="car", data_schema=self._get_car_schema())

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Meter configuration step."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_price_calendar()

        return self.async_show_form(
            step_id="meter", data_schema=self._get_meter_schema()
        )

    async def async_step_price_calendar(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Price & Calendar configuration step."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="EV Charger Manager", data=self._data)

        return self.async_show_form(
            step_id="price_calendar", data_schema=self._get_price_calendar_schema()
        )

    # --- Schema Helpers ---

    @staticmethod
    def _get_charger_schema(defaults=None):
        if defaults is None:
            defaults = {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_ZAPTEC_LIMITER, default=defaults.get(CONF_ZAPTEC_LIMITER)
                ): EntitySelector(EntitySelectorConfig(domain="number")),
                vol.Optional(
                    CONF_ZAPTEC_SWITCH, default=defaults.get(CONF_ZAPTEC_SWITCH)
                ): EntitySelector(EntitySelectorConfig(domain="switch")),
                vol.Optional(
                    CONF_ZAPTEC_RESUME, default=defaults.get(CONF_ZAPTEC_RESUME)
                ): EntitySelector(
                    EntitySelectorConfig(domain=["button", "switch", "script"])
                ),
                vol.Optional(
                    CONF_ZAPTEC_STOP, default=defaults.get(CONF_ZAPTEC_STOP)
                ): EntitySelector(
                    EntitySelectorConfig(domain=["button", "switch", "script"])
                ),
            }
        )

    @staticmethod
    def _get_car_schema(defaults=None):
        if defaults is None:
            defaults = {}

        # Define options with hardcoded labels to ensure display
        refresh_options = [
            {"value": REFRESH_NEVER, "label": "Never"},
            {"value": REFRESH_30_MIN, "label": "Every 30 minutes"},
            {"value": REFRESH_1_HOUR, "label": "Every hour"},
            {"value": REFRESH_2_HOURS, "label": "Every 2 hours"},
            {"value": REFRESH_3_HOURS, "label": "Every 3 hours"},
            {"value": REFRESH_4_HOURS, "label": "Every 4 hours"},
            {"value": REFRESH_AT_TARGET, "label": "When target reached (Smart)"},
        ]

        return vol.Schema(
            {
                vol.Required(
                    CONF_CAR_SOC_SENSOR, default=defaults.get(CONF_CAR_SOC_SENSOR)
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    CONF_CAR_PLUGGED_SENSOR,
                    default=defaults.get(CONF_CAR_PLUGGED_SENSOR),
                ): EntitySelector(
                    EntitySelectorConfig(domain=["sensor", "binary_sensor"])
                ),
                vol.Required(
                    CONF_CAR_CAPACITY,
                    default=defaults.get(CONF_CAR_CAPACITY, DEFAULT_CAPACITY),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=10,
                        max=150,
                        step=1,
                        unit_of_measurement="kWh",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_CAR_CHARGING_LEVEL_ENTITY,
                    default=defaults.get(CONF_CAR_CHARGING_LEVEL_ENTITY),
                ): EntitySelector(EntitySelectorConfig(domain="number")),
                vol.Optional(
                    CONF_CAR_ENTITY_ID, default=defaults.get(CONF_CAR_ENTITY_ID)
                ): DeviceSelector(DeviceSelectorConfig()),
                vol.Optional(
                    CONF_CAR_LIMIT_SERVICE, default=defaults.get(CONF_CAR_LIMIT_SERVICE)
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                vol.Optional(
                    CONF_CAR_REFRESH_ACTION,
                    default=defaults.get(CONF_CAR_REFRESH_ACTION),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                # Use explicit labels here
                vol.Optional(
                    CONF_CAR_REFRESH_INTERVAL,
                    default=defaults.get(CONF_CAR_REFRESH_INTERVAL, REFRESH_NEVER),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=refresh_options, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }
        )

    @staticmethod
    def _get_meter_schema(defaults=None):
        if defaults is None:
            defaults = {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_P1_L1, default=defaults.get(CONF_P1_L1)
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="current")
                ),
                vol.Required(
                    CONF_P1_L2, default=defaults.get(CONF_P1_L2)
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="current")
                ),
                vol.Required(
                    CONF_P1_L3, default=defaults.get(CONF_P1_L3)
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="current")
                ),
                vol.Optional(
                    CONF_CHARGER_CURRENT_L1,
                    default=defaults.get(CONF_CHARGER_CURRENT_L1),
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="current")
                ),
                vol.Optional(
                    CONF_CHARGER_CURRENT_L2,
                    default=defaults.get(CONF_CHARGER_CURRENT_L2),
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="current")
                ),
                vol.Optional(
                    CONF_CHARGER_CURRENT_L3,
                    default=defaults.get(CONF_CHARGER_CURRENT_L3),
                ): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="current")
                ),
                vol.Required(
                    CONF_MAX_FUSE, default=defaults.get(CONF_MAX_FUSE, DEFAULT_MAX_FUSE)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=10,
                        max=100,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

    @staticmethod
    def _get_price_calendar_schema(defaults=None):
        if defaults is None:
            defaults = {}
        return vol.Schema(
            {
                vol.Optional(
                    CONF_PRICE_SENSOR, default=defaults.get(CONF_PRICE_SENSOR)
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    CONF_CALENDAR_ENTITY, default=defaults.get(CONF_CALENDAR_ENTITY)
                ): EntitySelector(EntitySelectorConfig(domain="calendar")),
                vol.Required(
                    CONF_CHARGER_LOSS,
                    default=defaults.get(CONF_CHARGER_LOSS, DEFAULT_LOSS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=50,
                        step=1,
                        unit_of_measurement="%",
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Required(
                    CONF_CURRENCY, default=defaults.get(CONF_CURRENCY, DEFAULT_CURRENCY)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["SEK", "EUR", "NOK", "DKK", "USD", "GBP"],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options via a Menu."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "charger": "Charger Settings",
                "car": "Car Settings",
                "meter": "Meter & Fuse Settings",
                "price_calendar": "Price & Planning",
            },
        )

    async def async_step_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current_config = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(
            step_id="charger",
            data_schema=ConfigFlow._get_charger_schema(current_config),
        )

    async def async_step_car(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current_config = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(
            step_id="car", data_schema=ConfigFlow._get_car_schema(current_config)
        )

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current_config = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(
            step_id="meter", data_schema=ConfigFlow._get_meter_schema(current_config)
        )

    async def async_step_price_calendar(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current_config = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(
            step_id="price_calendar",
            data_schema=ConfigFlow._get_price_calendar_schema(current_config),
        )
