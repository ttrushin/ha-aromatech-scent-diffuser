"""Base entity for AromaTech integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from ..coordinator import AromaTechCoordinator


class AromaTechEntity(CoordinatorEntity["AromaTechCoordinator"]):
    """Base entity for AromaTech devices.

    Uses CoordinatorEntity to automatically handle state updates
    from the DataUpdateCoordinator.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: AromaTechCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)

        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.mac)},
            identifiers={(DOMAIN, coordinator.mac)},
            manufacturer="AromaTech",
            model="AroMini BT Plus",
            name=coordinator.device_name or "AromaTech Diffuser",
        )
        self._attr_unique_id = f"{coordinator.mac.replace(':', '')}_{key}"

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_seen is not None
