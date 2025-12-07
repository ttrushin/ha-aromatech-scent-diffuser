"""Select platform for AromaTech integration."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AromaTechCoordinator
from .core.const import DOMAIN
from .core.entity import AromaTechEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AromaTech select from a config entry."""
    coordinator: AromaTechCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([AromaTechIntensitySelect(coordinator)])


class AromaTechIntensitySelect(AromaTechEntity, SelectEntity):
    """Select entity for AromaTech diffuser intensity.

    This entity controls the intensity level of the diffuser.
    - When the switch is ON: changing intensity sends a command to the device
    - When the switch is OFF: changing intensity only stores the value locally,
      which will be used when the switch is turned on
    """

    _attr_icon = "mdi:gauge"
    _attr_translation_key = "intensity"

    def __init__(self, coordinator: AromaTechCoordinator) -> None:
        """Initialize the select."""
        super().__init__(coordinator, "intensity")

    @property
    def options(self) -> list[str]:
        """Return intensity options (1 through max_grade)."""
        return [str(i) for i in range(1, self.coordinator.info.max_grade + 1)]

    @property
    def current_option(self) -> str:
        """Return the current intensity level."""
        return str(self.coordinator.intensity)

    async def async_select_option(self, option: str) -> None:
        """Set the intensity level.

        If the diffuser is on, sends the command to change intensity immediately.
        If the diffuser is off, stores the value to be used when turned on.
        """
        intensity = int(option)

        if self.coordinator.is_on:
            # Diffuser is on
            # Send command to device
            try:
                await self.coordinator.async_set_intensity(intensity)
            except Exception as err:
                _LOGGER.error("Failed to set intensity: %s", err)
                raise HomeAssistantError(f"Failed to set intensity: {err}") from err
        else:
            # Diffuser is off, so just store the value locally
            # This value will be used when the switch is turned on
            self.coordinator.set_intensity_local(intensity)
            _LOGGER.debug(
                "Stored intensity %d (will be applied when turned on)", intensity
            )
