#!/usr/bin/env python3
"""
Avisos de vuelos de Ryanair en Menorca (LEMH) para Discord.

- Cada mañana publica el tablero del día (llegadas y salidas de Ryanair).
- Avisa cuando un vuelo despega, aterriza, se retrasa, se cancela o se desvía.

Pensado para ejecutarse con cron cada 5 minutos. Solo llama a la API cuando
hace falta (tablero del día + alrededor de la hora de cada vuelo), para gastar
pocas unidades del plan de AeroDataBox.

Configuración por variables de entorno (o fichero .env junto al script):
  DISCORD_WEBHOOK_URL   URL del webhook del canal #vuelos-ryanair (obligatoria)
  AERODATABOX_KEY       Clave de RapidAPI para AeroDataBox (obligatoria)
  AIRPORT_ICAO          Aeropuerto (por defecto LEMH = Menorca)
  AIRLINE_IATA          Aerolínea (por defecto FR = Ryanair)
  POLL_MINUTES          Mínimo de minutos entre consultas en directo (por defecto 15)
  BOARD_HOUR            Hora local a partir de la que se publica el tablero (por defecto 6)
  DELAY_MINUTES         Retraso mínimo para avisar (por defecto 20)
  STATE_FILE            Fichero de estado (por defecto state.json junto al script)
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Madrid")


# ---------------------------------------------------------------- config ----
def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file(HERE / ".env")

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
API_KEY = os.environ.get("AERODATABOX_KEY", "")
ICAO = os.environ.get("AIRPORT_ICAO", "LEMH").upper()
AIRLINE = os.environ.get("AIRLINE_IATA", "FR").upper()
POLL_MIN = int(os.environ.get("POLL_MINUTES", "15"))
BOARD_HOUR = int(os.environ.get("BOARD_HOUR", "6"))
DELAY_MIN = int(os.environ.get("DELAY_MINUTES", "20"))
STATE_FILE = Path(os.environ.get("STATE_FILE", HERE / "state.json"))
DRY_RUN = "--dry-run" in sys.argv  # imprime en vez de publicar

API_HOST = "aerodatabox.p.rapidapi.com"

# Ventana "activa" de un vuelo: desde X min antes de su hora hasta Y min después
ACTIVE_BEFORE = timedelta(minutes=10)
ACTIVE_AFTER = timedelta(minutes=120)
GIVE_UP_AFTER = timedelta(hours=4)

COLOR = {"board": 0x073590, "dep": 0x2B7BB9, "arr": 0x1F9D55,
         "delay": 0xF1C933, "cancel": 0xD83C3E, "divert": 0xE67E22}


# ------------------------------------------------------------- utilities ----
def parse_time(block):
    """{'local': '2026-10-16 07:05+02:00', 'utc': '...'} -> datetime local."""
    if not block:
        return None
    raw = block.get("local") or block.get("utc")
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).astimezone(TZ)
    except ValueError:
        return None


def hhmm(dt):
    return dt.strftime("%H:%M") if dt else "--:--"


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ------------------------------------------------------------------- API ----
def fetch_window(start: datetime, end: datetime) -> dict:
    """Llegadas y salidas del aeropuerto entre start y end (máx. 12 h)."""
    fmt = "%Y-%m-%dT%H:%M"
    path = f"/flights/airports/icao/{ICAO}/{start.strftime(fmt)}/{end.strftime(fmt)}"
    params = urllib.parse.urlencode({
        "withLeg": "true", "direction": "Both", "withCancelled": "true",
        "withCodeshared": "false", "withCargo": "false",
        "withPrivate": "false", "withLocation": "false",
    })
    req = urllib.request.Request(
        f"https://{API_HOST}{path}?{params}",
        headers={"X-RapidAPI-Key": API_KEY, "X-RapidAPI-Host": API_HOST},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status == 204:
            return {}
        body = r.read()
    return json.loads(body) if body else {}


def normalise(item: dict, kind: str):
    """Convierte un vuelo de la API en un dict simple. kind = 'dep' | 'arr'."""
    airline = (item.get("airline") or {}).get("iata", "")
    number = (item.get("number") or "").replace(" ", "")
    if airline != AIRLINE and not number.startswith(AIRLINE):
        return None

    dep, arr = item.get("departure") or {}, item.get("arrival") or {}
    mov = item.get("movement") or {}
    # Lado local (Menorca) y lado remoto (origen/destino)
    local, remote = (dep, arr) if kind == "dep" else (arr, dep)
    if not local and mov:  # formato sin withLeg
        local, remote = mov, mov

    sched = parse_time(local.get("scheduledTime"))
    if not sched:
        return None
    revised = parse_time(local.get("revisedTime"))
    runway = parse_time(local.get("runwayTime"))
    ap = remote.get("airport") or {}
    ac = item.get("aircraft") or {}
    return {
        "key": f"{kind}-{number}-{sched.strftime('%Y%m%d%H%M')}",
        "kind": kind,
        "number": number,
        "place": ap.get("municipalityName") or ap.get("name") or ap.get("iata") or "?",
        "place_iata": ap.get("iata") or ap.get("icao") or "",
        "sched": sched.isoformat(),
        "revised": revised.isoformat() if revised else None,
        "runway": runway.isoformat() if runway else None,
        "status": item.get("status") or "Unknown",
        "model": ac.get("model") or "",
        "reg": ac.get("reg") or "",
        "gate": local.get("gate") or "",
    }


def collect(data: dict):
    out = []
    for kind, field in (("dep", "departures"), ("arr", "arrivals")):
        for item in data.get(field) or []:
            f = normalise(item, kind)
            if f:
                out.append(f)
    return out


# --------------------------------------------------------------- Discord ----
def post(embed: dict):
    payload = {"username": "Torre de Menorca", "embeds": [embed],
               "allowed_mentions": {"parse": []}}
    if DRY_RUN or not WEBHOOK:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "vuelos-ryanair (discord webhook)"},
    )
    urllib.request.urlopen(req, timeout=15).read()


def route(f):
    place = f"{f['place']} ({f['place_iata']})" if f["place_iata"] else f["place"]
    return f"Menorca → {place}" if f["kind"] == "dep" else f"{place} → Menorca"


def aircraft(f):
    bits = [b for b in (f["model"], f["reg"]) if b]
    return " · ".join(bits)


def board_embed(flights, day):
    def lines(kind):
        rows = sorted((f for f in flights if f["kind"] == kind), key=lambda f: f["sched"])
        if not rows:
            return "Ninguna"
        out = []
        for f in rows:
            t = datetime.fromisoformat(f["sched"])
            cancelled = " ❌ cancelado" if f["status"].startswith("Cancel") else ""
            out.append(f"`{hhmm(t)}` **{f['number']}** {f['place']}{cancelled}")
        return "\n".join(out)[:1024]

    return {
        "title": f"📋 Ryanair en Menorca · {day.strftime('%d/%m/%Y')}",
        "color": COLOR["board"],
        "fields": [{"name": "🛬 Llegadas", "value": lines("arr"), "inline": True},
                   {"name": "🛫 Salidas", "value": lines("dep"), "inline": True}],
        "footer": {"text": "Horas locales programadas · datos públicos AeroDataBox"},
    }


def event_embed(f, event):
    sched = datetime.fromisoformat(f["sched"])
    revised = datetime.fromisoformat(f["revised"]) if f["revised"] else None
    runway = datetime.fromisoformat(f["runway"]) if f["runway"] else None
    if event == "dep":
        title, color = f"🛫 {f['number']} ha despegado", COLOR["dep"]
        desc = f"{route(f)}\nProgramado `{hhmm(sched)}` · Despegue `{hhmm(runway or revised)}`"
    elif event == "arr":
        title, color = f"🛬 {f['number']} ha aterrizado", COLOR["arr"]
        desc = f"{route(f)}\nProgramado `{hhmm(sched)}` · Aterrizaje `{hhmm(runway or revised)}`"
    elif event == "delay":
        mins = int((revised - sched).total_seconds() // 60)
        title, color = f"⏱️ {f['number']} va con retraso (+{mins} min)", COLOR["delay"]
        desc = f"{route(f)}\nProgramado `{hhmm(sched)}` · Estimado `{hhmm(revised)}`"
    elif event == "cancel":
        title, color = f"❌ {f['number']} cancelado", COLOR["cancel"]
        desc = f"{route(f)}\nProgramado `{hhmm(sched)}`"
    else:  # divert
        title, color = f"⚠️ {f['number']} desviado", COLOR["divert"]
        desc = f"{route(f)}\nProgramado `{hhmm(sched)}`"
    if aircraft(f):
        desc += f"\n✈️ {aircraft(f)}"
    return {"title": title, "description": desc, "color": color}


# ----------------------------------------------------------------- logic ----
DONE = {"dep", "arr", "cancel", "divert"}


def detect_events(f):
    """Eventos que corresponden a este vuelo según su estado actual."""
    st = f["status"]
    ev = []
    if st.startswith("Cancel"):
        ev.append("cancel")
    elif st == "Diverted":
        ev.append("divert")
    elif f["kind"] == "dep" and (st in ("Departed", "EnRoute") or f["runway"]):
        ev.append("dep")
    elif f["kind"] == "arr" and (st in ("Arrived", "Landed") or f["runway"]):
        ev.append("arr")
    elif f["revised"]:
        late = datetime.fromisoformat(f["revised"]) - datetime.fromisoformat(f["sched"])
        if late >= timedelta(minutes=DELAY_MIN):
            ev.append("delay")
    return ev


def merge(state, flights):
    store = state.setdefault("flights", {})
    for f in flights:
        old = store.get(f["key"], {})
        f["posted"] = old.get("posted", [])
        store[f["key"]] = f


def ref_time(f):
    return datetime.fromisoformat(f["revised"] or f["sched"])


def resolved(f):
    return any(e in DONE or e == "expired" for e in f.get("posted", []))


def run():
    if not API_KEY:
        sys.exit("Falta AERODATABOX_KEY")
    if not WEBHOOK and not DRY_RUN:
        sys.exit("Falta DISCORD_WEBHOOK_URL")

    now = datetime.now(TZ)
    today = now.date().isoformat()
    state = load_state()
    calls = 0

    # Nuevo día: limpiar vuelos viejos
    if state.get("date") != today:
        cutoff = now - timedelta(hours=12)
        state["flights"] = {k: v for k, v in state.get("flights", {}).items()
                            if ref_time(v) > cutoff}
        state["date"] = today
        state["board_posted"] = False

    # 1) Tablero del día (2 llamadas: 00-12 y 12-24)
    if not state.get("board_posted") and now.hour >= BOARD_HOUR:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        mid = start + timedelta(hours=12)
        flights = collect(fetch_window(start, mid - timedelta(minutes=1)))
        flights += collect(fetch_window(mid, mid + timedelta(hours=12) - timedelta(minutes=1)))
        calls += 2
        merge(state, flights)
        post(board_embed(flights, now))
        state["board_posted"] = True
        state["last_call"] = now.isoformat()

    # 2) Consultas en directo solo si hay algún vuelo "activo"
    active = [f for f in state.get("flights", {}).values()
              if not resolved(f)
              and datetime.fromisoformat(f["sched"]) - ACTIVE_BEFORE <= now
              and now <= ref_time(f) + ACTIVE_AFTER]
    last = state.get("last_call")
    due = not last or now - datetime.fromisoformat(last) >= timedelta(minutes=POLL_MIN)
    if active and due and calls == 0:
        flights = collect(fetch_window(now - timedelta(hours=4), now + timedelta(hours=2)))
        calls += 1
        merge(state, flights)
        state["last_call"] = now.isoformat()

    # 3) Publicar eventos nuevos
    for f in sorted(state.get("flights", {}).values(), key=ref_time):
        if resolved(f):
            continue
        for ev in detect_events(f):
            if ev not in f["posted"]:
                post(event_embed(f, ev))
                f["posted"].append(ev)
        # Dejar de seguir vuelos que ya no se van a actualizar
        if not resolved(f) and now > ref_time(f) + GIVE_UP_AFTER:
            f["posted"].append("expired")

    state["api_calls_total"] = state.get("api_calls_total", 0) + calls
    save_state(state)


if __name__ == "__main__":
    try:
        run()
    except urllib.error.HTTPError as e:
        sys.exit(f"Error HTTP {e.code}: {e.read()[:300]!r}")
