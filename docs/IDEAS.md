# Ideas · BiciMAD and MobilityLabs

Written 2 Nov 2026 after reviewing MobilityLabs, the city open data catalogue and the BiciMAD datasets.
Nothing here is committed work. Review, pick, then move the chosen one into a plan.

## Data we can use

| Source | What | Access | Notes |
|---|---|---|---|
| CityBikes `api.citybik.es/v2/networks/bicimad` | Live stations: free bikes, free docks, total docks, online, light | Public, no key, CORS `*` | 675 stations and 5,418 docked bikes on 2 Nov 2026 10:46. Source listed as MobilityLabs. Safe for a public page. |
| BiciMAD GBFS (datos.madrid.es `900021-0-bicimad-GBFS`) | Official General Bikeshare Feed | Public | Published Oct 2025. Standard format, same feed Google Maps style apps read. |
| MobilityLabs Block 4 TRANSPORT BICIMAD | Stations, one station, bikes, one bike | Token (our EMT login) | Server only. Never in a public page or the app. |
| datos.madrid.es "Bicimad. Histórico de total viajes por día desde 2024" | Daily total trips | Public CSV, monthly | Backbone of Madrid se mueve. |
| datos.madrid.es "Situación de estaciones bicimad por día y hora" | Station status per hour since Jul 2018 | Public, monthly | Seeds the prediction idea. |
| datos.madrid.es BiciMAD trip files | Unplug and plug station, hour slot, travel time | Public, monthly | Origin and destination flows. |
| datos.madrid.es "EMT. Demanda diaria de viajeros de autobús" | Daily bus riders | Public | Pair with bike trips and our disruptions. |
| Our own `line_episodes` and `estado_history` | Every stretch of trouble per line since we started | Supabase, anon SELECT | Nobody else has this history. |

The GitLab repo `mobilitylabsmadrid/helps_and_utilities/static_datasets/bicimad` is documentation of the open data files
(one commit, 2019). Useful as a field glossary, not as data.

## Ideas, best first

### 1. ¿Hay bici? (live artifact)
Verdict first, map second, same family as Ahí viene. "Lavapiés: 7 bicis. Sol: 0 anclajes libres, deja la bici en Jacometrezo, 3 min a pie."
The destination dock answer is the feature: arriving at a full station is the classic BiciMAD problem.
Reads CityBikes from the browser. No server, no keys. An afternoon.

### 2. Plan B in Madrid hoy
When a stretch is cut ("L3 cortada entre Embajadores y Sol"), add bikes at the start of the cut and free docks at the end.
We already compute the stretch. Needs bike counts in the Supabase row: estado-madrid reads CityBikes or GBFS
(no MobilityLabs quota) and writes a compact `bikes` block for stations near the cut stations only.

### 3. Bici o bus in Ahí viene
"Tu 27 llega en 14 min. Hay 8 bicis a 2 min. Pedalea." Pure verdict. The app reads the same `bikes` block or CityBikes directly.

### 4. Your station will be empty at 8:40 (prediction)
Archive a bike snapshot every 5 minutes with the existing cron (same pattern as `estado_history`, compact: station id, bikes, docks).
After a few weeks we have our own pattern per station, weekday and hour. Seed it with the official hourly history.
Feeds 1, 2 and 3 with "now" plus "in 20 min".

### 5. Madrid se mueve (dashboard)
Daily BiciMAD trips against our `line_episodes`: do bike trips jump on Metro and Cercanías trouble days?
Add weather and EMT daily riders. A data story only we can tell, because only we record the disruptions.
Status: first version being built on 2 Nov 2026.

## Rules that carry over
- Credentials stay in GitHub Secrets. Public pages read only public feeds or the Supabase anon key.
- Copy names the line, the station and the place. No "may be delays".
- Dark, vector icons, no emojis, no em dashes.
