"""AromaTech Scent Diffuser integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from .coordinator import AromaTechCoordinator
from .core.const import (
    CONF_HEALTH_CHECK_INTERVAL,
    CONF_TIME_SYNC,
    CONF_USES_PAIR_CODE,
    DEFAULT_HEALTH_CHECK_INTERVAL,
    DEFAULT_PASSWORD,
    DEFAULT_TIME_SYNC,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type AromaTechConfigEntry = ConfigEntry[AromaTechCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: AromaTechConfigEntry
) -> bool:
    """Set up AromaTech from a config entry."""
    address: str = entry.data[CONF_MAC]

    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is None:
        raise ConfigEntryNotReady(
            f"AromaTech device {address} not found. "
            "Make sure it is powered and in Bluetooth range."
        )

    coordinator = AromaTechCoordinator(
        hass,
        ble_device,
        entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
        health_check_interval=entry.options.get(
            CONF_HEALTH_CHECK_INTERVAL, DEFAULT_HEALTH_CHECK_INTERVAL
        ),
        time_sync=entry.options.get(CONF_TIME_SYNC, DEFAULT_TIME_SYNC),
        login_uses_pair_code=entry.data.get(CONF_USES_PAIR_CODE),
    )

    # Seed presence info from the most recent advertisement
    if service_info := bluetooth.async_last_service_info(
        hass, address, connectable=True
    ):
        coordinator.async_handle_advertisement(
            service_info, bluetooth.BluetoothChange.ADVERTISEMENT
        )

    try:
        await coordinator.async_initialize()
    except UpdateFailed as err:
        raise ConfigEntryNotReady(
            f"Could not connect to AromaTech device {address}: {err}"
        ) from err

    _LOGGER.info(
        "Initialized AromaTech device: %s (Protocol v%s)",
        coordinator.device_name or coordinator.mac,
        coordinator.info.blue_version,
    )

    # Remember which login variant the device accepted so future setups
    # need a single login write (the device beeps on every write)
    if entry.data.get(CONF_USES_PAIR_CODE) != coordinator.login_uses_pair_code:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_USES_PAIR_CODE: coordinator.login_uses_pair_code},
        )

    entry.runtime_data = coordinator

    # Advertisement callback: presence updates, reconnection trigger, and
    # zombie connection detection (the device only advertises when it has
    # no active connection)
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            coordinator.async_handle_advertisement,
            {"address": address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    # Mark the device unavailable when no scanner sees it anymore
    entry.async_on_unload(
        bluetooth.async_track_unavailable(
            hass, coordinator.async_handle_unavailable, address, connectable=True
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: AromaTechConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_disconnect()
    return unload_ok
