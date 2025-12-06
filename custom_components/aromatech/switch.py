"""Switch platform for AromaTech integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up AromaTech switch from a config entry."""
    device: Device = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([AromaTechSwitch(device)])


class AromaTechSwitch(AromaTechEntity, SwitchEntity):
    """Switch entity for AromaTech diffuser power control."""

    _attr_icon = "mdi:air-filter"
    _attr_translation_key = "power"

    def __init__(self, device: Device) -> None:
        """Initialize the switch."""
        super().__init__(device, "power")

    def internal_update(self) -> None:
        """Handle device state update."""
        self._attr_is_on = self.device.is_on

        if self.hass:
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return True if the diffuser is on."""
        return self.device.is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        info = self.device.info
        attrs = {
            "device_name": self.device.device_name,
            "protocol_version": info.blue_version,
            "max_intensity": info.max_grade,
            "current_intensity": self.device.intensity,
            "oil_support": info.oil,
            "battery_support": info.battery,
            "custom_intensity_support": info.custom,
            "fan_control_support": info.fan,
            "multiple_aroma_support": info.many_aroma,
            "connected": self.device.connected,
        }

        # Add version info if available
        if self.device.pcb_version:
            attrs["pcb_version"] = self.device.pcb_version
        if self.device.equipment_version:
            attrs["equipment_version"] = self.device.equipment_version

        # Add connection info
        if self.device.last_seen:
            attrs["last_seen"] = self.device.last_seen.isoformat()
        if self.device.rssi is not None:
            attrs["rssi"] = self.device.rssi

        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the diffuser."""
        try:
            if not await self.device.async_login():
                raise HomeAssistantError("Failed to connect to diffuser")

            await self.device.async_power_on(self.device.intensity)
            _LOGGER.info("Turned on diffuser with intensity %d", self.device.intensity)
        except Exception as e:
            _LOGGER.error("Failed to turn on diffuser: %s", e)
            raise HomeAssistantError(f"Failed to turn on diffuser: {e}") from e
        finally:
            await self.device.async_disconnect()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the diffuser."""
        try:
            if not await self.device.async_login():
                raise HomeAssistantError("Failed to connect to diffuser")

            await self.device.async_power_off()
            _LOGGER.info("Turned off diffuser")
        except Exception as e:
            _LOGGER.error("Failed to turn off diffuser: %s", e)
            raise HomeAssistantError(f"Failed to turn off diffuser: {e}") from e
        finally:
            await self.device.async_disconnect()
