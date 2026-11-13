"""WebSocket-API für die Alarm-Karte (Kachel + Popup).

alarm_system/config        → Einstellungen, Personen (ohne PIN), Kandidaten-Sensoren
alarm_system/arm           → {mode, code?, force?, source?} → {ok, open?}
alarm_system/disarm        → {code, source?}
alarm_system/verify        → {code} prüft eine Verwaltungs-PIN
alarm_system/save          → {code?, settings?, users?} (HA-Admin oder Verwaltungs-PIN)
alarm_system/history       → {limit?}
alarm_system/test_channel  → {code?, channel}
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, MODES
from .manager import AlarmError, AlarmManager


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_config, ws_arm, ws_disarm, ws_verify, ws_save, ws_history, ws_test):
        websocket_api.async_register_command(hass, cmd)


def _mgr(hass: HomeAssistant) -> AlarmManager:
    return hass.data[DOMAIN]


def _src(connection, msg) -> str:
    return str(msg.get("source") or "") or (connection.user.name if connection.user else "")


async def _run(connection, msg, coro) -> None:
    try:
        res = await coro
    except AlarmError as err:
        connection.send_error(msg["id"], "alarm_error", str(err))
        return
    connection.send_result(msg["id"], res)


def _admin(hass, connection, msg) -> str:
    """HA-Admin darf immer; sonst ist die PIN einer Person mit Verwaltungsrecht nötig."""
    if msg.get("code"):
        return _mgr(hass).check_code(msg["code"], _src(connection, msg), admin=True)["name"]
    if connection.user and connection.user.is_admin:
        return connection.user.name
    raise AlarmError("Verwaltungs-PIN nötig")


@websocket_api.websocket_command({vol.Required("type"): "alarm_system/config"})
@websocket_api.async_response
async def ws_config(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    m = _mgr(hass)
    connection.send_result(msg["id"], {**m.view_settings(), "candidates": m.candidates()})


@websocket_api.websocket_command({vol.Required("type"): "alarm_system/arm", vol.Required("mode"): vol.In(MODES),
                                  vol.Optional("code"): str, vol.Optional("force", default=False): bool,
                                  vol.Optional("source"): str})
@websocket_api.async_response
async def ws_arm(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    who = connection.user.name if connection.user else None
    await _run(connection, msg, _mgr(hass).async_arm(msg["mode"], msg.get("code"), msg["force"], _src(connection, msg), who))


@websocket_api.websocket_command({vol.Required("type"): "alarm_system/disarm", vol.Required("code"): str,
                                  vol.Optional("source"): str})
@websocket_api.async_response
async def ws_disarm(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    await _run(connection, msg, _mgr(hass).async_disarm(msg["code"], _src(connection, msg)))


@websocket_api.websocket_command({vol.Required("type"): "alarm_system/verify", vol.Required("code"): str,
                                  vol.Optional("source"): str})
@websocket_api.async_response
async def ws_verify(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    async def go():
        return {"name": _admin(hass, connection, msg)}
    await _run(connection, msg, go())


@websocket_api.websocket_command({vol.Required("type"): "alarm_system/save", vol.Optional("code"): str,
                                  vol.Optional("settings"): dict, vol.Optional("users"): list, vol.Optional("source"): str})
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    async def go():
        who = _admin(hass, connection, msg)
        return await _mgr(hass).async_save(msg.get("settings"), msg.get("users"), who)
    await _run(connection, msg, go())


@websocket_api.websocket_command({vol.Required("type"): "alarm_system/history", vol.Optional("limit", default=100): int})
@websocket_api.async_response
async def ws_history(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], _mgr(hass).history(msg["limit"]))


@websocket_api.websocket_command({vol.Required("type"): "alarm_system/test_channel", vol.Required("channel"): dict,
                                  vol.Optional("code"): str, vol.Optional("source"): str})
@websocket_api.async_response
async def ws_test(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    async def go():
        _admin(hass, connection, msg)
        await _mgr(hass).async_test(msg["channel"])
        return {"ok": True}
    await _run(connection, msg, go())
