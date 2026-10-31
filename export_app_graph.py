#!/usr/bin/env python3
"""
export_app_graph.py · graph.json in, graph-app.json out: what the phone needs, nothing else.

Keeps every stop and line (rings 1 and 3 need them) but:
  - stop sequences only for rail (metro, metro_ligero, cercanias): segment incidents only happen there
  - transfers only where at least one side is rail: bus-to-bus transfers are noise in ring 3
  - short keys, rounded coordinates, no indentation

Usage: python3 export_app_graph.py [graph.json] [graph-app.json]
"""
import json
import sys

RAIL = {"metro", "metro_ligero", "cercanias"}


def merge_sequences(seqs: list[list[str]]) -> list[str]:
    """One ordered list covering every stop of every itinerary. Starts from the longest; each other
    itinerary (reversed if it runs the other way) has its missing stops inserted after the last stop
    it shares with the merged list, so branches hang off their junction and termini are kept."""
    if not seqs:
        return []
    seqs = sorted(seqs, key=len, reverse=True)
    merged = list(seqs[0])
    for other in seqs[1:]:
        pos = {s: i for i, s in enumerate(merged)}
        shared = [pos[s] for s in other if s in pos]
        if len(shared) >= 2 and sum(1 for a, b in zip(shared, shared[1:]) if b < a) > len(shared) / 2:
            other = list(reversed(other))
        last = -1
        for s in other:
            if s in pos:
                last = pos[s]
                continue
            merged.insert(last + 1, s)
            last += 1
            pos = {x: i for i, x in enumerate(merged)}
    return merged


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "graph.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "graph-app.json"
    g = json.load(open(src, encoding="utf-8"))
    mode_of = {l["id"]: l["mode"] for l in g["lines"]}

    stops = [{"i": s["id"], "m": s["mode"], "n": s["name"], "la": s["lat"], "lo": s["lon"], "l": s["lines"]}
             for s in g["stops"]]
    lines = []
    for l in g["lines"]:
        item = {"i": l["id"], "m": l["mode"], "b": l["label"], "n": l["name"], "c": l["color"]}
        if l["mode"] in RAIL:
            # Longest sequence per line is enough for segment matching.
            item["s"] = merge_sequences(list(l["sequences"].values()))
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
