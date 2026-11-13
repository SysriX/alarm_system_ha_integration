"""Zustands-Sensoren der Alarmanlage.

binary_sensor.home_security_system_presence  an = laut Anwesenheits-Auswahl (BLE) ist jemand da
binary_sensor.home_security_system_openings  an = eine Tür/ein Fenster der „nur wenn zu“-Gruppe ist offen
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, OBJECT_ID, SIGNAL_UPDATE
from .manager import AlarmManager


async def async_setup_platform(hass: HomeAssistant, _config, async_add_entities: AddEntitiesCallback, _discovery=None) -> None:
    mgr: AlarmManager = hass.data[DOMAIN]
    async_add_entities([PresenceSensor(mgr), OpeningsSensor(mgr)])


class _Base(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(self, mgr: AlarmManager, key: str) -> None:
        self.mgr = mgr
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self.entity_id = f"binary_sensor.{OBJECT_ID}_{key}"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class PresenceSensor(_Base):
    _attr_name = "Alarmanlage · Anwesenheit"
    _attr_device_class = BinarySensorDeviceClass.PRESENCE

    def __init__(self, mgr: AlarmManager) -> None:
        super().__init__(mgr, "presence")

    @property
    def available(self) -> bool:
        return self.mgr.present is not None

    @property
    def is_on(self) -> bool:
        return bool(self.mgr.present)

    @property
    def extra_state_attributes(self) -> dict:
        return {"present": self.mgr._present_names(), "entities": self.mgr._pres,
                "auto_arm_at": self.mgr.away_end.isoformat() if self.mgr.away_end else None}


class OpeningsSensor(_Base):
    _attr_name = "Alarmanlage · Türen/Fenster offen"
    _attr_device_class = BinarySensorDeviceClass.OPENING

    def __init__(self, mgr: AlarmManager) -> None:
        super().__init__(mgr, "openings")

    @property
    def is_on(self) -> bool:
        return bool(self.mgr.closed_open())

    @property
    def extra_state_attributes(self) -> dict:
        return {"open": [o["entity_id"] for o in self.mgr.closed_open()], "entities": self.mgr._closed}
