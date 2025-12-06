"""AromaTech Scent Diffuser integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .core.const import DEFAULT_PASSWORD, DOMAIN
from .core.device import Device

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "select"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AromaTech from a config entry."""
    devices = hass.data.setdefault(DOMAIN, {})

    @callback
    def update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle BLE advertisement."""
        _LOGGER.debug("BLE update: %s %s", change, service_info.advertisement)

        if device := devices.get(entry.entry_id):
            # Update presence info
            device.update_ble(service_info.advertisement)
            return

        # First advertisement - create device
        password = entry.data.get("password", DEFAULT_PASSWORD)
        devices[entry.entry_id] = device = Device(
            service_info.device,
            password,
        )
        device.update_ble(service_info.advertisement)

        # Initialize device and setup platforms
        hass.async_create_task(_async_init_device(hass, device, entry))

    # Register BLE callback for this device's MAC address
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            update_ble,
            {"address": entry.data["mac"], "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    return True


async def _async_init_device(
    hass: HomeAssistant, device: Device, entry: ConfigEntry
) -> None:
    """Initialize device - read name, version, capabilities."""
    try:
        if await device.async_login():
            await device.async_read_device_info()
            await device.async_disconnect()
            _LOGGER.info(
                "Initialized AromaTech device: %s (Protocol v%s)",
                device.device_name or device.mac,
                device.info.blue_version,
            )
    except Exception as e:
        _LOGGER.error("Failed to initialize device: %s", e)
        await device.async_disconnect()

    # Forward entry setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.entry_id in hass.data[DOMAIN]:
        # Disconnect if connected
        device = hass.data[DOMAIN][entry.entry_id]
        await device.async_disconnect()

        # Unload platforms
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        hass.data[DOMAIN].pop(entry.entry_id)

    return True
