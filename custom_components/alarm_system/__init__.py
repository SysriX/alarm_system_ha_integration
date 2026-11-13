"""Alarmanlage (alarm_system) – alarm_control_panel.home_security_system mit Modi Abwesend / Zuhause·Nacht / Urlaub,
PIN je Person, Sensoren je Modus, Ein-/Ausgangsverzögerung, Verlauf und Benachrichtigungskanälen,
Automatik (Anwesenheit/BLE, Türschloss, nur wenn zu). Weitere Entitäten: switch./binary_sensor.home_security_system_*.
Bedient über die Karte home-twin-alarm-card (Flur-Tablet). Einstellungen in .storage/alarm_system.

configuration.yaml:

    alarm_system:
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .const import DOMAIN, MODES
from .manager import AlarmError, AlarmManager

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Any(None, dict)}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    mgr = AlarmManager(hass)
    hass.data[DOMAIN] = mgr
    websocket.async_register(hass)
    await mgr.async_start()

    async def arm(call: ServiceCall) -> ServiceResponse:
        try:
            return await mgr.async_arm(call.data["mode"], call.data.get("code"), call.data["force"],
                                       call.data.get("source") or "Dienst", await mgr.user_name(call.context))
        except AlarmError as err:
            raise ServiceValidationError(str(err)) from err

    async def disarm(call: ServiceCall) -> ServiceResponse:
        try:
            return await mgr.async_disarm(call.data["code"], call.data.get("source") or "Dienst")
        except AlarmError as err:
            raise ServiceValidationError(str(err)) from err

    async def check(call: ServiceCall) -> ServiceResponse:
        return {"open": mgr.open_sensors(call.data["mode"]), "state": mgr.state}

    hass.services.async_register(DOMAIN, "arm", arm, vol.Schema({
        vol.Required("mode"): vol.In(MODES), vol.Optional("code"): cv.string,
        vol.Optional("force", default=False): cv.boolean, vol.Optional("source"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "disarm", disarm, vol.Schema({
        vol.Required("code"): cv.string, vol.Optional("source"): cv.string}), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "check", check, vol.Schema({vol.Required("mode"): vol.In(MODES)}),
                                 supports_response=SupportsResponse.ONLY)
    for platform in ("alarm_control_panel", "switch", "binary_sensor"):
        hass.async_create_task(async_load_platform(hass, platform, DOMAIN, {}, config))
    return True
