"""Schalter der Alarmanlage – Automatik-Optionen als Entitäten (für Dashboards / eigene Automationen).

switch.home_security_system_cfg_auto_presence      Anwesenheit (BLE) → scharf / unscharf
switch.home_security_system_cfg_auto_lock          Türschloss → scharf / unscharf
switch.home_security_system_cfg_require_closed     scharf nur, wenn Türen/Fenster der Gruppe zu sind
switch.home_security_system_cfg_allow_close_away   Abwesend: offener Melder darf zugehen
switch.home_security_system_cfg_allow_close_home   Zuhause/Nacht: offener Melder darf zugehen
switch.home_security_system_cfg_allow_close_vacation  Urlaub: offener Melder darf zugehen
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, OBJECT_ID, SIGNAL_UPDATE
from .manager import AlarmManager

# key, Pfad in settings, Name, Symbol
SWITCHES = [
    ("cfg_auto_presence", "auto.presence.enabled", "Automatik Anwesenheit", "mdi:account-arrow-right"),
    ("cfg_auto_lock", "auto.lock.enabled", "Automatik Türschloss", "mdi:lock-clock"),
    ("cfg_require_closed", "auto.closed.enabled", "Nur scharf wenn zu", "mdi:window-closed-variant"),
    ("cfg_allow_close_away", "watch.armed_away.allow_close", "Schließen erlaubt · Abwesend", "mdi:window-shutter-alert"),
    ("cfg_allow_close_home", "watch.armed_home.allow_close", "Schließen erlaubt · Zuhause/Nacht", "mdi:window-shutter-alert"),
    ("cfg_allow_close_vacation", "watch.armed_vacation.allow_close", "Schließen erlaubt · Urlaub", "mdi:window-shutter-alert"),
]


async def async_setup_platform(hass: HomeAssistant, _config, async_add_entities: AddEntitiesCallback, _discovery=None) -> None:
    mgr: AlarmManager = hass.data[DOMAIN]
    async_add_entities([AlarmSwitch(mgr, *s) for s in SWITCHES])


class AlarmSwitch(SwitchEntity):
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, mgr: AlarmManager, key: str, path: str, name: str, icon: str) -> None:
        self.mgr, self._path = mgr, path
        self._attr_name = f"Alarmanlage · {name}"
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self.entity_id = f"switch.{OBJECT_ID}_{key}"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        node: Any = self.mgr.settings
        for k in self._path.split("."):
            node = node[k]
        return bool(node)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.mgr.async_set(self._path, True, await self.mgr.user_name(self._context))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.mgr.async_set(self._path, False, await self.mgr.user_name(self._context))
