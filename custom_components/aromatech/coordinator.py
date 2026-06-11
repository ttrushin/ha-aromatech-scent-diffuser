"""DataUpdateCoordinator for AromaTech integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .core.const import (
    CHARACTERISTIC_UUID,
    CMD_LIMITS_V2,
    CMD_LIMITS_V3,
    CMD_LOGIN,
    CMD_READ_NAME,
    CMD_SCHEDULE_WRITE_V2,
    CMD_SCHEDULE_WRITE_V3,
    CMD_TIME_V2,
    CMD_TIME_V3,
    CMD_VERSION_V2,
    DATA_BURST_TIMEOUT,
    DEFAULT_AROMA_SLOT,
    DEFAULT_INTENSITY,
    PAIR_CODE,
    RESP_BUFFER_CLEAR,
    RESP_DEVICE_LABEL_V3,
    RESP_IDENTIFIER,
    RESP_INTENSITY_PRESETS,
    RESP_LIMITS_V2,
    RESP_LIMITS_V3,
    RESP_NAME_V2,
    RESP_NAME_V3,
    RESP_OIL_AMOUNTS_V3,
    RESP_OIL_NAMES_V3,
    RESP_OIL_V2,
    RESP_PRODUCT_NAME,
    RESP_SCHEDULE_V2,
    RESP_SCHEDULE_V3,
    RESP_VERSION_V3,
)

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

# Connection settings
COMMAND_TIMEOUT = 5.0
# Login response timeout, and how long to wait for a response to the
# plain-password login before falling back to password+pair code.
# The official app uses 500 ms for the fallback.
LOGIN_TIMEOUT = 2.0
LOGIN_FALLBACK_TIMEOUT = 0.5
# Disconnect after 30 minutes of idle when device is OFF
DISCONNECT_DELAY_OFF = 30 * 60  # 30 minutes in seconds
# The device only advertises while it has no active connection, so an
# advertisement received while we believe we are connected means our
# connection is actually dead. Advertisements can lag behind a fresh
# connection (proxy buffering), so ignore them for a grace period.
ADVERTISEMENT_STALE_GRACE = 30.0  # seconds


@dataclass
class OilInfo:
    """Oil/fragrance information for a single aroma slot."""

    name: str = ""
    total: int = 0
    remainder: int = 0

    @property
    def percentage(self) -> float:
        """Calculate oil remaining percentage."""
        if self.total <= 0:
            return 0.0
        return round((self.remainder / self.total) * 100, 1)


@dataclass
class Schedule:
    """Represents a diffuser schedule slot."""

    index: int = 1
    enabled: bool = False
    hour_on: int = 0
    minute_on: int = 0
    hour_off: int = 0
    minute_off: int = 0
    repeat_days: str = "0000000"  # 7-bit binary: Sun(MSB) to Sat(LSB)
    intensity: int = 1
    # V3.0 specific
    aroma: int = 1
    fan_enabled: bool = True
    total_fan: bool = False
    total_fog: bool = False


class DeviceInfo:
    """Device capabilities discovered during login."""

    def __init__(self) -> None:
        """Initialize device info."""
        self.blue_version: float = 3.0
        self.hid_version: bool = False
        self.oil: bool = False
        self.battery: bool = False
        self.custom: bool = False
        self.many_aroma: bool = False
        self.fan: bool = False
        self.max_grade: int = 5
        self.limits_loaded: bool = False
        self.custom_on_min: int = 0
        self.custom_on_max: int = 0
        self.custom_off_min: int = 0
        self.custom_off_max: int = 0


class DeviceState:
    """Current device operational state."""

    def __init__(self) -> None:
        """Initialize device state."""
        # Power and intensity
        self.is_on: bool = False
        self.fan_on: bool = False
        self.intensity: int = DEFAULT_INTENSITY
        self.active_schedule: int = 0  # Currently active schedule slot (0=none)

        # Device identification
        self.device_name: str = ""
        self.product_name: str = ""
        self.device_label: str = ""
        self.device_identifier: str = ""

        # Firmware versions
        self.pcb_version: str = ""
        self.equipment_version: str = ""

        # Oil information
        self.oils: list[OilInfo] = []
        self.battery_level: int = 0

        # Schedules
        self.schedules: list[Schedule] = []

    def reset_lists(self) -> None:
        """Reset list fields to empty lists for fresh data burst parsing."""
        self.oils = []
        self.schedules = []


class AromaTechCoordinator(DataUpdateCoordinator[None]):
    """Coordinator for AromaTech device communication.

    This coordinator maintains a persistent BLE connection to the device
    and coordinates all communication through a single connection.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        ble_device: BLEDevice,
        password: str,
        health_check_interval: int = 0,
        time_sync: bool = False,
        login_uses_pair_code: bool | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"AromaTech {ble_device.address}",
            # Push updates from BLE; optional polling acts as a health check
            update_interval=(
                timedelta(seconds=health_check_interval)
                if health_check_interval > 0
                else None
            ),
        )

        self._ble_device = ble_device
        self._password = password

        # Connection state
        self._client: BleakClient | None = None
        self._connection_lock = asyncio.Lock()
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._response_event = asyncio.Event()
        self._last_response: bytes = b""
        self._command_pending = False
        self._connecting = False
        self._connected_at: float = 0.0
        self._expected_disconnect = False

        # Reconnection state (for when device is ON and connection drops)
        self._reconnect_task: asyncio.Task | None = None
        self._shutting_down = False

        # Data burst collection state (for post-login data collection)
        self._collecting_data_burst = False
        self._data_burst_responses: list[bytes] = []

        # Device state
        self.info = DeviceInfo()
        self.state = DeviceState()
        self._logged_in = False

        # Whether the device's login expects the pair code suffix.
        # Learned on first successful login and remembered so subsequent
        # connects need exactly one login write (each write beeps).
        self.login_uses_pair_code = login_uses_pair_code

        # Clock sync is optional: every write makes the device beep, and the
        # device clock only matters for on-device schedules
        self._time_sync = time_sync
        self._time_synced = False

        # Presence tracking
        self.last_seen: datetime | None = None
        self.rssi: int | None = None
        # True while HA's bluetooth stack considers the device in range.
        # The device stops advertising while connected, so availability is
        # "connected OR recently advertising".
        self._advertising = True

    @property
    def mac(self) -> str:
        """Return the device MAC address."""
        return self._ble_device.address

    @property
    def connected(self) -> bool:
        """Return True if connected to the device."""
        return self._client is not None and self._client.is_connected

    @property
    def device_available(self) -> bool:
        """Return True if the device is reachable.

        The device stops advertising while it holds a connection, so it is
        considered available if we are connected OR it was recently seen
        advertising.
        """
        return self.connected or self._advertising

    @property
    def is_on(self) -> bool:
        """Return True if the diffuser is on."""
        return self.state.is_on

    @property
    def intensity(self) -> int:
        """Return the current intensity level."""
        return self.state.intensity

    @property
    def device_name(self) -> str:
        """Return the device name."""
        return self.state.device_name

    @callback
    def async_handle_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a BLE advertisement from the device.

        The device only advertises while it has no active connection, so an
        advertisement is both a presence signal and a "connectable right now"
        signal. It also doubles as a health check: seeing one while we think
        we are connected proves our connection is a zombie.
        """
        self._ble_device = service_info.device
        self.last_seen = dt_util.utcnow()
        if service_info.rssi is not None:
            self.rssi = service_info.rssi
        self._advertising = True

        self._async_evaluate_connection_from_advertisement()
        self.async_update_listeners()

    @callback
    def async_handle_unavailable(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        """Handle the device no longer being seen by any scanner."""
        self._advertising = False
        if not self.connected:
            _LOGGER.warning(
                "%s is no longer seen by any Bluetooth scanner and is not "
                "connected - marking unavailable",
                self._ble_device.address,
            )
        self.async_update_listeners()

    @callback
    def _async_evaluate_connection_from_advertisement(self) -> None:
        """Decide whether an advertisement should trigger a (re)connection."""
        if self._shutting_down or self._connecting:
            return

        if self.connected:
            # Advertisements can lag a fresh connection; only treat them as a
            # zombie signal once the connection has had time to settle.
            if (
                self.hass.loop.time() - self._connected_at
                < ADVERTISEMENT_STALE_GRACE
            ):
                return
            _LOGGER.warning(
                "Received advertisement from %s while connected - the "
                "connection is stale, tearing it down",
                self._ble_device.address,
            )
            self._async_schedule_reconnect(teardown=True)
        elif self.state.is_on:
            # Device should be connected (it's ON) but we lost the link
            self._async_schedule_reconnect()

    @callback
    def _async_schedule_reconnect(self, teardown: bool = False) -> None:
        """Schedule a reconnect task if one is not already running."""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = self.hass.async_create_task(
            self._async_reconnect(teardown)
        )

    async def _async_reconnect(self, teardown: bool) -> None:
        """Tear down a stale connection and reconnect if the device is ON."""
        async with self._connection_lock:
            if self._shutting_down:
                return
            if teardown:
                await self._async_disconnect_internal()
            if self.state.is_on and await self._async_ensure_connected():
                _LOGGER.info("Reconnected to %s", self._ble_device.address)
                # The post-login data burst refreshed the device state; if it
                # turned OFF while we were disconnected, arm the idle timer
                self._schedule_disconnect()
        self.async_update_listeners()

    async def _async_update_data(self) -> None:
        """Periodic connection health check (opt-in via integration options)."""
        if self._shutting_down:
            return

        # OFF and disconnected is a healthy idle state - nothing to check
        if not self.connected and not self.state.is_on:
            return

        async with self._connection_lock:
            if self.connected and self._client is not None:
                # Probe with a GATT read: it forces an ATT response from the
                # device, so a dead link fails it - and unlike command writes
                # it doesn't make the device beep.
                try:
                    await asyncio.wait_for(
                        self._client.read_gatt_char(CHARACTERISTIC_UUID),
                        timeout=COMMAND_TIMEOUT,
                    )
                    return
                except (BleakError, asyncio.TimeoutError) as err:
                    _LOGGER.warning(
                        "Health check read on %s failed (%s) - connection "
                        "is stale",
                        self._ble_device.address,
                        err,
                    )
                    await self._async_disconnect_internal()

            if self.state.is_on and await self._async_ensure_connected():
                self._schedule_disconnect()

    def _notification_handler(self, sender: int, data: bytes) -> None:
        """Handle notifications from the device."""
        _LOGGER.debug("Received notification: %s", data.hex())
        self._last_response = data
        self._response_event.set()

        # Collect responses during data burst phase
        if self._collecting_data_burst:
            self._data_burst_responses.append(data)
            return

        # Notifications outside a pending command are pushed by the device
        # itself (e.g., state changed via the mobile app or physical buttons)
        if not self._command_pending:
            self._handle_unsolicited_notification(data)

    def _handle_unsolicited_notification(self, data: bytes) -> None:
        """Parse a state update the device pushed on its own."""
        if len(data) == 0:
            return

        cmd = data[0]
        try:
            if cmd == RESP_SCHEDULE_V3:
                self._parse_schedule_v3(data)
            elif cmd == RESP_OIL_AMOUNTS_V3:
                self._parse_oil_amounts(data, [oil.name for oil in self.state.oils])
            elif cmd == RESP_SCHEDULE_V2:
                self._parse_schedule_v2(data)
            elif cmd == RESP_OIL_V2:
                self._parse_oil_v2(data)
            else:
                return
        except Exception as err:
            _LOGGER.warning(
                "Error parsing pushed notification 0x%02X: %s", cmd, err
            )
            return

        _LOGGER.debug(
            "Device pushed state update: is_on=%s, intensity=%d",
            self.state.is_on,
            self.state.intensity,
        )
        self._schedule_disconnect()
        self.async_update_listeners()

    def _cancel_disconnect_timer(self) -> None:
        """Cancel the pending disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None

    def _cancel_reconnect_task(self) -> None:
        """Cancel any pending reconnection task."""
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._reconnect_task = None

    def _schedule_disconnect(self) -> None:
        """Schedule a disconnection based on device state.

        - If device is ON: don't schedule disconnect (keep connection alive)
        - If device is OFF: disconnect after 30 minutes
        """
        self._cancel_disconnect_timer()

        if self.state.is_on:
            # Device is ON - keep connection alive, don't schedule disconnect
            _LOGGER.debug("Device is ON - keeping connection alive")
            return

        # Device is OFF - schedule disconnect after 30 minutes
        _LOGGER.debug("Device is OFF - scheduling disconnect in 30 minutes")
        self._disconnect_timer = self.hass.loop.call_later(
            DISCONNECT_DELAY_OFF,
            lambda: self.hass.async_create_task(self._async_disconnect_if_idle()),
        )

    async def _async_disconnect_if_idle(self) -> None:
        """Disconnect if device is OFF and no commands are pending."""
        async with self._connection_lock:
            # Double-check device is still OFF before disconnecting
            if self.state.is_on:
                _LOGGER.debug("Device turned ON - cancelling idle disconnect")
                return
            if self._client and self._client.is_connected:
                _LOGGER.debug("Disconnecting after 30 minute idle timeout (device OFF)")
                await self._async_disconnect_internal()

    def _on_disconnect(self, client: BleakClient) -> None:
        """Handle disconnection from the device."""
        if client is not self._client:
            # Callback from a client we already discarded
            _LOGGER.debug("Ignoring disconnect from stale client")
            return

        self._logged_in = False

        if self._expected_disconnect or self._shutting_down:
            _LOGGER.debug("Disconnected from %s", self._ble_device.address)
            return

        _LOGGER.warning(
            "Unexpectedly disconnected from %s", self._ble_device.address
        )

        # Try one immediate reconnect for transient drops. If it fails, the
        # device will start advertising (it has no connection now), and the
        # advertisement callback takes over from there.
        if self.state.is_on:
            self._async_schedule_reconnect()

        self.async_update_listeners()

    async def _async_ensure_connected(self) -> bool:
        """Ensure we have an active connection to the device."""
        self._cancel_disconnect_timer()

        if self._client and self._client.is_connected and self._logged_in:
            return True

        # Need to connect and/or login
        try:
            if not self._client or not self._client.is_connected:
                _LOGGER.debug("Establishing connection to %s", self._ble_device.address)
                self._connecting = True
                try:
                    client = await establish_connection(
                        BleakClient,
                        self._ble_device,
                        self._ble_device.address,
                        max_attempts=3,
                        disconnected_callback=self._on_disconnect,
                    )
                    self._client = client
                    await client.start_notify(
                        CHARACTERISTIC_UUID, self._notification_handler
                    )
                finally:
                    self._connecting = False
                self._connected_at = self.hass.loop.time()
                self._logged_in = False

            if not self._logged_in:
                if not await self._async_login():
                    await self._async_disconnect_internal()
                    return False

            return True

        except (BleakError, asyncio.TimeoutError) as err:
            _LOGGER.error("Failed to connect to %s: %s", self._ble_device.address, err)
            await self._async_disconnect_internal()
            return False

    async def _async_disconnect_internal(self) -> None:
        """Internal disconnect without lock."""
        self._logged_in = False
        if self._client:
            self._expected_disconnect = True
            try:
                if self._client.is_connected:
                    await self._client.stop_notify(CHARACTERISTIC_UUID)
                    await self._client.disconnect()
            except Exception as err:
                _LOGGER.debug("Error during disconnect: %s", err)
            finally:
                self._client = None
                self._expected_disconnect = False

    async def async_disconnect(self) -> None:
        """Disconnect from the device (called during unload)."""
        # Mark as shutting down to prevent reconnection attempts
        self._shutting_down = True
        self._cancel_disconnect_timer()
        self._cancel_reconnect_task()
        async with self._connection_lock:
            await self._async_disconnect_internal()

    async def _async_write_command(
        self, data: bytes, timeout: float = COMMAND_TIMEOUT
    ) -> bytes:
        """Write a command and wait for response."""
        if not self._client or not self._client.is_connected:
            _LOGGER.error("Cannot write command: not connected")
            return b""

        self._response_event.clear()
        self._last_response = b""
        self._command_pending = True

        try:
            _LOGGER.debug("Writing command: %s", data.hex())
            await self._client.write_gatt_char(CHARACTERISTIC_UUID, data)
            await asyncio.wait_for(self._response_event.wait(), timeout=timeout)
            return self._last_response
        except asyncio.TimeoutError:
            _LOGGER.debug("Command timeout waiting for response")
            return b""
        except Exception as err:
            _LOGGER.error("Failed to write command: %s", err)
            return b""
        finally:
            self._command_pending = False

    async def _async_write_command_no_response(self, data: bytes) -> bool:
        """Write a command without waiting for response."""
        if not self._client or not self._client.is_connected:
            _LOGGER.error("Cannot write command: not connected")
            return False

        try:
            _LOGGER.debug("Writing command (no response): %s", data.hex())
            await self._client.write_gatt_char(CHARACTERISTIC_UUID, data)
            return True
        except Exception as err:
            _LOGGER.error("Failed to write command: %s", err)
            return False

    def _login_command(self, use_pair_code: bool) -> bytes:
        """Build the login command, optionally suffixed with the pair code."""
        suffix = PAIR_CODE if use_pair_code else ""
        return bytes([CMD_LOGIN]) + (self._password + suffix).encode("utf-8")

    async def _async_login(self) -> bool:
        """Authenticate with the device and collect post-login data burst.

        The device beeps on login, so once we know which login variant the
        device expects (with or without the pair code suffix), we remember it
        and send exactly one login write on subsequent connects - the same
        single-beep behavior as the official app.
        """
        # Prepare for data burst collection
        self._data_burst_responses = []
        self._collecting_data_burst = True

        try:
            if self.login_uses_pair_code is None:
                # Protocol unknown: mimic the official app - plain password
                # first (V2.0), short fallback to password+pair code (V3.0)
                order = [False, True]
            else:
                # Known variant first; the other only as a safety net
                order = [self.login_uses_pair_code, not self.login_uses_pair_code]

            response = b""
            used_pair_code = order[0]
            for attempt, use_pair_code in enumerate(order):
                timeout = (
                    LOGIN_FALLBACK_TIMEOUT
                    if self.login_uses_pair_code is None and attempt == 0
                    else LOGIN_TIMEOUT
                )
                response = await self._async_write_command(
                    self._login_command(use_pair_code), timeout=timeout
                )
                if response:
                    used_pair_code = use_pair_code
                    break

            if response and len(response) > 0 and response[0] == CMD_LOGIN:
                login_state = self._parse_login_response(response)
                self._logged_in = login_state == 0

                if self._logged_in:
                    self.login_uses_pair_code = used_pair_code
                    _LOGGER.debug(
                        "Logged in successfully (pair code: %s). "
                        "Protocol version: %s",
                        used_pair_code,
                        self.info.blue_version,
                    )

                    # Wait for post-login data burst from device
                    # The device automatically sends all state data after login
                    await asyncio.sleep(DATA_BURST_TIMEOUT)

                    # Stop collecting and parse the data burst
                    self._collecting_data_burst = False
                    self._parse_data_burst()

                    # Optionally sync the device clock (once per HA session).
                    # Only needed for on-device schedules, and it costs a beep.
                    if self._time_sync and not self._time_synced:
                        await self._async_send_time()
                        self._time_synced = True
                    return True

            _LOGGER.error("Login failed")
            return False

        finally:
            self._collecting_data_burst = False

    def _parse_login_response(self, data: bytes) -> int:
        """Parse login response.

        Returns:
            login_state: 0=success, 1=failed, 2=error
        """
        response_data = data[1:]
        response_str = response_data.decode("utf-8", errors="ignore")

        _LOGGER.debug("Login response string: %s", response_str)

        if response_str == "ERROR":
            return 2

        if len(response_str) <= 2:
            self.info.hid_version = True
            self.info.blue_version = 2.0
            self.info.many_aroma = False
        else:
            try:
                self.info.blue_version = float(response_str[4:7])
            except ValueError:
                self.info.blue_version = 3.0

            if self.info.blue_version == 3.0 and len(data) > 13:
                feature_byte = data[13]
                self.info.oil = bool(feature_byte & 0x01)
                self.info.battery = bool(feature_byte & 0x02)
                self.info.custom = bool(feature_byte & 0x04)
                self.info.many_aroma = bool(feature_byte & 0x08)
                self.info.fan = bool(feature_byte & 0x10)

        if len(response_str) >= 9:
            return 0 if response_str[7:9] == PAIR_CODE[:2] else 1
        return 0

    def _parse_data_burst(self) -> None:
        """Parse all responses collected during post-login data burst.

        The device automatically sends a burst of data after login containing:
        - Device limits and capabilities (0x46)
        - Device name (0x42)
        - Product name (0x45)
        - Schedules (0x4A) - includes current power/fan state
        - Device label (0x43)
        - Intensity presets (0x47)
        - Oil names (0x48)
        - Oil amounts and battery (0x4B)
        - Version info (0x44)
        - Various status bytes (0x41, 0x4C, 0x4D, 0x4E, 0x50)
        """
        _LOGGER.debug(
            "Parsing data burst with %d responses", len(self._data_burst_responses)
        )

        # Reset lists for fresh data
        self.state.reset_lists()

        # Track oil names separately to correlate with amounts
        oil_names: list[str] = []

        for data in self._data_burst_responses:
            if len(data) == 0:
                continue

            cmd = data[0]

            try:
                if cmd == RESP_BUFFER_CLEAR:
                    # 0x40: Buffer clear - signals start of data burst, ignore
                    pass

                elif cmd == RESP_LIMITS_V3:
                    # 0x46: Device limits (max intensity, custom time limits)
                    self._parse_limits_response(data)

                elif cmd == RESP_NAME_V3:
                    # 0x42: Bluetooth device name
                    self.state.device_name = (
                        data[1:].decode("utf-8", errors="ignore").rstrip("\x00")
                    )

                elif cmd == RESP_PRODUCT_NAME:
                    # 0x45: Product/model name (e.g., "AROMINI BT PLUS")
                    self.state.product_name = (
                        data[1:].decode("utf-8", errors="ignore").rstrip("\x00")
                    )

                elif cmd == RESP_SCHEDULE_V3:
                    # 0x4A: Schedule data - also contains current power state
                    self._parse_schedule_v3(data)

                elif cmd == RESP_DEVICE_LABEL_V3:
                    # 0x43: Custom device label
                    self.state.device_label = (
                        data[1:].decode("utf-8", errors="ignore").rstrip("\x00")
                    )

                elif cmd == RESP_INTENSITY_PRESETS:
                    # 0x47: Intensity preset table - informational only
                    _LOGGER.debug("Received intensity presets: %s", data.hex())

                elif cmd == RESP_OIL_NAMES_V3:
                    # 0x48: Oil/aroma names (16 bytes per name)
                    oil_names = self._parse_oil_names(data)

                elif cmd == RESP_OIL_AMOUNTS_V3:
                    # 0x4B: Oil amounts and battery level
                    self._parse_oil_amounts(data, oil_names)

                elif cmd == RESP_VERSION_V3:
                    # 0x44: PCB and equipment firmware versions
                    if len(data) > 17:
                        self.state.pcb_version = (
                            data[1:17].decode("utf-8", errors="ignore").rstrip("\x00")
                        )
                        self.state.equipment_version = (
                            data[17:].decode("utf-8", errors="ignore").rstrip("\x00")
                        )

                elif cmd == RESP_IDENTIFIER:
                    # 0x4C: Device identifier (e.g., "001")
                    self.state.device_identifier = (
                        data[1:].decode("utf-8", errors="ignore").rstrip("\x00")
                    )

                elif cmd == RESP_SCHEDULE_V2:
                    # 0x83: V2.0 schedule response - also contains oil info for slot 1
                    self._parse_schedule_v2(data)

                elif cmd == RESP_OIL_V2:
                    # 0x91: V2.0 dedicated oil response
                    self._parse_oil_v2(data)

                else:
                    # Unknown or status bytes - log for debugging
                    _LOGGER.debug(
                        "Unhandled data burst response 0x%02X: %s", cmd, data.hex()
                    )

            except Exception as err:
                _LOGGER.warning(
                    "Error parsing data burst response 0x%02X: %s", cmd, err
                )

        _LOGGER.info(
            "Data burst parsed: is_on=%s, intensity=%d, oils=%d, schedules=%d",
            self.state.is_on,
            self.state.intensity,
            len(self.state.oils),
            len(self.state.schedules),
        )

    def _parse_limits_response(self, data: bytes) -> None:
        """Parse limits response (0x46 for V3.0, 0x84 for V2.0)."""
        if len(data) >= 10 and data[0] == RESP_LIMITS_V3:
            self.info.max_grade = data[1]
            self.info.custom_on_min = (data[2] << 8) + data[3]
            self.info.custom_on_max = (data[4] << 8) + data[5]
            self.info.custom_off_min = (data[6] << 8) + data[7]
            self.info.custom_off_max = (data[8] << 8) + data[9]
            self.info.limits_loaded = True
            _LOGGER.debug("Parsed limits: max_grade=%d", self.info.max_grade)

    def _parse_schedule_v3(self, data: bytes) -> None:
        """Parse V3.0 schedule response (0x4A).

        This response contains both schedule configuration AND current power state.
        """
        if len(data) < 14:
            return

        schedule = Schedule(
            aroma=data[1],
            index=data[5],
            hour_on=data[7],
            minute_on=data[8],
            hour_off=data[9],
            minute_off=data[10],
            repeat_days=format(data[11], "07b"),
            intensity=data[13] if len(data) > 13 else 1,
        )

        # Parse total control byte (byte 3): bit0=totalFan, bit1=totalFog
        total_control = data[3]
        schedule.total_fan = bool(total_control & 0x01)
        schedule.total_fog = bool(total_control & 0x02)

        # Parse slot control byte (byte 6): bit0=fan, bit1=enabled, bit2=show
        slot_control = data[6]
        schedule.fan_enabled = bool(slot_control & 0x01)
        schedule.enabled = bool(slot_control & 0x02)

        # The total control bits carry the live power/fan state in every frame
        self.state.is_on = schedule.total_fog
        self.state.fan_on = schedule.total_fan
        if schedule.index == 1 or not self.state.schedules:
            self.state.intensity = schedule.intensity
            self.state.active_schedule = data[4]

        self._upsert_schedule(schedule)
        _LOGGER.debug(
            "Parsed schedule %d: enabled=%s, intensity=%d, is_on=%s",
            schedule.index,
            schedule.enabled,
            schedule.intensity,
            self.state.is_on,
        )

    def _parse_schedule_v2(self, data: bytes) -> None:
        """Parse V2.0 schedule response (0x83).

        For slot 1, this also contains embedded oil information.
        """
        if len(data) < 8:
            return

        # Parse control byte: bit0=enabled, bits1-4=index
        control = data[1]
        enabled = bool(control & 0x01)
        index = (control >> 1) & 0x0F

        schedule = Schedule(
            index=index,
            enabled=enabled,
            hour_on=data[2],
            minute_on=data[3],
            hour_off=data[4],
            minute_off=data[5],
            repeat_days=format(data[6], "07b"),
            intensity=data[7],
        )

        # Update current device state from the first schedule
        if index == 1:
            self.state.is_on = enabled
            self.state.intensity = schedule.intensity if schedule.intensity > 0 else 1

            # Byte 8 of slot 1 carries the max intensity grade
            if len(data) > 8 and data[8] > 0:
                self.info.max_grade = data[8]
                self.info.limits_loaded = True

            # Parse embedded oil info from slot 1 (if present)
            if len(data) > 14:
                hex_str = data.hex().upper()
                try:
                    remainder = int(hex_str[20:24], 16)
                    total = int(hex_str[24:28], 16)
                    battery = data[14]

                    oil = OilInfo(name="Oil 1", total=total, remainder=remainder)
                    self.state.oils = [oil]
                    self.state.battery_level = battery
                except (ValueError, IndexError):
                    pass

        self._upsert_schedule(schedule)

    def _upsert_schedule(self, schedule: Schedule) -> None:
        """Insert or replace a schedule slot by its index."""
        for i, existing in enumerate(self.state.schedules):
            if existing.index == schedule.index:
                self.state.schedules[i] = schedule
                return
        self.state.schedules.append(schedule)

    def _parse_oil_names(self, data: bytes) -> list[str]:
        """Parse oil names response (0x48).

        Each oil name is 16 bytes, UTF-8 encoded.
        """
        names: list[str] = []
        i = 1  # Skip command byte
        while i + 16 <= len(data):
            name = data[i : i + 16].decode("utf-8", errors="ignore").rstrip("\x00")
            # Clean any embedded null characters
            name = name.replace("\x00", "")
            names.append(name if name else f"Oil {len(names) + 1}")
            i += 16

        _LOGGER.debug("Parsed oil names: %s", names)
        return names

    def _parse_oil_amounts(self, data: bytes, oil_names: list[str]) -> None:
        """Parse oil amounts response (0x4B).

        Format: [cmd] [battery] [reserved] [oil1_total:2] [oil1_remain:2] ...
        """
        if len(data) < 4:
            return

        self.state.battery_level = data[1]

        oils: list[OilInfo] = []
        hex_str = data.hex().upper()
        i = 4  # Start after cmd byte + battery + reserved (2 bytes each in hex = 4 chars)
        idx = 0

        while i + 8 <= len(hex_str):
            try:
                total = int(hex_str[i : i + 4], 16)
                remainder = int(hex_str[i + 4 : i + 8], 16)

                name = oil_names[idx] if idx < len(oil_names) else f"Oil {idx + 1}"
                oil = OilInfo(name=name, total=total, remainder=remainder)
                oils.append(oil)

                _LOGGER.debug(
                    "Parsed oil %d: %s - %d/%d (%.1f%%)",
                    idx + 1,
                    name,
                    remainder,
                    total,
                    oil.percentage,
                )

                i += 8
                idx += 1
            except (ValueError, IndexError) as err:
                _LOGGER.warning("Error parsing oil amount at index %d: %s", idx, err)
                break

        self.state.oils = oils

    def _parse_oil_v2(self, data: bytes) -> None:
        """Parse V2.0 oil response (0x91)."""
        if len(data) < 4:
            return

        hex_str = data.hex().upper()
        try:
            remainder = int(hex_str[2:6], 16)
            battery = data[3]

            # V2.0 doesn't provide total, estimate based on typical capacity
            oil = OilInfo(name="Oil 1", total=0, remainder=remainder)
            self.state.oils = [oil]
            self.state.battery_level = battery
        except (ValueError, IndexError):
            pass

    async def _async_send_time(self) -> None:
        """Send current local time to the device (it drives schedules)."""
        dt = dt_util.now()
        day_of_week = (dt.weekday() + 1) % 7

        cmd_byte = CMD_TIME_V2 if self.info.blue_version == 2.0 else CMD_TIME_V3

        time_cmd = bytes([
            cmd_byte,
            day_of_week,
            dt.year % 100,
            dt.month,
            dt.day,
            dt.hour,
            dt.minute,
            dt.second,
        ])

        await self._async_write_command_no_response(time_cmd)

    async def async_execute_command(
        self,
        command: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Execute a command with proper connection management.

        This method ensures we have an active connection, executes the command,
        and schedules a delayed disconnect to keep the connection alive for
        subsequent commands.
        """
        async with self._connection_lock:
            if not await self._async_ensure_connected():
                raise UpdateFailed("Failed to connect to device")

            try:
                await command()
            finally:
                # Schedule disconnect after delay to keep connection alive
                self._schedule_disconnect()

        # Update state after command
        self.async_update_listeners()

    async def async_power_on(self, intensity: int | None = None) -> None:
        """Turn on the diffuser."""
        if intensity is None:
            intensity = self.state.intensity or DEFAULT_INTENSITY

        intensity = max(1, min(intensity, self.info.max_grade))

        async def _power_on() -> None:
            if self.info.blue_version >= 3.0:
                if intensity == self.state.intensity:
                    # Single 5-byte total-control write, exactly like the
                    # app's power toggle - doesn't touch schedule slots
                    control = 0x03  # fan=1, fog=1
                    cmd = bytes([
                        CMD_SCHEDULE_WRITE_V3, DEFAULT_AROMA_SLOT, 0x02, control, 0x00
                    ])
                    await self._async_write_command_no_response(cmd)
                else:
                    # The 14-byte schedule write carries both the power bits
                    # and the new intensity, so one write covers everything
                    await self._async_set_intensity_v3(intensity)
            else:
                await self._async_set_schedule_v2(enabled=True, intensity=intensity)

            self.state.is_on = True
            self.state.intensity = intensity

        await self.async_execute_command(_power_on)
        _LOGGER.info("Turned on diffuser with intensity %d", intensity)

    async def async_power_off(self) -> None:
        """Turn off the diffuser."""
        async def _power_off() -> None:
            if self.info.blue_version >= 3.0:
                control = 0x00  # fan=0, fog=0
                cmd = bytes([
                    CMD_SCHEDULE_WRITE_V3, DEFAULT_AROMA_SLOT, 0x02, control, 0x00
                ])
                await self._async_write_command_no_response(cmd)
            else:
                for i in range(1, 6):
                    await self._async_set_schedule_v2(enabled=False, index=i)

            self.state.is_on = False

        await self.async_execute_command(_power_off)
        _LOGGER.info("Turned off diffuser")

    async def async_set_intensity(self, intensity: int) -> None:
        """Set the diffuser intensity (sends command to device)."""
        intensity = max(1, min(intensity, self.info.max_grade))

        async def _set_intensity() -> None:
            if self.info.blue_version >= 3.0:
                await self._async_set_intensity_v3(intensity)
            else:
                await self._async_set_schedule_v2(enabled=True, intensity=intensity)

            self.state.intensity = intensity

        await self.async_execute_command(_set_intensity)
        _LOGGER.info("Set diffuser intensity to %d", intensity)

    def set_intensity_local(self, intensity: int) -> None:
        """Set the intensity locally without sending command to device."""
        intensity = max(1, min(intensity, self.info.max_grade))
        self.state.intensity = intensity
        self.async_set_updated_data(None)
        _LOGGER.debug("Set local intensity to %d (not sent to device)", intensity)

    async def _async_set_intensity_v3(self, intensity: int) -> None:
        """Set intensity using V3.0 schedule command."""
        total_control = 0x03  # fan=1, fog=1
        slot_control = 0x03  # fan=1, enabled=1
        repeat_days = 0x7F  # All days

        cmd = bytearray(14)
        cmd[0] = CMD_SCHEDULE_WRITE_V3
        cmd[1] = DEFAULT_AROMA_SLOT
        cmd[2] = 0x02
        cmd[3] = total_control
        cmd[4] = 0x00
        cmd[5] = 1  # schedule index
        cmd[6] = slot_control
        cmd[7] = 0  # hour_on
        cmd[8] = 0  # minute_on
        cmd[9] = 23  # hour_off
        cmd[10] = 59  # minute_off
        cmd[11] = repeat_days
        cmd[12] = 0  # custom_intensity flag
        cmd[13] = intensity

        # The app doesn't wait for a response to control writes; the device
        # pushes updated 0x4A state frames which the notification handler
        # parses as authoritative state
        await self._async_write_command_no_response(bytes(cmd))

    async def _async_set_schedule_v2(
        self, enabled: bool, intensity: int = 1, index: int = 1
    ) -> None:
        """Set schedule using V2.0 command."""
        control = (1 if enabled else 0) | (index << 1)
        repeat_byte = 0x7F if enabled else 0x00

        cmd = bytearray(15)
        cmd[0] = CMD_SCHEDULE_WRITE_V2
        cmd[1] = control
        cmd[2] = 0  # hour_on
        cmd[3] = 0  # minute_on
        cmd[4] = 23  # hour_off
        cmd[5] = 59  # minute_off
        cmd[6] = repeat_byte
        cmd[7] = intensity

        await self._async_write_command_no_response(bytes(cmd))

    async def async_initialize(self) -> None:
        """Connect, log in, and load initial device state.

        Called once during config entry setup. Raises UpdateFailed if the
        device cannot be reached so setup can be retried by Home Assistant.
        """
        await self.async_read_device_info()

    async def async_read_device_info(self) -> None:
        """Read device info - most data comes from post-login data burst.

        For V3.0 devices, the post-login data burst already provides all the
        information we need. This method only fetches additional data for V2.0
        devices or if the data burst didn't provide certain fields.
        """
        async def _read_info() -> None:
            # For V2.0 devices or if device name wasn't in data burst
            if not self.state.device_name:
                response = await self._async_write_command(bytes([CMD_READ_NAME]))
                if response:
                    if response[0] == RESP_NAME_V2:
                        self.state.device_name = (
                            response[2:].decode("utf-8", errors="ignore").rstrip("\x00")
                        )
                    elif response[0] == RESP_NAME_V3:
                        self.state.device_name = (
                            response[1:].decode("utf-8", errors="ignore").rstrip("\x00")
                        )

            # For V3.0 devices, version comes from data burst
            # For V2.0, we need to request it separately
            if self.info.blue_version < 3.0 and not self.state.pcb_version:
                response = await self._async_write_command(bytes([CMD_VERSION_V2]))
                if response and len(response) > 17:
                    self.state.pcb_version = (
                        response[1:17].decode("utf-8", errors="ignore").rstrip("\x00")
                    )
                    self.state.equipment_version = (
                        response[17:].decode("utf-8", errors="ignore").rstrip("\x00")
                    )

            # Read limits only if the data burst didn't deliver them
            if not self.info.limits_loaded:
                await self._async_read_limits()

        await self.async_execute_command(_read_info)

    async def _async_read_limits(self) -> None:
        """Read intensity limits from device."""
        if self.info.blue_version >= 3.0:
            response = await self._async_write_command(bytes([CMD_LIMITS_V3]))
            if response and response[0] == RESP_LIMITS_V3 and len(response) > 1:
                self.info.max_grade = response[1]
                self.info.limits_loaded = True
        else:
            response = await self._async_write_command(bytes([CMD_LIMITS_V2]))
            if response and response[0] == RESP_LIMITS_V2:
                # V2.0 doesn't return max_grade in limits, keep default
                self.info.limits_loaded = True
