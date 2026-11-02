// vivo: live vehicles for the Madrid se mueve map.
//   GET ?what=trains             Cercanías trains in the Madrid area (Renfe GTFS-RT, cached 15 s)
//   GET ?what=buses&stops=1,2,3  EMT buses approaching up to 8 stops (MobilityLabs arrivals, cached 25 s per stop)
//   GET ?city=bay&what=vehicles  every bus, train, tram and ferry in the SF Bay Area (511 regional GTFS-RT),
//                                plus BART trips with their next arrivals ("bart"), since BART sends no positions
//   GET ?city=bay&what=alerts    service alerts for the whole Bay Area (511)
//   GET ?city=bay&what=roads     road closures and lane closures (511 WZDx) and express lane and bridge tolls
// The 511 token is the secret 511_TOKEN. Its default quota is 60 calls an hour; the refresh times in BAY_EVERY
// spend about 50 (vehicles 20, BART 20, alerts 6, roads 2, tolls 2). After a refusal we wait 10 min.
//   GET ?what=planes&city=madrid|bay   aircraft within about 110 km (adsb.fi, adsb.lol as fallback; no key; cached 10 s)
//   GET ?city=bay&what=ships     vessels in the Bay (aisstream.io, secret AISSTREAM_KEY; a 6 s listen, cached 60 s)
// EMT credentials come from the function's secrets: EMT_CLIENT_ID + EMT_PASS_KEY first, EMT_EMAIL + EMT_PASSWORD as fallback.
// Never echo a secret, a token or a request header in a response or a log.

import GtfsRealtimeBindings from "npm:gtfs-realtime-bindings@1.1.1";

const RENFE = "https://gtfsrt.renfe.com/vehicle_positions.json";
const EMT = "https://openapi.emtmadrid.es/";
const BOX = { south: 39.85, north: 41.2, west: -4.6, east: -3.0 };
const MAX_STOPS = 8;
const EMT_CALLS_PER_MINUTE = 120;

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
};
const json = (body: unknown, status = 200, maxAge = 10) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, "Content-Type": "application/json", "Cache-Control": `public, max-age=${maxAge}` },
  });

let trains: { at: number; data: unknown[] } = { at: 0, data: [] };
let token: string | null = null;
let tokenUntil = 0;
const stopCache = new Map<string, { at: number; buses: Bus[] }>();
let window: { start: number; calls: number } = { start: 0, calls: 0 };

type Bus = { bus: string; line: string; dest: string; lat: number; lon: number; stop: string; secs: number | null; meters: number | null };

// ---- SF Bay Area (511) ----
const BAY = "https://api.511.org/transit/";
const BAY_EVERY = { vehicles: 180_000, bart: 180_000, alerts: 600_000, roads: 1_800_000, tolls: 1_800_000 };
type Cache = { at: number; data: unknown[] };
const empty = (): Cache => ({ at: 0, data: [] });
const bay = { vehicles: empty(), bart: empty(), alerts: empty(), roads: empty(), tolls: empty(), pauseUntil: 0 };
const FeedMessage = GtfsRealtimeBindings.transit_realtime.FeedMessage;
const text = (t: any) => (t?.translation ?? []).find((x: any) => !x.language || x.language.startsWith("en"))?.text ?? t?.translation?.[0]?.text ?? "";

async function get511(url: string) {
  const key = Deno.env.get("511_TOKEN")?.trim();
  if (!key) throw new Error("no511");
  if (Date.now() < bay.pauseUntil) throw new Error("busy");
  const r = await fetch(`${url}&api_key=${encodeURIComponent(key)}`);
  if (r.status === 429 || r.status === 401 || r.status === 403) { bay.pauseUntil = Date.now() + 600_000; throw new Error("busy"); }
  if (!r.ok) throw new Error(`bay${r.status}`);
  return r;
}
async function feed511(path: string, agency = "RG") {
  const r = await get511(`${BAY}${path}?agency=${agency}`);
  return FeedMessage.decode(new Uint8Array(await r.arrayBuffer()));
}
async function json511(path: string) {
  const r = await get511(`https://api.511.org/${path}?format=json`);
  return JSON.parse((await r.text()).replace(/^\uFEFF/, ""));
}
// Refresh one cache when it is due; on failure keep serving the last good copy.
async function due(c: Cache, every: number, load: () => Promise<unknown[]>) {
  if (Date.now() - c.at < every) return c;
  try { c.data = await load(); c.at = Date.now(); } catch (e) { if (!c.at) throw e; }
  return c;
}

async function bayVehicles() {
  return due(bay.vehicles, BAY_EVERY.vehicles, async () => {
    const m = await feed511("VehiclePositions");
    const out = [];
    for (const e of m.entity ?? []) {
      const v: any = e.vehicle, p = v?.position;
      if (!p) continue;
      out.push({ id: String(v.vehicle?.id ?? e.id), a: String(v.vehicle?.agencyId ?? (e.id.includes(":") ? e.id.split(":")[0] : "")), r: String(v.trip?.routeId ?? ""),
        d: v.trip?.directionId ?? null, lat: +p.latitude.toFixed(5), lon: +p.longitude.toFixed(5), b: p.bearing ?? null, s: String(v.stopId ?? ""),
        st: v.currentStatus ?? null, t: Number(v.timestamp ?? 0) });
    }
    return out;
  });
}

// BART: trips with their next three arrivals. The page places each train between stations from these times.
async function bayBart() {
  return due(bay.bart, BAY_EVERY.bart, async () => {
    const m = await feed511("TripUpdates", "BA");
    const out = [];
    for (const e of m.entity ?? []) {
      const t: any = e.tripUpdate;
      if (!t?.trip) continue;
      const u = (t.stopTimeUpdate ?? []).slice(0, 3).map((x: any) => [String(x.stopId ?? ""), Number(x.arrival?.time ?? x.departure?.time ?? 0), Number(x.departure?.time ?? x.arrival?.time ?? 0)]);
      if (!u.length) continue;
      out.push({ id: String(t.trip.tripId ?? e.id), r: "BA:" + String(t.trip.routeId ?? ""), car: String(t.vehicle?.id ?? ""), u });
    }
    return out;
  });
}

const latlon = (c: number[]) => [+c[1].toFixed(5), +c[0].toFixed(5)];
// Road closures: only what is happening now and closes at least some lanes, as short paths.
async function bayRoads() {
  return due(bay.roads, BAY_EVERY.roads, async () => {
    const d = await json511("traffic/wzdx");
    const now = Date.now(), out = [];
    for (const f of d.features ?? []) {
      const p = f.properties ?? {}, core = p.core_details ?? {};
      const impact = String(p.vehicle_impact ?? "");
      const kind = impact === "all-lanes-closed" ? "closed" : impact.startsWith("some-lanes-closed") ? "lanes" : core.event_type === "detour" ? "detour" : "";
      if (!kind) continue;
      if (p.event_status && p.event_status !== "active") continue;
      if (p.start_date && Date.parse(p.start_date) > now) continue;
      if (p.end_date && Date.parse(p.end_date) < now) continue;
      const g = f.geometry ?? {}, coords = g.type === "LineString" ? g.coordinates : g.type === "MultiPoint" ? g.coordinates : [];
      if (!coords?.length) continue;
      const step = Math.max(1, Math.ceil(coords.length / 24));
      const path = coords.filter((_: unknown, i: number) => i % step === 0 || i === coords.length - 1).map(latlon);
      out.push({ id: String(f.id), kind, road: (core.road_names ?? []).join(" / "), dir: core.direction ?? "", text: String(core.description ?? "").slice(0, 200),
        from: p.beginning_cross_street ?? "", to: p.ending_cross_street ?? "", until: p.end_date ?? null, path });
    }
    return out;
  });
}

// Tolls: every express lane sign and bridge with its current price to each destination.
async function bayTolls() {
  return due(bay.tolls, BAY_EVERY.tolls, async () => {
    const d = await json511("toll/programs");
    const out = [];
    for (const p of d["toll-programs"] ?? []) {
      const program = p["toll-authority-info"]?.["toll-program-name"] ?? "";
      for (const s of p["toll-signs"] ?? []) {
        const c = s.geography?.coordinates;
        if (!Array.isArray(c)) continue;
        const to = (s["toll-destinations"] ?? []).map((x: any) => ({ name: String(x.name ?? ""), price: Number(x["toll-rates"]?.[0]?.["toll-price"] ?? NaN) }))
          .filter((x: any) => Number.isFinite(x.price));
        if (!to.length) continue;
        const [lat, lon] = latlon(c);
        out.push({ id: String(s.id), program, name: String(s.name ?? ""), lat, lon, to: to.slice(0, 4) });
      }
    }
    return out;
  });
}

async function bayAlerts() {
  return due(bay.alerts, BAY_EVERY.alerts, async () => {
    const m = await feed511("servicealerts");
    const out = [];
    for (const e of m.entity ?? []) {
      const a: any = e.alert;
      if (!a) continue;
      out.push({ id: String(e.id), effect: a.effect ?? null, title: text(a.headerText), text: text(a.descriptionText).slice(0, 600),
        on: (a.informedEntity ?? []).map((x: any) => ({ a: x.agencyId ?? "", r: x.routeId ?? "", s: x.stopId ?? "" })).slice(0, 40),
        from: Number(a.activePeriod?.[0]?.start ?? 0), until: Number(a.activePeriod?.[0]?.end ?? 0) });
    }
    return out;
  });
}

// ---- Planes (both cities) ----
const SKY: Record<string, [number, number]> = { madrid: [40.45, -3.62], bay: [37.66, -122.25] };
const planes = new Map<string, Cache>();
async function getPlanes(city: string) {
  const c = planes.get(city) ?? empty(); planes.set(city, c);
  const [lat, lon] = SKY[city];
  return due(c, 10_000, async () => {
    const urls = [`https://opendata.adsb.fi/api/v2/lat/${lat}/lon/${lon}/dist/60`, `https://api.adsb.lol/v2/point/${lat}/${lon}/60`];
    for (const u of urls) {
      try {
        const r = await fetch(u, { headers: { "User-Agent": "madrid-se-mueve (lessane.es)" } });
        if (!r.ok) continue;
        const d = await r.json();
        return (d.ac ?? d.aircraft ?? []).filter((a: any) => Number.isFinite(a.lat) && Number.isFinite(a.lon)).map((a: any) => ({
          id: String(a.hex ?? ""), f: String(a.flight ?? "").trim(), lat: +a.lat.toFixed(4), lon: +a.lon.toFixed(4),
          alt: a.alt_baro === "ground" ? 0 : Number(a.alt_baro ?? a.alt_geom ?? 0), gs: Number(a.gs ?? 0), trk: Number(a.track ?? a.true_heading ?? 0),
          t: String(a.t ?? ""), sq: String(a.squawk ?? "") }));
      } catch { /* try the next network */ }
    }
    throw new Error("planes");
  });
}

// ---- Ships (the Bay) ----
const ships = empty();
const SHIP_BOX = [[37.40, -122.65], [38.15, -122.00]];
async function getShips() {
  return due(ships, 60_000, () => new Promise<unknown[]>((resolve, reject) => {
    const key = Deno.env.get("AISSTREAM_KEY")?.trim();
    if (!key) return reject(new Error("noais"));
    const seen = new Map<string, any>(), names = new Map<string, any>();
    let ws: WebSocket;
    try { ws = new WebSocket("wss://stream.aisstream.io/v0/stream"); } catch { return reject(new Error("ships")); }
    const done = () => {
      try { ws.close(); } catch { /* closed */ }
      const prev = new Map((ships.data as any[]).map((x) => [x.id, x]));
      const out = [...seen.values()].map((x) => ({ ...x, ...(names.get(x.id) ?? (prev.get(x.id) ? { name: prev.get(x.id).name, to: prev.get(x.id).to, type: prev.get(x.id).type } : {})) }));
      // Keep ships heard in the last 10 minutes so quiet ones do not blink out between listens.
      for (const [id, x] of prev) if (!seen.has(id) && Date.now() - (x.at ?? 0) < 600_000) out.push(x);
      resolve(out);
    };
    const timer = setTimeout(done, 6000);
    ws.onopen = () => ws.send(JSON.stringify({ APIKey: key, BoundingBoxes: [SHIP_BOX], FilterMessageTypes: ["PositionReport", "ShipStaticData", "StandardClassBPositionReport"] }));
    ws.onerror = () => { clearTimeout(timer); if (seen.size) done(); else reject(new Error("ships")); };
    ws.onmessage = async (ev) => {
      try {
        const m = JSON.parse(typeof ev.data === "string" ? ev.data : await (ev.data as Blob).text());
        const id = String(m.MetaData?.MMSI ?? "");
        if (!id) return;
        const pr = m.Message?.PositionReport ?? m.Message?.StandardClassBPositionReport;
        if (pr) seen.set(id, { id, name: String(m.MetaData?.ShipName ?? "").trim(), lat: +Number(pr.Latitude).toFixed(5), lon: +Number(pr.Longitude).toFixed(5), sog: Number(pr.Sog ?? 0), cog: Number(pr.Cog ?? 0), at: Date.now() });
        const sd = m.Message?.ShipStaticData;
        if (sd) names.set(id, { name: String(sd.Name ?? "").trim(), to: String(sd.Destination ?? "").trim(), type: Number(sd.Type ?? 0) });
      } catch { /* skip a bad message */ }
    };
  }));
}

async function getTrains() {
  if (Date.now() - trains.at < 15_000) return trains.data;
  const r = await fetch(RENFE);
  if (!r.ok) throw new Error("renfe");
  const d = await r.json();
  const out = [];
  for (const e of d.entity ?? []) {
    const v = e.vehicle;
    const p = v?.position;
    if (!p) continue;
    if (p.latitude < BOX.south || p.latitude > BOX.north || p.longitude < BOX.west || p.longitude > BOX.east) continue;
    const label: string = v.vehicle?.label ?? "";
    const line = (String(e.id).match(/^VP_([A-Z0-9a-z]+)-/) ?? [])[1] ?? label.split("-")[0];
    out.push({ id: v.vehicle?.id ?? e.id, line, lat: p.latitude, lon: p.longitude, status: v.currentStatus ?? "", stop: v.stopId ?? "", t: Number(v.timestamp ?? 0) });
  }
  trains = { at: Date.now(), data: out };
  return out;
}

function spend(n: number) {
  const now = Date.now();
  if (now - window.start > 60_000) window = { start: now, calls: 0 };
  if (window.calls + n > EMT_CALLS_PER_MINUTE) return false;
  window.calls += n;
  return true;
}

async function login() {
  if (token && Date.now() < tokenUntil) return token;
  const id = Deno.env.get("EMT_CLIENT_ID"), key = Deno.env.get("EMT_PASS_KEY");
  const email = Deno.env.get("EMT_EMAIL"), pass = Deno.env.get("EMT_PASSWORD");
  const attempts: Record<string, string>[] = [];
  if (id && key) attempts.push({ "X-ClientId": id, passKey: key });
  if (email && pass) attempts.push({ email, password: pass });
  for (const headers of attempts) {
    if (!spend(1)) throw new Error("busy");
    const r = await fetch(EMT + "v1/mobilitylabs/user/login/", { headers });
    if (!r.ok) continue;
    const d = await r.json();
    const first = d?.data?.[0];
    if ((d.code === "00" || d.code === "01") && first?.accessToken) {
      token = first.accessToken;
      tokenUntil = Date.now() + Math.max(60, Number(first.tokenSecExpiration ?? 3600) - 60) * 1000;
      return token;
    }
  }
  throw new Error("login");
}

async function arrivals(stop: string): Promise<Bus[]> {
  const hit = stopCache.get(stop);
  if (hit && Date.now() - hit.at < 25_000) return hit.buses;
  if (!spend(1)) return hit?.buses ?? [];
  const t = await login();
  const r = await fetch(EMT + `v2/transport/busemtmad/stops/${stop}/arrives/`, {
    method: "POST",
    headers: { accessToken: t!, "Content-Type": "application/json" },
    body: JSON.stringify({ cultureInfo: "ES", Text_StopRequired_YN: "N", Text_EstimationsRequired_YN: "Y", Text_IncidencesRequired_YN: "N" }),
  });
  if (r.status === 401) { token = null; return hit?.buses ?? []; }
  const d = await r.json();
  if (String(d.code ?? "").startsWith("8")) { token = null; return hit?.buses ?? []; }
  const list = d?.data?.[0]?.Arrive ?? [];
  const buses: Bus[] = [];
  for (const a of list) {
    const c = a?.geometry?.coordinates;
    if (!Array.isArray(c) || c.length < 2) continue;
    const secs = Number(a.estimateArrive);
    buses.push({ bus: String(a.bus ?? ""), line: String(a.line ?? ""), dest: String(a.destination ?? ""), lon: Number(c[0]), lat: Number(c[1]), stop,
      secs: Number.isFinite(secs) && secs < 999_999 ? secs : null, meters: Number.isFinite(Number(a.DistanceBus)) ? Number(a.DistanceBus) : null });
  }
  stopCache.set(stop, { at: Date.now(), buses });
  if (stopCache.size > 2000) stopCache.clear();
  return buses;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  const url = new URL(req.url);
  const what = url.searchParams.get("what");
  try {
    if (what === "planes") {
      const city = url.searchParams.get("city") === "bay" ? "bay" : "madrid";
      const p = await getPlanes(city); return json({ at: p.at, planes: p.data }, 200, 8);
    }
    if (url.searchParams.get("city") === "bay" && what === "ships") { const s = await getShips(); return json({ at: s.at, ships: s.data }, 200, 30); }
    if (url.searchParams.get("city") === "bay") {
      if (what === "vehicles") {
        const v = await bayVehicles();
        let b: Cache = bay.bart; try { b = await bayBart(); } catch { /* vehicles still go out */ }
        return json({ at: v.at, vehicles: v.data, bartAt: b.at, bart: b.data }, 200, 30);
      }
      if (what === "alerts") { const a = await bayAlerts(); return json({ at: a.at, alerts: a.data }, 200, 60); }
      if (what === "roads") {
        let r: Cache = bay.roads, t: Cache = bay.tolls;
        try { r = await bayRoads(); } catch { /* tolls may still work */ }
        try { t = await bayTolls(); } catch { /* roads may still work */ }
        if (!r.at && !t.at) throw new Error("bay");
        return json({ at: Math.max(r.at, t.at), roads: r.data, tolls: t.data }, 200, 300);
      }
      return json({ error: "what must be vehicles, alerts or roads" }, 400);
    }
    if (what === "trains") return json({ at: Date.now(), trains: await getTrains() }, 200, 10);
    if (what === "buses") {
      const stops = (url.searchParams.get("stops") ?? "").split(",").filter((s) => /^\d{1,5}$/.test(s)).slice(0, MAX_STOPS);
      const seen = new Map<string, Bus>();
      for (const s of stops) {
        for (const b of await arrivals(s)) {
          const key = b.bus || `${b.line}-${b.lat}-${b.lon}`;
          const old = seen.get(key);
          if (!old || (b.meters ?? 1e9) < (old.meters ?? 1e9)) seen.set(key, b);
        }
      }
      return json({ at: Date.now(), buses: [...seen.values()] }, 200, 15);
    }
    return json({ error: "what must be trains or buses" }, 400);
  } catch (e) {
    const m = e instanceof Error ? e.message : "";
    const kind = ["renfe", "login", "busy", "no511", "planes", "ships", "noais"].includes(m) || /^bay\d{3}$/.test(m) ? m : "upstream";
    return json({ error: kind }, 502, 5);
  }
});
