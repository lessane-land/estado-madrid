#!/usr/bin/env python3
"""
around.py · what public transport is around a point, and which lines it reaches.

  ring 1  stops within --radius metres and the lines that serve them
  ring 2  those lines, with their full stop sequences (so an incident segment can be matched)
  ring 3  lines one transfer away, and where you would change

Usage
  python3 around.py 40.3458 -3.8249              Alcorcón Central
  python3 around.py 40.4089 -3.7012 --radius 500 Lavapiés
  python3 around.py 40.3458 -3.8249 --json       machine-readable
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict

MODE_LABEL = {"metro": "Metro", "metro_ligero": "Metro Ligero", "cercanias": "Cercanías",
              "emt": "EMT", "interurbanos": "Interurbano", "urbanos": "Urbano"}
MODE_ORDER = ["cercanias", "metro", "metro_ligero", "emt", "interurbanos", "urbanos"]


def haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


CIRCULAR = {"metro:L6", "metro:L12"}


def is_loop(line_id: str, name: str | None, sequences) -> bool:
    """The one definition of a circular line, used by the status poller and by the app export (`lp`):
    a known ring, 'circular' in the name, or an itinerary that returns to its first stop."""
    if line_id in CIRCULAR or "circular" in (name or "").lower():
        return True
    return any(len(sq) > 2 and sq[0] == sq[-1] for sq in sequences)


class Graph:
    def __init__(self, path: str = "graph.json"):
        data = json.load(open(path, encoding="utf-8"))
        self.built = data["built"]
        self.stops = {s["id"]: s for s in data["stops"]}
        self.lines = {l["id"]: l for l in data["lines"]}
        self.neighbours: dict[str, list[dict]] = defaultdict(list)
        for t in data["transfers"]:
            self.neighbours[t["a"]].append({"line": t["b"], "at": t["at"], "meters": t["meters"]})
            self.neighbours[t["b"]].append({"line": t["a"], "at": t["at"], "meters": t["meters"]})
        # Spatial grid for ring 1.
        self.cell = 0.005
        self.grid: dict[tuple[int, int], list[str]] = defaultdict(list)
        for s in self.stops.values():
            self.grid[(int(s["lat"] / self.cell), int(s["lon"] / self.cell))].append(s["id"])

    def stops_near(self, lat: float, lon: float, radius: float) -> list[tuple[dict, float]]:
        gx, gy = int(lat / self.cell), int(lon / self.cell)
        span = int(radius / 500) + 1
        found = []
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                for sid in self.grid.get((gx + dx, gy + dy), []):
                    s = self.stops[sid]
                    d = haversine(lat, lon, s["lat"], s["lon"])
                    if d <= radius:
                        found.append((s, d))
        return sorted(found, key=lambda x: x[1])

    def around(self, lat: float, lon: float, radius: float = 600) -> dict:
        near = self.stops_near(lat, lon, radius)

        # Ring 1: lines reachable on foot, with the closest stop for each.
        ring1: dict[str, dict] = {}
        for s, d in near:
            for lid in s["lines"]:
                if lid not in ring1:
                    ring1[lid] = {"line": lid, "label": self.lines[lid]["label"], "mode": s["mode"],
                                  "name": self.lines[lid]["name"], "stop": s["name"], "stop_id": s["id"],
                                  "meters": int(d)}

        # Ring 3: one transfer away, excluding what is already walkable.
        ring3: dict[str, dict] = {}
        for lid in ring1:
            for n in self.neighbours.get(lid, []):
                if n["line"] in ring1:
                    continue
                prev = ring3.get(n["line"])
                if prev is None:
                    ring3[n["line"]] = {"line": n["line"], "label": self.lines[n["line"]]["label"],
                                        "mode": self.lines[n["line"]]["mode"], "via": lid,
                                        "via_label": self.lines[lid]["label"], "at": n["at"]}

        return {
            "query": {"lat": lat, "lon": lon, "radius": radius},
            "stops": [{"id": s["id"], "mode": s["mode"], "name": s["name"], "meters": int(d), "lines": s["lines"]}
                      for s, d in near],
            "ring1": sorted(ring1.values(), key=lambda r: (MODE_ORDER.index(r["mode"]), r["meters"])),
            "ring3": sorted(ring3.values(), key=lambda r: (MODE_ORDER.index(r["mode"]), r["label"])),
        }

    def is_loop(self, line_id: str) -> bool:
        line = self.lines[line_id]
        return is_loop(line_id, line.get("name"), line["sequences"].values())

    def line_stop_names(self, line_id: str) -> list[str]:
        """Longest sequence of the line, as stop names, for segment matching.
        A loop that repeats its first stop at the end lists it once."""
        seqs = self.lines[line_id]["sequences"].values()
        best = list(max(seqs, key=len)) if seqs else []
        if len(best) > 2 and best[0] == best[-1]:
            best = best[:-1]
        return [self.stops[s]["name"] for s in best if s in self.stops]


def print_report(g: Graph, result: dict) -> None:
    q = result["query"]
    print(f"Around {q['lat']}, {q['lon']} within {q['radius']} m  (graph built {g.built})")
    print(f"\n{len(result['stops'])} stops on foot")
    by_mode = defaultdict(list)
    for r in result["ring1"]:
        by_mode[r["mode"]].append(r)
    print("\nRING 1 · lines you can walk to")
    for mode in MODE_ORDER:
        if mode in by_mode:
            items = ", ".join(f"{r['label']} ({r['meters']} m, {r['stop']})" for r in by_mode[mode])
            print(f"  {MODE_LABEL[mode]:12} {items}")
    by_mode = defaultdict(list)
    for r in result["ring3"]:
        by_mode[r["mode"]].append(r)
    print("\nRING 3 · one change away")
    for mode in MODE_ORDER:
        if mode in by_mode:
            items = by_mode[mode]
            shown = ", ".join(f"{r['label']} (via {r['via_label']} at {r['at']})" for r in items[:8])
            more = f" and {len(items) - 8} more" if len(items) > 8 else ""
            print(f"  {MODE_LABEL[mode]:12} {shown}{more}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--radius", type=float, default=600)
    ap.add_argument("--graph", default="graph.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    g = Graph(args.graph)
    result = g.around(args.lat, args.lon, args.radius)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print_report(g, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
