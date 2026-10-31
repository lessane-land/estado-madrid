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

    def test_open_ended(self):
        self.assertIsNone(self.until("Suspensión METRO L10 Cuzco - Nuevos Ministerios"))

    def test_recovery_is_dropped(self):
        inc = {"text": "#MadC3 #MadC4 Los trenes recuperan sus frecuencias de paso habituales.", "since": None}
        self.assertFalse(self.m.still_active(inc, self.now))

    def test_past_notice_is_dropped(self):
        inc = {"text": "#MadC8b del 10 al 12 de octubre, los trenes no prestan servicio", "since": "2026-10-01T17:00:00+00:00"}
        self.assertFalse(self.m.still_active(inc, self.now))


if __name__ == "__main__":
    unittest.main()
