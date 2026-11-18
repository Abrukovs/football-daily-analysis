#!/usr/bin/env python3
"""Free football result and recent-form sources used by the daily workflow.

ESPN's public JSON endpoints are undocumented and may change. TheSportsDB's
shared v1 key is the fallback for results and recent form. football-data.org
is an optional additional fallback when FOOTBALL_DATA_TOKEN is configured.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

ESPN_LEAGUES = {
    "soccer_epl": "eng.1",
    "soccer_uefa_champs_league": "uefa.champions",
    "soccer_uefa_europa_league": "uefa.europa",
    "soccer_uefa_europa_conference_league": "uefa.europa.conf",
    "soccer_spain_la_liga": "esp.1",
    "soccer_italy_serie_a": "ita.1",
    "soccer_germany_bundesliga": "ger.1",
    "soccer_france_ligue_one": "fra.1",
}

# The documented free football-data.org tier covers these competitions among
# the Odds API leagues currently used by the repository.
FOOTBALL_DATA_COMPETITIONS = {
    "soccer_epl": "PL",
    "soccer_uefa_champs_league": "CL",
    "soccer_spain_la_liga": "PD",
    "soccer_italy_serie_a": "SA",
    "soccer_germany_bundesliga": "BL1",
    "soccer_france_ligue_one": "FL1",
}


def normalize_team_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def teams_match(left: str, right: str) -> bool:
    a, b = normalize_team_name(left), normalize_team_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # Accept common provider suffix/prefix differences only when the shorter
    # normalized name is specific enough; final selection still requires one
    # unique fixture candidate.
    if min(len(a), len(b)) >= 6 and (a in b or b in a):
        return True
    generic = {"fc", "cf", "sc", "afc", "club", "football"}
    aw = set(a.split()) - generic
    bw = set(b.split()) - generic
    shared = aw & bw
    return len(shared) >= 2 and len(shared) >= min(len(aw), len(bw)) - 1


def parse_score(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("value", value.get("displayValue"))
    if value is None or value == "":
        return None
    try:
        numeric = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return int(numeric) if numeric.is_integer() and numeric >= 0 else None


def _record(home: str, away: str, home_goals: Any, away_goals: Any, event_date: str, source: str) -> dict[str, Any] | None:
    hg, ag = parse_score(home_goals), parse_score(away_goals)
    if not home or not away or hg is None or ag is None:
        return None
    return {
        "home": str(home),
        "away": str(away),
        "home_goals": hg,
        "away_goals": ag,
        "event_date": str(event_date or ""),
        "source": source,
    }


def parse_espn_event(event: dict[str, Any]) -> dict[str, Any] | None:
    competitions = event.get("competitions") or []
    competition = competitions[0] if competitions else {}
    status = competition.get("status") or event.get("status") or {}
    status_type = status.get("type") or {}
    state = str(status_type.get("state", "")).lower()
    if status_type.get("completed") is not True and state not in {"post", "finished", "ft"}:
        return None
    competitors = competition.get("competitors") or event.get("competitors") or []
    home = next((x for x in competitors if x.get("homeAway") == "home"), None)
    away = next((x for x in competitors if x.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    return _record(
        (home.get("team") or {}).get("displayName") or (home.get("team") or {}).get("name", ""),
        (away.get("team") or {}).get("displayName") or (away.get("team") or {}).get("name", ""),
        home.get("score"),
        away.get("score"),
        event.get("date", ""),
        "ESPN",
    )


def parse_espn_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for event in events if (row := parse_espn_event(event)) is not None]


def parse_thesportsdb_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    finished = {"ft", "full time", "finished", "match finished", "aet", "after extra time", "pen", "penalties", "after penalties"}
    rows = []
    for event in events:
        status = normalize_team_name(event.get("strStatus", ""))
        if status not in finished or str(event.get("strPostponed", "no")).lower() == "yes":
            continue
        row = _record(
            event.get("strHomeTeam", ""),
            event.get("strAwayTeam", ""),
            event.get("intHomeScore"),
            event.get("intAwayScore"),
            event.get("dateEvent") or event.get("strTimestamp", ""),
            "TheSportsDB",
        )
        if row:
            rows.append(row)
    return rows


def parse_football_data_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for match in matches:
        if str(match.get("status", "")).upper() not in {"FINISHED", "AWARDED"}:
            continue
        score = (match.get("score") or {}).get("fullTime") or {}
        row = _record(
            (match.get("homeTeam") or {}).get("name", ""),
            (match.get("awayTeam") or {}).get("name", ""),
            score.get("home"),
            score.get("away"),
            match.get("utcDate", ""),
            "football-data.org",
        )
        if row:
            rows.append(row)
    return rows


def local_match_date(record: dict[str, Any]) -> str:
    value = str(record.get("event_date", ""))
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(MSK).date().isoformat()
    except ValueError:
        return value[:10]


def find_result(records: list[dict[str, Any]], match: str, expected_date: str | None = None) -> dict[str, Any] | None:
    parts = re.split(r"\s+[—–-]\s+", match)
    if len(parts) != 2:
        return None
    home, away = parts[0].strip(), parts[1].strip()
    candidates = []
    for record in records:
        if expected_date and local_match_date(record) != expected_date:
            continue
        if teams_match(home, record["home"]) and teams_match(away, record["away"]):
            candidates.append(record)
    unique = {
        (normalize_team_name(x["home"]), normalize_team_name(x["away"]), x["home_goals"], x["away_goals"]): x
        for x in candidates
    }
    return next(iter(unique.values())) if len(unique) == 1 else None


def _date_sort_key(record: dict[str, Any]) -> float:
    value = str(record.get("event_date", ""))
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(value[:10]).replace(tzinfo=UTC).timestamp()
        except ValueError:
            return 0.0


class FootballSources:
    """Thin, cached clients for public scoreboards and optional free APIs."""

    ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
    TSD_BASE = "https://www.thesportsdb.com/api/v1/json"
    FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"

    def __init__(self, football_data_token: str | None = None, thesportsdb_key: str | None = None):
        self.football_data_token = football_data_token or ""
        self.thesportsdb_key = thesportsdb_key or "123"
        self._espn_teams_cache: dict[str, list[dict[str, Any]]] = {}
        self._espn_schedule_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._fdo_competition_cache: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self._tsd_day_cache: dict[str, list[dict[str, Any]]] = {}
        self._recent_cache: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
        self._last_tsd_request = 0.0
        self._last_fdo_request = 0.0

    @staticmethod
    def _request_json(url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> Any:
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        request = Request(url, headers={"User-Agent": "football-daily-analysis/2.0", **(headers or {})})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            parts = urlsplit(url)
            safe_path = re.sub(r"(/json/)[^/]+(/)", r"\1[redacted]\2", parts.path)
            raise RuntimeError(f"{parts.netloc}{safe_path} returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, ValueError) as exc:
            parts = urlsplit(url)
            safe_path = re.sub(r"(/json/)[^/]+(/)", r"\1[redacted]\2", parts.path)
            raise RuntimeError(f"{parts.netloc}{safe_path} request failed ({exc.__class__.__name__})") from None

    @staticmethod
    def _throttle(last_request: float, minimum_seconds: float) -> float:
        now = time.monotonic()
        wait = minimum_seconds - (now - last_request) if last_request else 0.0
        if wait > 0:
            time.sleep(wait)
        return time.monotonic()

    def espn_scores_for_date(self, date_text: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"{self.ESPN_BASE}/all/scoreboard",
            params={"dates": date_text.replace("-", "")},
        )
        return parse_espn_events(payload.get("events", []))

    def thesportsdb_scores_for_date(self, date_text: str) -> list[dict[str, Any]]:
        if date_text in self._tsd_day_cache:
            return self._tsd_day_cache[date_text]
        self._last_tsd_request = self._throttle(self._last_tsd_request, 2.1)
        payload = self._request_json(
            f"{self.TSD_BASE}/{self.thesportsdb_key}/eventsday.php",
            params={"d": date_text, "s": "Soccer"},
        )
        rows = parse_thesportsdb_events(payload.get("events") or [])
        self._tsd_day_cache[date_text] = rows
        return rows

    def football_data_scores_for_range(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        if not self.football_data_token:
            return []
        self._last_fdo_request = self._throttle(self._last_fdo_request, 6.1)
        payload = self._request_json(
            f"{self.FOOTBALL_DATA_BASE}/matches",
            headers={"X-Auth-Token": self.football_data_token},
            params={"dateFrom": date_from, "dateTo": date_to, "limit": 500},
        )
        return parse_football_data_matches(payload.get("matches", []))

    def _espn_teams(self, league_code: str) -> list[dict[str, Any]]:
        if league_code not in self._espn_teams_cache:
            payload = self._request_json(
                f"{self.ESPN_BASE}/{league_code}/teams",
                params={"limit": 1000},
            )
            teams = []
            for sport in payload.get("sports", []):
                for league in sport.get("leagues", []):
                    teams.extend(
                        wrapper.get("team", wrapper)
                        for wrapper in league.get("teams", [])
                    )
            self._espn_teams_cache[league_code] = teams
        return self._espn_teams_cache[league_code]

    @staticmethod
    def _resolve_team(teams: list[dict[str, Any]], requested_name: str) -> dict[str, Any] | None:
        requested = normalize_team_name(requested_name)
        exact = []
        fuzzy = []
        for team in teams:
            aliases = {
                str(team.get(key, ""))
                for key in ("displayName", "name", "shortDisplayName", "nickname", "location")
                if team.get(key)
            }
            if any(normalize_team_name(alias) == requested for alias in aliases):
                exact.append(team)
            elif any(teams_match(requested_name, alias) for alias in aliases):
                fuzzy.append(team)
        unique_exact = {str(t.get("id")): t for t in exact}
        if len(unique_exact) == 1:
            return next(iter(unique_exact.values()))
        unique_fuzzy = {str(t.get("id")): t for t in fuzzy}
        return next(iter(unique_fuzzy.values())) if len(unique_fuzzy) == 1 else None

    def _recent_espn(self, team_name: str, sport_key: str, last: int) -> list[dict[str, Any]]:
        league_code = ESPN_LEAGUES.get(sport_key)
        if not league_code:
            return []
        team = self._resolve_team(self._espn_teams(league_code), team_name)
        if not team or not team.get("id"):
            return []
        team_id = str(team["id"])
        cache_key = (league_code, team_id)
        if cache_key not in self._espn_schedule_cache:
            payload = self._request_json(f"{self.ESPN_BASE}/{league_code}/teams/{team_id}/schedule")
            self._espn_schedule_cache[cache_key] = parse_espn_events(payload.get("events", []))
        rows = [
            row for row in self._espn_schedule_cache[cache_key]
            if teams_match(team_name, row["home"]) or teams_match(team_name, row["away"])
        ]
        rows.sort(key=_date_sort_key, reverse=True)
        return rows[:last]

    def _football_data_competition_matches(self, competition_code: str) -> list[dict[str, Any]]:
        today = datetime.now(MSK).date()
        date_from = (today - timedelta(days=370)).isoformat()
        date_to = today.isoformat()
        cache_key = (competition_code, date_from, date_to)
        if cache_key not in self._fdo_competition_cache:
            self._last_fdo_request = self._throttle(self._last_fdo_request, 6.1)
            payload = self._request_json(
                f"{self.FOOTBALL_DATA_BASE}/competitions/{competition_code}/matches",
                headers={"X-Auth-Token": self.football_data_token},
                params={"dateFrom": date_from, "dateTo": date_to, "status": "FINISHED", "limit": 500},
            )
            self._fdo_competition_cache[cache_key] = parse_football_data_matches(payload.get("matches", []))
        return self._fdo_competition_cache[cache_key]

    def _recent_football_data(self, team_name: str, sport_key: str, last: int) -> list[dict[str, Any]]:
        competition_code = FOOTBALL_DATA_COMPETITIONS.get(sport_key)
        if not competition_code or not self.football_data_token:
            return []
        rows = [
            row for row in self._football_data_competition_matches(competition_code)
            if teams_match(team_name, row["home"]) or teams_match(team_name, row["away"])
        ]
        rows.sort(key=_date_sort_key, reverse=True)
        return rows[:last]

    def _merge_recent(self, primary: list[dict[str, Any]], backup: list[dict[str, Any]], last: int) -> list[dict[str, Any]]:
        merged = {}
        for row in primary + backup:
            key = (
                normalize_team_name(row["home"]),
                normalize_team_name(row["away"]),
                row["home_goals"],
                row["away_goals"],
                local_match_date(row),
            )
            merged.setdefault(key, row)
        rows = list(merged.values())
        rows.sort(key=_date_sort_key, reverse=True)
        return rows[:last]

    def _recent_thesportsdb(self, team_name: str, last: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        today = datetime.now(MSK).date()
        # eventsday is broad rather than team-specific. Walk back up to 60
        # days, caching each day, and stop once enough completed fixtures exist.
        for offset in range(60):
            day = (today - timedelta(days=offset)).isoformat()
            records = self.thesportsdb_scores_for_date(day)
            found.extend(
                row for row in records
                if teams_match(team_name, row["home"]) or teams_match(team_name, row["away"])
            )
            found = self._merge_recent([], found, last)
            if len(found) >= last:
                break
        return found

    def recent(self, team_name: str, sport_key: str, last: int = 5) -> list[dict[str, Any]]:
        cache_key = (sport_key, normalize_team_name(team_name), last)
        if cache_key in self._recent_cache:
            return self._recent_cache[cache_key]
        rows: list[dict[str, Any]] = []
        try:
            rows = self._recent_espn(team_name, sport_key, last)
        except Exception as exc:
            print(f"ESPN form unavailable for {team_name}: {exc}")
        if len(rows) < last:
            try:
                rows = self._merge_recent(rows, self._recent_thesportsdb(team_name, last), last)
            except Exception as exc:
                print(f"TheSportsDB form fallback unavailable for {team_name}: {exc}")
        if len(rows) < last and self.football_data_token:
            try:
                rows = self._merge_recent(rows, self._recent_football_data(team_name, sport_key, last), last)
            except Exception as exc:
                print(f"football-data.org form fallback unavailable for {team_name}: {exc}")
        self._recent_cache[cache_key] = rows
        return rows
