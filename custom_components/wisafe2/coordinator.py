"""Data coordinator for WiSafe2 FireAngel Bridge."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import serial
import serial.tools.list_ports

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    HEARTBEAT_TIMEOUT,
    CMD_TEST_CO,
    CMD_TEST_SMOKE,
    CMD_TEST_ALL,
    CMD_EMERGENCY_CO,
    CMD_EMERGENCY_SMOKE,
    CMD_SILENCE_CO,
    CMD_SILENCE_SMOKE,
    CMD_GET_PAIRING,
    CMD_START_PAIRING,
    DEVICE_MODELS,
    DEVICE_TYPE_BRIDGE,
)

_LOGGER = logging.getLogger(__name__)


def get_serial_ports() -> list[str]:
    """Get list of available serial ports."""
    ports = serial.tools.list_ports.comports()
    return [port.device for port in ports]


class WiSafe2Device:
    """Represents a WiSafe2 device."""

    def __init__(self, device_id: str, model_id: str | None = None) -> None:
        """Initialize the device."""
        self.device_id = device_id
        self.model_id = model_id
        self.name: str | None = None
        self.location: str | None = None
        self.device_type: str | None = None
        self.last_seen: datetime | None = None
        self.battery_status: str = "unknown"
        self.base_status: str = "unknown"
        self.last_event: str | None = None
        self.last_test_result: str | None = None
        self.is_online: bool = True

        if model_id and model_id in DEVICE_MODELS:
            model_info = DEVICE_MODELS[model_id]
            self.name = model_info["name"]
            self.device_type = model_info["type"]

    def update_from_message(self, data: dict[str, Any]) -> None:
        """Update device state from a parsed message."""
        self.last_seen = dt_util.utcnow()

        if "battery" in data:
            self.battery_status = data["battery"]

        if "base" in data:
            self.base_status = data["base"]

        if "event" in data:
            self.last_event = data["event"]

        if "test_result" in data:
            self.last_test_result = data["test_result"]


class WiSafe2Coordinator(DataUpdateCoordinator):
    """Coordinator to manage WiSafe2 bridge communication."""

    def __init__(
        self,
        hass: HomeAssistant,
        serial_port: str,
        baud_rate: int,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=5),
        )
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self._serial: serial.Serial | None = None
        self._devices: dict[str, WiSafe2Device] = {}
        self._bridge_online = False
        self._last_heartbeat: datetime | None = None
        self._read_task: asyncio.Task | None = None
        self._running = False
        self._raw_data: str | None = None
        self._last_message: str | None = None

        # Create bridge device
        self._bridge_device = WiSafe2Device("bridge", None)
        self._bridge_device.name = "WiSafe2 Bridge"
        self._bridge_device.device_type = DEVICE_TYPE_BRIDGE

    @property
    def bridge_online(self) -> bool:
        """Return if bridge is online."""
        if self._last_heartbeat is None:
            return False
        return (dt_util.utcnow() - self._last_heartbeat).total_seconds() < HEARTBEAT_TIMEOUT

    @property
    def devices(self) -> dict[str, WiSafe2Device]:
        """Return all discovered devices."""
        return self._devices

    @property
    def bridge_device(self) -> WiSafe2Device:
        """Return the bridge device."""
        return self._bridge_device

    @property
    def raw_data(self) -> str | None:
        """Return raw serial data."""
        return self._raw_data

    @property
    def last_message(self) -> str | None:
        """Return last parsed message."""
        return self._last_message

    async def async_start(self) -> bool:
        """Start the serial connection and reader."""
        try:
            self._serial = await self.hass.async_add_executor_job(
                self._open_serial_connection
            )
            self._running = True
            self._read_task = self.hass.async_create_task(self._async_read_serial())
            _LOGGER.info("WiSafe2 bridge connected on %s", self.serial_port)
            return True
        except Exception as err:
            _LOGGER.error("Failed to connect to WiSafe2 bridge: %s", err)
            return False

    def _open_serial_connection(self) -> serial.Serial:
        """Open serial connection (runs in executor)."""
        return serial.Serial(
            port=self.serial_port,
            baudrate=self.baud_rate,
            timeout=0.1,
        )

    async def async_stop(self) -> None:
        """Stop the serial connection and reader."""
        self._running = False
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._serial:
            await self.hass.async_add_executor_job(self._serial.close)
            self._serial = None
        _LOGGER.info("WiSafe2 bridge disconnected")

    async def _async_read_serial(self) -> None:
        """Read data from serial port."""
        buffer = ""
        while self._running:
            try:
                if self._serial and self._serial.in_waiting:
                    data = await self.hass.async_add_executor_job(
                        self._serial.readline
                    )
                    if data:
                        try:
                            line = data.decode("utf-8").strip()
                            if line:
                                self._raw_data = line
                                await self._process_line(line)
                        except UnicodeDecodeError:
                            _LOGGER.debug("Failed to decode serial data")
                else:
                    await asyncio.sleep(0.1)
            except Exception as err:
                _LOGGER.error("Error reading serial: %s", err)
                await asyncio.sleep(1)

    async def _process_line(self, line: str) -> None:
        """Process a line of data from the bridge."""
        try:
            # Try to parse as JSON
            if line.startswith("{"):
                data = json.loads(line)
                await self._handle_json_message(data)
            else:
                _LOGGER.debug("Non-JSON data received: %s", line)
        except json.JSONDecodeError:
            _LOGGER.debug("Invalid JSON: %s", line)

    async def _handle_json_message(self, data: dict[str, Any]) -> None:
        """Handle a parsed JSON message from Arduino.

        Arduino JSON formats:
        - Heartbeat: {"heartBeat":"0"}
        - Test: {"device":"AD1A05", "model":"0401", "event":"FIRE TEST", "result":"PASS", "base":"ON", "battery":"OK"}
        - Emergency: {"device":"AD1A05", "event":"FIRE EMERGENCY", "base":"ON"}
        - Status: {"device":"AD1A05", "model":"0401", "base":"ON", "battery":"OK"}
        - Missing: {"device":"AD1A05", "event":"MISSING", "base":"MISSING", "battery":"MISSING"}
        - Silence: {"device":"AD1A05", "event":"SILENCE", "base":"ON"}
        """
        self._last_message = json.dumps(data)

        # Check for heartbeat (Arduino uses "heartBeat" with capital B)
        if "heartBeat" in data:
            self._last_heartbeat = dt_util.utcnow()
            self._bridge_online = True
            self._bridge_device.last_seen = self._last_heartbeat
            self._bridge_device.is_online = True
            _LOGGER.debug("Heartbeat received: %s", data.get("heartBeat"))
            self.async_set_updated_data(data)
            return

        # Check for device messages (Arduino uses "device" key with uppercase hex)
        device_id = data.get("device")
        if device_id:
            # Normalize device ID to lowercase
            device_id = device_id.lower()

            # Get or create device
            if device_id not in self._devices:
                model_id = data.get("model")
                self._devices[device_id] = WiSafe2Device(device_id, model_id)
                _LOGGER.info("Discovered new WiSafe2 device: %s", device_id)

            device = self._devices[device_id]
            device.update_from_message(data)

            # Update model if provided
            if data.get("model") and not device.model_id:
                device.model_id = data.get("model")

            # Handle event-based messages
            event = data.get("event", "")
            if event:
                await self._handle_device_event(device, event, data)

        # Trigger update for listeners
        self.async_set_updated_data(data)

    async def _handle_device_event(
        self, device: WiSafe2Device, event: str, data: dict[str, Any]
    ) -> None:
        """Handle a specific device event.

        Event strings from Arduino:
        - "FIRE TEST", "CARBON MONOXIDE TEST", "TEST"
        - "FIRE EMERGENCY", "CARBON MONOXIDE EMERGENCY"
        - "SILENCE"
        - "MISSING"
        """
        event_upper = event.upper()

        # Test events
        if "TEST" in event_upper:
            result = data.get("result", "unknown")
            device.last_test_result = f"{event}: {result}"
            device.last_event = event
            _LOGGER.info(
                "Test result from %s: %s - %s",
                device.device_id,
                event,
                result,
            )

        # Emergency events
        elif "EMERGENCY" in event_upper:
            device.last_event = event
            event_type = "FIRE" if "FIRE" in event_upper else "CO"
            _LOGGER.warning(
                "EMERGENCY from %s: %s",
                device.device_id,
                event,
            )
            # Fire an event for automations
            self.hass.bus.async_fire(
                f"{DOMAIN}_emergency",
                {
                    "device_id": device.device_id,
                    "event_type": event_type,
                    "event": event,
                    "device_name": device.name,
                    "location": device.location,
                },
            )

        # Silence events
        elif "SILENCE" in event_upper:
            device.last_event = event
            _LOGGER.info("Silence from %s", device.device_id)

        # Missing device events
        elif "MISSING" in event_upper:
            device.last_event = event
            device.is_online = False
            _LOGGER.warning("Device %s reported as missing", device.device_id)
            self.hass.bus.async_fire(
                f"{DOMAIN}_device_missing",
                {
                    "device_id": device.device_id,
                    "device_name": device.name,
                },
            )

        else:
            # Unknown event type, just store it
            device.last_event = event
            _LOGGER.debug("Unknown event from %s: %s", device.device_id, event)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the bridge."""
        # Check if bridge is still online
        if not self.bridge_online and self._bridge_online:
            self._bridge_online = False
            self._bridge_device.is_online = False
            _LOGGER.warning("WiSafe2 bridge appears offline")

        return {
            "bridge_online": self.bridge_online,
            "devices": {
                device_id: {
                    "name": device.name,
                    "model_id": device.model_id,
                    "device_type": device.device_type,
                    "battery_status": device.battery_status,
                    "base_status": device.base_status,
                    "last_event": device.last_event,
                    "last_test_result": device.last_test_result,
                    "last_seen": device.last_seen.isoformat() if device.last_seen else None,
                    "is_online": device.is_online,
                }
                for device_id, device in self._devices.items()
            },
        }

    async def async_send_command(self, command: str) -> bool:
        """Send a command to the bridge."""
        if not self._serial or not self._running:
            _LOGGER.error("Cannot send command: bridge not connected")
            return False

        try:
            await self.hass.async_add_executor_job(
                self._serial.write, command.encode("utf-8")
            )
            _LOGGER.debug("Sent command: %s", command)
            return True
        except Exception as err:
            _LOGGER.error("Failed to send command: %s", err)
            return False

    async def async_test_co(self) -> bool:
        """Send CO test command."""
        return await self.async_send_command(CMD_TEST_CO)

    async def async_test_smoke(self) -> bool:
        """Send smoke test command."""
        return await self.async_send_command(CMD_TEST_SMOKE)

    async def async_test_all(self) -> bool:
        """Send test all command."""
        return await self.async_send_command(CMD_TEST_ALL)

    async def async_emergency_co(self) -> bool:
        """Send CO emergency command."""
        return await self.async_send_command(CMD_EMERGENCY_CO)

    async def async_emergency_smoke(self) -> bool:
        """Send smoke emergency command."""
        return await self.async_send_command(CMD_EMERGENCY_SMOKE)

    async def async_silence_co(self) -> bool:
        """Send CO silence command."""
        return await self.async_send_command(CMD_SILENCE_CO)

    async def async_silence_smoke(self) -> bool:
        """Send smoke silence command."""
        return await self.async_send_command(CMD_SILENCE_SMOKE)

    async def async_get_pairing(self) -> bool:
        """Send get pairing status command."""
        return await self.async_send_command(CMD_GET_PAIRING)

    async def async_start_pairing(self) -> bool:
        """Send start pairing command."""
        return await self.async_send_command(CMD_START_PAIRING)

    def add_device(
        self,
        device_id: str,
        model_id: str | None = None,
        name: str | None = None,
    ) -> WiSafe2Device:
        """Manually add a device."""
        if device_id not in self._devices:
            device = WiSafe2Device(device_id, model_id)
            device.name = name
            self._devices[device_id] = device
        else:
            device = self._devices[device_id]
            if name:
                device.name = name
            if model_id:
                device.model_id = model_id
        return device
