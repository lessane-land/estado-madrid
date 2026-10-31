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

    def test_cercanias_strike_in_alcorcon_warns_metro(self):
        lines, h = here(ALCORCON, "huelga-cercanias.json", radius=900)
        self.assertEqual(lines["C5"]["status"], "rojo")
        self.assertTrue(any("Cercanías" in f for f in h["forecast"]))

    def test_cercanias_strike_is_irrelevant_where_there_is_no_cercanias(self):
        lines, h = here(PLAZA_CASTILLA, "huelga-cercanias.json", radius=300)
        self.assertNotIn("C1", lines)
        self.assertTrue(all(l["status"] == "verde" for l in lines.values() if l["mode"] == "metro"))


if __name__ == "__main__":
    unittest.main()
