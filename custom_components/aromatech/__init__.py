"""AromaTech Scent Diffuser integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .coordinator import AromaTechCoordinator
from .core.const import DEFAULT_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "select"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AromaTech from a config entry."""
    coordinators: dict[str, AromaTechCoordinator] = hass.data.setdefault(DOMAIN, {})

    @callback
    def update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle BLE advertisement."""
        _LOGGER.debug("BLE update: %s %s", change, service_info.advertisement)

        if coordinator := coordinators.get(entry.entry_id):
            # Update BLE device reference in case it changed
            coordinator.update_ble_device(service_info.device)
            # Update presence info
            coordinator.update_ble(service_info.advertisement)
            return

        # First advertisement - create coordinator
        password = entry.data.get("password", DEFAULT_PASSWORD)
        coordinators[entry.entry_id] = coordinator = AromaTechCoordinator(
            hass,
            service_info.device,
            password,
        )
        coordinator.update_ble(service_info.advertisement)

        # Initialize device and setup platforms
        hass.async_create_task(_async_init_device(hass, coordinator, entry))

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
    hass: HomeAssistant, coordinator: AromaTechCoordinator, entry: ConfigEntry
) -> None:
    """Initialize device - read name, version, capabilities."""
    try:
        await coordinator.async_read_device_info()
        _LOGGER.info(
            "Initialized AromaTech device: %s (Protocol v%s)",
            coordinator.device_name or coordinator.mac,
            coordinator.info.blue_version,
        )
    except Exception as err:
        _LOGGER.error("Failed to initialize device: %s", err)
        await coordinator.async_disconnect()

    # Forward entry setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.entry_id in hass.data[DOMAIN]:
        # Disconnect if connected
        coordinator: AromaTechCoordinator = hass.data[DOMAIN][entry.entry_id]
        await coordinator.async_disconnect()

        # Unload platforms
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        hass.data[DOMAIN].pop(entry.entry_id)

    return True
