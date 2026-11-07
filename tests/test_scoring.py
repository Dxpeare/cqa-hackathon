import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cqa.scoring import compute_score, normalize_answer  # noqa: E402

with open(os.path.join(ROOT, "checklists", "chat_v1.json"), encoding="utf-8") as f:
    CHAT = json.load(f)


def answers(**kw):
    base = {"c01": "yes", "c02": "yes", "c03": "yes", "c04": "20", "c05": "yes",
            "c06": "10", "c07": "yes", "c08": "no"}
    base.update(kw)
    return base


class ScoringTest(unittest.TestCase):
    def test_perfect(self):
        s = compute_score(CHAT, answers())
        self.assertEqual(s["total"], 100.0)
        self.assertFalse(s["critical_error"])

    def test_scale_proportional(self):
        # c04 = 15/20 of 20 -> 15; c06 = 7/10 of 15 -> 10.5; loss = 5 + 4.5
        s = compute_score(CHAT, answers(c04="15", c06="7"))
        self.assertEqual(s["total"], 90.5)

    def test_na_redistribution(self):
        # c03 (20) N/A -> applicable 80; c07 "no" loses 5/80*100 = 6.25
        s = compute_score(CHAT, answers(c03="na", c07="no"))
        self.assertEqual(s["total"], 93.75)
        row = [r for r in s["criteria"] if r["criterion_id"] == "c01"][0]
        self.assertEqual(row["max_points"], 12.5)

    def test_rounding_two_digits(self):
        # c02, c07 N/A -> applicable 80; c06=4 -> 4/10*15 = 6 of 15 -> loss 9/80*100 = 11.25
        s = compute_score(CHAT, answers(c02="na", c07="na", c06="4"))
        self.assertEqual(s["total"], 88.75)
        # three N/A -> applicable 60, c01 no: 10/60*100 = 16.666.. -> 83.33
        s = compute_score(CHAT, answers(c02="na", c03="na", c07="na", c04="20", c01="no"))
        self.assertEqual(s["applicable_weight"], 60.0)
        self.assertEqual(s["total"], 83.33)

    def test_critical_flag_no_points(self):
        s = compute_score(CHAT, answers(c08="yes"))
        self.assertTrue(s["critical_error"])
        self.assertEqual(s["total"], 100.0)

    def test_no_applicable(self):
        cl = {"criteria": [{"id": "a", "title": "A", "type": "binary", "weight": 10, "allow_na": True}]}
        s = compute_score(cl, {"a": "na"})
        self.assertIsNone(s["total"])

    def test_normalize(self):
        c04 = [c for c in CHAT["criteria"] if c["id"] == "c04"][0]
        c01 = [c for c in CHAT["criteria"] if c["id"] == "c01"][0]
        self.assertEqual(normalize_answer(c04, 15), "15")
        self.assertEqual(normalize_answer(c04, "N/A"), "na")
        self.assertEqual(normalize_answer(c01, "Да"), "yes")
        with self.assertRaises(ValueError):
            normalize_answer(c01, "na")    # N/A not allowed for c01
        with self.assertRaises(ValueError):
            normalize_answer(c04, "12")    # not a scale step

    def test_sections(self):
        s = compute_score(CHAT, answers(c04="10"))
        sec = {x["section_id"]: x for x in s["sections"]}
        self.assertEqual(sec["safety"]["points"], 30.0)
        self.assertEqual(sec["safety"]["max_points"], 40.0)


if __name__ == "__main__":
    unittest.main()
