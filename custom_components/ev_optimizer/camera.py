"""Camera platform for EV Smart Charger."""

from __future__ import annotations

import os
import logging
from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EVSmartChargerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the camera platform."""
    coordinator: EVSmartChargerCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            EVReportCamera(coordinator, "last_session", "Last Session Report"),
            EVReportCamera(coordinator, "plan", "Charging Plan Report"),
        ]
    )


class EVReportCamera(CoordinatorEntity, Camera):
    """Camera entity that serves the generated thermal printer images."""

    def __init__(self, coordinator, type_key, name):
        """Initialize the camera."""
        super().__init__(coordinator)  # CoordinatorEntity
        Camera.__init__(self)  # Camera

        self._type_key = type_key
        self._attr_name = name
        self._attr_unique_id = f"ev_optimizer_camera_{type_key}"
        self._image_filename = f"ev_optimizer_{type_key}.png"
        self._attr_content_type = "image/png"

    @property
    def brand(self):
        """Return the camera brand."""
        return "EV Smart Charger"

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the bytes of the image file."""
        # Define path
        path = self.hass.config.path("www", self._image_filename)

        if not os.path.exists(path):
            return None

        try:
            with open(path, "rb") as file:
                return file.read()
        except Exception as e:
            _LOGGER.error(f"Could not read image file {path}: {e}")
            return None
