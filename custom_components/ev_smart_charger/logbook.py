"""Describe logbook events."""

from homeassistant.core import callback
from homeassistant.const import ATTR_NAME
from .const import DOMAIN


@callback
def async_describe_events(hass, async_describe_event):
    """Describe logbook events."""

    @callback
    def async_describe_logbook_event(event):
        """Describe a logbook event."""
        return {
            "name": event.data.get(ATTR_NAME, "EV Smart Charger"),
            "message": event.data.get("message"),
        }

    async_describe_event(DOMAIN, f"{DOMAIN}_log_event", async_describe_logbook_event)
