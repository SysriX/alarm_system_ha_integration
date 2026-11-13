"""Konstanten der Alarmanlage (alarm_system)."""

DOMAIN = "alarm_system"
VERSION = "1.2.0"

STORE_KEY = "alarm_system"
STORE_VERSION = 1

# Alle Entitäten heißen <domain>.home_security_system[_…] (seit 1.2.0, vorher haus_security_alarm)
OBJECT_ID = "home_security_system"
ENTITY_ID = f"alarm_control_panel.{OBJECT_ID}"
SIGNAL_UPDATE = "alarm_system_update"
EVENT_ALARM = "alarm_system_event"          # wird bei jedem Verlaufseintrag gefeuert (für eigene Automationen)

# Modi: Abwesend, Zuhause/Nacht, Urlaub
MODES = ["armed_away", "armed_home", "armed_vacation"]
MODE_NAMES = {"armed_away": "Abwesend", "armed_home": "Zuhause / Nacht", "armed_vacation": "Urlaub"}

STATE_NAMES = {
    "disarmed": "Unscharf", "arming": "Wird scharf", "pending": "Eingangsverzögerung", "triggered": "ALARM",
    **MODE_NAMES,
}

MAX_LOG = 300                    # Verlaufseinträge in .storage
LOG_ATTR = 25                    # davon im Attribut der Entität (für die Karte)

# Sensor gilt als „aktiv“ (offen / Bewegung / Sabotage …)
ACTIVE_STATES = {"on", "open", "opened", "tilted", "true", "detected", "motion", "unlocked"}
OFFLINE_STATES = {"unavailable", "unknown"}

# Kandidaten für die Sensor-Auswahl (binary_sensor nach Geräteklasse)
SENSOR_CLASSES = {
    "door": "Tür", "window": "Fenster", "opening": "Öffnung", "garage_door": "Tor",
    "motion": "Bewegung", "occupancy": "Belegung", "presence": "Präsenz",
    "smoke": "Rauch", "gas": "Gas", "carbon_monoxide": "CO", "moisture": "Wasser",
    "tamper": "Sabotage", "vibration": "Erschütterung", "sound": "Geräusch", "safety": "Sicherheit",
}

DEFAULT_MODE = {"enabled": True, "exit_delay": 60, "entry_delay": 30, "trigger_time": 180}
DEFAULT_MODES = {
    "armed_away": {**DEFAULT_MODE},
    "armed_home": {**DEFAULT_MODE, "exit_delay": 0},
    "armed_vacation": {**DEFAULT_MODE},
}

EVENTS = ["pending", "triggered", "armed", "disarmed", "wrong_pin", "auto_blocked"]
EVENT_NAMES = {"pending": "Eingangsverzögerung", "triggered": "Auslösung", "armed": "Scharf", "disarmed": "Unscharf",
               "wrong_pin": "Falsche PIN", "auto_blocked": "Automatik blockiert", "test": "Test"}

DEFAULT_TEXTS = {
    "title": "Alarmanlage",
    "pending": "Eingangsverzögerung: {sensor}. Alarm in {sekunden} s, wenn niemand entschärft.",
    "triggered": "ALARM! Ausgelöst durch {sensor} um {zeit}.",
    "armed": "Alarmanlage scharf ({modus}) – {person}.",
    "disarmed": "Alarmanlage unscharf – {person}.",
    "wrong_pin": "Falsche PIN eingegeben ({quelle}, {zeit}).",
    "auto_blocked": "Alarmanlage NICHT scharf ({quelle}): offen ist {sensor}.",
    "test": "Test der Alarmanlage über Kanal „{kanal}“.",
}

# Benachrichtigungskanäle. tablet/siren/rest haben eigene Logik, alle anderen = Dienst + Daten-Vorlage.
# Platzhalter in Texten und Daten: {title} {message} {sensor} {zustand} {modus} {person} {quelle} {zeit} {sekunden} {nummer} {kanal}
CHANNEL_TYPES = ["tablet", "push", "siren", "email", "sms", "phone", "imessage", "rest"]
CHANNEL_DEFAULTS = {
    "tablet": {"name": "Tablets (Vollbild)", "events": ["pending", "triggered"], "browsers": [], "sound": True},
    "push": {"name": "Push aufs Handy", "events": ["triggered", "wrong_pin"], "service": "",
             "data": {"title": "{title}", "message": "{message}",
                      "data": {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}}}}},
    "siren": {"name": "Sirenen", "events": ["triggered"], "entities": []},
    "email": {"name": "E-Mail", "events": ["triggered"], "service": "", "data": {"title": "{title}", "message": "{message}"}},
    "sms": {"name": "SMS", "events": ["triggered"], "service": "", "nummer": "",
            "data": {"message": "{message}", "target": ["{nummer}"]}},
    "phone": {"name": "Telefonanruf", "events": ["triggered"], "service": "", "nummer": "",
              "data": {"number": "{nummer}", "message": "{message}"}},
    "imessage": {"name": "iMessage", "events": ["triggered"], "service": "", "nummer": "",
                 "data": {"message": "{message}", "target": ["{nummer}"]}},
    "rest": {"name": "REST-API", "events": ["triggered", "armed", "disarmed"], "url": "", "method": "POST",
             "headers": {"Content-Type": "application/json"},
             "body": {"event": "{ereignis}", "state": "{zustand}", "mode": "{modus}", "sensor": "{sensor}",
                      "person": "{person}", "time": "{zeit}", "message": "{message}"}},
}

# Anwesenheit: diese Zustände zählen als „da“ (person / device_tracker / binary_sensor / input_boolean)
PRESENT_STATES = {"home", "on", "true", "present"}
LOCKED_STATES = {"locked"}
UNLOCKED_STATES = {"unlocked", "open"}

# Überwachung je Modus: allow_close = ein beim Scharfschalten offener Melder darf zugehen (z. B. Wind) und wird
# nach „settle“ Sekunden geschlossen wieder normal überwacht; aus = auch das Schließen eines offenen Melders löst aus.
DEFAULT_WATCH = {"allow_close": True, "settle": 15}

DEFAULT_AUTO = {
    # automatisch scharf, wenn alle gewählten Personen / BLE-Tracker weg sind; unscharf, sobald jemand da ist
    "presence": {"enabled": False, "entities": [], "mode": "armed_away", "away_delay": 120,
                 "arm": True, "disarm": True, "disarm_modes": ["armed_away"]},
    # Schloss abgeschlossen → scharf, aufgeschlossen → unscharf
    "lock": {"enabled": False, "entities": [], "mode": "armed_away", "arm": True, "disarm": True,
             "require_away": False, "disarm_modes": ["armed_away"]},
    # scharf nur, wenn diese Türen/Fenster zu sind (Automatik immer, Scharfschalten von Hand nur mit „manual“)
    "closed": {"enabled": False, "entities": [], "manual": False},
}

DEFAULT_SETTINGS = {
    "modes": DEFAULT_MODES,
    "sensors": {},                   # entity_id → {modes: [...], delayed: bool, always: bool}
    "code_arm_required": False,      # Scharfschalten ohne PIN (Tablet, „Ich gehe“)
    "service_open": "bypass",        # Dienst-Aufrufe bei offenen Sensoren: bypass (überspringen) | block
    "disarm_after_trigger": False,   # nach Ablauf der Alarmdauer: False = wieder scharf, True = unscharf
    "max_wrong": 5,                  # so viele falsche PINs in 10 min …
    "lock_s": 120,                   # … sperren die Eingabe so lange
    "texts": DEFAULT_TEXTS,
    "channels": [],
    "watch": {m: dict(DEFAULT_WATCH) for m in MODES},
    "auto": DEFAULT_AUTO,
}
