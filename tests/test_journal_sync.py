import unittest
from collections import Counter
import csv
import tempfile
from pathlib import Path
from unittest.mock import patch

import journal_sync as js
from journal_sync import apply_results, canonical_market, correct_pending_dates, outcome


class JournalDateSafetyTests(unittest.TestCase):
    def test_settlement_rejects_same_fixture_on_wrong_local_date(self):
        row = {"date": "2026-11-17", "match": "Home — Away", "market": "ML: Home", "result": "PENDING"}
        result = {"home": "Home", "away": "Away", "home_goals": 1, "away_goals": 0, "event_date": "2026-11-17T23:00Z", "source": "ESPN"}
        source_counts = Counter()
        changed = apply_results([row], [result], source_counts, expected_date="2026-11-17")
        self.assertEqual(changed, 0)
        self.assertEqual(row["result"], "PENDING")
        changed = apply_results([row], [result], source_counts, expected_date="2026-11-18")
        self.assertEqual(changed, 1)
        self.assertEqual(row["result"], "WON")

    def test_totals_market_accepts_decimal_point_and_comma(self):
        self.assertEqual(canonical_market("ТБ 2.5"), "Over 2.5")
        self.assertEqual(canonical_market("ТБ 2,5"), "Over 2.5")
        self.assertTrue(outcome("ТБ 2.5", "Home — Away", 1, 2))
        self.assertTrue(outcome("ТМ 3,5", "Home — Away", 1, 2))

    def test_parser_extracts_kickoff_date_from_current_and_legacy_reports(self):
        report = """# Daily report

## Вариант 1 — Home FC — Away FC: Home FC
| Поле | Данные |
|---|---|
| Турнир | League |
| Начало | 18.11.2026 00:30 MSK |
| Рынок | **ML: Home FC** |

## Вариант 2 — Alpha — Beta
- **Турнир:** Cup.
- **Начало:** **18 ноября, 22:00 MSK**.
- **Рынок:** ML — победа Alpha.
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "daily_football_report_2026-11-17.md").write_text(report, encoding="utf-8")
            with patch.object(js, "ROOT", root):
                rows = js.parse_reports()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["match"], "Home FC — Away FC")
        self.assertEqual(rows[0]["date"], "2026-11-18")
        self.assertEqual(rows[0]["_report_date"], "2026-11-17")
        self.assertEqual(rows[1]["match"], "Alpha — Beta")
        self.assertEqual(rows[1]["date"], "2026-11-18")

    def test_pending_date_correction_does_not_rewrite_settled_rows(self):
        report = {"_report_date": "2026-11-17", "date": "2026-11-18", "match": "Home — Away", "market": "ML: Home"}
        pending = {"date": "2026-11-17", "match": "Home — Away", "market": "ML: Home", "result": "PENDING"}
        settled = {"date": "2026-11-17", "match": "Home — Away", "market": "ML: Home", "result": "WON"}
        self.assertTrue(correct_pending_dates([pending, settled], [report]))
        self.assertEqual(pending["date"], "2026-11-18")
        self.assertEqual(settled["date"], "2026-11-17")

    def test_sync_does_not_duplicate_existing_report_when_kickoff_date_differs(self):
        report = """## Вариант 1 — Home — Away: Home
| Поле | Данные |
|---|---|
| Турнир | League |
| Начало | 18.11.2026 00:30 MSK |
| Рынок | **ML: Home** |
"""
        fields = ["date", "match", "tournament", "market", "result"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "daily_football_report_2026-11-17.md").write_text(report, encoding="utf-8")
            journal = root / "bets_journal.csv"
            with journal.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                writer.writerow({"date": "2026-11-17", "match": "Home — Away", "tournament": "League", "market": "ML: Home", "result": "WON"})
            with patch.object(js, "ROOT", root), patch.object(js, "SIMPLE", journal):
                self.assertEqual(js.sync_reports(), 0)
            with journal.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-11-17")


if __name__ == "__main__":
    unittest.main()
