"""Zustandsmaschine, Einstellungen, PINs, Verlauf und Benachrichtigungen der Alarmanlage.

Ablauf: unscharf → (Ausgangsverzögerung „arming“) → scharf <Modus> → Sensor aktiv →
  verzögerter Sensor: Eingangsverzögerung „pending“ → Auslösung „triggered“
  sofortiger Sensor: direkt „triggered“
Nach der Alarmdauer zurück zu scharf (oder unscharf, Einstellung). 24h-Sensoren („always“) lösen in jedem Zustand aus.
Der Zustand überlebt einen Neustart (.storage/alarm_system).

Seit 1.2.0:
- Melder-Gruppen: als Melder darf auch eine HA-Gruppe (group.* / Gruppen-binary_sensor) eingetragen sein – ihre
  Mitglieder gelten dann mit denselben Einstellungen (self._eff).
- Überwachung je Modus (settings.watch): ein beim Scharfschalten offener (übersprungener) Melder darf zugehen
  (allow_close) und wird nach „settle“ Sekunden geschlossen wieder normal überwacht. Ohne allow_close löst auch
  das Zugehen aus.
- Automatik (settings.auto): presence (BLE/Personen weg → scharf, jemand da → unscharf), lock (abgeschlossen →
  scharf, aufgeschlossen → unscharf), closed (scharf nur, wenn die Gruppe Türen/Fenster zu ist).
"""

from __future__ import annotations

import copy
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ACTIVE_STATES, CHANNEL_DEFAULTS, CHANNEL_TYPES, DEFAULT_AUTO, DEFAULT_MODE, DEFAULT_SETTINGS, DEFAULT_TEXTS,
    DEFAULT_WATCH, EVENT_ALARM, EVENT_NAMES, EVENTS, LOCKED_STATES, LOG_ATTR, MAX_LOG, MODE_NAMES, MODES,
    OFFLINE_STATES, PRESENT_STATES, SENSOR_CLASSES, SIGNAL_UPDATE, STATE_NAMES, STORE_KEY, STORE_VERSION,
    UNLOCKED_STATES, VERSION,
)

_LOGGER = logging.getLogger(__name__)

WRONG_WINDOW_S = 600


class AlarmError(ValueError):
    """Fehler mit Text für Karte / Dienst."""


def _hash(salt: str, pin: str) -> str:
    return hashlib.sha256((salt + pin).encode()).hexdigest()


def _fill(obj: Any, v: dict[str, str]) -> Any:
    """Platzhalter {name} in allen Texten einer (verschachtelten) Vorlage ersetzen."""
    if isinstance(obj, str):
        for k, val in v.items():
            obj = obj.replace("{" + k + "}", str(val))
        return obj
    if isinstance(obj, list):
        return [_fill(x, v) for x in obj]
    if isinstance(obj, dict):
        return {k: _fill(x, v) for k, x in obj.items()}
    return obj


class AlarmManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.settings: dict = copy.deepcopy(DEFAULT_SETTINGS)
        self.users: list[dict] = []
        self.log: list[dict] = []
        # Laufzeit
        self.state = "disarmed"
        self.mode: str | None = None          # Ziel-/aktueller Scharf-Modus
        self.prev_state: str | None = None
        self.delay_end: datetime | None = None
        self.delay_total = 0
        self.trigger_end: datetime | None = None
        self.trigger_entity: str | None = None
        self.bypassed: list[str] = []
        self.changed_by: str | None = None
        self.armed_at: str | None = None
        self.wrong: list[float] = []
        self.locked_until: datetime | None = None
        self._timer = None
        self._unsubs: list = []
        self._watch: set[str] = set()
        self._eff: dict[str, dict] = {}       # Melder nach Auflösen der Gruppen → Einstellung
        self._closed: list[str] = []          # Türen/Fenster für „nur wenn zu“ (aufgelöst)
        self._pres: list[str] = []            # Anwesenheits-Entitäten (aufgelöst)
        self._locks: list[str] = []
        self._settle: dict[str, Any] = {}     # übersprungener Melder ist zugegangen → Timer bis wieder überwacht
        self._away_timer = None
        self.away_end: datetime | None = None
        self.present: bool | None = None      # jemand da? None = unbekannt
        self.auto_src: str | None = None      # wodurch zuletzt automatisch scharf (presence | lock)
        self._siren_on = False
        self.entity = None                    # wird von alarm_control_panel.py gesetzt

    # ================================================================ Lebenszyklus
    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.settings = self._clean_settings(data.get("settings") or {})
        self.users = data.get("users") or []
        self.log = data.get("log") or []
        rt = data.get("rt") or {}
        if not data.get("seeded"):
            # erst wenn alle Integrationen geladen sind, sonst fehlen z. B. die Homematic-Melder
            if self.hass.is_running:
                self._seed()
            else:
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._seed_started)
        mode = rt.get("mode")
        if rt.get("state") in ("arming", "pending", "triggered") or rt.get("state") in MODES:
            # nach einem Neustart sicher scharf weitermachen (Verzögerungen/Alarm laufen nicht weiter)
            if mode in MODES:
                self.state, self.mode = mode, mode
                self.bypassed = rt.get("bypassed") or []
                self.changed_by = rt.get("changed_by")
                self.armed_at = rt.get("armed_at")
                self.auto_src = rt.get("auto_src")
                self._log("restart", mode=mode, info=f"Neustart von Home Assistant – weiter {MODE_NAMES[mode]}")
        self._unsubs.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state, event_filter=self._is_watched))
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop)
        self._rewatch()
        if self.hass.is_running:
            self.present = self._presence()
        else:
            # Gruppen und Tracker sind erst nach dem Start vollständig da
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._started)

    @callback
    def _started(self, _ev: Event | None = None) -> None:
        self._rewatch()
        self.present = self._presence()
        self._push()

    @callback
    def _seed_started(self, _ev: Event | None = None) -> None:
        self._seed()
        self._rewatch()
        self._push()

    def _seed(self) -> None:
        """Erster Start: Tür-/Fensterkontakte (nach Geräteklasse) vorbelegen und alle Kanäle anlegen.

        Personen und PINs werden nicht vorbelegt – sie werden in der Karte angelegt (HA-Admins dürfen immer verwalten).
        """
        st = self.hass.states
        sens: dict[str, dict] = {}
        for s in st.async_all("binary_sensor"):
            if s.attributes.get("device_class") in ("door", "window", "garage_door"):
                sens[s.entity_id] = {"modes": list(MODES), "delayed": False, "always": False}
        self.settings["sensors"] = sens
        self.settings["channels"] = [self._clean_channel({"type": t, "id": t, "enabled": t == "tablet"}) for t in CHANNEL_TYPES]
        self._save()

    async def _async_stop(self, _ev: Event | None = None) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        await self.store.async_save(self._data())

    def _data(self) -> dict:
        return {"settings": self.settings, "users": self.users, "log": self.log[-MAX_LOG:], "seeded": True,
                "rt": {"state": self.state, "mode": self.mode, "bypassed": self.bypassed,
                       "changed_by": self.changed_by, "armed_at": self.armed_at, "auto_src": self.auto_src}}

    def _save(self) -> None:
        self.store.async_delay_save(self._data, 2)

    def _push(self) -> None:
        self._save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # ================================================================ Bereinigen
    @staticmethod
    def _int(v: Any, lo: int, hi: int, dflt: int) -> int:
        try:
            return max(lo, min(hi, int(float(v))))
        except (TypeError, ValueError):
            return dflt

    def _clean_mode(self, m: dict) -> dict:
        m = {**DEFAULT_MODE, **(m or {})}
        return {"enabled": bool(m["enabled"]), "exit_delay": self._int(m["exit_delay"], 0, 600, 60),
                "entry_delay": self._int(m["entry_delay"], 0, 600, 30), "trigger_time": self._int(m["trigger_time"], 10, 3600, 180)}

    def _clean_channel(self, c: dict) -> dict:
        t = c.get("type")
        if t not in CHANNEL_TYPES:
            raise AlarmError(f"Unbekannter Kanal-Typ {t}")
        d = CHANNEL_DEFAULTS[t]
        out = {"id": str(c.get("id") or secrets.token_hex(4)), "type": t, "enabled": bool(c.get("enabled", False)),
               "name": str(c.get("name") or d["name"])[:60],
               "events": [e for e in (c.get("events") if isinstance(c.get("events"), list) else d["events"]) if e in EVENTS]}
        if t == "tablet":
            out["browsers"] = [str(b) for b in (c.get("browsers") or []) if b]
            out["sound"] = bool(c.get("sound", d["sound"]))
        elif t == "siren":
            out["entities"] = [str(e) for e in (c.get("entities") or []) if "." in str(e)]
        elif t == "rest":
            out["url"] = str(c.get("url") or "")
            out["method"] = str(c.get("method") or "POST").upper() if str(c.get("method") or "POST").upper() in ("GET", "POST", "PUT") else "POST"
            out["headers"] = c.get("headers") if isinstance(c.get("headers"), dict) else copy.deepcopy(d["headers"])
            out["body"] = c.get("body") if isinstance(c.get("body"), (dict, list, str)) else copy.deepcopy(d["body"])
        else:
            out["service"] = str(c.get("service") or "")
            if out["service"] and "." not in out["service"]:
                raise AlarmError(f"Dienst „{out['service']}“ muss domain.dienst sein")
            out["data"] = c.get("data") if isinstance(c.get("data"), dict) else copy.deepcopy(d["data"])
            if "nummer" in d:
                out["nummer"] = str(c.get("nummer") or "")
        return out

    def _clean_settings(self, s: dict) -> dict:
        base = copy.deepcopy(DEFAULT_SETTINGS)
        out = {**base, **{k: v for k, v in (s or {}).items() if k in base}}
        out["modes"] = {m: self._clean_mode((out.get("modes") or {}).get(m)) for m in MODES}
        sens = {}
        for eid, v in (out.get("sensors") or {}).items():
            if not isinstance(v, dict) or "." not in eid:
                continue
            sens[eid] = {"modes": [m for m in (v.get("modes") or []) if m in MODES], "delayed": bool(v.get("delayed")),
                         "always": bool(v.get("always"))}
        out["sensors"] = sens
        out["code_arm_required"] = bool(out["code_arm_required"])
        out["service_open"] = out["service_open"] if out["service_open"] in ("bypass", "block") else "bypass"
        out["disarm_after_trigger"] = bool(out["disarm_after_trigger"])
        out["max_wrong"] = self._int(out["max_wrong"], 2, 20, 5)
        out["lock_s"] = self._int(out["lock_s"], 10, 3600, 120)
        out["texts"] = {k: str((out.get("texts") or {}).get(k) or v)[:300] for k, v in DEFAULT_TEXTS.items()}
        chans = []
        for c in out.get("channels") or []:
            try:
                chans.append(self._clean_channel(c))
            except AlarmError as err:
                _LOGGER.warning("Kanal verworfen: %s", err)
        out["channels"] = chans
        w = out.get("watch") if isinstance(out.get("watch"), dict) else {}
        out["watch"] = {m: {"allow_close": bool({**DEFAULT_WATCH, **(w.get(m) or {})}["allow_close"]),
                            "settle": self._int((w.get(m) or {}).get("settle", DEFAULT_WATCH["settle"]), 0, 600, 15)}
                        for m in MODES}
        out["auto"] = self._clean_auto(out.get("auto"))
        return out

    def _clean_auto(self, a: Any) -> dict:
        a = a if isinstance(a, dict) else {}
        ents = lambda v: [str(e) for e in (v or []) if isinstance(e, str) and "." in e]  # noqa: E731
        modes = lambda v, d: [m for m in (v if isinstance(v, list) else d) if m in MODES]  # noqa: E731
        p = {**DEFAULT_AUTO["presence"], **(a.get("presence") or {})}
        lk = {**DEFAULT_AUTO["lock"], **(a.get("lock") or {})}
        c = {**DEFAULT_AUTO["closed"], **(a.get("closed") or {})}
        return {
            "presence": {"enabled": bool(p["enabled"]), "entities": ents(p["entities"]),
                         "mode": p["mode"] if p["mode"] in MODES else "armed_away",
                         "away_delay": self._int(p["away_delay"], 0, 3600, 120),
                         "arm": bool(p["arm"]), "disarm": bool(p["disarm"]),
                         "disarm_modes": modes(p["disarm_modes"], ["armed_away"])},
            "lock": {"enabled": bool(lk["enabled"]), "entities": ents(lk["entities"]),
                     "mode": lk["mode"] if lk["mode"] in MODES else "armed_away",
                     "arm": bool(lk["arm"]), "disarm": bool(lk["disarm"]), "require_away": bool(lk["require_away"]),
                     "disarm_modes": modes(lk["disarm_modes"], ["armed_away"])},
            "closed": {"enabled": bool(c["enabled"]), "entities": ents(c["entities"]), "manual": bool(c["manual"])},
        }

    async def async_set(self, path: str, value: Any, who: str | None = None) -> None:
        """Einzelne Einstellung ändern (Schalter-Entitäten), z. B. auto.presence.enabled."""
        s = copy.deepcopy(self.settings)
        node, parts = s, path.split(".")
        for k in parts[:-1]:
            node = node[k]
        node[parts[-1]] = value
        self.settings = self._clean_settings(s)
        self._rewatch()
        self.present = self._presence()
        self._log("settings", user=who, info=f"{path} = {value}")
        self._push()

    # ================================================================ Sensoren
    def _members(self, eid: str, depth: int = 0) -> list[str]:
        """Gruppe → Mitglieder (rekursiv), sonst die Entität selbst."""
        s = self.hass.states.get(eid)
        mem = s.attributes.get("entity_id") if s else None
        if depth < 4 and isinstance(mem, (list, tuple)) and mem and not eid.startswith(("person.", "zone.")):
            out: list[str] = []
            for m in mem:
                for x in self._members(str(m), depth + 1):
                    if x not in out:
                        out.append(x)
            return out
        return [eid]

    def _expand(self, ids: list[str]) -> list[str]:
        out: list[str] = []
        for e in ids:
            for x in self._members(e):
                if x not in out:
                    out.append(x)
        return out

    def _rewatch(self) -> None:
        eff: dict[str, dict] = {}
        for eid, c in self.settings["sensors"].items():
            for m in self._members(eid):
                x = eff.setdefault(m, {"modes": [], "delayed": False, "always": False, "group": None})
                x["modes"] = sorted(set(x["modes"]) | set(c["modes"]), key=MODES.index)
                x["delayed"] = x["delayed"] or c["delayed"]
                x["always"] = x["always"] or c["always"]
                if m != eid:
                    x["group"] = eid
        self._eff = eff
        a = self.settings["auto"]
        self._closed = self._expand(a["closed"]["entities"])
        self._pres = list(a["presence"]["entities"])       # person/device_tracker nicht auflösen
        self._locks = self._expand(a["lock"]["entities"])
        self._watch = set(eff) | set(self.settings["sensors"]) | set(self._closed) | set(self._pres) | set(self._locks)

    # ---------------------------------------------------------------- Anwesenheit / Türen zu
    def _presence(self) -> bool | None:
        """True = jemand da, False = alle gewählten weg, None = nichts gewählt / alles unbekannt."""
        known = False
        for eid in self._pres:
            s = self.hass.states.get(eid)
            if s is None or s.state in OFFLINE_STATES:
                continue
            known = True
            if str(s.state).lower() in PRESENT_STATES:
                return True
        return False if known else None

    def _present_names(self) -> list[str]:
        return [self._name(e) for e in self._pres
                if (s := self.hass.states.get(e)) is not None and str(s.state).lower() in PRESENT_STATES]

    def closed_open(self) -> list[dict]:
        """Türen/Fenster der „nur wenn zu“-Gruppe, die offen oder nicht erreichbar sind."""
        out = []
        for eid in self._closed:
            s = self.hass.states.get(eid)
            if s is None or s.state in OFFLINE_STATES:
                out.append({"entity_id": eid, "name": self._name(eid), "state": "offline"})
            elif self._active(s):
                out.append({"entity_id": eid, "name": self._name(eid), "state": s.state})
        return out

    @callback
    def _is_watched(self, data: Any) -> bool:
        return data.get("entity_id") in self._watch

    @staticmethod
    def _active(state) -> bool:
        return state is not None and str(state.state).lower() in ACTIVE_STATES

    def _name(self, eid: str | None) -> str:
        if not eid:
            return ""
        s = self.hass.states.get(eid)
        return (s.name if s else None) or eid

    def open_sensors(self, mode: str) -> list[dict]:
        """Sensoren des Modus, die gerade offen/aktiv oder nicht erreichbar sind."""
        out = []
        for eid, c in self._eff.items():
            if mode not in c["modes"] and not c["always"]:
                continue
            s = self.hass.states.get(eid)
            if s is None or s.state in OFFLINE_STATES:
                out.append({"entity_id": eid, "name": self._name(eid), "state": "offline"})
            elif self._active(s):
                out.append({"entity_id": eid, "name": self._name(eid), "state": s.state})
        return out

    def candidates(self) -> list[dict]:
        """Alle Sensoren, die als Melder in Frage kommen (für die Verwaltung)."""
        ents, areas = er.async_get(self.hass), ar.async_get(self.hass)
        out = []
        for s in self.hass.states.async_all(["binary_sensor", "sensor", "group"]):
            dc = s.attributes.get("device_class")
            if s.object_id.startswith("home_security_system"):
                continue   # eigene Status-Sensoren
            if isinstance(s.attributes.get("entity_id"), (list, tuple)) and s.domain in ("group", "binary_sensor"):
                mem = self._members(s.entity_id)
                if not any(m.startswith("binary_sensor.") for m in mem):
                    continue   # z. B. Licht-Gruppen
                out.append({"entity_id": s.entity_id, "name": s.name, "state": s.state, "class": "group",
                            "kind": f"Gruppe ({len(mem)})", "area": "", "members": mem})
                continue
            if s.domain == "group":
                continue
            if s.domain == "binary_sensor" and dc not in SENSOR_CLASSES:
                continue
            if s.domain == "sensor" and s.state not in ("open", "closed", "tilted"):
                continue   # Fenster-Tür-Manager: offen / gekippt / zu
            e = ents.async_get(s.entity_id)
            area_id = e and (e.area_id or (e.device_id and self._dev_area(e.device_id)))
            a = areas.async_get_area(area_id) if area_id else None
            out.append({"entity_id": s.entity_id, "name": s.name, "state": s.state, "class": dc or "contact",
                        "kind": SENSOR_CLASSES.get(dc, "Kontakt"), "area": a.name if a else ""})
        return sorted(out, key=lambda x: (x["area"] or "~", x["kind"], x["name"]))

    def _dev_area(self, device_id: str) -> str | None:
        from homeassistant.helpers import device_registry as dr
        d = dr.async_get(self.hass).async_get(device_id)
        return d.area_id if d else None

    async def _on_state(self, ev: Event) -> None:
        eid, old, new = ev.data["entity_id"], ev.data.get("old_state"), ev.data.get("new_state")
        if eid in self._pres:
            self._on_presence()
        if eid in self._locks:
            self._on_lock(eid, old, new)
        if eid in self.settings["sensors"] and eid not in self._eff:
            self._rewatch()                      # Gruppe hat neue Mitglieder
        if eid in self._closed:
            async_dispatcher_send(self.hass, SIGNAL_UPDATE)   # binary_sensor …_openings
        cfg = self._eff.get(eid)
        if cfg is None:
            return
        was, now = self._active(old), self._active(new)
        if self.entity:
            self.entity.async_write_ha_state()   # offene Sensoren im Attribut aktuell halten
        if eid in self.bypassed and not cfg["always"]:
            self._bypassed_change(eid, was, now, new)
            return
        if not now or was:
            return
        if cfg["always"]:
            if self.state != "triggered":
                self._trigger(eid)
            return
        st = self.state
        if st in MODES and st in cfg["modes"] and eid not in self.bypassed:
            ent = self.settings["modes"][st]["entry_delay"]
            if cfg["delayed"] and ent > 0:
                self._pending(eid, ent)
            else:
                self._trigger(eid)
        elif st == "pending" and self.mode in cfg["modes"] and not cfg["delayed"] and eid not in self.bypassed:
            self._trigger(eid)

    # ---------------------------------------------------------------- übersprungene (offene) Melder
    def _bypassed_change(self, eid: str, was: bool, now: bool, new) -> None:
        """Ein beim Scharfschalten offener Melder ändert sich. Zugehen ist je nach Modus erlaubt (Wind)."""
        if self.state not in MODES and self.state not in ("arming", "pending"):
            return
        if now:
            # wieder auf (Fenster schlägt hin und her) → noch nicht wieder überwachen
            u = self._settle.pop(eid, None)
            if u:
                u()
            return
        if not was or new is None or new.state in OFFLINE_STATES:
            return
        w = self.settings["watch"].get(self.mode or "", DEFAULT_WATCH)
        if self.state == "arming" or w["allow_close"]:
            secs = w["settle"] if self.state != "arming" else 0
            u = self._settle.pop(eid, None)
            if u:
                u()
            if secs <= 0:
                self._resume(eid)
            else:
                self._settle[eid] = async_track_point_in_utc_time(
                    self.hass, callback(lambda _n, e=eid: self._resume(e)), dt_util.utcnow() + timedelta(seconds=secs))
            return
        # Schließen nicht erlaubt → wie Öffnen behandeln
        self.bypassed = [b for b in self.bypassed if b != eid]
        cfg = self._eff.get(eid) or {}
        if self.state in MODES and self.state in cfg.get("modes", []):
            ent = self.settings["modes"][self.state]["entry_delay"]
            if cfg.get("delayed") and ent > 0:
                self._pending(eid, ent)
            else:
                self._trigger(eid)
        elif self.state == "pending" and not cfg.get("delayed"):
            self._trigger(eid)

    @callback
    def _resume(self, eid: str) -> None:
        """Zugegangener Melder ist lange genug zu → ab jetzt wieder normal überwacht."""
        self._settle.pop(eid, None)
        if eid not in self.bypassed or self._active(self.hass.states.get(eid)):
            return
        self.bypassed = [b for b in self.bypassed if b != eid]
        self._log("resumed", mode=self.mode, sensor=eid, info="zugegangen – wird wieder überwacht")
        self._push()

    def _clear_settle(self) -> None:
        for u in self._settle.values():
            u()
        self._settle.clear()

    # ---------------------------------------------------------------- Automatik: Anwesenheit
    @callback
    def _on_presence(self) -> None:
        p = self._presence()
        if p == self.present:
            return
        before, self.present = self.present, p
        cfg = self.settings["auto"]["presence"]
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        if not cfg["enabled"]:
            return
        if p is False:
            if cfg["arm"] and before is True:
                self._cancel_away()
                if cfg["away_delay"] > 0:
                    self.away_end = dt_util.utcnow() + timedelta(seconds=cfg["away_delay"])
                    self._away_timer = async_track_point_in_utc_time(self.hass, self._away_due, self.away_end)
                else:
                    self.hass.async_create_task(self._auto_arm("presence", cfg["mode"], "Alle weg"))
        elif p is True:
            self._cancel_away()
            if cfg["disarm"]:
                who = ", ".join(self._present_names()) or "Anwesenheit"
                self._auto_disarm("presence", cfg["disarm_modes"], who)

    def _cancel_away(self) -> None:
        if self._away_timer:
            self._away_timer()
            self._away_timer = None
        self.away_end = None

    @callback
    def _away_due(self, _now=None) -> None:
        self._away_timer = None
        self.away_end = None
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        cfg = self.settings["auto"]["presence"]
        if cfg["enabled"] and cfg["arm"] and self._presence() is False:
            self.hass.async_create_task(self._auto_arm("presence", cfg["mode"], "Alle weg"))

    # ---------------------------------------------------------------- Automatik: Türschloss
    @callback
    def _on_lock(self, eid: str, old, new) -> None:
        cfg = self.settings["auto"]["lock"]
        if not cfg["enabled"] or old is None or new is None or old.state in OFFLINE_STATES:
            return
        o, n = str(old.state).lower(), str(new.state).lower()
        name = self._name(eid)
        if n in LOCKED_STATES and o not in LOCKED_STATES and cfg["arm"]:
            if cfg["require_away"] and self._presence() is not False:
                self._log("auto_skip", src=f"Schloss {name}", info="abgeschlossen, aber jemand ist da – nicht scharf")
                self._push()
                return
            self.hass.async_create_task(self._auto_arm("lock", cfg["mode"], f"{name} abgeschlossen"))
        elif n in UNLOCKED_STATES and o not in UNLOCKED_STATES and cfg["disarm"]:
            self._auto_disarm("lock", cfg["disarm_modes"], f"{name} aufgeschlossen")

    # ---------------------------------------------------------------- Automatik: gemeinsam
    async def _auto_arm(self, kind: str, mode: str, why: str) -> None:
        src = ("Anwesenheit: " if kind == "presence" else "Schloss: ") + why
        if self.state != "disarmed":
            return
        if not self.settings["modes"][mode]["enabled"]:
            self._log("auto_skip", src=src, info=f"Modus {MODE_NAMES[mode]} ist ausgeschaltet")
            self._push()
            return
        try:
            res = await self.async_arm(mode, None, force=self.settings["service_open"] == "bypass", src=src,
                                       who="Automatik", auto=True)
        except AlarmError as err:
            self._log("auto_blocked", src=src, info=str(err))
            self._push()
            return
        if res["ok"]:
            self.auto_src = kind
            self._save()
            return
        names = ", ".join(o["name"] for o in res["open"])
        self._log("auto_blocked", mode=mode, src=src, info=("Nicht zu: " if res.get("blocked") else "Offen: ") + names)
        self._notify("auto_blocked", quelle=src, sensor_txt=names)
        self._push()

    def _auto_disarm(self, kind: str, modes: list[str], who: str) -> None:
        if self.state == "disarmed" or (self.mode not in modes):
            return
        src = "Anwesenheit" if kind == "presence" else "Schloss"
        self._do_disarm(who, src)
    # ================================================================ Zustände
    def _cancel(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None

    def _at(self, secs: int, cb) -> datetime:
        self._cancel()
        end = dt_util.utcnow() + timedelta(seconds=secs)
        self._timer = async_track_point_in_utc_time(self.hass, cb, end)
        return end

    def _set(self, state: str) -> None:
        if state != self.state:
            self.prev_state = self.state
        self.state = state

    @callback
    def _armed(self, _now=None) -> None:
        self._timer = None
        self._set(self.mode)
        self.delay_end = None
        self._log("armed", mode=self.mode, user=self.changed_by)
        self._notify("armed")
        self._push()

    def _pending(self, eid: str, secs: int) -> None:
        self._set("pending")
        self.trigger_entity = eid
        self.delay_total = secs
        self.delay_end = self._at(secs, callback(lambda _n: self._trigger(eid)))
        self._log("pending", mode=self.mode, sensor=eid)
        self._notify("pending", sensor=eid, sekunden=secs)
        self._push()

    def _trigger(self, eid: str | None, user: str | None = None, src: str = "") -> None:
        self._cancel()
        back = self.mode if self.state in MODES or self.state in ("arming", "pending", "triggered") else None
        self._set("triggered")
        self.trigger_entity = eid
        self.mode = back
        tt = self.settings["modes"][back]["trigger_time"] if back else 180
        self.delay_end = None
        self.trigger_end = self._at(tt, self._trigger_over)
        self._log("triggered", mode=back, sensor=eid, user=user, src=src)
        self._notify("triggered", sensor=eid, person=user or "")
        self._sirens(True)
        self._push()

    @callback
    def _trigger_over(self, _now=None) -> None:
        self._timer = None
        self._sirens(False)
        self.trigger_end = None
        if self.mode and not self.settings["disarm_after_trigger"]:
            self._set(self.mode)
            self._log("rearmed", mode=self.mode, info="Alarmdauer abgelaufen – wieder scharf")
        else:
            self._set("disarmed")
            self.mode = None
            self._log("reset", info="Alarmdauer abgelaufen – unscharf")
        self._close_popups()
        self._push()

    # ================================================================ PIN
    def _locked(self) -> int:
        if self.locked_until and dt_util.utcnow() < self.locked_until:
            return int((self.locked_until - dt_util.utcnow()).total_seconds()) + 1
        return 0

    def check_code(self, code: str | None, src: str = "", admin: bool = False) -> dict:
        """PIN prüfen → Benutzer. Falsche PINs werden gezählt und sperren die Eingabe."""
        left = self._locked()
        if left:
            raise AlarmError(f"Eingabe gesperrt – noch {left} s")
        code = str(code or "").strip()
        for u in self.users:
            if u.get("enabled", True) and u.get("pin") and code and _hash(u.get("salt", ""), code) == u["pin"]:
                if admin and not u.get("admin"):
                    raise AlarmError(f"{u['name']} darf die Verwaltung nicht öffnen")
                self.wrong.clear()
                return u
        now = dt_util.utcnow().timestamp()
        self.wrong = [t for t in self.wrong if now - t < WRONG_WINDOW_S] + [now]
        self._log("wrong_pin", src=src)
        self._notify("wrong_pin", quelle=src or "unbekannt")
        if len(self.wrong) >= self.settings["max_wrong"]:
            self.locked_until = dt_util.utcnow() + timedelta(seconds=self.settings["lock_s"])
            self.wrong.clear()
            self._log("locked", src=src, info=f"Eingabe {self.settings['lock_s']} s gesperrt")
            self._push()
            raise AlarmError(f"Falsche PIN – Eingabe {self.settings['lock_s']} s gesperrt")
        self._push()
        raise AlarmError("Falsche PIN")

    async def user_name(self, context: Context | None) -> str | None:
        if context and context.user_id:
            u = await self.hass.auth.async_get_user(context.user_id)
            if u:
                return u.name
        return None

    # ================================================================ Aktionen
    async def async_arm(self, mode: str, code: str | None = None, force: bool = False, src: str = "",
                        who: str | None = None, auto: bool = False) -> dict:
        if mode not in MODES:
            raise AlarmError(f"Unbekannter Modus {mode}")
        if not self.settings["modes"][mode]["enabled"]:
            raise AlarmError(f"Modus {MODE_NAMES[mode]} ist ausgeschaltet")
        if self.state in ("pending", "triggered"):
            raise AlarmError("Erst unscharf schalten")
        user = None
        if not auto and (self.settings["code_arm_required"] or code):
            user = self.check_code(code, src)
        cl = self.settings["auto"]["closed"]
        if cl["enabled"] and (auto or cl["manual"]):
            co = self.closed_open()
            if co:
                # „Nur wenn zu“: kein Trotzdem-scharf möglich
                return {"ok": False, "open": co, "blocked": True}
        open_ = self.open_sensors(mode)
        if open_ and not force:
            return {"ok": False, "open": open_}
        self._cancel()
        self._clear_settle()
        self.auto_src = None
        self.mode = mode
        self.bypassed = [o["entity_id"] for o in open_]
        self.changed_by = (user or {}).get("name") or who or src or None
        self.armed_at = dt_util.utcnow().isoformat()
        self.trigger_entity = None
        if self.bypassed:
            self._log("bypass", mode=mode, user=self.changed_by, src=src,
                      info="Trotzdem scharf: " + ", ".join(o["name"] for o in open_))
        ex = self.settings["modes"][mode]["exit_delay"]
        if ex > 0:
            self._set("arming")
            self.delay_total = ex
            self.delay_end = self._at(ex, self._armed)
            self._log("arming", mode=mode, user=self.changed_by, src=src, info=f"Ausgangsverzögerung {ex} s")
            self._push()
        else:
            self._armed()
        return {"ok": True, "state": self.state, "bypassed": self.bypassed}

    async def async_disarm(self, code: str | None, src: str = "", who: str | None = None) -> dict:
        user = self.check_code(code, src)
        self._do_disarm(user["name"], src)
        return {"ok": True, "user": user["name"]}

    def _do_disarm(self, name: str, src: str) -> None:
        was = self.state
        self._cancel()
        self._clear_settle()
        self._sirens(False)
        self._set("disarmed")
        self.mode = None
        self.auto_src = None
        self.delay_end = self.trigger_end = None
        self.bypassed = []
        self.trigger_entity = None
        self.changed_by = name
        self._log("disarmed", user=name, src=src, info=f"vorher {STATE_NAMES.get(was, was)}")
        if was != "disarmed":
            self._notify("disarmed", person=name, quelle=src)
        self._close_popups()
        self._push()

    async def async_trigger(self, src: str = "", who: str | None = None) -> None:
        self._trigger(None, user=who, src=src or "Panik / manuell")

    # ================================================================ Verwaltung
    def view_settings(self) -> dict:
        return {"settings": self.settings,
                "users": [{k: v for k, v in u.items() if k not in ("pin", "salt")} | {"has_pin": bool(u.get("pin"))}
                          for u in self.users],
                "modes": MODE_NAMES, "events": EVENT_NAMES, "channel_types": CHANNEL_TYPES,
                "channel_defaults": CHANNEL_DEFAULTS}

    async def async_save(self, settings: dict | None, users: list | None, who: str) -> dict:
        if settings is not None:
            self.settings = self._clean_settings({**self.settings, **settings})
            self._rewatch()
            self.present = self._presence()
            if not self.settings["auto"]["presence"]["enabled"]:
                self._cancel_away()
        if users is not None:
            old = {u["id"]: u for u in self.users}
            pins: dict[str, str] = {}
            new = []
            for u in users:
                uid = str(u.get("id") or secrets.token_hex(4))
                o = old.get(uid, {})
                nu = {"id": uid, "name": str(u.get("name") or "Person")[:40], "person": str(u.get("person") or ""),
                      "admin": bool(u.get("admin")), "enabled": bool(u.get("enabled", True)),
                      "salt": o.get("salt", ""), "pin": o.get("pin", "")}
                pin = str(u.get("new_pin") or "").strip()
                if u.get("clear_pin"):
                    nu["salt"] = nu["pin"] = ""
                elif pin:
                    if not pin.isdigit() or not 4 <= len(pin) <= 8:
                        raise AlarmError(f"PIN von {nu['name']}: 4–8 Ziffern")
                    if pin in pins.values():
                        raise AlarmError("Zwei Personen haben dieselbe PIN")
                    pins[uid] = pin
                    nu["salt"] = secrets.token_hex(8)
                    nu["pin"] = _hash(nu["salt"], pin)
                new.append(nu)
            # neue PIN darf nicht mit einer bestehenden PIN eines anderen übereinstimmen
            for uid, pin in pins.items():
                for u in new:
                    if u["id"] != uid and u["pin"] and _hash(u["salt"], pin) == u["pin"]:
                        raise AlarmError("Diese PIN hat schon eine andere Person")
            if not any(u["admin"] and u["pin"] and u["enabled"] for u in new):
                raise AlarmError("Mindestens eine aktive Person mit Verwaltungsrecht braucht eine PIN")
            self.users = new
        self._log("settings", user=who, info="Einstellungen geändert")
        self._push()
        return self.view_settings()

    def history(self, limit: int = 100) -> list[dict]:
        return list(reversed(self.log[-limit:]))

    # ================================================================ Verlauf
    def _log(self, ev: str, mode: str | None = None, user: str | None = None, src: str = "", sensor: str | None = None,
             info: str = "") -> None:
        e = {"t": dt_util.utcnow().isoformat(), "ev": ev}
        if mode:
            e["mode"] = mode
        if user:
            e["user"] = user
        if src:
            e["src"] = src
        if sensor:
            e["sensor"] = sensor
            e["sensor_name"] = self._name(sensor)
        if info:
            e["info"] = info
        self.log.append(e)
        del self.log[:-MAX_LOG]
        self.hass.bus.async_fire(EVENT_ALARM, e)

    def log_attr(self) -> list[dict]:
        return list(reversed(self.log[-LOG_ATTR:]))

    # ================================================================ Benachrichtigungen
    def _vars(self, ev: str, **kw) -> dict[str, str]:
        sensor = kw.get("sensor")
        v = {"ereignis": ev, "zustand": STATE_NAMES.get(self.state, self.state), "modus": MODE_NAMES.get(self.mode or "", ""),
             "sensor": kw.get("sensor_txt") or (self._name(sensor) if sensor else ""), "person": kw.get("person") or self.changed_by or "",
             "quelle": kw.get("quelle", ""), "zeit": dt_util.now().strftime("%H:%M:%S"),
             "sekunden": str(kw.get("sekunden", "")), "kanal": kw.get("kanal", "")}
        t = self.settings["texts"]
        v["title"] = _fill(t["title"], v)
        v["message"] = _fill(t.get(ev, ""), v)
        return v

    def _notify(self, ev: str, **kw) -> None:
        for c in self.settings["channels"]:
            if c["enabled"] and ev in c["events"]:
                self.hass.async_create_task(self._send(c, ev, self._vars(ev, kanal=c["name"], **kw)))

    async def async_test(self, channel: dict) -> None:
        c = self._clean_channel(channel)
        await self._send(c, "test", self._vars("test", kanal=c["name"]), raise_err=True)

    async def _send(self, c: dict, ev: str, v: dict, raise_err: bool = False) -> None:
        try:
            t = c["type"]
            if t == "tablet":
                await self._tablet(c, ev)
            elif t == "siren":
                if ev == "test":
                    await self._siren_call(c["entities"], True)
                    self.hass.loop.call_later(3, lambda: self.hass.async_create_task(self._siren_call(c["entities"], False)))
            elif t == "rest":
                if not c["url"]:
                    raise AlarmError("Keine URL eingetragen")
                body = _fill(c["body"], v)
                sess = async_get_clientsession(self.hass)
                kw = {"headers": _fill(c["headers"], v), "timeout": 10}
                if c["method"] != "GET":
                    kw["data" if isinstance(body, str) else "json"] = body
                async with sess.request(c["method"], c["url"], **kw) as r:
                    if r.status >= 400:
                        raise AlarmError(f"REST-API antwortet {r.status}")
            else:
                if not c["service"]:
                    raise AlarmError("Kein Dienst eingetragen")
                v = {**v, "nummer": c.get("nummer", "")}
                dom, _, srv = c["service"].partition(".")
                await self.hass.services.async_call(dom, srv, _fill(c["data"], v), blocking=True)
        except Exception as err:  # noqa: BLE001 – ein Kanal darf die anderen nie aufhalten
            _LOGGER.warning("Alarm-Kanal %s (%s) fehlgeschlagen: %s", c["name"], ev, err)
            if raise_err:
                raise AlarmError(f"{c['name']}: {err}") from err
            self._log("channel_error", info=f"{c['name']}: {err}")

    async def _tablet(self, c: dict, ev: str) -> None:
        if not self.hass.services.has_service("browser_mod", "popup"):
            raise AlarmError("Browser Mod fehlt")
        data = {"tag": "alarm", "title": "Alarmanlage", "initial_style": "fullscreen", "dismissable": ev == "test",
                "content": {"type": "custom:home-twin-alarm-popup", "entity": self.entity.entity_id if self.entity else "",
                            "ton": c["sound"], "schliessen": True}}
        if ev == "test":
            data["timeout"] = 8000
        if c["browsers"]:
            data["browser_id"] = c["browsers"]
        await self.hass.services.async_call("browser_mod", "popup", data, blocking=False)

    def _close_popups(self) -> None:
        if any(c["enabled"] and c["type"] == "tablet" for c in self.settings["channels"]) and \
                self.hass.services.has_service("browser_mod", "close_popup"):
            self.hass.async_create_task(self.hass.services.async_call("browser_mod", "close_popup", {"tag": "alarm"}, blocking=False))

    def _sirens(self, on: bool) -> None:
        if not on and not self._siren_on:
            return
        self._siren_on = on
        ents = [e for c in self.settings["channels"] if c["enabled"] and c["type"] == "siren"
                and (not on or "triggered" in c["events"]) for e in c["entities"]]
        if ents:
            self.hass.async_create_task(self._siren_call(ents, on))

    async def _siren_call(self, ents: list[str], on: bool) -> None:
        for e in ents:
            dom = e.split(".", 1)[0]
            srv = "turn_on" if on else "turn_off"
            if dom == "media_player":
                srv = "media_play" if on else "media_stop"
            try:
                await self.hass.services.async_call(dom, srv, {"entity_id": e}, blocking=False)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Sirene %s: %s", e, err)

    # ================================================================ Ansicht
    def attrs(self) -> dict:
        def iso(d):
            return d.isoformat() if d else None
        open_now = {m: [o["entity_id"] for o in self.open_sensors(m)] for m in MODES}
        return {"mode": self.mode, "previous_state": self.prev_state,
                "next_state": self.mode if self.state in ("arming", "pending") else None,
                "delay_end": iso(self.delay_end) if self.state in ("arming", "pending") else None,
                "delay_total": self.delay_total if self.state in ("arming", "pending") else None,
                "trigger_end": iso(self.trigger_end) if self.state == "triggered" else None,
                "triggered_by": self.trigger_entity, "triggered_by_name": self._name(self.trigger_entity),
                "bypassed": self.bypassed, "armed_at": self.armed_at, "open_sensors": open_now,
                "modes_enabled": [m for m in MODES if self.settings["modes"][m]["enabled"]],
                "auto_presence": self.settings["auto"]["presence"]["enabled"],
                "auto_lock": self.settings["auto"]["lock"]["enabled"],
                "require_closed": self.settings["auto"]["closed"]["enabled"],
                "present": self.present, "present_names": self._present_names(),
                "away_arm_at": iso(self.away_end) if self._away_timer else None, "armed_by_auto": self.auto_src,
                "not_closed": [o["entity_id"] for o in self.closed_open()],
                "settling": list(self._settle), "watch": self.settings["watch"],
                "locked_until": iso(self.locked_until) if self._locked() else None,
                "log": self.log_attr(), "version": VERSION}
