"""Binary sensor platform for AromaTech integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AromaTechConfigEntry
from .coordinator import AromaTechCoordinator
from .core.entity import AromaTechEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AromaTechConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AromaTech binary sensors from a config entry."""
    async_add_entities([AromaTechConnectivitySensor(config_entry.runtime_data)])


class AromaTechConnectivitySensor(AromaTechEntity, BinarySensorEntity):
    """Reports whether Home Assistant holds a BLE connection to the diffuser."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AromaTechCoordinator) -> None:
        """Initialize the connectivity sensor."""
        super().__init__(coordinator, "connected")

    @property
    def is_on(self) -> bool:
        """Return True if connected to the device."""
        return self.coordinator.connected

    @property
    def available(self) -> bool:
        """Always available - 'off' already conveys the disconnected state."""
        return True
