"""Scenario tests. Run: python3 -m unittest -v test_estado.py   (needs graph.json next to it)"""
import json, subprocess, sys, unittest
from pathlib import Path

HERE = Path(__file__).parent
LAVAPIES, ALCORCON, PLAZA_CASTILLA, NUEVOS_MIN = "40.4089,-3.7012", "40.3466,-3.8277", "40.4663,-3.6889", "40.4463,-3.6924"


def here(at, scenario, radius=400):
    out = subprocess.run([sys.executable, "-I", str(HERE / "estado.py"), "--at", at, "--radius", str(radius),
                          "--scenario", str(HERE / "scenarios" / scenario)], capture_output=True, text=True, check=True).stdout
    h = json.loads(out)["for_here"]
    return {l["label"]: l for l in h["lines"]}, h


class Scenarios(unittest.TestCase):
    def test_nothing_happening_is_all_green(self):
        lines, h = here(LAVAPIES, "todo-verde.json")
        self.assertTrue(all(l["status"] == "verde" for l in lines.values()))
        self.assertEqual(h["forecast"], [])

    def test_l3_cut_is_red_at_lavapies(self):
        lines, _ = here(LAVAPIES, "lavapies-cerrada.json")
        self.assertEqual(lines["L3"]["status"], "rojo")
        self.assertTrue(lines["L3"]["incidents"][0]["touches_you"])

    def test_l3_cut_does_not_touch_moncloa_end(self):
        lines, _ = here("40.4350,-3.7193", "lavapies-cerrada.json")  # Moncloa
        self.assertEqual(lines["L3"]["status"], "verde")
        self.assertFalse(lines["L3"]["incidents"][0]["touches_you"])

    def test_l3_cut_spills_to_l1_at_sol_not_elsewhere(self):
        lines, _ = here("40.4169,-3.7035", "lavapies-cerrada.json", radius=250)  # Sol
        self.assertEqual(lines["L1"]["status"], "amarillo")
        self.assertEqual(lines["L1"].get("spill_from"), "L3")

    def test_l3_cut_does_not_spill_onto_buses(self):
        lines, h = here(LAVAPIES, "lavapies-cerrada.json", radius=600)
        buses = [l for l in lines.values() if l["mode"] in ("emt", "interurbanos", "urbanos")]
        self.assertTrue(buses)
        self.assertTrue(all(l["status"] == "verde" and not l.get("spill_from") for l in buses))
        self.assertEqual(lines["L3"]["status"], "rojo")
        self.assertTrue(all(s["mode"] in ("metro", "metro_ligero", "cercanias") for s in h["spill"]))

    def test_loop_cut_goes_the_short_way_round(self):
        lines, _ = here("40.30993,-3.73403", "metrosur-corte.json", radius=300)  # Getafe Central
        self.assertEqual(lines["L12"]["status"], "verde")
        self.assertFalse(lines["L12"]["incidents"][0]["touches_you"])

    def test_loop_cut_across_the_seam_is_red_inside_it(self):
        # Puerta del Sur is last in the stored list and Parque Lisboa first: the cut crosses the seam.
        lines, _ = here("40.3466,-3.8277", "metrosur-corte-seam.json", radius=600)  # Alcorcón Central
        self.assertEqual(lines["L12"]["status"], "rojo")
        self.assertTrue(lines["L12"]["incidents"][0]["touches_you"])
        lines, _ = here("40.30993,-3.73403", "metrosur-corte-seam.json", radius=300)  # Getafe Central
        self.assertEqual(lines["L12"]["status"], "verde")

    def test_cercanias_strike_in_alcorcon_warns_metro(self):
        lines, h = here(ALCORCON, "huelga-cercanias.json", radius=900)
        self.assertEqual(lines["C5"]["status"], "rojo")
        self.assertTrue(any("Cercanías" in f for f in h["forecast"]))

    def test_cercanias_strike_is_irrelevant_where_there_is_no_cercanias(self):
        lines, h = here(PLAZA_CASTILLA, "huelga-cercanias.json", radius=300)
        self.assertNotIn("C1", lines)
        self.assertTrue(all(l["status"] == "verde" for l in lines.values() if l["mode"] == "metro"))


class Expiry(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("estado", HERE / "estado.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        import datetime as dt
        self.dt = dt
        self.now = dt.datetime(2026, 10, 31, 20, 0, tzinfo=dt.timezone.utc)

    def until(self, text, since=None):
        return self.m.expiry(text, since, self.now)

    def test_range(self):
        self.assertEqual(self.until("del 10 al 12 de octubre no hay trenes").day, 12)

    def test_weekend_list(self):
        u = self.until("los fines de semana del 3-4, 17-18 y 24-25 de octubre entre las 00:30h y las 10:00h")
        self.assertEqual((u.month, u.day), (10, 25))

    def test_hasta(self):
        u = self.until("no para en Puerta de Toledo hasta el 4 de noviembre")
        self.assertEqual((u.month, u.day), (11, 4))

    def test_end_of_year(self):
        self.assertEqual(self.until("desde el 28 de marzo de 2026 y hasta finales de año").month, 12)

    def test_hoy_expires_same_day(self):
        since = self.dt.datetime(2026, 10, 7, 7, 0, tzinfo=self.dt.timezone.utc)
        self.assertEqual(self.until("tren que hoy inicia su recorrido en Tres Cantos", since).day, 7)

    def test_time_range_on_the_day(self):
        u = self.until("El 1 de noviembre de 11:30 a 14:30 horas aproximadamente, las líneas 001, 002")
        self.assertEqual(u.isoformat(), "2026-11-01T13:30:00+00:00")   # 14:30 Madrid, winter time

    def test_source_until_not_overwritten(self):
        inc = {"text": "El 4 de noviembre las líneas 1 y 2 sufrirán desvíos", "since": None,
               "until": "2026-11-04T10:00:00+00:00"}
        self.m.still_active(inc, self.now)
        self.assertEqual(inc["until"], "2026-11-04T10:00:00+00:00")

    def test_text_hours_narrow_a_date_only_until(self):
        inc = {"text": "El 1 de noviembre de 11:30 a 14:30 horas, desvíos", "since": None,
               "until": "2026-11-01T22:59:00+00:00"}
        self.m.still_active(inc, self.now)
        self.assertEqual(inc["until"], "2026-11-01T13:30:00+00:00")

    def test_window_start_with_hours(self):
        s = self.m.window_start("El domingo 1 de noviembre de 11:30 a 14:30 horas, por manifestación", self.now)
        self.assertEqual(s.isoformat(), "2026-11-01T10:30:00+00:00")

    def test_window_start_desde(self):
        s = self.m.window_start("Desde el 28 de marzo de 2026 y hasta finales de año, quedará interrumpido", self.now)
        local = s.astimezone(self.m.MADRID)
        self.assertEqual((local.year, local.month, local.day, local.hour), (2026, 3, 28, 0))

    def test_open_ended(self):
        self.assertIsNone(self.until("Suspensión METRO L10 Cuzco - Nuevos Ministerios"))

    def test_recovery_is_dropped(self):
        inc = {"text": "#MadC3 #MadC4 Los trenes recuperan sus frecuencias de paso habituales.", "since": None}
        self.assertFalse(self.m.still_active(inc, self.now))

    def test_past_notice_is_dropped(self):
        inc = {"text": "#MadC8b del 10 al 12 de octubre, los trenes no prestan servicio", "since": "2026-10-01T17:00:00+00:00"}
        self.assertFalse(self.m.still_active(inc, self.now))



class Episodes(unittest.TestCase):
    def setUp(self):
        import importlib.util, datetime as dt
        spec = importlib.util.spec_from_file_location("episodes", HERE / "episodes.py")
        self.ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ep)
        self.dt = dt
        self.t0 = dt.datetime(2026, 11, 2, 8, 0, tzinfo=dt.timezone.utc)

    def inc(self, mode, lines, sev, scope=None, until=None, since=None):
        return {"mode": mode, "lines": lines, "severity": sev, "scope": scope, "until": until, "since": since,
                "source": "CRTM", "title": "t", "text": "t"}

    def at(self, minutes):
        return self.t0 + self.dt.timedelta(minutes=minutes)

    def test_opens_and_closes(self):
        state = {}
        cut = self.inc("metro", ["L3"], "rojo", {"from": "Embajadores", "to": "Sol"})
        self.assertEqual(len(self.ep.advance(state, self.at(0), [cut])["opened"]), 1)
        self.assertEqual(list(state)[0], ("metro:L3", "Embajadores", "Sol"))
        self.assertEqual(self.ep.advance(state, self.at(5), [cut])["closed"], [])
        closed = self.ep.advance(state, self.at(30), [])["closed"]       # gone for 25 min: past the gap
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["ended"], self.at(5).isoformat())
        self.assertEqual(state, {})

    def test_short_gap_does_not_close(self):
        state = {}
        cut = self.inc("metro", ["L3"], "rojo")
        self.ep.advance(state, self.at(0), [cut])
        self.assertEqual(self.ep.advance(state, self.at(10), [])["closed"], [])   # one missed run
        self.assertEqual(len(self.ep.advance(state, self.at(15), [cut])["updated"]), 1)
        self.assertEqual(len(state), 1)

    def test_new_stretch_is_new_episode(self):
        state = {}
        self.ep.advance(state, self.at(0), [self.inc("metro", ["L3"], "rojo", {"from": "Embajadores", "to": "Sol"})])
        ch = self.ep.advance(state, self.at(5), [self.inc("metro", ["L3"], "rojo", {"from": "Sol", "to": "Moncloa"})])
        self.assertEqual(len(ch["opened"]), 1)
        self.assertEqual(len(state), 2)

    def test_worst_severity_kept(self):
        state = {}
        self.ep.advance(state, self.at(0), [self.inc("cercanias", ["C5"], "amarillo")])
        self.ep.advance(state, self.at(5), [self.inc("cercanias", ["C5"], "rojo")])
        self.ep.advance(state, self.at(10), [self.inc("cercanias", ["C5"], "amarillo")])
        self.assertEqual(list(state.values())[0]["severity"], "Rojo")

    def test_until_closes_at_until(self):
        state = {}
        until = self.at(60).isoformat()
        self.ep.advance(state, self.at(0), [self.inc("emt", ["27"], "amarillo", until=until)])
        closed = self.ep.advance(state, self.at(120), [])["closed"]
        self.assertEqual(closed[0]["ended"], until)

    def test_one_notice_many_lines(self):
        state = {}
        ch = self.ep.advance(state, self.at(0), [self.inc("emt", ["1", "2", "C1"], "amarillo")])
        self.assertEqual(sorted(e["line"] for e in ch["opened"]), ["emt:1", "emt:2", "emt:C1"])

    def test_since_sets_started(self):
        state = {}
        since = self.at(-30).isoformat()
        ch = self.ep.advance(state, self.at(0), [self.inc("cercanias", ["C4"], "amarillo", since=since)])
        self.assertEqual(ch["opened"][0]["started"], since)



class EmtNotices(unittest.TestCase):
    def setUp(self):
        import importlib.util, datetime as dt
        spec = importlib.util.spec_from_file_location("estado", HERE / "estado.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.now = dt.datetime(2026, 11, 1, 17, 0, tzinfo=dt.timezone.utc)

    def test_channel_header_dropped(self):
        item = {"title": "Canal de Noticias de la Empresa Municipal de Transportes de Madrid",
                "description": "Informaciones de interés para el cliente de EMT de Madrid"}
        self.assertIsNone(self.m.emt_notice(item, "27", self.now))

    def test_information_dropped(self):
        for title in ("Refuerzo de líneas: Festividad Todos los Santos", "Cambio Horario Invierno: Ajustes horarios",
                      "Cambio de Horarios. Afectada línea 17 de EMT.", "Servicio Especial: SE [Canillejas – Estadio Metropolitano]."):
            self.assertIsNone(self.m.emt_notice({"title": title, "description": "x", "guid": title}, "27", self.now), title)

    def test_same_notice_twice_is_merged(self):
        a = {"guid": "a", "title": "Supresión de Parada: Paseo de la Castellana.", "since": "s", "until": "u", "lines": ["7", "14"]}
        b = {"guid": "b", "title": "Supresión de Parada: Paseo de la Castellana.", "since": "s", "until": "u", "lines": ["14", "27"]}
        out = self.m.merge_same_notice([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["lines"], ["7", "14", "27"])

    def test_detour_kept_with_its_lines(self):
        item = {"guid": "g1", "title": "Manifestación en Princesa y Gran Vía. Afectadas 27 líneas de EMT.",
                "description": "las líneas 001, 002, 1, 2, C1 y C2 sufrirán retenciones.",
                "GoogleTransitEffect": "05 - DESVÍO PROGRAMADO", "rssAfectaDesde": "2026-11-01 11:30:00"}
        n = self.m.emt_notice(item, "27", self.now)
        self.assertEqual(n["severity"], "amarillo")
        self.assertTrue({"001", "1", "C2", "27"} <= set(n["lines"]))


if __name__ == "__main__":
    unittest.main()
