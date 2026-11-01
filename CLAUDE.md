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
- The real clock is Supabase `pg_cron` (`supabase/cron.sql`), which dispatches the workflow every 5 minutes; GitHub's own
  schedule in the workflow is a backup only (it fired 3 times in 11 hours on day one).
- `.github/workflows/estado.yml` polls and upserts row id=1 of table `estado` in Supabase (secrets `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`; optional `EMT_EMAIL`, `EMT_PASSWORD`).
- `.github/workflows/graph.yml` rebuilds `graph.json` monthly and commits it, which also keeps the schedule alive.
- The phone reads `GET /rest/v1/estado?id=eq.1` with the anon key. `graph.json` ships inside the app.

## Incident contract with the app
`source`, `mode`, `lines`, `severity`, `title`, `text`, `scope`, `since`, `until`. The app (madrid-hoy, HoyKit)
decodes these; adding a field is fine, renaming or removing one needs a matching app change.
`graph-app.json` per line: `s` ordered stops (rail: merged union of every itinerary, both directions and branches, so
every stop that lists a rail line is in its sequence; buses: the drawn direction plus same-direction branches),
`lp` loop flag, `sh` Google encoded polyline (precision 5, simplified to 8 m), `shk: "stops"` when the feed's
shape was unusable and the path is stop-to-stop straight lines. Metro R has no path.

## Line health
`line_episodes` (supabase/episodes.sql): one row per line per stretch of trouble, maintained by `episodes.py` from
publish.py after every poll; 15 min gap tolerance, `until` closes, 120 days kept, anon SELECT. Backfill: the
`episodes-backfill` workflow replays `estado_history`. Spill is not stored; the app derives it.

## State
1 Nov 2026: Supabase pg_cron is the clock (verified every 5 min). Line shapes exported. line_episodes added. 24 tests.
Names: source casing kept unless shouting; separators and acronyms handled.
Next: app phase 2 reads line_episodes; EMT app credentials when MobilityLabs approves.
