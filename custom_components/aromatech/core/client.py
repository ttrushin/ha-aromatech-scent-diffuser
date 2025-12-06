"""BLE client for AromaTech devices."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from .const import CHARACTERISTIC_UUID

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)


class AromaTechClient:
    """BLE client for communicating with AromaTech devices."""

    def __init__(self, ble_device: BLEDevice) -> None:
        """Initialize the client."""
        self.ble_device = ble_device
        self._client: BleakClient | None = None
        self._response_event = asyncio.Event()
        self._last_response: bytes = b""

    @property
    def is_connected(self) -> bool:
        """Return True if connected to the device."""
        return self._client is not None and self._client.is_connected

    def _notification_handler(self, sender: int, data: bytes) -> None:
        """Handle notifications from the device."""
        _LOGGER.debug("Received notification: %s", data.hex())
        self._last_response = data
        self._response_event.set()

    async def connect(self) -> bool:
        """Connect to the device."""
        try:
            self._client = await establish_connection(
                BleakClient,
                self.ble_device,
                self.ble_device.address,
            )
            await self._client.start_notify(
                CHARACTERISTIC_UUID, self._notification_handler
            )
            _LOGGER.debug("Connected to %s", self.ble_device.address)
            return True
        except Exception as e:
            _LOGGER.error("Failed to connect to %s: %s", self.ble_device.address, e)
            self._client = None
            return False

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(CHARACTERISTIC_UUID)
            except Exception:
                pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
            _LOGGER.debug("Disconnected from %s", self.ble_device.address)
        self._client = None

    async def write_command(self, data: bytes, timeout: float = 2.0) -> bytes:
        """Write a command and wait for response.

        Args:
            data: Command bytes to send
            timeout: Response timeout in seconds

        Returns:
            Response bytes or empty bytes on timeout
        """
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
        except Exception as e:
            _LOGGER.error("Failed to write command: %s", e)
            return b""

    async def write_command_no_response(self, data: bytes) -> bool:
        """Write a command without waiting for response.

        Args:
            data: Command bytes to send

        Returns:
            True if write was successful
        """
        if not self._client or not self._client.is_connected:
            _LOGGER.error("Cannot write command: not connected")
            return False

        try:
            _LOGGER.debug("Writing command (no response): %s", data.hex())
            await self._client.write_gatt_char(CHARACTERISTIC_UUID, data)
            return True
        except Exception as e:
            _LOGGER.error("Failed to write command: %s", e)
            return False
