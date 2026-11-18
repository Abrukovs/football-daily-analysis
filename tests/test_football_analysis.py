import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import football_analysis as fa


class FootballAnalysisSafetyTests(unittest.TestCase):
    def test_append_journal_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "post_match_journal.csv"
            event = fa.Event("fixture-1", "soccer_epl", datetime(2026, 11, 19, tzinfo=timezone.utc), "Home", "Away", "EPL", {"ML: Home": 2.0}, "The Odds API")
            pick = fa.Pick(event, "ML", "Home", 2.0, 0.6, [], [])
            checked = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
            with patch.object(fa, "JOURNAL", journal):
                fa.append_journal([pick], checked)
                fa.append_journal([pick], checked)
            with journal.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["prediction_id"], fa.pick_id(pick))

    def test_unknown_market_is_not_marked_as_a_loss(self):
        fields = ["prediction_id", "report_date", "match_date", "fixture_id", "home", "away", "market", "selection", "odds", "checked_at_utc", "status", "home_goals", "away_goals", "profit_units", "source"]
        row = {
            "prediction_id": "id-1", "report_date": "2026-11-18", "match_date": "2026-11-19", "fixture_id": "fixture-1",
            "home": "Home", "away": "Away", "market": "unrecognized market", "selection": "something",
            "odds": "2.0", "checked_at_utc": "2026-11-18T12:00:00+00:00", "status": "PENDING",
            "home_goals": "", "away_goals": "", "profit_units": "", "source": "test",
        }
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "post_match_journal.csv"
            results = Path(directory) / "results.json"
            with journal.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            results.write_text(json.dumps([{"fixture_id": "fixture-1", "home_goals": 2, "away_goals": 0}]), encoding="utf-8")
            with patch.object(fa, "JOURNAL", journal):
                fa.settle(results)
            with journal.open(newline="", encoding="utf-8") as fh:
                updated = next(csv.DictReader(fh))
            self.assertEqual(updated["status"], "PENDING")
            self.assertEqual(updated["profit_units"], "")

    def test_totals_settlement_accepts_decimal_point_and_comma(self):
        self.assertTrue(fa.settlement_outcome("ТБ 2.5", "ТБ 2.5", "Home", "Away", 1, 2))
        self.assertTrue(fa.settlement_outcome("ТБ 2,5", "ТБ 2,5", "Home", "Away", 1, 2))
        self.assertTrue(fa.settlement_outcome("ТМ 3.5", "ТМ 3.5", "Home", "Away", 1, 2))
        self.assertFalse(fa.settlement_outcome("Over 2.5", "Over 2.5", "Home", "Away", 1, 1))


if __name__ == "__main__":
    unittest.main()
