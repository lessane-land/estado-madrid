#!/usr/bin/env python3
"""
publish.py · run estado.py and push the result where the phone can read it.

Targets (pick with environment variables; several can be on at once):

  Supabase   SUPABASE_URL, SUPABASE_SERVICE_KEY            upserts one row into table `estado`
             (create it once with supabase/schema.sql)
  Gist       GITHUB_TOKEN, GIST_ID                           overwrites estado.json in a secret gist
  File       ESTADO_OUT=/var/www/estado.json                 just writes a file (nginx, Pi, anything)

Cron on a Raspberry Pi, every 5 minutes:
  */5 * * * * cd /home/pi/estado && /usr/bin/python3 publish.py >> publish.log 2>&1
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def previous_from_supabase() -> Path | None:
    """Last published incidents, so 'since' survives between stateless runs."""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        return None
    try:
        req = urllib.request.Request(f"{url}/rest/v1/estado?id=eq.1&select=incidents,levels",
                                     headers={"apikey": key, "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.load(r)
        path = Path("previous.json")
        path.write_text(json.dumps(rows[0] if rows else {}), encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001
        return None


def run_estado() -> dict:
    args = [sys.executable, "-I", str(HERE / "estado.py")]
    if (HERE / "commute.json").exists():
        args += ["--commute", str(HERE / "commute.json")]
    if prev := previous_from_supabase():
        args += ["--previous", str(prev)]
    out = subprocess.run(args, capture_output=True, text=True, timeout=120, check=True).stdout
    return json.loads(out)


def to_file(status: dict) -> None:
    path = os.environ.get("ESTADO_OUT")
    if path:
        Path(path).write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {path}")


def to_supabase(status: dict) -> None:
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        return
    body = json.dumps({"id": 1, "generated": status["generated"], "levels": status["levels"],
                       "incidents": status["incidents"], "sources": status["sources"]}).encode()
    req = urllib.request.Request(f"{url}/rest/v1/estado", data=body, method="POST", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("supabase", r.status)


def to_gist(status: dict) -> None:
    token, gist = os.environ.get("GITHUB_TOKEN"), os.environ.get("GIST_ID")
    if not (token and gist):
        return
    body = json.dumps({"files": {"estado.json": {"content": json.dumps(status, ensure_ascii=False)}}}).encode()
    req = urllib.request.Request(f"https://api.github.com/gists/{gist}", data=body, method="PATCH", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "estado-madrid"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("gist", r.status)


def to_episodes(status: dict) -> None:
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY")):
        return
    try:
        sys.path.insert(0, str(HERE))
        import episodes
        print(episodes.update_from_run(status))
    except Exception as e:  # noqa: BLE001
        print(f"episodes: skipped ({type(e).__name__})")


def main() -> int:
    status = run_estado()
    to_file(status)
    to_supabase(status)
    to_episodes(status)
    to_gist(status)
    n = len(status["incidents"])
    print(f"{status['generated']} levels={status['levels']} incidents={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
