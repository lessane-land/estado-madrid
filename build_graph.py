#!/usr/bin/env python3
"""
build_graph.py · CRTM GTFS in, one compact graph of Madrid's public transport out.

Runs once a month, anywhere with Python 3.10+ (no dependencies). Downloads the six CRTM feeds
(Metro, Metro Ligero, Cercanías, EMT, interurbanos, urbanos) and writes graph.json with:

  stops     every boarding point: id, mode, name, lat, lon, lines served
  lines     every line: id, mode, label (what people call it: L10, C5, 27), name, colour,
            and its stop sequences, one per route direction
  transfers line-to-line edges: where two lines meet (same stop, or stops within 150 m)

Usage
  python3 build_graph.py                 download feeds into ./gtfs and build ./graph.json
  python3 build_graph.py --no-download   reuse ./gtfs/*.zip
"""
from __future__ import annotations

import argparse
import csv
import re
import io
import json
import math
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

FEEDS = {
    # mode: ArcGIS item id on datos.crtm.es
    "metro": "5c7f2951962540d69ffe8f640d94c246",
    "metro_ligero": "aaed26cc0ff64b0c947ac0bc3e033196",
    "cercanias": "1a25440bf66f499bae2657ec7fb40144",
    "emt": "868df0e58fca47e79b942902dffd7da0",
    "interurbanos": "885399f83408473c8d815e40c5e702b7",
    "urbanos": "357e63c2904f43aeb5d8a267a64346d8",
}
DOWNLOAD = "https://www.arcgis.com/sharing/rest/content/items/{id}/data"
TRANSFER_METERS = 150
UA = "estado-madrid-graph/0.1 (personal project)"


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def download(mode: str, dest: Path) -> None:
    url = DOWNLOAD.format(id=FEEDS[mode])
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)


def rows(z: zipfile.ZipFile, name: str):
    """Stream a GTFS table as dicts. Strips the BOM some CRTM files carry."""
    if name not in z.namelist():
        return
    with z.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        for row in csv.DictReader(text):
            yield {k.strip(): (v or "").strip() for k, v in row.items() if k}


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def simplify(points: list[list[float]], tolerance_m: float = 8.0) -> list[list[float]]:
    """Douglas-Peucker on [lat, lon] points, tolerance in metres (local flat projection)."""
    if len(points) <= 2:
        return points
    lat0 = math.radians(points[0][0])
    kx = 111_320.0 * math.cos(lat0)
    ky = 110_540.0
    xy = [(p[1] * kx, p[0] * ky) for p in points]

    def dist(p, a, b):
        ax, ay = a; bx, by = b; px, py = p
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + u * dx), py - (ay + u * dy))

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        best, bi = 0.0, -1
        for k in range(i + 1, j):
            d = dist(xy[k], xy[i], xy[j])
            if d > best:
                best, bi = d, k
        if best > tolerance_m:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [p for p, k in zip(points, keep) if k]


def cut_at_first_repeat(stops: list[str]) -> list[str]:
    """Some feeds store a trip as the same route run many times in a row. Keep one run: stop at the
    first stop seen twice, including it only when it closes a loop back to the first stop."""
    seen = set()
    out = []
    for s in stops:
        if s in seen:
            if s == stops[0]:
                out.append(s)
            break
        seen.add(s)
        out.append(s)
    return out


def import_feed(mode: str, path: Path, graph: dict) -> None:
    t0 = time.time()
    z = zipfile.ZipFile(path)

    # Lines: group GTFS routes by what people call them (route_short_name).
    route_to_line: dict[str, str] = {}
    for r in rows(z, "routes.txt"):
        label = r.get("route_short_name") or r.get("route_id")
        # Town buses reuse "1", "2" in every municipality: key them by route id (…__013_ is the town).
        town = re.search(r"_(\d{3})_?$", r["route_id"]) if mode == "urbanos" else None
        line_id = f"{mode}:{label}:{town.group(1)}" if town else f"{mode}:{label}"
        route_to_line[r["route_id"]] = line_id
        line = graph["lines"].setdefault(line_id, {
            "id": line_id, "mode": mode, "label": label,
            "name": r.get("route_long_name", "").replace("  ", " ").strip(),
            "color": r.get("route_color") or None,
            "sequences": {},
        })
        if not line["name"] and r.get("route_long_name"):
            line["name"] = r["route_long_name"]

    # Stops: boarding points only (location_type 0 or blank); keep the parent for station grouping.
    stop_ids: set[str] = set()
    for s in rows(z, "stops.txt"):
        if s.get("location_type", "0") not in ("", "0"):
            continue
        try:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        except (KeyError, ValueError):
            continue
        sid = f"{mode}:{s['stop_id']}"
        stop_ids.add(sid)
        graph["stops"][sid] = {
            "id": sid, "mode": mode,
            "code": s.get("stop_code") or s["stop_id"],
            "name": s["stop_name"].strip(),
            "lat": round(lat, 5), "lon": round(lon, 5),
            "station": f"{mode}:{s['parent_station']}" if s.get("parent_station") else None,
            "lines": [],
        }

    # Trips: one representative per (route, direction) = the trip with the most stops.
    trip_route: dict[str, tuple[str, str]] = {}
    trip_shape: dict[str, str] = {}
    for t in rows(z, "trips.txt"):
        trip_route[t["trip_id"]] = (t["route_id"], t.get("direction_id") or "0")
        if t.get("shape_id"):
            trip_shape[t["trip_id"]] = t["shape_id"]

    counts: dict[str, int] = defaultdict(int)
    for st in rows(z, "stop_times.txt"):
        counts[st["trip_id"]] += 1

    best: dict[tuple[str, str], tuple[int, str]] = {}
    for trip_id, n in counts.items():
        key = trip_route.get(trip_id)
        if key is None:
            continue
        if key not in best or n > best[key][0]:
            best[key] = (n, trip_id)
    chosen = {trip_id: key for key, (_, trip_id) in best.items()}

    seqs: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for st in rows(z, "stop_times.txt"):
        if st["trip_id"] in chosen:
            seqs[st["trip_id"]].append((int(st["stop_sequence"]), f"{mode}:{st['stop_id']}"))

    # Shapes: only the ones the chosen trips use (interurbanos' shapes.txt is 200 MB, so stream once).
    wanted = {trip_shape[t] for t in chosen if t in trip_shape}
    raw_shapes: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    if wanted:
        for sp in rows(z, "shapes.txt"):
            if sp["shape_id"] in wanted:
                try:
                    raw_shapes[sp["shape_id"]].append((int(sp["shape_pt_sequence"]), float(sp["shape_pt_lat"]), float(sp["shape_pt_lon"])))
                except ValueError:
                    pass
    shapes = {sid: simplify([[round(la, 5), round(lo, 5)] for _, la, lo in sorted(pts)]) for sid, pts in raw_shapes.items()}

    for trip_id, (route_id, direction) in chosen.items():
        line_id = route_to_line.get(route_id)
        if not line_id:
            continue
        ordered = cut_at_first_repeat([sid for _, sid in sorted(seqs[trip_id]) if sid in stop_ids])
        if len(ordered) < 2:
            continue
        # Cercanías encodes direction as separate routes; key sequences by route+direction to keep both.
        key = f"{route_id}/{direction}"
        graph["lines"][line_id]["sequences"][key] = ordered
        if trip_id in trip_shape and trip_shape[trip_id] in shapes:
            graph["lines"][line_id].setdefault("shapes", {})[key] = shapes[trip_shape[trip_id]]
        for sid in ordered:
            lines = graph["stops"][sid]["lines"]
            if line_id not in lines:
                lines.append(line_id)

    # Drop stops no chosen trip touches (depots, retired poles) and lines with no sequence.
    for sid in list(stop_ids):
        if not graph["stops"][sid]["lines"]:
            del graph["stops"][sid]
    for lid in [l for l, v in graph["lines"].items() if v["mode"] == mode and not v["sequences"]]:
        del graph["lines"][lid]

    n_lines = sum(1 for l in graph["lines"].values() if l["mode"] == mode)
    n_stops = sum(1 for s in graph["stops"].values() if s["mode"] == mode)
    log(f"  {mode:13} {n_lines:4} lines {n_stops:6} stops  ({time.time() - t0:.1f}s)")


METRO_GIS = "https://services5.arcgis.com/UxADft6QPcvFyDU1/arcgis/rest/services/M4_Red/FeatureServer"


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def import_metro_gis(graph: dict) -> None:
    """Metro from CRTM's live network layers. The Metro GTFS on the portal is from May 2025 and has
    no trips for L3, so stations come from M4_Estaciones and ordered sequences from M4_Tramos.
    Branch codes (10a/10b, 7a/7b, 9A/9B, 6-1/6-2, 12-1/12-2) fold into one line per number."""
    t0 = time.time()
    mode = "metro"
    stations = fetch_json(f"{METRO_GIS}/0/query?where=1%3D1&outFields=CODIGOESTACION,DENOMINACION,LINEAS"
                          f"&returnGeometry=true&outSR=4326&f=geojson&resultRecordCount=2000")
    tramos = fetch_json(f"{METRO_GIS}/4/query?where=1%3D1&outFields=NUMEROLINEAUSUARIO,SENTIDO,NUMEROORDEN,"
                        f"CODIGOESTACION,CODIGOITINERARIO&returnGeometry=true&outSR=4326&f=geojson&resultRecordCount=5000")
    colours = {"1": "2DBEF0", "2": "ED1C24", "3": "FFD000", "4": "B65518", "5": "8FD400", "6": "98989B",
               "7": "FF8300", "8": "F373B7", "9": "A60084", "10": "003DA5", "11": "009A3E", "12": "A49800", "R": "FFFFFF"}
    # One stop per station (several platform rows share a code; keep the first geometry).
    for f in stations["features"]:
        p = f["properties"]
        code = str(p["CODIGOESTACION"])
        sid = f"{mode}:{code}"
        if sid in graph["stops"]:
            continue
        lon, lat = f["geometry"]["coordinates"]
        graph["stops"][sid] = {"id": sid, "mode": mode, "code": code, "name": (p.get("DENOMINACION") or "").strip(),
                               "lat": round(lat, 5), "lon": round(lon, 5), "station": sid, "lines": []}
    seqs: dict[tuple[str, str, int], list[tuple[int, str]]] = defaultdict(list)
    paths: dict[tuple[str, str, int], list[tuple[int, list]]] = defaultdict(list)
    for f in tramos["features"]:
        a = f["properties"]
        raw = str(a["NUMEROLINEAUSUARIO"])
        number = re.match(r"\d+|R", raw).group(0)
        key = (number, raw, int(a["SENTIDO"]))
        seqs[key].append((int(a["NUMEROORDEN"]), f"{mode}:{a['CODIGOESTACION']}"))
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "MultiLineString":
            coords = [c for part in coords for c in part]
        if coords:
            paths[key].append((int(a["NUMEROORDEN"]), [[round(lat, 5), round(lon, 5)] for lon, lat in coords]))
    for (number, raw, sentido), items in seqs.items():
        line_id = f"{mode}:L{number}"
        line = graph["lines"].setdefault(line_id, {"id": line_id, "mode": mode, "label": f"L{number}", "name": "",
                                                    "color": colours.get(number), "sequences": {}})
        ordered = [sid for _, sid in sorted(items) if sid in graph["stops"]]
        if len(ordered) < 2:
            continue
        line["sequences"][f"{raw}/{sentido}"] = ordered
        pts = [p for _, seg in sorted(paths.get((number, raw, sentido), [])) for p in seg]
        if pts:
            line.setdefault("shapes", {})[f"{raw}/{sentido}"] = simplify(pts)
        if not line["name"]:
            line["name"] = f"{graph['stops'][ordered[0]]['name']} - {graph['stops'][ordered[-1]]['name']}"
        for sid in ordered:
            if line_id not in graph["stops"][sid]["lines"]:
                graph["stops"][sid]["lines"].append(line_id)
    for sid in [s for s, v in graph["stops"].items() if v["mode"] == mode and not v["lines"]]:
        del graph["stops"][sid]
    n_lines = sum(1 for l in graph["lines"].values() if l["mode"] == mode)
    n_stops = sum(1 for s in graph["stops"].values() if s["mode"] == mode)
    log(f"  {mode:13} {n_lines:4} lines {n_stops:6} stops  (ArcGIS topology, {time.time() - t0:.1f}s)")


PARTICLES = {"de", "del", "la", "las", "los", "el", "y", "e", "en", "a", "al", "o", "u", "da", "do", "das", "dos"}
ROMAN = re.compile(r"^(?=[ivxlc]+$)i{0,3}(?:v|x|l|c)?i{0,3}$|^(xii|xiii|xi|xii|iv|vi|vii|viii|ix|ii|iii)$", re.I)


def smart_title(name: str) -> str:
    """Spanish title case: 'PINAR DE CHAMARTIN' -> 'Pinar de Chamartin', 'Pio Xii' -> 'Pio XII',
    'Puerta del Sol/sevilla' -> 'Puerta del Sol/Sevilla'. Accents are restored elsewhere."""
    def cap(word: str, first: bool) -> str:
        if not word:
            return word
        low = word.lower()
        if ROMAN.match(low):
            return low.upper()
        if low in PARTICLES and not first:
            return low
        return low[0].upper() + low[1:]
    out = []
    for i, token in enumerate(re.split(r"(\s+)", name.strip())):
        if not token or token.isspace():
            out.append(token)
            continue
        parts = re.split(r"([/\-\.])", token)
        rebuilt = "".join(cap(part, i == 0 or j > 0) if part not in "/-." else part for j, part in enumerate(parts))
        out.append(rebuilt)
    return "".join(out)


# Words the Metro feed strips accents from and no other feed spells for us.
ACCENTS = {w.lower().translate(str.maketrans("áéíóúñ", "aeioun")): w for w in (
    "Sofía", "Católicos", "Comunicación", "Joaquín", "María", "José", "Ramón", "García", "Martínez", "Fernández",
    "Gómez", "López", "Pérez", "Sánchez", "González", "Rodríguez", "Hernández", "Jiménez", "Álvarez", "Aviación",
    "Estación", "Andrés", "Nicolás", "León", "Inés", "Alcalá", "Peñagrande", "Begoña", "Chamartín", "Batán",
    "Lavapiés", "Pacífico", "Pirámides", "Ópera", "Núñez", "Balboa", "Marañón", "Bernabéu", "Vicálvaro", "Canillejas",
    "Príncipe", "Pío", "Alameda", "Osuna", "Antón", "Martín", "Barajas", "Valdebernardo", "Elíptica", "Méndez",
    "Álvaro", "Carpetana", "Oporto", "Urgel", "Usera", "Legazpi", "Delicias", "Atocha", "Sáinz", "Baranda",
    "Conde", "Casal", "Sáenz", "Ibiza", "Goya", "Velázquez", "Serrano", "Rubén", "Darío", "Colón", "Iglesia",
    "Ríos", "Rosas", "Argüelles", "Moncloa", "Metropolitano", "Henares", "Coslada", "Jarama", "Vinateros",
    "Artilleros", "Pavones", "Valdecarros", "Villaverde", "Hortaleza", "Pitis", "Lacoma", "Avenida", "Ilustración",
    "Tetuán", "Valdeacederas", "Chueca", "Bilbao", "Quevedo", "Canal", "Islas", "Filipinas", "Guzmán", "Bueno",
    "Alvarado", "Estrecho", "Duque", "Pastrana", "Parque", "Avenidas", "Diego", "Ventas", "Ascao", "Bambú",
    "Nuevos", "Ministerios", "Tribunal", "Noviciado", "Rodríguez", "Manuel", "Becerra", "Suanzes", "Gregorio",
    "Ciudad", "Universitaria", "Vicente", "Aleixandre", "Lucero", "Laguna", "Almendrales", "Opañel", "Abrantes",
    "Mirasierra", "Herrera", "Oria", "Paco", "Lucía", "Jesús", "Cármenes", "Puñonrostro", "Sainz", "Ángel",
    "Alcorcón", "Móstoles", "Leganés", "Julián", "Núñez", "Malasaña", "Chamberí", "Fermín", "Ángeles", "Cristóbal",
    "Concepción", "Américas", "Cánovas", "Abrantes", "Pradillo", "Sebastián", "Jiménez", "Mendoza", "Bercial",
    "Cierva", "Montijo", "Villaviciosa", "Odón", "Boadilla", "Pozuelo", "Aravaca", "Majadahonda", "Rozas", "Tetuán",
)}


def restore_accents(name: str) -> str:
    fold = str.maketrans("áéíóúñü", "aeiounu")
    def fix(w: str) -> str:
        target = ACCENTS.get(w.lower().translate(fold))
        return target if target and target.lower() != w.lower() else w
    # Words are separated by spaces, hyphens, slashes or dots: "Ramón y Cajal-Nuñez de Balboa".
    return "".join(fix(part) if part and part not in " -/." else part for part in re.split(r"([ \-/\.])", name))


def fold_name(name: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def unify_names(graph: dict) -> None:
    """Across feeds the same station is 'Piramides', 'Pirámides', 'PIRAMIDES'. Pick the best spelling
    (most accents, sensible case) for every name that folds to the same key, then title-case the rest."""
    import unicodedata
    def score(n: str) -> tuple:
        accents = sum(1 for ch in n if unicodedata.decomposition(ch))
        shouting = n.isupper()
        naive_title = n == n.title()
        return (accents, not shouting, not naive_title, len(n))
    best: dict[str, str] = {}
    pool = [s["name"] for s in graph["stops"].values()] + [l["name"] for l in graph["lines"].values()]
    for n in pool:
        if not n:
            continue
        k = fold_name(n)
        if k not in best or score(n) > score(best[k]):
            best[k] = n
    for s in graph["stops"].values():
        s["name"] = restore_accents(smart_title(best.get(fold_name(s["name"]), s["name"])))
    for l in graph["lines"].values():
        if l["name"]:
            l["name"] = restore_accents(smart_title(best.get(fold_name(l["name"]), l["name"])))
    log("  names unified")


def build_transfers(graph: dict) -> None:
    """Line-to-line edges. Two lines connect where they share a stop, a parent station,
    or have stops within TRANSFER_METERS of each other. Grid-bucketed so it stays fast."""
    t0 = time.time()
    cell = 0.002  # about 220 m in latitude; buckets are compared with their neighbours
    grid: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for s in graph["stops"].values():
        grid[(int(s["lat"] / cell), int(s["lon"] / cell))].append(s)

    edges: dict[tuple[str, str], dict] = {}

    def connect(a: dict, b: dict, meters: float) -> None:
        for la in a["lines"]:
            for lb in b["lines"]:
                if la == lb:
                    continue
                key = (la, lb) if la < lb else (lb, la)
                where = a["name"] if a["mode"] in ("metro", "cercanias", "metro_ligero") else b["name"]
                prev = edges.get(key)
                if prev is None or meters < prev["meters"]:
                    edges[key] = {"a": key[0], "b": key[1], "at": where, "meters": int(meters)}

    for (gx, gy), bucket in grid.items():
        neighbours = [s for dx in (-1, 0, 1) for dy in (-1, 0, 1) for s in grid.get((gx + dx, gy + dy), [])]
        for a in bucket:
            for b in neighbours:
                if a["id"] >= b["id"]:
                    continue
                if a["station"] and a["station"] == b["station"]:
                    connect(a, b, 0)
                    continue
                d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                if d <= TRANSFER_METERS:
                    connect(a, b, d)

    graph["transfers"] = sorted(edges.values(), key=lambda e: (e["a"], e["b"]))
    log(f"  transfers     {len(graph['transfers']):6} line pairs  ({time.time() - t0:.1f}s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--gtfs-dir", default="gtfs")
    ap.add_argument("--out", default="graph.json")
    ap.add_argument("--modes", default=",".join(FEEDS))
    args = ap.parse_args()

    gtfs_dir = Path(args.gtfs_dir)
    gtfs_dir.mkdir(exist_ok=True)
    graph = {"built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stops": {}, "lines": {}, "transfers": []}

    for mode in args.modes.split(","):
        if mode == "metro":
            import_metro_gis(graph)
            continue
        path = gtfs_dir / f"{mode}.zip"
        if not args.no_download or not path.exists():
            log(f"downloading {mode}")
            download(mode, path)
        import_feed(mode, path, graph)

    unify_names(graph)
    build_transfers(graph)

    # Lists are smaller and simpler for clients than dicts keyed by id.
    out = {
        "built": graph["built"],
        "stops": sorted(graph["stops"].values(), key=lambda s: s["id"]),
        "lines": sorted(graph["lines"].values(), key=lambda l: l["id"]),
        "transfers": graph["transfers"],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"wrote {args.out}: {len(out['stops'])} stops, {len(out['lines'])} lines, "
        f"{len(out['transfers'])} transfers, {Path(args.out).stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
