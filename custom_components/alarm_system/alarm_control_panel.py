"""alarm_control_panel.home_security_system – die Alarmanlage als HA-Entität.

Standard-Dienste alarm_control_panel.alarm_arm_away / _home / _vacation / alarm_disarm / alarm_trigger funktionieren;
offene Sensoren werden dabei je nach Einstellung „service_open“ übersprungen (bypass) oder blockieren (block).
"""

from __future__ import annotations

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity, AlarmControlPanelEntityFeature, AlarmControlPanelState, CodeFormat,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ENTITY_ID, SIGNAL_UPDATE
from .manager import AlarmError, AlarmManager


async def async_setup_platform(hass: HomeAssistant, _config, async_add_entities: AddEntitiesCallback, _discovery=None) -> None:
    async_add_entities([AlarmPanel(hass.data[DOMAIN])])


class AlarmPanel(AlarmControlPanelEntity):
    _attr_should_poll = False
    _attr_name = "Alarmanlage"
    _attr_unique_id = f"{DOMAIN}_main"
    _attr_icon = "mdi:shield-home"
    _attr_code_format = CodeFormat.NUMBER
    _attr_supported_features = (AlarmControlPanelEntityFeature.ARM_AWAY | AlarmControlPanelEntityFeature.ARM_HOME
                                | AlarmControlPanelEntityFeature.ARM_VACATION | AlarmControlPanelEntityFeature.TRIGGER)
    _unrecorded_attributes = frozenset({"log", "open_sensors", "modes_enabled", "version", "delay_total", "watch",
                                        "present_names", "not_closed", "settling", "away_arm_at"})

    def __init__(self, mgr: AlarmManager) -> None:
        self.mgr = mgr
        self.entity_id = ENTITY_ID
        mgr.entity = self

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def alarm_state(self) -> AlarmControlPanelState:
        return AlarmControlPanelState(self.mgr.state)

    @property
    def code_arm_required(self) -> bool:
        return self.mgr.settings["code_arm_required"]

    @property
    def changed_by(self) -> str | None:
        return self.mgr.changed_by

    @property
    def extra_state_attributes(self) -> dict:
        return self.mgr.attrs()

    async def _arm(self, mode: str, code: str | None) -> None:
        who = await self.mgr.user_name(self._context)
        force = self.mgr.settings["service_open"] == "bypass"
        try:
            res = await self.mgr.async_arm(mode, code, force=force, src="Dienst", who=who)
        except AlarmError as err:
            raise ServiceValidationError(str(err)) from err
        if not res["ok"]:
            raise HomeAssistantError("Offen: " + ", ".join(o["name"] for o in res["open"]))

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._arm("armed_away", code)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._arm("armed_home", code)

    async def async_alarm_arm_vacation(self, code: str | None = None) -> None:
        await self._arm("armed_vacation", code)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        try:
            await self.mgr.async_disarm(code, src="Dienst", who=await self.mgr.user_name(self._context))
        except AlarmError as err:
            raise ServiceValidationError(str(err)) from err

    async def async_alarm_trigger(self, code: str | None = None) -> None:
        await self.mgr.async_trigger("Dienst", who=await self.mgr.user_name(self._context))
