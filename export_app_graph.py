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
            seqs = list(l["sequences"].values())
            item["s"] = max(seqs, key=len) if seqs else []
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
