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
        attrs: dict[str, Any] = {
            # Device identification
            "device_name": self.coordinator.device_name,
            "protocol_version": f"V{info.blue_version}",
            "connected": self.coordinator.connected,

            # Current operation state
            "current_intensity": self.coordinator.intensity,
            "max_intensity": info.max_grade,
            "fan_on": state.fan_on,

            # Device capabilities
            "oil_support": info.oil,
            "battery_support": info.battery,
            "custom_intensity_support": info.custom,
            "fan_control_support": info.fan,
            "multiple_aroma_support": info.many_aroma,
        }

        # Product and label info
        if state.product_name:
            attrs["product_name"] = state.product_name
        if state.device_label:
            attrs["device_label"] = state.device_label
        if state.device_identifier:
            attrs["device_identifier"] = state.device_identifier

        # Firmware versions
        if state.pcb_version:
            attrs["pcb_version"] = state.pcb_version
        if state.equipment_version:
            attrs["equipment_version"] = state.equipment_version

        # Oil information - expose each oil slot's data
        if state.oils:
            for i, oil in enumerate(state.oils, 1):
                prefix = f"oil_{i}" if len(state.oils) > 1 else "oil"
                if oil.name:
                    attrs[f"{prefix}_name"] = oil.name
                if oil.total > 0:
                    attrs[f"{prefix}_total"] = oil.total
                    attrs[f"{prefix}_remaining"] = oil.remainder
                    attrs[f"{prefix}_percentage"] = oil.percentage
                elif oil.remainder > 0:
                    # V2.0 devices may not report total
                    attrs[f"{prefix}_remaining"] = oil.remainder

        # Battery level (if supported)
        if info.battery and state.battery_level > 0:
            attrs["battery_level"] = state.battery_level

        # Connection/presence info
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
