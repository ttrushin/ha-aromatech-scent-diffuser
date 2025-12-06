"""Base entity for AromaTech integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN
from .device import Device


class AromaTechEntity(Entity):
    """Base entity for AromaTech devices."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, device: Device, attr: str) -> None:
        """Initialize the entity."""
        self.device = device
        self.attr = attr

        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, device.mac)},
            identifiers={(DOMAIN, device.mac)},
            manufacturer="AromaTech",
            model="AroMini BT Plus",
            name=device.device_name or "AromaTech Diffuser",
        )
        self._attr_unique_id = f"{device.mac.replace(':', '')}_{attr}"
        self.entity_id = f"{DOMAIN}.{self._attr_unique_id}"

        self.internal_update()
        device.register_update_callback(self.internal_update)

    def internal_update(self) -> None:
        """Handle device state update. Override in subclasses."""
        pass

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.device.last_seen is not None
