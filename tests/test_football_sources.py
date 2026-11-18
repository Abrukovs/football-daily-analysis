import unittest
from unittest.mock import patch

from football_sources import (
    ESPN_LEAGUES,
    FootballSources,
    find_result,
    local_match_date,
    parse_espn_event,
    parse_football_data_matches,
    parse_thesportsdb_events,
)


class FootballSourceParsingTests(unittest.TestCase):
    def test_espn_requires_completed_event_and_reads_scores(self):
        event = {
            "date": "2026-11-17T19:00Z",
            "competitions": [{
                "status": {"type": {"state": "post", "completed": True}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Belize"}, "score": "1"},
                    {"homeAway": "away", "team": {"displayName": "Sint Maarten"}, "score": "2"},
                ],
            }],
        }
        row = parse_espn_event(event)
        self.assertEqual((row["home"], row["away"], row["home_goals"], row["away_goals"]), ("Belize", "Sint Maarten", 1, 2))
        event["competitions"][0]["status"]["type"]["completed"] = False
        event["competitions"][0]["status"]["type"]["state"] = "pre"
        self.assertIsNone(parse_espn_event(event))

    def test_thesportsdb_only_accepts_finished_matches(self):
        events = [
            {"strStatus": "FT", "strPostponed": "no", "strHomeTeam": "A", "strAwayTeam": "B", "intHomeScore": "0", "intAwayScore": "0", "dateEvent": "2026-11-17"},
            {"strStatus": "NS", "strPostponed": "no", "strHomeTeam": "C", "strAwayTeam": "D", "intHomeScore": None, "intAwayScore": None, "dateEvent": "2026-11-17"},
            {"strStatus": "FT", "strPostponed": "yes", "strHomeTeam": "E", "strAwayTeam": "F", "intHomeScore": "3", "intAwayScore": "1", "dateEvent": "2026-11-17"},
        ]
        rows = parse_thesportsdb_events(events)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["home_goals"], rows[0]["away_goals"]), (0, 0))

    def test_football_data_accepts_finished_final_score_only(self):
        matches = [
            {"status": "FINISHED", "utcDate": "2026-11-17T20:00:00Z", "homeTeam": {"name": "Home"}, "awayTeam": {"name": "Away"}, "score": {"fullTime": {"home": 2, "away": 1}}},
            {"status": "IN_PLAY", "utcDate": "2026-11-17T21:00:00Z", "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}, "score": {"fullTime": {"home": None, "away": None}}},
        ]
        rows = parse_football_data_matches(matches)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "football-data.org")

    def test_match_must_be_unique_and_ordered_home_away(self):
        records = [
            {"home": "São Paulo FC", "away": "Corinthians", "home_goals": 2, "away_goals": 0, "event_date": "2026-11-17T19:00Z", "source": "ESPN"},
        ]
        self.assertEqual(find_result(records, "Sao Paulo — Corinthians")["home_goals"], 2)
        self.assertIsNone(find_result(records, "Corinthians — Sao Paulo"))
        self.assertIsNone(find_result(records + [dict(records[0], home_goals=1)], "Sao Paulo — Corinthians"))

    def test_expected_date_uses_moscow_calendar_day(self):
        record = {"home": "A", "away": "B", "home_goals": 1, "away_goals": 0, "event_date": "2026-11-17T23:00Z", "source": "ESPN"}
        self.assertEqual(local_match_date(record), "2026-11-18")
        self.assertIsNone(find_result([record], "A — B", expected_date="2026-11-17"))
        self.assertEqual(find_result([record], "A — B", expected_date="2026-11-18"), record)

    def test_russian_premier_league_is_not_in_espn_source_map_or_allowlist(self):
        from football_analysis import APPROVED_ODDS_SPORT_KEYS, configured_sport_keys
        self.assertNotIn("soccer_russia_premier_league", ESPN_LEAGUES)
        selected, ignored = configured_sport_keys(",".join(APPROVED_ODDS_SPORT_KEYS) + ",soccer_russia_premier_league,soccer_fake")
        self.assertEqual(selected, list(APPROVED_ODDS_SPORT_KEYS))
        self.assertEqual(ignored, ["soccer_russia_premier_league", "soccer_fake"])

    def test_thesportsdb_is_recent_form_fallback_when_espn_has_too_few(self):
        import datetime
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        events = [{
            "strStatus": "FT", "strPostponed": "no", "strHomeTeam": "Harbor FC" if i % 2 == 0 else f"Guest {i}",
            "strAwayTeam": f"Guest {i}" if i % 2 == 0 else "Harbor FC", "intHomeScore": "2", "intAwayScore": "1", "dateEvent": today,
        } for i in range(5)]
        source = FootballSources()
        with patch.object(FootballSources, "_recent_espn", return_value=[]), \
             patch.object(FootballSources, "_request_json", return_value={"events": events}), \
             patch.object(FootballSources, "_throttle", return_value=0):
            rows = source.recent("Harbor FC", "soccer_epl", last=5)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["source"] == "TheSportsDB" for row in rows))

    def test_espn_recent_form_keeps_league_schedules_separate(self):
        teams_payload = {"sports": [{"leagues": [{"teams": [{"team": {"id": "364", "displayName": "Liverpool"}}]}]}]}
        epl_schedule = {"events": [{
            "date": "2026-11-08T14:00Z",
            "competitions": [{
                "status": {"type": {"state": "post", "completed": True}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Crystal Palace"}, "score": {"value": 1}},
                    {"homeAway": "away", "team": {"displayName": "Liverpool"}, "score": {"value": 3}},
                ],
            }],
        }]}
        ucl_schedule = {"events": [{
            "date": "2026-11-04T20:00Z",
            "competitions": [{
                "status": {"type": {"state": "post", "completed": True}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Liverpool"}, "score": {"value": 2}},
                    {"homeAway": "away", "team": {"displayName": "Real Madrid"}, "score": {"value": 0}},
                ],
            }],
        }]}
        source = FootballSources()
        with patch.object(FootballSources, "_request_json", side_effect=[teams_payload, epl_schedule, teams_payload, ucl_schedule]) as request, \
             patch.object(FootballSources, "_recent_thesportsdb", return_value=[]):
            epl_rows = source.recent("Liverpool", "soccer_epl")
            ucl_rows = source.recent("Liverpool", "soccer_uefa_champs_league")
        self.assertEqual(epl_rows[0]["away_goals"], 3)
        self.assertEqual(ucl_rows[0]["home_goals"], 2)
        self.assertEqual(request.call_count, 4)


if __name__ == "__main__":
    unittest.main()
