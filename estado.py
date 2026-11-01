#!/usr/bin/env python3
"""
estado.py · one JSON for the state of Madrid's public transport, scoped to a commute.

Sources
  Renfe   GTFS-RT service alerts, https://gtfsrt.renfe.com/alerts.json   (official, every 20 s)
  CRTM    Estado del sistema de transporte, https://www.crtm.es/informacion-actualizada
          per-mode traffic light (Verde / Amarillo / Rojo) plus incident titles and bodies (HTML, scraped)
  EMT     MobilityLabs incidents per line (needs EMT_EMAIL and EMT_PASSWORD in the environment; skipped otherwise)

Usage
  python3 estado.py                       full normalised status as JSON
  python3 estado.py --commute vane.json   plus a "for you" section filtered to the lines in the file

A commute file lists the lines you touch, by mode:
  {"name": "Casa a oficina", "metro": ["L6"], "cercanias": ["C5"], "emt": ["27"], "interurbanos": []}
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request

UA = "estado-madrid-prototype/0.1 (personal project)"
TIMEOUT = 20

RAIL = {"metro", "metro_ligero", "cercanias"}
MODES = {"Metro": "metro", "ML": "metro_ligero", "EMT": "emt", "Interurbano": "interurbanos", "Cercanias": "cercanias"}
LEVEL_SCORE = {"Verde": 0, "Amarillo": 1, "Amarillo_Oscuro": 2, "Rojo": 3}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def visible_text(raw: str) -> list[str]:
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.S)
    txt = html.unescape(re.sub(r"<[^>]+>", "\n", raw))
    return [l.strip() for l in txt.split("\n") if l.strip()]


# ---------------------------------------------------------------- Dates in notice text

MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
          "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
RECOVERY = re.compile(r"recuperan|recuperado|restablec|con normalidad|normalidad en el servicio|finaliza(?:da)? la incidencia", re.I)


def expiry(text: str, since: dt.datetime | None, now: dt.datetime) -> dt.datetime | None:
    """When a notice stops mattering, read from its own words. None means open-ended.
    'del 10 al 12 de octubre' -> end of 12 Oct. 'los fines de semana del 3-4, 17-18 y 24-25 de octubre' -> end of 25 Oct.
    'hasta el 4 de noviembre' -> end of 4 Nov. 'hasta finales de año' -> 31 Dec. 'hoy ...' with no dates -> end of the day it was published."""
    low = text.lower()
    ref = since or now
    candidates: list[dt.datetime] = []
    # Every "day[s] de month" group: numbers before the month name, e.g. "3-4, 17-18 y 24-25 de octubre"
    for m in re.finditer(r"((?:\d{1,2}\s*(?:[-,]|y|al|a|el|del|de|los|las|d[ií]as?|fines? de semana)\s*)*\d{1,2})\s+de\s+(" + "|".join(MONTHS) + r")(?:\s+de\s+(\d{4}))?", low):
        days = [int(d) for d in re.findall(r"\d{1,2}", m.group(1)) if 1 <= int(d) <= 31]
        month = MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else ref.year
        if not m.group(3) and month < ref.month - 6:
            year += 1
        for d in days:
            try:
                candidates.append(dt.datetime(year, month, d, 23, 59, tzinfo=dt.timezone.utc))
            except ValueError:
                pass
    if "finales de año" in low or "final de año" in low or "fin de año" in low:
        candidates.append(dt.datetime(ref.year, 12, 31, 23, 59, tzinfo=dt.timezone.utc))
    if candidates:
        return max(candidates)
    if re.search(r"\bhoy\b", low):
        return ref.replace(hour=23, minute=59, second=0, microsecond=0)
    return None


def incident_key(inc: dict) -> str:
    return f"{inc['mode']}|{','.join(inc['lines'])}|{inc.get('title') or inc['text'][:80]}"


def carry_since(incidents: list[dict], previous: list[dict], now: dt.datetime) -> None:
    """CRTM gives no timestamps. First time we see a notice we stamp it; later runs keep the stamp."""
    seen = {incident_key(p): p.get("since") for p in previous if p.get("since")}
    for inc in incidents:
        if not inc.get("since"):
            inc["since"] = seen.get(incident_key(inc)) or now.isoformat()


def still_active(inc: dict, now: dt.datetime) -> bool:
    """Drop recovery notices and anything whose own dates are past."""
    if RECOVERY.search(inc["text"]):
        return False
    since = dt.datetime.fromisoformat(inc["since"]) if inc.get("since") else None
    until = expiry(inc["text"], since, now)
    inc["until"] = until.isoformat() if until else None
    return until is None or until >= now


# ---------------------------------------------------------------- Renfe Cercanías

def renfe_alerts() -> dict:
    """Madrid-nucleus alerts from the GTFS-RT feed. Route ids end in the line code (…C4, …C4a)."""
    out = {"source": "gtfsrt.renfe.com/alerts.json", "ok": False, "feed_time": None, "stale": None, "incidents": []}
    try:
        data = json.loads(fetch("https://gtfsrt.renfe.com/alerts.json"))
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out
    out["ok"] = True
    ts = int(data.get("header", {}).get("timestamp", 0))
    feed_time = dt.datetime.fromtimestamp(ts, dt.timezone.utc) if ts else None
    out["feed_time"] = feed_time.isoformat() if feed_time else None
    out["stale"] = bool(feed_time and dt.datetime.now(dt.timezone.utc) - feed_time > dt.timedelta(hours=6))
    for ent in data.get("entity", []):
        alert = ent.get("alert", {})
        text = " ".join(t.get("text", "") for t in alert.get("descriptionText", {}).get("translation", [])).strip()
        if not text.startswith("#Mad"):
            continue
        lines = sorted({re.sub(r"^\d+T\d{4}", "", ie.get("routeId", "")) for ie in alert.get("informedEntity", [])})
        lines = [l for l in lines if l]
        start = alert.get("activePeriod", [{}])[0].get("start")
        severity = "rojo" if re.search(r"suspendid|no prestan servicio|interrumpid", text, re.I) else "amarillo"
        out["incidents"].append({
            "source": "Renfe",
            "mode": "cercanias",
            "lines": lines,
            "severity": severity,
            "since": dt.datetime.fromtimestamp(int(start), dt.timezone.utc).isoformat() if start else None,
            "text": text,
            "scope": extract_segment(text),
        })
    return out


STATION_SEGMENT = re.compile(r"entre (?:las estaciones de )?([A-ZÁÉÍÓÚÑ][\w\s\.'-]+?)\s+y\s+([A-ZÁÉÍÓÚÑ][\w\s\.'-]+?)(?:[\.,;]|\s+(?:en|por|los|las|del|de|se|y)\b)")


def extract_segment(text: str) -> dict | None:
    """'entre Villalba de Guadarrama y Cercedilla' -> {"from": ..., "to": ...}. Best effort."""
    m = STATION_SEGMENT.search(text)
    if not m:
        m2 = re.search(r"(?:estación de|en) ([A-ZÁÉÍÓÚÑ][\w\s'-]{2,40}?)(?:[\.,;]|\s+(?:a las|hoy|se|y)\b)", text)
        return {"station": m2.group(1).strip()} if m2 else None
    return {"from": m.group(1).strip(), "to": m.group(2).strip()}


# ---------------------------------------------------------------- CRTM

def crtm_status() -> dict:
    out = {"source": "crtm.es/informacion-actualizada", "ok": False, "updated": None, "levels": {}, "incidents": []}
    try:
        raw = fetch("https://www.crtm.es/informacion-actualizada")
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out
    out["ok"] = True
    live = re.sub(r"<!--.*?-->", "", raw, flags=re.S)  # the page keeps an old block commented out
    m = re.search(r"(\d{2}/\d{2}/\d{2}) (\d{2}:\d{2}) h", live)
    if m:
        out["updated"] = dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d/%m/%y %H:%M").isoformat()
    for mode_key, mode in MODES.items():
        lm = re.search(rf"Estado_{mode_key}_([A-Za-z_]+)\.png", live)
        if lm:
            out["levels"][mode] = lm.group(1)

    # Each mode has its own page with title and body for every notice; bodies name the bus lines.
    pages = {"cercanias": "avisos-cercanias", "emt": "avisos-emt", "metro_ligero": "avisos-metro-ligero",
             "metro": "avisos-metro", "interurbanos": "avisos-interurbanos"}
    for mode, slug in pages.items():
        try:
            page = fetch(f"https://www.crtm.es/comunicacion/actualidad-del-servicio/avisos/{slug}/")
        except Exception:  # noqa: BLE001
            continue
        for title, body in crtm_notices(page):
            out["incidents"].append(normalise_crtm(mode, title, body))
    return out


TITLE = re.compile(r"^(Desvío|Suspensión|Cierre|Corte|Obras|Interrupción|Plan Especial|Huelga|Refuerzo|Cambio|Modificación|Traslado|Incidencia)\b")


def crtm_notices(page: str) -> list[tuple[str, str]]:
    """(title, body) pairs from a CRTM avisos page. Titles repeat (summary list, then detail);
    the body is the text between a title and the next title or 'Más información'."""
    lines = visible_text(page)
    # The notices start right after the "dd/mm/yy hh:mm h" stamp of "Estado del sistema de transporte".
    try:
        start = next(i for i, l in enumerate(lines) if re.match(r"\d{2}/\d{2}/\d{2} \d{2}:\d{2} h", l)) + 1
    except StopIteration:
        return []
    notices: dict[str, list[str]] = {}
    order: list[str] = []
    current = None
    for l in lines[start:]:
        if l.startswith("Vover a") or l.startswith("Volver a") or l == "© CRTM":
            break
        if TITLE.match(l) and len(l) < 120:
            current = l
            if l not in notices:
                notices[l] = []
                order.append(l)
            continue
        if l.startswith("Avisos ") or l.startswith("Más información") or "no hay incidencias" in l:
            continue  # section heading between the title and its body, or the read-more link
        if current:
            notices[current].append(l)
    return [(title, " ".join(notices[title]).strip()) for title in order]


def normalise_crtm(mode: str, title: str, body: str = "") -> dict:
    lines = [l.replace("C-", "C") for l in re.findall(r"\bL\d{1,2}[AB]?\b|\bC-?\d{1,2}[ab]?\b|\bML\d\b", title)]
    if mode in ("emt", "interurbanos", "urbanos") and not lines:
        lines = bus_lines_in(body, interurban=(mode == "interurbanos"))
    if mode == "metro" and not lines:
        lines = [f"L{n}" for n in re.findall(r"l[ií]nea\s+(\d{1,2})", body, re.I)]
    severity = "rojo" if re.search(r"Suspensi|Cierre|Interrup|Huelga", title) else "amarillo"
    seg = re.search(r"(?:L\d{1,2}[AB]?|C\d{1,2}[ab]?)\s+([^-]+?)\s+-\s+(.+)$", title)
    scope = {"from": seg.group(1).strip(), "to": seg.group(2).strip()} if seg else None
    if scope is None and body and mode in ("metro", "metro_ligero", "cercanias"):
        scope = extract_segment(body)
    return {
        "source": "CRTM",
        "mode": mode,
        "lines": lines,
        "severity": severity,
        "since": None,
        "title": title,
        "text": title if not body else f"{title}. {body[:400]}",
        "scope": scope,
    }


def bus_lines_in(body: str, interurban: bool) -> list[str]:
    """'afectando a las líneas de EMT 36, 39 y N19 e interurbanas 521, 523' -> the right group."""
    found: list[str] = []
    key = r"interurban\w*" if interurban else r"(?:EMT|l[ií]neas?)"
    for m in re.finditer(key + r"[^0-9A-Z]{0,20}((?:\b(?:N?\d{1,3}[A-Z]?|[A-Z]{1,2}\d{1,3}[A-Z]?)\b[\s,]*(?:y|e)?\s*)+)", body):
        found += re.findall(r"\b(?:N?\d{1,3}[A-Z]?|[A-Z]{1,2}\d{1,3}[A-Z]?)\b", m.group(1))
    if not found and not interurban:
        found = re.findall(r"l[ií]neas?\s+((?:\b(?:N?\d{1,3}[A-Z]?|[A-Z]\d{1,2})\b[\s,]*(?:y|e)?\s*)+)", body)
        found = re.findall(r"\b(?:N?\d{1,3}[A-Z]?|[A-Z]\d{1,2})\b", " ".join(found))
    # Drop stray numbers that are clearly not lines (stop numbers, distances, years).
    out = []
    for f in found:
        if f.isdigit() and (int(f) > 999 or int(f) == 0):
            continue
        if f not in out:
            out.append(f)
    return out


# ---------------------------------------------------------------- EMT MobilityLabs

EMT_REFRESH_MINUTES = 65        # a line's incidents are kept this long between polls of that line
EMT_SLOTS = 12                  # lines are polled in 12 rotating groups: every line about once an hour


def emt_line_labels(graph_path: str) -> list[str]:
    """Every EMT line label (27, C1, N15...) from the graph; the whole network, not a commute."""
    try:
        g = json.load(open(graph_path, encoding="utf-8"))
        return sorted(l["label"] for l in g["lines"] if l["mode"] == "emt")
    except Exception:  # noqa: BLE001
        return []


def emt_incidents(lines: list[str], previous: list[dict], now: dt.datetime) -> dict:
    """Per-line incidents from MobilityLabs, polled in rotation. Lines not polled this run keep what the
    previous row had for them, as long as it is younger than EMT_REFRESH_MINUTES."""
    out = {"source": "openapi.emtmadrid.es", "ok": False, "polled": 0, "carried": 0, "incidents": []}
    # Registered app (X-ClientId + passKey) wins when present; portal user (email + password) otherwise.
    client_id, pass_key = os.environ.get("EMT_CLIENT_ID"), os.environ.get("EMT_PASS_KEY")
    email, password = os.environ.get("EMT_EMAIL"), os.environ.get("EMT_PASSWORD")
    if client_id and pass_key:
        login_headers = {"X-ClientId": client_id, "passKey": pass_key}
    elif email and password:
        login_headers = {"email": email, "password": password}
    else:
        out["skipped"] = "set EMT_CLIENT_ID and EMT_PASS_KEY (or EMT_EMAIL and EMT_PASSWORD) to include EMT line incidents"
        return out
    if not lines:
        out["skipped"] = "no EMT line list (graph.json missing)"
        return out

    slot = (now.minute // 5) % EMT_SLOTS
    todo = [l for i, l in enumerate(lines) if i % EMT_SLOTS == slot]
    by_guid: dict[str, dict] = {}
    try:
        req = urllib.request.Request("https://openapi.emtmadrid.es/v1/mobilitylabs/user/login/",
                                     headers={**login_headers, "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            token = json.load(r)["data"][0]["accessToken"]
        logged_shape = False
        for line in todo:
            req = urllib.request.Request(f"https://openapi.emtmadrid.es/v1/transport/busemtmad/lines/incidents/{line}/",
                                         headers={"accessToken": token, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = json.load(r)
            out["polled"] += 1
            if str(body.get("code")) not in ("00", "01"):
                continue
            items = emt_flatten(body.get("data") or [])
            if not logged_shape and body.get("data"):
                # Diagnostic, key names only, never values: the answer shape is not documented anywhere we found.
                out["shape"] = emt_shape(body.get("data"))
                logged_shape = True
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("item") is not None or item.get("items") is not None:
                    continue   # a channel wrapper, not a notice
                notice = emt_notice(item, line, now)
                if notice is None:
                    continue
                guid = notice.pop("_guid")
                if guid in by_guid:
                    for l in notice["lines"]:
                        if l not in by_guid[guid]["lines"]:
                            by_guid[guid]["lines"].append(l)
                else:
                    by_guid[guid] = notice
        out["incidents"] = list(by_guid.values())
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        # Public repo, public logs: never echo the exception verbatim, it may contain request headers.
        out["error"] = f"{type(e).__name__} while talking to EMT"
        todo = []          # nothing was refreshed; carry everything that is still young

    # Carry the previous row's EMT notices that this run did not get a fresh answer about: a notice is
    # refreshed when any of its lines was polled (it would have come back if still published).
    refreshed = set(todo) if out["ok"] else set()
    have = {i.get("guid") for i in out["incidents"]}
    for inc in previous:
        if inc.get("source") != "EMT" or not inc.get("checked") or not inc.get("guid") or inc.get("guid") in have:
            continue
        if EMT_CHANNEL.search(inc.get("title") or "") or EMT_NOT_DISRUPTION.search(inc.get("title") or ""):
            continue
        if set(inc.get("lines") or []) & refreshed:
            continue
        checked = dt.datetime.fromisoformat(inc["checked"])
        if now - checked <= dt.timedelta(minutes=EMT_REFRESH_MINUTES):
            out["incidents"].append(inc)
            have.add(inc["guid"])
            out["carried"] += 1
    return out


# Not disruption: dropped before they reach the row (and so before line_episodes).
EMT_NOT_DISRUPTION = re.compile(
    r"^(refuerzo|cambio( de)? horario|horario|servicio especial|nueva l[ií]nea|ampliaci[oó]n|informaci[oó]n)", re.I)
EMT_CHANNEL = re.compile(r"canal de noticias|informaciones de inter[eé]s para el cliente", re.I)


def emt_notice(item: dict, polled_line: str, now: dt.datetime) -> dict | None:
    """One MobilityLabs RSS item -> one incident. Lines come from the text ('las líneas 001, 002, 1, 2 ... C1 y C2'),
    the polled line always included. Window from rssAfectaDesde / rssAfectaHasta. Effect from GoogleTransitEffect."""
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
    desc = re.sub(r"<[^>]+>", " ", str(item.get("description") or ""))
    desc = re.sub(r"\s+", " ", html.unescape(desc)).strip()
    if not title or EMT_CHANNEL.search(title) or EMT_CHANNEL.search(desc[:80]):
        return None
    if EMT_NOT_DISRUPTION.search(title):
        return None
    # EMT's own effect codes: "04 - DESVÍO SOBREVENIDO", "05 - DESVÍO PROGRAMADO", "07 - MODIFICACIÓN DEL SERVICIO".
    effect = re.sub(r"\s+", " ", str(item.get("GoogleTransitEffect") or "")).strip()
    if re.search(r"SIN SERVICIO|SUSPENSI|INTERRUP", effect, re.I) or re.search(r"suspend|interrump|sin servicio", desc, re.I):
        severity = "rojo"
    else:
        severity = "amarillo"
    lines = bus_lines_in(desc, interurban=False)
    if polled_line not in lines:
        lines.insert(0, polled_line)
    since = _emt_date(item.get("rssAfectaDesde")) or _emt_date(item.get("pubDate")) or now.isoformat()
    until = _emt_date(item.get("rssAfectaHasta"))
    return {
        "_guid": str(item.get("guid") or item.get("link") or title),
        "guid": str(item.get("guid") or item.get("link") or title),
        "source": "EMT", "mode": "emt", "lines": lines, "severity": severity,
        "since": since, "until": until,
        "title": title[:160],
        "text": f"{title}. {desc}"[:600] if desc and desc != title else title,
        "scope": None, "effect": effect or None, "checked": now.isoformat(),
    }


def emt_flatten(data) -> list[dict]:
    """The incidents answer is RSS-like: a channel dict with the notices under 'item' (or 'items').
    Return the notices themselves, whatever the nesting; a bare list of notices passes through."""
    found: list[dict] = []
    def walk(node, depth=0):
        if depth > 4:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
        elif isinstance(node, dict):
            nested = node.get("item") if node.get("item") is not None else node.get("items")
            if nested is not None:
                walk(nested, depth + 1)
            elif any(k in node for k in ("title", "description", "text", "name")):
                found.append(node)
            else:
                for v in node.values():
                    if isinstance(v, (list, dict)):
                        walk(v, depth + 1)
    walk(data)
    return found


def emt_shape(data, depth=0) -> object:
    """Key names of an answer, recursively, with no values. For the sources block."""
    if depth > 3:
        return "..."
    if isinstance(data, list):
        return [emt_shape(data[0], depth + 1)] if data else []
    if isinstance(data, dict):
        return {k: emt_shape(v, depth + 1) for k, v in list(data.items())[:12]}
    return type(data).__name__


def _emt_date(value) -> str | None:
    """EMT dates come in several spellings; keep ISO when we can, else None."""
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:19], fmt).replace(tzinfo=dt.timezone(dt.timedelta(hours=1))).astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            continue
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return dt.datetime.strptime(s, fmt).astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- Commute matching

def for_commute(commute: dict, incidents: list[dict], levels: dict) -> dict:
    """Only what touches your lines, plus the cross-mode forecast."""
    mine = {m: {l.upper().replace("-", "") for l in commute.get(m, [])} for m in ("metro", "cercanias", "emt", "interurbanos")}
    hits = []
    for inc in incidents:
        inc_lines = {l.upper().replace("-", "") for l in inc["lines"]}
        if inc_lines & mine.get(inc["mode"], set()):
            hits.append(inc)
    worst = max((["verde", "amarillo", "rojo"].index(h["severity"]) for h in hits), default=0)
    verdict = ["verde", "amarillo", "rojo"][worst]

    # Editorial rules. Short, honest, labelled as forecast.
    forecasts = []
    cerc_level = LEVEL_SCORE.get(levels.get("cercanias", "Verde"), 0)
    metro_level = LEVEL_SCORE.get(levels.get("metro", "Verde"), 0)
    if cerc_level >= 2 and mine["metro"]:
        forecasts.append("Cercanías is badly hit today. Expect your Metro line to be crowded, especially at Atocha, Nuevos Ministerios, Sol and Chamartín. Leave earlier.")
    if metro_level >= 2 and (mine["emt"] or mine["cercanias"]):
        forecasts.append("Metro is badly hit today. Buses along the same corridor will fill up; Cercanías is the better bet if it covers your route.")
    if cerc_level >= 3 and not mine["cercanias"]:
        forecasts.append("You do not take Cercanías, but everyone who does is on the Metro and the buses today.")
    return {"name": commute.get("name"), "verdict": verdict, "incidents": hits, "forecast": forecasts}


# ---------------------------------------------------------------- Around a point (needs graph.json)

def normalise_label(mode: str, label: str) -> str:
    label = label.upper().replace("-", "")
    if mode == "metro" and not label.startswith("L"):
        label = "L" + label
    return label


def fold(name: str) -> str:
    """Compare station names loosely: accents, case, 'Madrid-' prefixes, punctuation."""
    import unicodedata
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"^madrid[\s-]*", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def segment_indices(names: list[str], i: int, j: int, loop: bool) -> list[int]:
    """Indices of the stretch between two stations. On a loop, the shorter way round."""
    if not loop or i == j:
        lo, hi = min(i, j), max(i, j)
        return list(range(lo, hi + 1))
    n = len(names)
    forward = [(i + k) % n for k in range((j - i) % n + 1)]
    backward = [(j + k) % n for k in range((i - j) % n + 1)]
    return forward if len(forward) <= len(backward) else backward


def for_here(lat: float, lon: float, radius: float, incidents: list[dict], levels: dict, graph_path: str) -> dict:
    """Ring 1 lines coloured by the incidents that touch them, plus one-hop spread."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from around import Graph  # local module
    g = Graph(graph_path)
    near = g.around(lat, lon, radius)

    def incidents_for(line: dict) -> list[dict]:
        hits = []
        for inc in incidents:
            if inc["mode"] != line["mode"] or inc.get("upcoming"):
                continue
            labels = {normalise_label(inc["mode"], l) for l in inc["lines"]}
            if normalise_label(line["mode"], line["label"]) not in labels:
                continue
            hit = dict(inc)
            # Segment check: does the incident name stations on this line, and is my stop inside?
            scope = inc.get("scope") or {}
            names = [fold(n) for n in g.line_stop_names(line["line"])]
            mine = fold(line["stop"])
            if scope.get("from") and scope.get("to") and names:
                try:
                    i, j = names.index(fold(scope["from"])), names.index(fold(scope["to"]))
                    stretch = segment_indices(names, i, j, g.is_loop(line["line"]))
                    hit["touches_you"] = mine in names and names.index(mine) in stretch
                    hit["segment_known"] = True
                except ValueError:
                    hit["touches_you"] = None
                    hit["segment_known"] = False
            else:
                hit["touches_you"] = None  # whole line or unknown scope: assume it matters
                hit["segment_known"] = False
            hits.append(hit)
        return hits

    sev_rank = {"verde": 0, "amarillo": 1, "rojo": 2}
    ring1 = []
    # red line -> set of folded station names where the trouble is (whole line when unknown)
    red_zone: dict[str, set[str]] = {}
    for line in near["ring1"]:
        hits = incidents_for(line)
        worst = "verde"
        for h in hits:
            effective = h["severity"] if h["touches_you"] is not False else "verde"
            if sev_rank[effective] > sev_rank[worst]:
                worst = effective
            if h["severity"] == "rojo" and h["touches_you"] is not False:
                names = [fold(n) for n in g.line_stop_names(line["line"])]
                scope = h.get("scope") or {}
                zone = set(names)
                if h.get("segment_known"):
                    i, j = names.index(fold(scope["from"])), names.index(fold(scope["to"]))
                    zone = {names[k] for k in segment_indices(names, i, j, g.is_loop(line["line"]))}
                red_zone.setdefault(line["line"], set()).update(zone)
        ring1.append({**line, "status": worst, "incidents": hits})

    def spills(from_line: str, at: str) -> bool:
        return fold(at) in red_zone.get(from_line, set())

    # One hop: a red line spills amber onto the lines it meets, only at interchanges inside the trouble.
    spill = []
    for r in near["ring3"]:
        if r["via"] in red_zone and spills(r["via"], r["at"]):
            spill.append({"line": r["label"], "mode": r["mode"], "status": "amarillo",
                          "why": f"{r['via_label']} is cut at {r['at']}, where they meet"})
    for line in ring1:
        if line["status"] == "verde" and line["mode"] in RAIL:
            for n in g.neighbours.get(line["line"], []):
                if n["line"] in red_zone and spills(n["line"], n["at"]):
                    line["status"] = "amarillo"
                    line["spill_from"] = g.lines[n["line"]]["label"]
                    break

    # Mode-level forecast from CRTM's traffic light, only for modes you actually have around.
    modes_here = {l["mode"] for l in ring1}
    forecast = []
    if LEVEL_SCORE.get(levels.get("cercanias", "Verde"), 0) >= 2 and "metro" in modes_here:
        forecast.append("Cercanías is badly hit. Your Metro lines will be crowded, especially at interchanges.")
    if LEVEL_SCORE.get(levels.get("metro", "Verde"), 0) >= 2 and modes_here & {"emt", "interurbanos", "urbanos"}:
        forecast.append("Metro is badly hit. Buses on the same corridors will fill up.")
    return {"query": near["query"], "stops_on_foot": len(near["stops"]), "lines": ring1,
            "spill": spill[:30], "forecast": forecast}


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commute", help="JSON file with the lines you take")
    ap.add_argument("--at", help="lat,lon: status for the lines around this point (needs graph.json)")
    ap.add_argument("--radius", type=float, default=600)
    ap.add_argument("--graph", default="graph.json")
    ap.add_argument("--scenario", help="JSON file with invented incidents and levels, replaces the live sources")
    ap.add_argument("--previous", help="JSON file with the previous run's incidents, to keep 'since' stable")
    args = ap.parse_args()

    commute = json.load(open(args.commute)) if args.commute else None
    now = dt.datetime.now(dt.timezone.utc)
    previous_row = json.load(open(args.previous)) if args.previous and os.path.exists(args.previous) else {}
    if isinstance(previous_row, list):
        previous_row = {"incidents": previous_row}
    previous = previous_row.get("incidents", [])
    if args.scenario:
        sc = json.load(open(args.scenario))
        renfe = {"source": "scenario", "ok": True, "incidents": []}
        crtm = {"source": "scenario", "ok": True, "updated": None, "levels": sc.get("levels", {}),
                "incidents": [{"scope": None, **i} for i in sc["incidents"]]}
        emt = {"source": "scenario", "ok": True, "incidents": []}
    else:
        renfe = renfe_alerts()
        crtm = crtm_status()
        if not crtm["levels"] and previous_row.get("levels"):
            # CRTM did not answer: keep the last known lights rather than publishing an empty set.
            crtm["levels"] = previous_row["levels"]
            crtm["carried"] = True
        emt = emt_incidents(emt_line_labels(args.graph), previous, now)

    incidents = renfe["incidents"] + crtm["incidents"] + emt["incidents"]
    if not args.scenario:
        carry_since(incidents, previous, now)
        incidents = [i for i in incidents if still_active(i, now)]
        # EMT announces detours days ahead. Keep them (the evening-before warning is the point of the app),
        # but mark them so nothing colours a line or opens an episode before they start.
        for i in incidents:
            since = dt.datetime.fromisoformat(i["since"]) if i.get("since") else None
            if since and since > now:
                i["upcoming"] = True
    status = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "levels": crtm["levels"],
        "sources": {
            "renfe": {k: v for k, v in renfe.items() if k != "incidents"},
            "crtm": {k: v for k, v in crtm.items() if k != "incidents"},
            "emt": {k: v for k, v in emt.items() if k != "incidents"},
        },
        "incidents": incidents,
    }
    if commute:
        status["for_you"] = for_commute(commute, incidents, crtm["levels"])
    if args.at:
        lat, lon = (float(x) for x in args.at.split(","))
        status["for_here"] = for_here(lat, lon, args.radius, incidents, crtm["levels"], args.graph)
    json.dump(status, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
