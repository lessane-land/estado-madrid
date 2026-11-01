# Estado Madrid

What is the state of public transport around me, right now, and what does it mean for my lines.

Three scripts, Python 3.10+, no dependencies:

| Script | What | How often |
| --- | --- | --- |
| `build_graph.py` | CRTM GTFS and GIS in, `graph.json` out: 15k stops, 711 lines, 19k line-to-line transfers across Metro, Metro Ligero, Cercanías, EMT, interurbanos and town buses | monthly, on any laptop |
| `estado.py` | Renfe GTFS-RT alerts plus the CRTM status page (plus EMT line incidents with credentials) into one normalised incident list; with `--at lat,lon` it colours the lines around a point | every 5 minutes |
| `around.py` | the graph query: stops on foot, their lines, lines one change away | library, also a CLI |
| `publish.py` | runs `estado.py` and pushes the JSON to Supabase, a Gist or a file | cron |

## Try it

```bash
python3 build_graph.py                                  # downloads about 110 MB, takes a minute
python3 around.py 40.4089 -3.7012 --radius 500          # Lavapiés
python3 estado.py --at 40.4463,-3.6924 --radius 350     # Nuevos Ministerios, inside the L10 cut
python3 estado.py --at 40.4663,-3.6889 --radius 350     # Plaza de Castilla, outside it
```

## The model

- **Ring 1**: stops within the radius and the lines serving them.
- **Ring 2**: each line's stop sequence, so an incident that names a segment ("Cuzco - Nuevos Ministerios") is matched against *your* stop. Inside the segment: red for you. Outside: green for you, even though the line is red somewhere else.
- **Ring 3**: lines that meet yours. A red line spills amber onto them, but only at interchanges inside the affected segment.
- **Forecast**: a couple of hand-written rules from the CRTM mode-level traffic light ("Cercanías badly hit, expect Metro crowding"). Short on purpose. Labelled as forecast.

Severity: `rojo` when the text says suspendido / interrumpido / cierre / huelga, else `amarillo`.

## Incident shape

```json
{"source": "CRTM", "mode": "metro", "lines": ["L10"], "severity": "rojo",
 "title": "Suspensión METRO L10 Cuzco - Nuevos Ministerios",
 "text": "Suspensión METRO L10 Cuzco - Nuevos Ministerios. Desde el 28 de marzo de 2026 y hasta finales de año...",
 "scope": {"from": "Cuzco", "to": "Nuevos Ministerios"},
 "since": "2026-10-31T21:10:02+00:00", "until": "2026-12-31T23:59:00+00:00"}
```

- `source`: CRTM, Renfe or EMT.
- `since`: Renfe gives it; CRTM does not, so the first run that sees a notice stamps it and later runs keep the stamp (publish.py reads the previous row).
- `until`: parsed from the notice's own words ("del 10 al 12 de octubre", "hasta finales de año", "hoy"). Notices past their `until` are dropped; recovery notices ("recuperan", "restablecido", "normalidad") are dropped outright.
- `lines` for bus notices come from the notice body ("las líneas 001, 1, 2, 3 ... C1 y C2").
- Spill (a red line turning its neighbours amber) only reaches rail lines, and only at interchanges inside the cut.

## Sources and their quirks

- **CRTM `informacion-actualizada`**: per-mode level in the image name `Estado_<Mode>_<Nivel>.png`, incident titles per mode, structured enough to parse line and segment. HTML, so keep it server side. The page keeps the previous block commented out, which is free history.
- **Renfe GTFS-RT `alerts.json`**: proper feed, Madrid alerts start with `#MadC5`. Found stale (header timestamp three weeks old) on 31 Oct 2026; `sources.renfe.stale` flags it. Do not trust it alone for strikes.
- **CRTM Metro GTFS**: dated May 2025 and has no trips for L3. Metro therefore comes from the live ArcGIS layers `M4_Estaciones` and `M4_Tramos`, which are current (they include El Casar).
- **Town buses (`urbanos`)**: every municipality has a line "1", so lines are keyed per town (`urbanos:1:013`).
- **EMT MobilityLabs**: line incidents need `EMT_CLIENT_ID` and `EMT_PASS_KEY` (registered app) or, meanwhile, `EMT_EMAIL` and `EMT_PASSWORD`. Endpoint shape untested; see `emt_incidents()`.

## Running it somewhere

**Raspberry Pi** (recommended if you have one on): copy the folder, `python3 build_graph.py` once, then

```
*/5 * * * * cd /home/pi/estado && /usr/bin/python3 publish.py >> publish.log 2>&1
```

with `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in the environment (or `ESTADO_OUT=/var/www/html/estado.json` if nginx serves it).

**The clock.** GitHub's schedule proved unreliable (three runs in eleven hours on `*/10`). `supabase/cron.sql` makes
Postgres the clock: `pg_cron` calls GitHub's `workflow_dispatch` every five minutes through `pg_net`, with a
fine-grained token kept in the Supabase Vault. The GitHub schedule stays on as a backup.

**Supabase only** (no machine at all): run `supabase/schema.sql` once. Then port `estado.py` to an Edge Function in TypeScript (it is three fetches and some regex) and schedule it with `pg_cron` every five minutes. The phone reads `GET /rest/v1/estado?id=eq.1` with the anon key. `graph.json` ships inside the app and is rebuilt monthly on a laptop.

**Vercel**: same idea, Vercel Cron plus a serverless function writing to Vercel KV or straight to Supabase. Hobby functions are limited to 10 s, enough for the poller, not for the graph build.

## Next

- Phase 2 of the app proper: location, three rings on a map, the morning notification.
- EMT incidents against real credentials.
- Renfe X and WhatsApp channels as a fallback source when the GTFS-RT feed is stale.
- Line health: `estado_history` fills up on its own; a month of it tells you which lines break on Fridays.
