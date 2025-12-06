"""Config flow for AromaTech integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow

from .core.const import (
    DEFAULT_PASSWORD,
    DEVICE_NAME_PATTERNS,
    DOMAIN,
    MANUFACTURER_ID,
)


def is_aromatech_device(
    name: str | None, manufacturer_data: dict[int, bytes] | None = None
) -> bool:
    """Check if a device is an AromaTech diffuser."""
    # Check by manufacturer ID
    if manufacturer_data and MANUFACTURER_ID in manufacturer_data:
        return True

    # Check by name patterns
    if name is None:
        return False

    # Strip common prefixes
    if name.startswith("SA_") or name.startswith("SE_"):
        name = name[3:]

    return any(name.startswith(pattern) for pattern in DEVICE_NAME_PATTERNS)


class AromaTechConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AromaTech."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            # Validate password is 4 digits
            password = user_input.get("password", DEFAULT_PASSWORD)
            if len(password) != 4 or not password.isdigit():
                errors["password"] = "invalid_password"
            else:
                mac = user_input["mac"]
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()

                # Get the device name for the entry title
                device_name = "AromaTech Diffuser"
                service_infos = bluetooth.async_discovered_service_info(self.hass)
                for service_info in service_infos:
                    if service_info.address == mac:
                        device_name = service_info.name or device_name
                        break

                return self.async_create_entry(
                    title=device_name,
                    data={
                        "mac": mac,
                        "password": password,
                    },
                )

        # Discover devices
        devices = {}
        service_infos = bluetooth.async_discovered_service_info(self.hass)

        for service_info in service_infos:
            if is_aromatech_device(
                service_info.name, service_info.manufacturer_data
            ):
                devices[service_info.address] = (
                    f"{service_info.name} ({service_info.address})"
                )

        if not devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("mac"): vol.In(devices),
                    vol.Optional("password", default=DEFAULT_PASSWORD): str,
                }
            ),
            errors=errors,
        )
