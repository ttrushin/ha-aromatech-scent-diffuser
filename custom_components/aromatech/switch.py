"""Switch platform for AromaTech integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up AromaTech switch from a config entry."""
    coordinator: AromaTechCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([AromaTechSwitch(coordinator)])


class AromaTechSwitch(AromaTechEntity, SwitchEntity):
    """Switch entity for AromaTech diffuser power control."""

    _attr_icon = "mdi:air-filter"
    _attr_translation_key = "power"

    def __init__(self, coordinator: AromaTechCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, "power")

    @property
    def is_on(self) -> bool:
        """Return True if the diffuser is on."""
        return self.coordinator.is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        info = self.coordinator.info
        state = self.coordinator.state
        attrs = {
            "device_name": self.coordinator.device_name,
            "protocol_version": info.blue_version,
            "max_intensity": info.max_grade,
            "current_intensity": self.coordinator.intensity,
            "oil_support": info.oil,
            "battery_support": info.battery,
            "custom_intensity_support": info.custom,
            "fan_control_support": info.fan,
            "multiple_aroma_support": info.many_aroma,
            "connected": self.coordinator.connected,
        }

        # Add version info if available
        if state.pcb_version:
            attrs["pcb_version"] = state.pcb_version
        if state.equipment_version:
            attrs["equipment_version"] = state.equipment_version

        # Add connection info
        if self.coordinator.last_seen:
            attrs["last_seen"] = self.coordinator.last_seen.isoformat()
        if self.coordinator.rssi is not None:
            attrs["rssi"] = self.coordinator.rssi

        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the diffuser.

        Uses the currently selected intensity from the select entity.
        """
        try:
            # Use the stored intensity (set by the select entity)
            intensity = self.coordinator.intensity
            await self.coordinator.async_power_on(intensity)
        except Exception as err:
            _LOGGER.error("Failed to turn on diffuser: %s", err)
            raise HomeAssistantError(f"Failed to turn on diffuser: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the diffuser."""
        try:
            await self.coordinator.async_power_off()
        except Exception as err:
            _LOGGER.error("Failed to turn off diffuser: %s", err)
            raise HomeAssistantError(f"Failed to turn off diffuser: {err}") from err
