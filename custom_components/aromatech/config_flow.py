"""Config flow for AromaTech integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_MAC, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .core.const import (
    CONF_HEALTH_CHECK_INTERVAL,
    DEFAULT_HEALTH_CHECK_INTERVAL,
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


def _is_valid_password(password: str) -> bool:
    """Validate the device password format (4 digits)."""
    return len(password) == 4 and password.isdigit()


class AromaTechConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AromaTech."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: bluetooth.BluetoothServiceInfoBleak | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AromaTechOptionsFlow:
        """Create the options flow."""
        return AromaTechOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered via Bluetooth."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        if not is_aromatech_device(
            discovery_info.name, discovery_info.manufacturer_data
        ):
            return self.async_abort(reason="not_supported")

        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device and collect the password."""
        assert self._discovery_info is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD)
            if not _is_valid_password(password):
                errors[CONF_PASSWORD] = "invalid_password"
            else:
                return self.async_create_entry(
                    title=self._discovery_info.name or self._discovery_info.address,
                    data={
                        CONF_MAC: self._discovery_info.address,
                        CONF_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or self._discovery_info.address
            },
            data_schema=vol.Schema(
                {vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str}
            ),
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD)
            if not _is_valid_password(password):
                errors[CONF_PASSWORD] = "invalid_password"
            else:
                mac = user_input[CONF_MAC]
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()

                # Get the device name for the entry title
                device_name = "AromaTech Diffuser"
                for service_info in bluetooth.async_discovered_service_info(
                    self.hass
                ):
                    if service_info.address == mac:
                        device_name = service_info.name or device_name
                        break

                return self.async_create_entry(
                    title=device_name,
                    data={
                        CONF_MAC: mac,
                        CONF_PASSWORD: password,
                    },
                )

        # Discover devices, excluding ones already configured
        current_ids = self._async_current_ids()
        devices = {
            service_info.address: f"{service_info.name} ({service_info.address})"
            for service_info in bluetooth.async_discovered_service_info(self.hass)
            if service_info.address not in current_ids
            and is_aromatech_device(service_info.name, service_info.manufacturer_data)
        }

        if not devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC): vol.In(devices),
                    vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class AromaTechOptionsFlow(OptionsFlowWithReload):
    """Handle AromaTech options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_HEALTH_CHECK_INTERVAL: int(
                        user_input[CONF_HEALTH_CHECK_INTERVAL]
                    )
                }
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HEALTH_CHECK_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_HEALTH_CHECK_INTERVAL,
                            DEFAULT_HEALTH_CHECK_INTERVAL,
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=3600,
                            step=10,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                }
            ),
        )
