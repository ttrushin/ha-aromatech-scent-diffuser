"""DataUpdateCoordinator for AromaTech integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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

        # Data burst collection state (for post-login data collection)
        self._collecting_data_burst = False
        self._data_burst_responses: list[bytes] = []

        # Device state
        self.info = DeviceInfo()
        self.state = DeviceState()
        self._logged_in = False
        self._initial_state_loaded = False

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

        # Collect responses during data burst phase
        if self._collecting_data_burst:
            self._data_burst_responses.append(data)

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
        """Authenticate with the device and collect post-login data burst."""
        # Prepare for data burst collection
        self._data_burst_responses = []
        self._collecting_data_burst = True

        try:
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
                    _LOGGER.debug(
                        "Logged in successfully. Protocol version: %s",
                        self.info.blue_version,
                    )

                    # Wait for post-login data burst from device
                    # The device automatically sends all state data after login
                    await asyncio.sleep(DATA_BURST_TIMEOUT)

                    # Stop collecting and parse the data burst
                    self._collecting_data_burst = False
                    self._parse_data_burst()

                    # Send current time after collecting data
                    await self._async_send_time()

                    self._initial_state_loaded = True
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

        # Update current device state from the first schedule
        # The total_fan and total_fog represent actual current device state
        if schedule.index == 1 or not self.state.schedules:
            self.state.is_on = schedule.total_fog
            self.state.fan_on = schedule.total_fan
            self.state.intensity = schedule.intensity
            self.state.active_schedule = data[4] if len(data) > 4 else 0

        self.state.schedules.append(schedule)
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

        hex_str = data.hex().upper()
        i = 4  # Start after cmd byte + battery + reserved (2 bytes each in hex = 4 chars)
        idx = 0

        while i + 8 <= len(hex_str):
            try:
                total = int(hex_str[i : i + 4], 16)
                remainder = int(hex_str[i + 4 : i + 8], 16)

                name = oil_names[idx] if idx < len(oil_names) else f"Oil {idx + 1}"
                oil = OilInfo(name=name, total=total, remainder=remainder)
                self.state.oils.append(oil)

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
                response = await self._async_write_command(bytes([CMD_VERSION_V3]))
                if response and len(response) > 17:
                    self.state.pcb_version = (
                        response[1:17].decode("utf-8", errors="ignore").rstrip("\x00")
                    )
                    self.state.equipment_version = (
                        response[17:].decode("utf-8", errors="ignore").rstrip("\x00")
                    )

            # Read limits if not already populated from data burst
            if self.info.max_grade == 5:  # Default value, may not be set
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
