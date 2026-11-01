#!/usr/bin/env python3
"""
export_app_graph.py · graph.json in, graph-app.json out: what the phone needs, nothing else.

Keeps every stop and line (rings 1 and 3 need them) but:
  - stop sequences for every line (merged over its itineraries); loop flag and segment matching only matter for rail
  - "sh": the line's path as a Google encoded polyline (precision 5), simplified to about 8 m;
    "shk": "stops" marks the lines whose feed shape was unusable and whose path is stop-to-stop straight lines
  - transfers only where at least one side is rail: bus-to-bus transfers are noise in ring 3
  - short keys, rounded coordinates, no indentation

Usage: python3 export_app_graph.py [graph.json] [graph-app.json]
"""
import json
import math
import sys

RAIL = {"metro", "metro_ligero", "cercanias"}


import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from around import is_loop  # noqa: E402  (the single definition of "circular", shared with estado.py)


def merge_sequences(seqs: list[list[str]]) -> list[str]:
    """One ordered list covering every stop of every itinerary of a line.

    Start from the longest itinerary. Then, repeatedly, take the pending itinerary that shares the most
    stops with the merged list, orient it to run the same way, and splice in its missing stops:
    the head (before its first shared stop) goes in front of that stop; the middle goes after each
    predecessor; the tail (after its last shared stop) goes after it, or in front of the whole list
    when that junction is the first stop (a branch beyond a terminus). Itineraries that share nothing
    wait for a sibling to anchor them; anything still unanchored is appended."""
    if not seqs:
        return []
    pending = sorted(seqs, key=len, reverse=True)
    merged = list(pending.pop(0))
    while pending:
        pos = {s: i for i, s in enumerate(merged)}
        pending.sort(key=lambda sq: -sum(1 for s in sq if s in pos))
        other = pending.pop(0)
        shared = [s for s in other if s in pos]
        if not shared:
            if any(any(s in pos for s in sq) for sq in pending):
                pending.append(other)
                continue
            merged.extend(s for s in other if s not in pos)
            continue
        if len(shared) >= 2:
            if sum(1 for a, b in zip(shared, shared[1:]) if pos[b] < pos[a]) > (len(shared) - 1) / 2:
                other = list(reversed(other))
        elif other.index(shared[0]) > len(other) / 2:
            other = list(reversed(other))

        idx = [i for i, s in enumerate(other) if s in pos]
        first_i, last_i = idx[0], idx[-1]
        head = [s for s in other[:first_i] if s not in pos]
        tail = [s for s in other[last_i + 1:] if s not in pos]
        middle = other[first_i:last_i + 1]

        # Head: in front of the first shared stop, keeping its order.
        anchor = pos[other[first_i]]
        merged[anchor:anchor] = head
        pos = {x: i for i, x in enumerate(merged)}
        # Middle: after the predecessor.
        last = pos[middle[0]]
        for s in middle[1:]:
            if s in pos:
                last = pos[s]
                continue
            merged.insert(last + 1, s)
            last += 1
            pos = {x: i for i, x in enumerate(merged)}
        # Tail: beyond the junction, on the side the junction is on.
        if tail:
            j = pos[other[last_i]]
            if j == 0 and len(merged) > 1:
                merged[0:0] = list(reversed(tail))
            else:
                merged[j + 1:j + 1] = tail
    return merged


def encode_polyline(points: list[list[float]], precision: int = 5) -> str:
    """Google encoded polyline."""
    factor = 10 ** precision
    out = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for v in (ilat - prev_lat, ilon - prev_lon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def trim_round_trip(path: list[list[float]], route_len_m: float) -> list[list[float]]:
    """Some feeds (Renfe) store a line's shape as there-and-back. If the path ends near where it began
    and is about twice the stop route, keep only the half up to the farthest point."""
    if len(path) < 4 or route_len_m <= 0:
        return path
    lat0 = math.radians(path[0][0])
    kx, ky = 111_320.0 * math.cos(lat0), 110_540.0
    def dist(a, b):
        return math.hypot((a[1] - b[1]) * kx, (a[0] - b[0]) * ky)
    length = sum(dist(a, b) for a, b in zip(path, path[1:]))
    if dist(path[0], path[-1]) > 1500 or length < 1.7 * route_len_m:
        return path
    far = max(range(len(path)), key=lambda i: dist(path[0], path[i]))
    return path[:far + 1]


def stop_route_length(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    lat0 = math.radians(points[0][0])
    kx, ky = 111_320.0 * math.cos(lat0), 110_540.0
    return sum(math.hypot((b[1] - a[1]) * kx, (b[0] - a[0]) * ky) for a, b in zip(points, points[1:]))


def line_shape(line: dict, merged_seq: list[str]) -> tuple[list[list[float]] | None, list[str]]:
    """The line as ONE polyline: the longest itinerary's path, with each branch spliced in at its
    junction as an out-and-back detour (the return leg overdraws the outbound one, so it looks like a
    branch on screen and the polyline never jumps across the map). Identical shapes count once."""
    shapes = line.get("shapes") or {}
    seqs = line.get("sequences") or {}
    if not shapes:
        return None, []
    keys = sorted(seqs, key=lambda k: len(seqs[k]), reverse=True)
    base_key = next((k for k in keys if k in shapes), None)
    if base_key is None:
        return None, []
    used_keys = [base_key]
    path = list(shapes[base_key])
    covered = set(seqs[base_key])
    used = [shapes[base_key]]
    lat0 = math.radians(path[0][0])
    kx, ky = 111_320.0 * math.cos(lat0), 110_540.0

    def d2(a, b):
        return ((a[1] - b[1]) * kx) ** 2 + ((a[0] - b[0]) * ky) ** 2

    for k in keys:
        sh = shapes.get(k)
        if k == base_key or not sh or any(sh == u for u in used):
            continue
        extra = [s for s in seqs[k] if s not in covered]
        if len(extra) < 2:          # a short-turn variant, not a branch
            continue
        # A branch goes where the trunk does not. The return direction of a bus has different stop ids
        # but runs along the same streets, so it is not a branch: require real distance from the path.
        far = sum(1 for q in sh if min(d2(q, p) for p in path) > 250.0 ** 2)
        if far < max(3, len(sh) // 4):
            covered.update(seqs[k])
            continue
        branch = list(sh)
        # Junction end = the end closer to the current path. Make it the start of the branch.
        near_start = min(d2(branch[0], p) for p in path)
        near_end = min(d2(branch[-1], p) for p in path)
        if near_end < near_start:
            branch.reverse()
        j = min(range(len(path)), key=lambda i: d2(branch[0], path[i]))
        # Drop the part of the branch that merely retraces the trunk up to the junction.
        detour = branch + branch[-2::-1]
        path[j + 1:j + 1] = detour
        covered.update(seqs[k])
        used.append(sh)
        used_keys.append(k)
    return path, used_keys


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "graph.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "graph-app.json"
    g = json.load(open(src, encoding="utf-8"))
    mode_of = {l["id"]: l["mode"] for l in g["lines"]}

    stops = [{"i": s["id"], "m": s["mode"], "n": s["name"], "la": s["lat"], "lo": s["lon"], "l": s["lines"]}
             for s in g["stops"]]
    stop_xy = {s["id"]: (s["lat"], s["lon"]) for s in g["stops"]}
    lines = []
    for l in g["lines"]:
        item = {"i": l["id"], "m": l["mode"], "b": l["label"], "n": l["name"], "c": l["color"]}
        seqs = list(l["sequences"].values())
        item["s"] = merge_sequences(seqs)
        if l["mode"] in RAIL and is_loop(l["id"], l.get("name"), seqs):
            item["lp"] = True
        path, used_keys = line_shape(l, item["s"])
        if l["mode"] not in RAIL and used_keys:
            # Bus stops differ per direction: list the stops of the direction the path draws, plus
            # branches that share at least one stop with it (same direction). The other direction's
            # stops still carry the line in their own "l".
            base = set(l["sequences"][used_keys[0]])
            same_way = [l["sequences"][k] for k in used_keys if k == used_keys[0] or base & set(l["sequences"][k])]
            item["s"] = merge_sequences(same_way)
        stop_points = [stop_xy[s] for s in item["s"] if s in stop_xy]
        route_len = stop_route_length(stop_points)
        if path:
            path = trim_round_trip(path, route_len)
            path_len = stop_route_length([(p[0], p[1]) for p in path])
            if route_len > 0 and not (0.8 * route_len <= path_len <= 1.8 * route_len):
                path = None   # the feed's shape does not match this line; fall back below
        if not path and len(stop_points) >= 2:
            # Honest fallback: straight lines between consecutive stops.
            path = [[round(a, 5), round(b, 5)] for a, b in stop_points]
            item["shk"] = "stops"
        if path:
            item["sh"] = encode_polyline(path)
        lines.append(item)
    transfers = [{"a": t["a"], "b": t["b"], "at": t["at"]} for t in g["transfers"]
                 if mode_of.get(t["a"]) in RAIL or mode_of.get(t["b"]) in RAIL]

    out = {"built": g["built"], "stops": stops, "lines": lines, "transfers": transfers}
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    import os
    print(f"{dst}: {len(stops)} stops, {len(lines)} lines, {len(transfers)} transfers, "
          f"{os.path.getsize(dst) / 1e6:.2f} MB (from {os.path.getsize(src) / 1e6:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
