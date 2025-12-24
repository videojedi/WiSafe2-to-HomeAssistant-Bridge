"""Config flow for WiSafe2 FireAngel Bridge integration."""
from __future__ import annotations

import logging
from typing import Any

import serial
import serial.tools.list_ports
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_SERIAL_PORT,
    CONF_BAUD_RATE,
    DEFAULT_BAUD_RATE,
    DEVICE_MODELS,
)

_LOGGER = logging.getLogger(__name__)


def get_serial_ports() -> list[str]:
    """Get list of available serial ports."""
    ports = serial.tools.list_ports.comports()
    return [port.device for port in ports]


async def validate_serial_port(hass: HomeAssistant, port: str, baud_rate: int) -> bool:
    """Validate the serial port connection."""
    try:
        def _test_connection():
            ser = serial.Serial(port=port, baudrate=baud_rate, timeout=2)
            ser.close()
            return True

        return await hass.async_add_executor_job(_test_connection)
    except serial.SerialException as err:
        _LOGGER.error("Failed to connect to serial port %s: %s", port, err)
        raise CannotConnect from err


class WiSafe2ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WiSafe2 FireAngel Bridge."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        # Get available serial ports
        ports = await self.hass.async_add_executor_job(get_serial_ports)

        if not ports:
            return self.async_abort(reason="no_serial_ports")

        if user_input is not None:
            try:
                await validate_serial_port(
                    self.hass,
                    user_input[CONF_SERIAL_PORT],
                    user_input.get(CONF_BAUD_RATE, DEFAULT_BAUD_RATE),
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Check if already configured
                await self.async_set_unique_id(user_input[CONF_SERIAL_PORT])
                self._abort_if_unique_id_configured()

                self._data = user_input
                return await self.async_step_devices()

        # Build port options
        port_options = [
            selector.SelectOptionDict(value=port, label=port)
            for port in ports
        ]

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERIAL_PORT): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=port_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_BAUD_RATE, default=DEFAULT_BAUD_RATE
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=9600,
                            max=115200,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure devices step."""
        if user_input is not None:
            if user_input.get("add_device"):
                return await self.async_step_add_device()

            # Save configuration
            return self.async_create_entry(
                title="WiSafe2 Bridge",
                data=self._data,
                options={"devices": self._devices},
            )

        # Show current devices and option to add more
        device_list = "\n".join(
            f"- {d.get('name', d['device_id'])} ({d['device_id']})"
            for d in self._devices
        ) if self._devices else "No devices configured yet"

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Optional("add_device", default=False): selector.BooleanSelector(),
                }
            ),
            description_placeholders={
                "device_list": device_list,
                "device_count": str(len(self._devices)),
            },
        )

    async def async_step_add_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Add a device step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input.get("device_id", "").strip().lower()

            if not device_id or len(device_id) != 6:
                errors["device_id"] = "invalid_device_id"
            elif any(d["device_id"] == device_id for d in self._devices):
                errors["device_id"] = "device_already_exists"
            else:
                self._devices.append({
                    "device_id": device_id,
                    "model": user_input.get("model"),
                    "name": user_input.get("name"),
                    "location": user_input.get("location"),
                })
                return await self.async_step_devices()

        # Build model options
        model_options = [
            selector.SelectOptionDict(
                value=model_id,
                label=f"{info['name']} - {info['description']}"
            )
            for model_id, info in DEVICE_MODELS.items()
        ]

        return self.async_show_form(
            step_id="add_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                    vol.Optional("model"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=model_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional("name"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                    vol.Optional("location"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> WiSafe2OptionsFlow:
        """Get the options flow handler."""
        return WiSafe2OptionsFlow(config_entry)


class WiSafe2OptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for WiSafe2."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self._devices: list[dict[str, Any]] = list(
            config_entry.options.get("devices", [])
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options - main menu."""
        errors: dict[str, str] = {}

        if user_input is not None:
            action = user_input.get("action")
            if action == "add_device":
                return await self.async_step_add_device()
            elif action == "remove_device":
                return await self.async_step_remove_device()
            elif action == "save":
                return self.async_create_entry(
                    title="",
                    data={"devices": self._devices},
                )

        # Build device list for display
        device_count = len(self._devices)
        device_list = ", ".join(
            d.get("name", d["device_id"]) for d in self._devices
        ) if self._devices else "None"

        action_options = [
            selector.SelectOptionDict(value="add_device", label="Add a new device"),
            selector.SelectOptionDict(value="remove_device", label="Remove a device"),
            selector.SelectOptionDict(value="save", label="Save and exit"),
        ]

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="save"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=action_options,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={
                "device_count": str(device_count),
                "device_list": device_list,
            },
            errors=errors,
        )

    async def async_step_add_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Add a new device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = user_input.get("device_id", "").strip().lower()

            if not device_id or len(device_id) != 6:
                errors["device_id"] = "invalid_device_id"
            elif any(d["device_id"] == device_id for d in self._devices):
                errors["device_id"] = "device_already_exists"
            else:
                self._devices.append({
                    "device_id": device_id,
                    "model": user_input.get("model"),
                    "name": user_input.get("name"),
                    "location": user_input.get("location"),
                })
                return await self.async_step_init()

        model_options = [
            selector.SelectOptionDict(
                value=model_id,
                label=f"{info['name']} - {info['description']}"
            )
            for model_id, info in DEVICE_MODELS.items()
        ]

        return self.async_show_form(
            step_id="add_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                    vol.Optional("model"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=model_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional("name"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                    vol.Optional("location"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_remove_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Remove an existing device."""
        if user_input is not None:
            device_to_remove = user_input.get("remove_device")
            if device_to_remove:
                self._devices = [
                    d for d in self._devices if d["device_id"] != device_to_remove
                ]
            return await self.async_step_init()

        if not self._devices:
            return await self.async_step_init()

        device_options = [
            selector.SelectOptionDict(
                value=d["device_id"],
                label=f"{d.get('name', d['device_id'])} ({d['device_id']})"
            )
            for d in self._devices
        ]

        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema(
                {
                    vol.Optional("remove_device"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=device_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
