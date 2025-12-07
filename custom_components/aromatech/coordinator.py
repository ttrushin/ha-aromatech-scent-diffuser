"""DataUpdateCoordinator for AromaTech integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
    CMD_VERSION_V3,
    DEFAULT_AROMA_SLOT,
    DEFAULT_INTENSITY,
    PAIR_CODE,
    RESP_LIMITS_V2,
    RESP_LIMITS_V3,
    RESP_NAME_V2,
    RESP_NAME_V3,
)

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

_LOGGER = logging.getLogger(__name__)

# Connection settings
CONNECTION_TIMEOUT = 15.0
COMMAND_TIMEOUT = 5.0
# Disconnect after 30 minutes of idle when device is OFF
DISCONNECT_DELAY_OFF = 30 * 60  # 30 minutes in seconds
# Reconnection settings for when device is ON
RECONNECT_MIN_INTERVAL = 5  # Start with 5 seconds
RECONNECT_MAX_INTERVAL = 60  # Max 60 seconds between attempts
RECONNECT_MAX_ATTEMPTS = 10  # Give up after this many consecutive failures


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


class DeviceState:
    """Current device operational state."""

    def __init__(self) -> None:
        """Initialize device state."""
        self.is_on: bool = False
        self.intensity: int = DEFAULT_INTENSITY
        self.device_name: str = ""
        self.pcb_version: str = ""
        self.equipment_version: str = ""


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
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"AromaTech {ble_device.address}",
            # No polling - we use push updates from BLE advertisements
            update_interval=None,
        )

        self._ble_device = ble_device
        self._password = password

        # Connection state
        self._client: BleakClient | None = None
        self._connection_lock = asyncio.Lock()
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._response_event = asyncio.Event()
        self._last_response: bytes = b""

        # Reconnection state (for when device is ON and connection drops)
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_attempts = 0
        self._shutting_down = False

        # Device state
        self.info = DeviceInfo()
        self.state = DeviceState()
        self._logged_in = False

        # Presence tracking
        self.last_seen: datetime | None = None
        self.rssi: int | None = None

    @property
    def mac(self) -> str:
        """Return the device MAC address."""
        return self._ble_device.address

    @property
    def connected(self) -> bool:
        """Return True if connected to the device."""
        return self._client is not None and self._client.is_connected

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

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the BLE device reference."""
        self._ble_device = ble_device

    @callback
    def update_ble(self, advertisement: AdvertisementData) -> None:
        """Update device info from BLE advertisement."""
        self.last_seen = datetime.now()
        if advertisement.rssi is not None:
            self.rssi = advertisement.rssi
        self.async_set_updated_data(None)

    async def _async_update_data(self) -> None:
        """Fetch data from device - not used for polling, only for state updates."""
        # This coordinator doesn't poll; it uses push updates
        return None

    def _notification_handler(self, sender: int, data: bytes) -> None:
        """Handle notifications from the device."""
        _LOGGER.debug("Received notification: %s", data.hex())
        self._last_response = data
        self._response_event.set()

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
        self._reconnect_attempts = 0

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
        """Handle unexpected disconnection from the device."""
        _LOGGER.warning("Disconnected from %s", self._ble_device.address)
        self._logged_in = False

        # If device is ON and we're not shutting down, attempt to reconnect
        if self.state.is_on and not self._shutting_down:
            _LOGGER.info("Device is ON - will attempt to reconnect")
            self._start_reconnect_task()

        self.async_set_updated_data(None)

    def _start_reconnect_task(self) -> None:
        """Start the reconnection task if not already running."""
        if self._reconnect_task and not self._reconnect_task.done():
            return  # Already reconnecting

        self._reconnect_task = self.hass.async_create_task(
            self._async_reconnect_loop()
        )

    async def _async_reconnect_loop(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        self._reconnect_attempts = 0

        while (
            self.state.is_on
            and not self._shutting_down
            and self._reconnect_attempts < RECONNECT_MAX_ATTEMPTS
        ):
            self._reconnect_attempts += 1

            # Calculate backoff delay with exponential increase
            delay = min(
                RECONNECT_MIN_INTERVAL * (2 ** (self._reconnect_attempts - 1)),
                RECONNECT_MAX_INTERVAL,
            )

            _LOGGER.info(
                "Reconnection attempt %d/%d in %d seconds",
                self._reconnect_attempts,
                RECONNECT_MAX_ATTEMPTS,
                delay,
            )

            await asyncio.sleep(delay)

            # Check again if we should still reconnect
            if not self.state.is_on or self._shutting_down:
                _LOGGER.debug("Reconnection cancelled - device OFF or shutting down")
                break

            # Attempt to reconnect
            async with self._connection_lock:
                try:
                    if await self._async_ensure_connected():
                        _LOGGER.info("Successfully reconnected to device")
                        self._reconnect_attempts = 0
                        self.async_set_updated_data(None)
                        return
                except Exception as err:
                    _LOGGER.warning("Reconnection attempt failed: %s", err)

        if self._reconnect_attempts >= RECONNECT_MAX_ATTEMPTS:
            _LOGGER.error(
                "Failed to reconnect after %d attempts - giving up",
                RECONNECT_MAX_ATTEMPTS,
            )

    async def _async_ensure_connected(self) -> bool:
        """Ensure we have an active connection to the device."""
        self._cancel_disconnect_timer()

        if self._client and self._client.is_connected and self._logged_in:
            return True

        # Need to connect and/or login
        try:
            if not self._client or not self._client.is_connected:
                _LOGGER.debug("Establishing connection to %s", self._ble_device.address)
                self._client = await establish_connection(
                    BleakClient,
                    self._ble_device,
                    self._ble_device.address,
                    max_attempts=3,
                    disconnected_callback=self._on_disconnect,
                )
                await self._client.start_notify(
                    CHARACTERISTIC_UUID, self._notification_handler
                )
                self._logged_in = False
                # Reset reconnect counter on successful connection
                self._reconnect_attempts = 0

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
            try:
                if self._client.is_connected:
                    await self._client.stop_notify(CHARACTERISTIC_UUID)
                    await self._client.disconnect()
            except Exception as err:
                _LOGGER.debug("Error during disconnect: %s", err)
            finally:
                self._client = None

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

    async def _async_login(self) -> bool:
        """Authenticate with the device."""
        # Try login without pair code first (works for V2.0)
        login_cmd = bytes([CMD_LOGIN]) + self._password.encode("utf-8")
        response = await self._async_write_command(login_cmd, timeout=2.0)

        if not response:
            # Retry with pair code for V3.0 devices
            login_cmd_v3 = (
                bytes([CMD_LOGIN]) + (self._password + PAIR_CODE).encode("utf-8")
            )
            response = await self._async_write_command(login_cmd_v3, timeout=2.0)

        if response and len(response) > 0 and response[0] == CMD_LOGIN:
            login_state = self._parse_login_response(response)
            self._logged_in = login_state == 0

            if self._logged_in:
                await self._async_send_time()
                _LOGGER.debug(
                    "Logged in successfully. Protocol version: %s",
                    self.info.blue_version,
                )
                return True

        _LOGGER.error("Login failed")
        return False

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

    async def _async_send_time(self) -> None:
        """Send current time to the device."""
        dt = datetime.now()
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
        self.async_set_updated_data(None)

    async def async_power_on(self, intensity: int | None = None) -> None:
        """Turn on the diffuser."""
        if intensity is None:
            intensity = self.state.intensity or DEFAULT_INTENSITY

        intensity = max(1, min(intensity, self.info.max_grade))

        async def _power_on() -> None:
            if self.info.blue_version >= 3.0:
                control = 0x03  # fan=1, fog=1
                cmd = bytes([
                    CMD_SCHEDULE_WRITE_V3, DEFAULT_AROMA_SLOT, 0x02, control, 0x00
                ])
                await self._async_write_command_no_response(cmd)
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

        await self._async_write_command(bytes(cmd))

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

        await self._async_write_command(bytes(cmd))

    async def async_read_device_info(self) -> None:
        """Read device name, version, and limits."""
        async def _read_info() -> None:
            # Read device name
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

            # Read version (V3.0 only)
            if self.info.blue_version >= 3.0:
                response = await self._async_write_command(bytes([CMD_VERSION_V3]))
                if response and len(response) > 17:
                    self.state.pcb_version = (
                        response[1:17].decode("utf-8", errors="ignore").rstrip("\x00")
                    )
                    self.state.equipment_version = (
                        response[17:].decode("utf-8", errors="ignore").rstrip("\x00")
                    )

            # Read limits
            await self._async_read_limits()

        await self.async_execute_command(_read_info)

    async def _async_read_limits(self) -> None:
        """Read intensity limits from device."""
        if self.info.blue_version >= 3.0:
            response = await self._async_write_command(bytes([CMD_LIMITS_V3]))
            if response and response[0] == RESP_LIMITS_V3 and len(response) > 1:
                self.info.max_grade = response[1]
        else:
            response = await self._async_write_command(bytes([CMD_LIMITS_V2]))
            if response and response[0] == RESP_LIMITS_V2:
                # V2.0 doesn't return max_grade in limits, keep default
                pass
