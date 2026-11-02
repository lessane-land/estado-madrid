// vivo: live vehicles for the Madrid se mueve map.
//   GET ?what=trains             Cercanías trains in the Madrid area (Renfe GTFS-RT, cached 15 s)
//   GET ?what=buses&stops=1,2,3  EMT buses approaching up to 8 stops (MobilityLabs arrivals, cached 25 s per stop)
// EMT credentials come from the function's secrets: EMT_CLIENT_ID + EMT_PASS_KEY first, EMT_EMAIL + EMT_PASSWORD as fallback.
// Never echo a secret, a token or a request header in a response or a log.

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
    const kind = e instanceof Error && ["renfe", "login", "busy"].includes(e.message) ? e.message : "upstream";
    return json({ error: kind }, 502, 5);
  }
});
