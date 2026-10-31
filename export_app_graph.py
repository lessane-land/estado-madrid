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
            item["s"] = merge_sequences(seqs)
            if is_loop(l["id"], l.get("name"), seqs):
                item["lp"] = True
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
