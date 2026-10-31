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

    lines = visible_text(raw)
    # Incident titles sit between each "<Mode>: Estado en tiempo real" header and the next header.
    headers = {
        "Cercanías: Estado en tiempo real": "cercanias",
        "Autobuses EMT: Estado en tiempo real": "emt",
        "Metro Ligero/Tranvía: Estado en tiempo real": "metro_ligero",
        "Metro: Estado en tiempo real": "metro",
        "Autobuses Interurbanos: Estado en tiempo real": "interurbanos",
    }
    current = None
    seen = set()
    for l in lines:
        if l in headers:
            current = headers[l]
            continue
        if l.startswith("Vover a") or l.startswith("Volver a"):
            break
        if current and l not in seen and len(l) < 120 and not l.startswith("Más información") and "no hay incidencias" not in l:
            if not re.match(r"^(Desvío|Suspensión|Cierre|Corte|Obras|Interrupción|Plan|Huelga|Refuerzo|Cambio|Modificación|Traslado)", l):
                continue  # body text, not a title
            seen.add(l)
            out["incidents"].append(normalise_crtm(current, l))
    return out


def normalise_crtm(mode: str, title: str) -> dict:
    lines = re.findall(r"\bL\d{1,2}[AB]?\b|\bC-?\d[ab]?\b|\bML\d\b", title)
    lines = [l.replace("C-", "C") for l in lines]
    # EMT titles often list lines in the body; the title alone carries the reason.
    severity = "rojo" if re.search(r"Suspensi|Cierre|Interrup", title) else "amarillo"
    seg = re.search(r"(?:L\d{1,2}[AB]?|C\d[ab]?)\s+([^-]+?)\s+-\s+(.+)$", title)
    return {
        "mode": mode,
        "lines": lines,
        "severity": severity,
        "text": title,
        "scope": {"from": seg.group(1).strip(), "to": seg.group(2).strip()} if seg else None,
    }


# ---------------------------------------------------------------- EMT MobilityLabs

def emt_incidents(lines: list[str]) -> dict:
    out = {"source": "openapi.emtmadrid.es", "ok": False, "incidents": []}
    email, password = os.environ.get("EMT_EMAIL"), os.environ.get("EMT_PASSWORD")
    if not (email and password):
        out["skipped"] = "set EMT_EMAIL and EMT_PASSWORD to include EMT line incidents"
        return out
    try:
        req = urllib.request.Request("https://openapi.emtmadrid.es/v1/mobilitylabs/user/login/",
                                     headers={"email": email, "password": password, "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            token = json.load(r)["data"][0]["accessToken"]
        for line in lines:
            req = urllib.request.Request(f"https://openapi.emtmadrid.es/v1/transport/busemtmad/lines/incidents/{line}/",
                                         headers={"accessToken": token, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = json.load(r)
            for item in body.get("data", []) or []:
                out["incidents"].append({"mode": "emt", "lines": [line], "severity": "amarillo",
                                         "text": json.dumps(item, ensure_ascii=False)[:300], "scope": None})
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out


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


def for_here(lat: float, lon: float, radius: float, incidents: list[dict], levels: dict, graph_path: str) -> dict:
    """Ring 1 lines coloured by the incidents that touch them, plus one-hop spread."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from around import Graph  # local module
    g = Graph(graph_path)
    near = g.around(lat, lon, radius)

    def incidents_for(line: dict) -> list[dict]:
        hits = []
        for inc in incidents:
            if inc["mode"] != line["mode"]:
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
                    lo, hi = min(i, j), max(i, j)
                    hit["touches_you"] = mine in names and lo <= names.index(mine) <= hi
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
                    zone = set(names[min(i, j):max(i, j) + 1])
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
        if line["status"] == "verde":
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
    args = ap.parse_args()

    commute = json.load(open(args.commute)) if args.commute else None
    if args.scenario:
        sc = json.load(open(args.scenario))
        renfe = {"source": "scenario", "ok": True, "incidents": []}
        crtm = {"source": "scenario", "ok": True, "updated": None, "levels": sc.get("levels", {}),
                "incidents": [{"scope": None, **i} for i in sc["incidents"]]}
        emt = {"source": "scenario", "ok": True, "incidents": []}
    else:
        renfe = renfe_alerts()
        crtm = crtm_status()
        emt = emt_incidents(commute.get("emt", []) if commute else [])

    incidents = renfe["incidents"] + crtm["incidents"] + emt["incidents"]
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
