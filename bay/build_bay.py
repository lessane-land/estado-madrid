#!/usr/bin/env python3
"""
build_bay.py · the SF Bay Area version of graph-app.json, for the Bay edition of Madrid se mueve.

Reads 511's regional GTFS (every operator in one zip) and writes graph-bay.json:
  stops  rail, tram, cable car and ferry stations only (platforms folded into their parent station):
         {i, m, n, la, lo, l}
  lines  rail, tram, cable car and ferry routes with their stops in order and their path:
         {i, a, m, b, n, c, s, sh, shk?}   (same keys as graph-app.json, plus "a" agency)
  routes every bus route, for labelling live buses: {i, a, b, n, c}
Route ids are 511's own, so they match "r" (and "a") in the vivo?city=bay vehicles feed.

Usage:
  SF511_TOKEN=... python3 -I bay/build_bay.py [graph-bay.json]     downloads the regional feed
  python3 -I bay/build_bay.py out.json path/to/gtfs.zip             reads a local zip (tests)
Plain Python, standard library only. The token is never printed.
"""
import csv
import io
import json
import math
import os
import sys
import urllib.request
import zipfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from export_app_graph import encode_polyline, merge_sequences  # noqa: E402

# GTFS route_type to our mode. BART is its own mode whatever type it declares.
MODES = {0: "tram", 1: "metro", 2: "rail", 4: "ferry", 5: "cable", 7: "cable", 12: "rail"}
COLORS = {"BA": "#0099D8", "CT": "#E31837", "SF": "#BA0C2F", "SC": "#29A6E0", "SA": "#0E7C3A", "CE": "#7A2182",
          "GF": "#006F51", "SB": "#00A3E0", "AM": "#1D4F91"}


def rows(z: zipfile.ZipFile, name: str):
    if name not in z.namelist():
        return
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def mode_of(agency: str, route_type: str) -> str | None:
    try:
        t = int(route_type)
    except ValueError:
        return None
    if 100 <= t < 200: t = 2          # extended types: railway
    elif 400 <= t < 500: t = 1        # urban rail
    elif 900 <= t < 1000: t = 0       # tram
    elif 1000 <= t < 1300: t = 4      # water
    elif 700 <= t < 800: t = 3        # bus
    if agency == "BA":
        return "bart"
    return MODES.get(t)


def simplify(pts: list[list[float]], tol_m: float = 8.0) -> list[list[float]]:
    """Douglas-Peucker in metres, iterative."""
    if len(pts) < 3:
        return pts
    lat0 = math.radians(pts[0][0])
    kx, ky = 111_320.0 * math.cos(lat0), 110_540.0
    xy = [(p[1] * kx, p[0] * ky) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]; bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / L
            if d > best:
                best, bi = d, i
        if best > tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [[round(p[0], 5), round(p[1], 5)] for p, k in zip(pts, keep) if k]


def color(route: dict, agency: str) -> str:
    c = (route.get("route_color") or "").strip().lstrip("#")
    if len(c) == 6 and c.upper() not in ("FFFFFF", "000000"):
        return "#" + c.upper()
    return COLORS.get(agency, "#8A8F98")


def build(z: zipfile.ZipFile) -> dict:
    agencies = {r.get("agency_id", ""): r.get("agency_name", "") for r in rows(z, "agency.txt")}
    single_agency = next(iter(agencies)) if len(agencies) == 1 else ""
    routes, rail, buses = {}, {}, []
    for r in rows(z, "routes.txt"):
        a = r.get("agency_id") or single_agency
        m = mode_of(a, r.get("route_type", ""))
        rid = r["route_id"]
        short = (r.get("route_short_name") or "").strip()
        long_ = (r.get("route_long_name") or "").strip()
        item = {"i": rid, "a": a, "b": short or long_[:12], "n": long_ or short, "c": color(r, a)}
        routes[rid] = item
        if m is None:
            buses.append(item)
        else:
            item["m"] = m
            rail[rid] = item

    # Every trip of a rail route; the longest stop pattern per (route, shape) wins below.
    trip_route: dict[str, str] = {}
    for t in rows(z, "trips.txt"):
        rid = t["route_id"]
        if rid not in rail:
            continue
        trip_route[t["trip_id"]] = rid
    wanted = set(trip_route)
    seqs: dict[str, list[tuple[int, str]]] = {}
    for st in rows(z, "stop_times.txt"):
        tid = st["trip_id"]
        if tid in wanted:
            seqs.setdefault(tid, []).append((int(st["stop_sequence"]), st["stop_id"]))

    stops_raw = {s["stop_id"]: s for s in rows(z, "stops.txt")}

    def station(sid: str) -> str:
        s = stops_raw.get(sid)
        return s["parent_station"] if s and s.get("parent_station") and s["parent_station"] in stops_raw else sid

    # Longest pattern per (route, shape).
    patterns: dict[tuple[str, str], list[str]] = {}
    for t in rows(z, "trips.txt"):
        tid = t["trip_id"]
        if tid not in seqs:
            continue
        key = (t["route_id"], t.get("shape_id") or f"dir{t.get('direction_id', '')}")
        seq = [station(s) for _, s in sorted(seqs[tid])]
        seq = [s for i, s in enumerate(seq) if i == 0 or s != seq[i - 1]]
        if len(seq) > len(patterns.get(key, [])):
            patterns[key] = seq

    needed_shapes = {k[1] for k in patterns if not k[1].startswith("dir")}
    shapes: dict[str, list[tuple[int, list[float]]]] = {}
    for p in rows(z, "shapes.txt"):
        if p["shape_id"] in needed_shapes:
            shapes.setdefault(p["shape_id"], []).append((int(p["shape_pt_sequence"]), [float(p["shape_pt_lat"]), float(p["shape_pt_lon"])]))

    lines, stop_lines = [], {}
    for rid, item in rail.items():
        keys = sorted((k for k in patterns if k[0] == rid), key=lambda k: len(patterns[k]), reverse=True)
        if not keys:
            continue
        item["s"] = merge_sequences([patterns[k] for k in keys])
        for s in item["s"]:
            stop_lines.setdefault(s, []).append(rid)
        best = next((k for k in keys if k[1] in shapes), None)
        if best:
            path = [p for _, p in sorted(shapes[best[1]])]
            item["sh"] = encode_polyline(simplify(path))
        else:
            pts = [[float(stops_raw[s]["stop_lat"]), float(stops_raw[s]["stop_lon"])] for s in item["s"] if s in stops_raw]
            if len(pts) >= 2:
                item["sh"] = encode_polyline([[round(a, 5), round(b, 5)] for a, b in pts])
                item["shk"] = "stops"
        lines.append(item)

    stops = []
    for sid, ls in stop_lines.items():
        s = stops_raw.get(sid)
        if not s or not s.get("stop_lat"):
            continue
        modes = sorted({rail[r]["m"] for r in ls})
        stops.append({"i": sid, "m": modes[0], "n": s["stop_name"].strip(), "la": round(float(s["stop_lat"]), 5),
                      "lo": round(float(s["stop_lon"]), 5), "l": sorted(set(ls))})
    return {"built": date.today().isoformat(), "agencies": agencies, "stops": stops, "lines": lines, "routes": buses}


def main() -> int:
    dst = sys.argv[1] if len(sys.argv) > 1 else "graph-bay.json"
    if len(sys.argv) > 2:
        z = zipfile.ZipFile(sys.argv[2])
    else:
        key = (os.environ.get("SF511_TOKEN") or "").strip()
        if not key:
            print("SF511_TOKEN is not set", file=sys.stderr)
            return 1
        url = "https://api.511.org/transit/datafeeds?operator_id=RG&api_key=" + key
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                z = zipfile.ZipFile(io.BytesIO(r.read()))
        except Exception as e:  # never echo the URL, it carries the token
            print(f"511 download failed: {type(e).__name__} {getattr(e, 'code', '')}", file=sys.stderr)
            return 1
    out = build(z)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    by_mode: dict[str, int] = {}
    for l in out["lines"]:
        by_mode[l["m"]] = by_mode.get(l["m"], 0) + 1
    print(f"{dst}: {len(out['stops'])} stations, {len(out['lines'])} lines {by_mode}, {len(out['routes'])} bus routes, "
          f"{len(out['agencies'])} agencies, {os.path.getsize(dst) / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
