"""Device state manager for AromaTech integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from .client import AromaTechClient
from .const import (
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
    DEFAULT_PASSWORD,
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


@dataclass
class DeviceInfo:
    """Device capabilities discovered during login."""

    blue_version: float = 3.0
    hid_version: bool = False
    oil: bool = False
    battery: bool = False
    custom: bool = False
    many_aroma: bool = False
    fan: bool = False
    max_grade: int = 5


@dataclass
class DeviceState:
    """Current device operational state."""

    is_on: bool = False
    intensity: int = DEFAULT_INTENSITY
    device_name: str = ""
    pcb_version: str = ""
    equipment_version: str = ""


class Device:
    """Manages state and communication for an AromaTech device."""

    def __init__(
        self,
        ble_device: BLEDevice,
        password: str = DEFAULT_PASSWORD,
    ) -> None:
        """Initialize the device."""
        self.client = AromaTechClient(ble_device)
        self.password = password

        self.info = DeviceInfo()
        self.state = DeviceState()

        self.logged_in = False
        self.last_seen: datetime | None = None
        self.rssi: int | None = None

        self._update_callbacks: list[Callable[[], None]] = []

    @property
    def mac(self) -> str:
        """Return the device MAC address."""
        return self.client.ble_device.address

    @property
    def connected(self) -> bool:
        """Return True if connected to the device."""
        return self.client.is_connected

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

    @property
    def pcb_version(self) -> str:
        """Return the PCB version."""
        return self.state.pcb_version

    @property
    def equipment_version(self) -> str:
        """Return the equipment version."""
        return self.state.equipment_version

    def register_update_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback for state updates."""
        self._update_callbacks.append(callback)

    def _notify_updates(self) -> None:
        """Notify all registered callbacks of state changes."""
        for callback in self._update_callbacks:
            try:
                callback()
            except Exception as e:
                _LOGGER.error("Error in update callback: %s", e)

    def update_ble(self, advertisement: AdvertisementData) -> None:
        """Update device info from BLE advertisement."""
        self.last_seen = datetime.now()
        if advertisement.rssi is not None:
            self.rssi = advertisement.rssi
        self._notify_updates()

    async def async_login(self) -> bool:
        """Connect and authenticate with the device."""
        if not await self.client.connect():
            return False

        # Try login without pair code first (works for V2.0)
        login_cmd = bytes([CMD_LOGIN]) + self.password.encode("utf-8")
        response = await self.client.write_command(login_cmd, timeout=1.0)

        if not response:
            # Retry with pair code for V3.0 devices
            login_cmd_v3 = (
                bytes([CMD_LOGIN]) + (self.password + PAIR_CODE).encode("utf-8")
            )
            response = await self.client.write_command(login_cmd_v3, timeout=1.0)

        if response and len(response) > 0 and response[0] == CMD_LOGIN:
            login_state, self.info = self._parse_login_response(response)
            self.logged_in = login_state == 0

            if self.logged_in:
                await self._send_time()
                _LOGGER.debug(
                    "Logged in successfully. Protocol version: %s",
                    self.info.blue_version,
                )
                return True

        _LOGGER.error("Login failed")
        await self.client.disconnect()
        return False

    def _parse_login_response(self, data: bytes) -> tuple[int, DeviceInfo]:
        """Parse login response.

        Returns:
            tuple: (login_state, device_info) where login_state is:
                0 = success
                1 = failed
                2 = error
        """
        info = DeviceInfo()
        response_data = data[1:]
        response_str = response_data.decode("utf-8", errors="ignore")

        _LOGGER.debug("Login response string: %s", response_str)

        if response_str == "ERROR":
            return 2, info

        if len(response_str) <= 2:
            info.hid_version = True
            info.blue_version = 2.0
            info.many_aroma = False
        else:
            try:
                info.blue_version = float(response_str[4:7])
            except ValueError:
                info.blue_version = 3.0

            if info.blue_version == 3.0 and len(data) > 13:
                feature_byte = data[13]
                info.oil = bool(feature_byte & 0x01)
                info.battery = bool(feature_byte & 0x02)
                info.custom = bool(feature_byte & 0x04)
                info.many_aroma = bool(feature_byte & 0x08)
                info.fan = bool(feature_byte & 0x10)

        if len(response_str) >= 9:
            login_state = 0 if response_str[7:9] == PAIR_CODE[:2] else 1
        else:
            login_state = 0

        return login_state, info

    async def _send_time(self) -> None:
        """Send current time to the device."""
        dt = datetime.now()
        # Convert weekday
        # Python uses 0=Monday, but device uses 0=Sunday
        day_of_week = (dt.weekday() + 1) % 7

        cmd_byte = CMD_TIME_V2 if self.info.blue_version == 2.0 else CMD_TIME_V3

        time_cmd = bytes(
            [
                cmd_byte,
                day_of_week,
                dt.year % 100,
                dt.month,
                dt.day,
                dt.hour,
                dt.minute,
                dt.second,
            ]
        )

        await self.client.write_command_no_response(time_cmd)

    async def async_power_on(self, intensity: int | None = None) -> None:
        """Turn on the diffuser."""
        if intensity is None:
            intensity = self.state.intensity or DEFAULT_INTENSITY

        intensity = max(1, min(intensity, self.info.max_grade))

        if self.info.blue_version >= 3.0:
            # Quick power on for V3.0
            control = 0x03  # fan=1, fog=1
            cmd = bytes(
                [CMD_SCHEDULE_WRITE_V3, DEFAULT_AROMA_SLOT, 0x02, control, 0x00]
            )

            await self.client.write_command_no_response(cmd)

            # Set intensity via schedule
            await self._set_intensity_v3(intensity)
        else:
            # For V2.0, set a schedule that covers all day
            await self._set_schedule_v2(enabled=True, intensity=intensity)

        self.state.is_on = True
        self.state.intensity = intensity
        self._notify_updates()

    async def async_power_off(self) -> None:
        """Turn off the diffuser."""
        if self.info.blue_version >= 3.0:
            # Quick power off for V3.0
            control = 0x00  # fan=0, fog=0
            cmd = bytes(
                [CMD_SCHEDULE_WRITE_V3, DEFAULT_AROMA_SLOT, 0x02, control, 0x00]
            )

            await self.client.write_command_no_response(cmd)
        else:
            # For V2.0, disable all schedules
            for i in range(1, 6):
                await self._set_schedule_v2(enabled=False, index=i)

        self.state.is_on = False
        self._notify_updates()

    async def async_set_intensity(self, intensity: int) -> None:
        """Set the diffuser intensity."""
        intensity = max(1, min(intensity, self.info.max_grade))

        if self.info.blue_version >= 3.0:
            await self._set_intensity_v3(intensity)
        else:
            await self._set_schedule_v2(enabled=True, intensity=intensity)

        self.state.intensity = intensity
        self._notify_updates()

    async def _set_intensity_v3(self, intensity: int) -> None:
        """Set intensity using V3.0 schedule command."""
        # Build schedule command with intensity
        total_control = 0x03  # fan=1, fog=1
        slot_control = 0x03  # fan=1, enabled=1
        repeat_days = 0x7F  # All days (1111111)

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

        await self.client.write_command(bytes(cmd))

    async def _set_schedule_v2(
        self, enabled: bool, intensity: int = 1, index: int = 1
    ) -> None:
        """Set schedule using V2.0 command."""
        control = (1 if enabled else 0) | (index << 1)
        repeat_byte = 0x7F if enabled else 0x00  # All days or none

        cmd = bytearray(15)
        cmd[0] = CMD_SCHEDULE_WRITE_V2
        cmd[1] = control
        cmd[2] = 0  # hour_on
        cmd[3] = 0  # minute_on
        cmd[4] = 23  # hour_off
        cmd[5] = 59  # minute_off
        cmd[6] = repeat_byte
        cmd[7] = intensity

        await self.client.write_command(bytes(cmd))

    async def async_read_device_info(self) -> None:
        """Read device name, version, and limits."""
        # Read device name
        response = await self.client.write_command(bytes([CMD_READ_NAME]))
        if response:
            if response[0] == RESP_NAME_V2:
                self.state.device_name = (
                    response[2:].decode("utf-8", errors="ignore").rstrip("\x00")
                )
            elif response[0] == RESP_NAME_V3:
                self.state.device_name = (
                    response[1:].decode("utf-8", errors="ignore").rstrip("\x00")
                )

        # Read version (V3.0 only has combined version command)
        if self.info.blue_version >= 3.0:
            response = await self.client.write_command(bytes([CMD_VERSION_V3]))
            if response and len(response) > 17:
                self.state.pcb_version = (
                    response[1:17].decode("utf-8", errors="ignore").rstrip("\x00")
                )
                self.state.equipment_version = (
                    response[17:].decode("utf-8", errors="ignore").rstrip("\x00")
                )

        # Read limits (to get max_grade)
        await self._read_limits()

    async def _read_limits(self) -> None:
        """Read intensity limits from device."""
        if self.info.blue_version >= 3.0:
            response = await self.client.write_command(bytes([CMD_LIMITS_V3]))
            if response and response[0] == RESP_LIMITS_V3 and len(response) > 1:
                self.info.max_grade = response[1]
        else:
            response = await self.client.write_command(bytes([CMD_LIMITS_V2]))
            if response and response[0] == RESP_LIMITS_V2:
                # V2.0 doesn't return max_grade in limits, keep default
                pass

    async def async_disconnect(self) -> None:
        """Disconnect from the device."""
        self.logged_in = False
        await self.client.disconnect()
