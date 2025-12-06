"""Select platform for AromaTech integration."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .core.const import DOMAIN
from .core.device import Device
from .core.entity import AromaTechEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AromaTech select from a config entry."""
    device: Device = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([AromaTechIntensitySelect(device)])


class AromaTechIntensitySelect(AromaTechEntity, SelectEntity):
    """Select entity for AromaTech diffuser intensity."""

    _attr_icon = "mdi:gauge"
    _attr_translation_key = "intensity"

    def __init__(self, device: Device) -> None:
        """Initialize the select."""
        super().__init__(device, "intensity")

    def internal_update(self) -> None:
        """Handle device state update."""
        self._attr_current_option = str(self.device.intensity)

        if self.hass:
            self.async_write_ha_state()

    @property
    def options(self) -> list[str]:
        """Return intensity options (1 through max_grade)."""
        return [str(i) for i in range(1, self.device.info.max_grade + 1)]

    @property
    def current_option(self) -> str:
        """Return the current intensity level."""
        return str(self.device.intensity)

    async def async_select_option(self, option: str) -> None:
        """Set the intensity level."""
        intensity = int(option)

        try:
            if not await self.device.async_login():
                raise HomeAssistantError("Failed to connect to diffuser")

            await self.device.async_set_intensity(intensity)
            _LOGGER.info("Set diffuser intensity to %d", intensity)
        except Exception as e:
            _LOGGER.error("Failed to set intensity: %s", e)
            raise HomeAssistantError(f"Failed to set intensity: {e}") from e
        finally:
            await self.device.async_disconnect()
