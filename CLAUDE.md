# Estado Madrid · notes for Claude

Status of Madrid public transport around a point, with impact per line. Read README.md first.

## Rules
- Plain Python 3.10+, standard library only. It runs in GitHub Actions and must stay fast (under a minute).
- Never scrape from the phone. HTML parsing (CRTM) lives here; the app reads one Supabase row.
- Every behaviour claim has a scenario in `scenarios/` and an assertion in `test_estado.py`. Run `python3 test_estado.py` before pushing.
- Metro comes from CRTM's ArcGIS layers, not their GTFS (stale, no L3). Keep it that way until their GTFS is current.
- No emojis, no em dashes, anywhere.
- The repo is public and Actions logs are public. Never print, log or include in an error message anything
  derived from a secret (credentials, tokens, request headers). EMT errors are reported by exception type only.
- EMT credentials: `EMT_CLIENT_ID` + `EMT_PASS_KEY` (registered app) take priority; `EMT_EMAIL` + `EMT_PASSWORD`
  (portal user) are the interim fallback until the MobilityLabs app is approved.

## Deployment
- `.github/workflows/estado.yml` polls every 5 minutes and upserts row id=1 of table `estado` in Supabase (secrets `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`; optional `EMT_EMAIL`, `EMT_PASSWORD`).
- `.github/workflows/graph.yml` rebuilds `graph.json` monthly and commits it, which also keeps the schedule alive.
- The phone reads `GET /rest/v1/estado?id=eq.1` with the anon key. `graph.json` ships inside the app.

## State
First push 31 Oct 2026. Sources verified live: CRTM status page and Renfe GTFS-RT (found stale). EMT line incidents untested.
Next: the iOS app (three rings on a dark map, one sentence, morning notification).
