#!/usr/bin/env python3
"""
episodes.py · stretches of trouble per line, joined across consecutive status rows.

An episode is one line, one stretch (from_stop, to_stop), from the first status row where the line
appears in an incident to the first row where it no longer does. Gaps up to GAP (15 min) do not close
it. A notice whose `until` has passed closes at `until`. Spill is not recorded; the app derives it.

Used two ways:
  publish.py        advance(state, generated, incidents) after every run, writing changes to Supabase
  --backfill        replay estado_history from the start (needs SUPABASE_URL and SUPABASE_SERVICE_KEY)

Table: public.line_episodes (see supabase/episodes.sql).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

GAP = dt.timedelta(minutes=15)
KEEP = dt.timedelta(days=120)
SEV_RANK = {"amarillo": 1, "rojo": 2}


def line_id(mode: str, label: str) -> str:
    """Same ids as graph-app.json: metro:L10, cercanias:C5, emt:27. Metro labels get their L."""
    label = label.strip().replace("C-", "C")
    if mode == "metro" and not label.upper().startswith("L"):
        label = "L" + label
    return f"{mode}:{label}"


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def episode_key(inc: dict, label: str) -> tuple:
    scope = inc.get("scope") or {}
    return (line_id(inc["mode"], label), scope.get("from"), scope.get("to"))


def advance(state: dict, now: dt.datetime, incidents: list[dict]) -> dict:
    """Apply one status row to the open episodes.

    state: {key: episode dict with started, last_seen, severity, until, ...}
    Returns {"opened": [...], "updated": [...], "closed": [...]} with the episode dicts affected.
    """
    seen: dict[tuple, dict] = {}
    for inc in incidents:
        if inc.get("severity") not in SEV_RANK or inc.get("upcoming"):
            continue
        for label in inc.get("lines") or []:
            key = episode_key(inc, label)
            cur = seen.get(key)
            if cur is None or SEV_RANK[inc["severity"]] > SEV_RANK[cur["severity"]]:
                seen[key] = inc

    out = {"opened": [], "updated": [], "closed": []}

    # Close what is gone (beyond the gap) or expired.
    for key, ep in list(state.items()):
        inc = seen.get(key)
        until = parse_ts(ep.get("until"))
        if inc is None:
            if until and until <= now:
                ep["ended"] = min(until, now).isoformat()
            elif now - parse_ts(ep["last_seen"]) > GAP:
                ep["ended"] = ep["last_seen"]
            else:
                continue
            out["closed"].append(state.pop(key))

    # Open or extend what is present.
    for key, inc in seen.items():
        ep = state.get(key)
        if ep is None:
            since = parse_ts(inc.get("since"))
            started = since if since and since <= now else now   # the notice's own stamp when it has one
            ep = {
                "line": key[0], "mode": inc["mode"],
                "severity": inc["severity"].capitalize(),
                "from_stop": key[1], "to_stop": key[2],
                "started": started.isoformat(),
                "ended": None, "last_seen": now.isoformat(),
                "until": inc.get("until"),
                "source": inc.get("source"), "title": inc.get("title") or inc.get("text", "")[:120],
            }
            state[key] = ep
            out["opened"].append(ep)
        else:
            changed = False
            if SEV_RANK[inc["severity"]] > SEV_RANK[ep["severity"].lower()]:
                ep["severity"] = inc["severity"].capitalize()
                changed = True
            if inc.get("until") != ep.get("until"):
                ep["until"] = inc.get("until")
                changed = True
            ep["last_seen"] = now.isoformat()
            out["updated"].append(ep)
            ep["_changed"] = changed
    return out


# ---------------------------------------------------------------- Supabase

class Store:
    def __init__(self) -> None:
        self.url = os.environ["SUPABASE_URL"].rstrip("/")
        key = os.environ["SUPABASE_SERVICE_KEY"]
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _req(self, method: str, path: str, body=None, prefer: str | None = None):
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={**self.headers, **({"Prefer": prefer} if prefer else {})})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def open_episodes(self) -> dict:
        rows = self._req("GET", "line_episodes?ended=is.null&select=*")
        return {(r["line"], r["from_stop"], r["to_stop"]): r for r in rows}

    def history(self, since: str | None = None) -> list[dict]:
        rows, offset = [], 0
        while True:
            q = f"estado_history?select=generated,incidents&order=generated.asc&limit=500&offset={offset}"
            if since:
                q += f"&generated=gt.{urllib.parse.quote(since)}"
            page = self._req("GET", q)
            rows += page
            if len(page) < 500:
                return rows
            offset += 500

    def write(self, changes: dict) -> None:
        def clean(ep: dict) -> dict:
            return {k: v for k, v in ep.items() if not k.startswith("_")}
        rows = [clean(e) for e in changes["opened"]]
        if rows:
            self._req("POST", "line_episodes", rows, prefer="return=minimal")
        for ep in changes["updated"]:
            if "id" in ep:
                self._req("PATCH", f"line_episodes?id=eq.{ep['id']}",
                          {"last_seen": ep["last_seen"], "severity": ep["severity"], "until": ep.get("until")},
                          prefer="return=minimal")
        for ep in changes["closed"]:
            if "id" in ep:
                self._req("PATCH", f"line_episodes?id=eq.{ep['id']}", {"ended": ep["ended"]}, prefer="return=minimal")

    def prune(self, now: dt.datetime) -> None:
        cutoff = (now - KEEP).isoformat()
        self._req("DELETE", f"line_episodes?ended=lt.{urllib.parse.quote(cutoff)}", prefer="return=minimal")

    def truncate(self) -> None:
        self._req("DELETE", "line_episodes?id=gt.0", prefer="return=minimal")


def update_from_run(status: dict) -> str:
    """Called by publish.py after each poll. Returns a one-line summary."""
    store = Store()
    state = store.open_episodes()
    now = parse_ts(status["generated"])
    changes = advance(state, now, status["incidents"])
    store.write(changes)
    store.prune(now)
    return f"episodes: +{len(changes['opened'])} open, {len(changes['closed'])} closed, {len(state)} active"


def backfill() -> None:
    """Replay the whole history in memory, then write every episode in one go."""
    store = Store()
    rows = store.history()
    state: dict = {}
    closed: list[dict] = []
    for row in rows:
        now = parse_ts(row["generated"])
        changes = advance(state, now, row["incidents"] or [])
        closed += changes["closed"]
    store.truncate()
    everything = closed + list(state.values())
    for i in range(0, len(everything), 200):
        batch = [{k: v for k, v in e.items() if not k.startswith("_")} for e in everything[i:i + 200]]
        store._req("POST", "line_episodes", batch, prefer="return=minimal")
    print(f"backfilled {len(rows)} history rows into {len(closed)} closed and {len(state)} open episodes")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    args = ap.parse_args()
    if args.backfill:
        backfill()
    else:
        ap.print_help()
        sys.exit(1)
