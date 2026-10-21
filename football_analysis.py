#!/usr/bin/env python3
"""Daily football screening, Markdown reports, and post-match ROI journal.

The script deliberately produces a statistical screen, not a guaranteed forecast.
Required environment variables for `daily`: API_FOOTBALL_KEY and ODDS_API_KEY.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
ROOT = Path(__file__).resolve().parent
JOURNAL = ROOT / "post_match_journal.csv"
MIN_ODDS = float(os.getenv("MIN_ODDS", "1.50"))
MAX_PICKS = int(os.getenv("MAX_PICKS", "2"))
WINDOW_HOURS = int(os.getenv("WINDOW_HOURS", "24"))
ODDS_SPORT_KEYS = [x.strip() for x in os.getenv(
    "ODDS_SPORT_KEYS",
    "soccer_epl,soccer_uefa_champs_league,soccer_uefa_europa_league,"
    "soccer_uefa_europa_conference_league,soccer_spain_la_liga,"
    "soccer_italy_serie_a,soccer_germany_bundesliga,soccer_france_ligue_one,"
    "soccer_russia_premier_league",
).split(",") if x.strip()]


def fail(message: str) -> None:
    raise SystemExit(f"Ошибка: {message}")


def get_json(url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> Any:
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    req = Request(url, headers={"User-Agent": "football-daily-analysis/1.0", **(headers or {})})
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except Exception as exc:
        raise RuntimeError(f"API request failed: {url}: {exc}") from exc


@dataclass
class Event:
    event_id: str
    sport_key: str
    commence_time: datetime
    home: str
    away: str
    league: str
    odds: dict[str, float]
    odds_source: str


@dataclass
class Pick:
    event: Event
    market: str
    selection: str
    odds: float
    estimated_probability: float
    reasons: list[str]
    risks: list[str]


class OddsApi:
    def __init__(self, key: str):
        self.key = key

    def upcoming(self) -> list[Event]:
        events: list[Event] = []
        for sport in ODDS_SPORT_KEYS:
            data = get_json(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                params={"apiKey": self.key, "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal"},
            )
            for raw in data:
                when = datetime.fromisoformat(raw["commence_time"].replace("Z", "+00:00"))
                odds = self._best_odds(raw.get("bookmakers", []))
                events.append(Event(str(raw["id"]), sport, when, raw["home_team"], raw["away_team"], sport, odds, "The Odds API"))
        return events

    @staticmethod
    def _best_odds(bookmakers: list[dict[str, Any]]) -> dict[str, float]:
        best: dict[str, float] = {}
        for bookmaker in bookmakers:
            for market in bookmaker.get("markets", []):
                key = market.get("key")
                for outcome in market.get("outcomes", []):
                    name, price = str(outcome.get("name")), float(outcome.get("price", 0))
                    if key == "h2h":
                        best[f"ML:{name}"] = max(best.get(f"ML:{name}", 0), price)
                    elif key == "totals" and str(outcome.get("point")) in {"2.5", "3.5"}:
                        prefix = "OVER" if name.lower() == "over" else "UNDER"
                        best[f"{prefix}:{outcome['point']}"] = max(best.get(f"{prefix}:{outcome['point']}", 0), price)
        return best


class FootballApi:
    BASE = "https://v3.football.api-sports.io"

    def __init__(self, key: str):
        self.key = key
        self.cache: dict[str, Any] = {}

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        data = get_json(self.BASE + path, headers={"x-apisports-key": self.key}, params=params)
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            raise RuntimeError(f"API-Football: {errors}")
        return data.get("response", [])

    def team_id(self, name: str) -> int | None:
        if name not in self.cache:
            rows = self._get("/teams", {"search": name})
            self.cache[name] = rows[0]["team"]["id"] if rows else None
        return self.cache[name]

    def recent(self, name: str, last: int = 5) -> list[dict[str, Any]]:
        tid = self.team_id(name)
        if not tid:
            return []
        return self._get("/fixtures", {"team": tid, "last": last, "status": "FT-AET-PEN"})

    @staticmethod
    def form(rows: list[dict[str, Any]], team: str) -> dict[str, float]:
        wins = draws = losses = goals_for = goals_against = 0
        for row in rows:
            teams, goals = row["teams"], row["goals"]
            is_home = teams["home"]["name"].casefold() == team.casefold()
            gf = goals["home"] if is_home else goals["away"]
            ga = goals["away"] if is_home else goals["home"]
            goals_for += int(gf or 0); goals_against += int(ga or 0)
            if gf > ga: wins += 1
            elif gf == ga: draws += 1
            else: losses += 1
        n = max(len(rows), 1)
        return {"wins": wins, "draws": draws, "losses": losses, "gf": goals_for / n, "ga": goals_against / n, "win_rate": wins / n}


def candidates(event: Event, football: FootballApi) -> list[Pick]:
    home_form = football.form(football.recent(event.home), event.home)
    away_form = football.form(football.recent(event.away), event.away)
    picks: list[Pick] = []
    for key, price in event.odds.items():
        if price < MIN_ODDS:
            continue
        implied = 1 / price
        if key.startswith("ML:"):
            selection = key.split(":", 1)[1]
            is_home = selection.casefold() == event.home.casefold()
            advantage = (home_form["win_rate"] - away_form["win_rate"]) if is_home else (away_form["win_rate"] - home_form["win_rate"])
            estimated = max(0.01, min(0.95, implied + advantage * 0.30))
            if advantage >= 0.20 and estimated - implied >= 0.04:
                picks.append(Pick(event, "ML", selection, price, estimated,
                    [f"Форма последних матчей: {event.home} {home_form['wins']}-{home_form['draws']}-{home_form['losses']}; {event.away} {away_form['wins']}-{away_form['draws']}-{away_form['losses']}.",
                     f"Преимущество по доле побед в доступной выборке: {advantage:.0%}.",
                     f"Коэффициент {price:.2f}, грубая implied probability {implied:.1%}."],
                    ["Автоматическая проверка не учитывает подтверждённые составы, травмы и свежие новости.", "Малый объём последних матчей может искажать форму."]))
        elif key.startswith("OVER:") or key.startswith("UNDER:"):
            line = key.split(":", 1)[1]
            avg_goals = home_form["gf"] + home_form["ga"] + away_form["gf"] + away_form["ga"]
            over = key.startswith("OVER:")
            estimated = 0.50 + (avg_goals - float(line)) * (0.08 if over else -0.08)
            estimated = max(0.01, min(0.95, estimated))
            if estimated - implied >= 0.04:
                label = ("ТБ " if over else "ТМ ") + line
                picks.append(Pick(event, label, label, price, estimated,
                    [f"Средний суммарный темп голов по последней выборке: {avg_goals:.2f}.", f"Коэффициент {price:.2f}, грубая implied probability {implied:.1%}.", "Сигнал основан на форме и голах, а не на одном крупном счёте."],
                    ["Нет автоматической проверки составов и тактики.", "История результатов не гарантирует будущую результативность."]))
    return picks


def pick_id(pick: Pick) -> str:
    raw = f"{pick.event.event_id}|{pick.market}|{pick.selection}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def write_report(picks: list[Pick], checked: datetime, output: Path) -> None:
    date = checked.astimezone(MSK).date().isoformat()
    lines = [f"# Ежедневный футбольный анализ — {checked.astimezone(MSK):%d.%m.%Y}",
             f"**Время проверки:** {checked.astimezone(MSK):%d.%m.%Y %H:%M} MSK.",
             f"**Окно матчей:** ближайшие {WINDOW_HOURS} часов.",
             "**Разрешённые рынки:** ML, ТБ 2,5, ТМ 3,5, ОЗ.", f"**Отобрано:** {len(picks)} вариант(а/ов).", "",
             "> Это автоматический статистический скрининг, а не гарантия исхода. Составы и новости требуют ручной проверки. Ставки не совершаются.", ""]
    if not picks:
        lines += ["## Итог", "**Подходящих вариантов нет.** Доступные данные не дали достаточного запаса над грубой рыночной вероятностью при установленном пороге коэффициента."]
    for i, pick in enumerate(picks, 1):
        e = pick.event
        lines += [f"## Вариант {i} — {e.home} — {e.away}: {pick.selection}", "| Поле | Данные |", "|---|---|",
                  f"| Турнир | {e.league} |", f"| Начало | {e.commence_time.astimezone(MSK):%d.%m.%Y %H:%M} MSK |",
                  f"| Рынок | **{pick.market}: {pick.selection}** |", f"| Коэффициент | **{pick.odds:.2f}** |",
                  f"| Проверено | {checked.astimezone(MSK):%d.%m.%Y %H:%M} MSK |", "", "### Аргументы"]
        lines += [f"{n}. {reason}" for n, reason in enumerate(pick.reasons, 1)] + ["", "### Главные риски"]
        lines += [f"- {risk}" for risk in pick.risks] + ["", f"**ID варианта:** `{pick_id(pick)}`", ""]
    output.write_text("\n".join(lines), encoding="utf-8")
    append_journal(picks, checked)


def append_journal(picks: list[Pick], checked: datetime) -> None:
    fields = ["prediction_id", "report_date", "match_date", "fixture_id", "home", "away", "market", "selection", "odds", "checked_at_utc", "status", "home_goals", "away_goals", "profit_units", "source"]
    exists = JOURNAL.exists()
    with JOURNAL.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if not exists: writer.writeheader()
        for pick in picks:
            writer.writerow({"prediction_id": pick_id(pick), "report_date": checked.astimezone(MSK).date().isoformat(), "match_date": pick.event.commence_time.astimezone(MSK).date().isoformat(), "fixture_id": pick.event.event_id, "home": pick.event.home, "away": pick.event.away, "market": pick.market, "selection": pick.selection, "odds": f"{pick.odds:.4f}", "checked_at_utc": checked.astimezone(UTC).isoformat(), "status": "PENDING", "home_goals": "", "away_goals": "", "profit_units": "", "source": pick.event.odds_source})


def settle(results_file: Path) -> None:
    if not JOURNAL.exists(): fail("post_match_journal.csv не найден")
    results = {str(x["fixture_id"]): x for x in json.loads(results_file.read_text(encoding="utf-8"))}
    with JOURNAL.open(newline="", encoding="utf-8") as fh: rows = list(csv.DictReader(fh))
    for row in rows:
        if row["status"] != "PENDING" or row["fixture_id"] not in results: continue
        result = results[row["fixture_id"]]; hg, ag = int(result["home_goals"]), int(result["away_goals"]); row["home_goals"], row["away_goals"] = str(hg), str(ag)
        market, selection = row["market"], row["selection"]; won = False
        if market == "ML": won = (hg > ag if selection.casefold() == row["home"].casefold() else ag > hg)
        elif market == "ТБ 2,5": won = hg + ag >= 3
        elif market == "ТМ 3,5": won = hg + ag <= 3
        elif market == "ОЗ: Да": won = hg > 0 and ag > 0
        elif market == "ОЗ: Нет": won = hg == 0 or ag == 0
        row["status"] = "WON" if won else "LOST"; row["profit_units"] = f"{float(row['odds']) - 1:.4f}" if won else "-1.0000"
    with JOURNAL.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    settled = [r for r in rows if r["status"] in {"WON", "LOST", "VOID"}]
    profit = sum(float(r["profit_units"]) for r in settled)
    stakes = len(settled)
    wins = sum(r["status"] == "WON" for r in settled)
    print(json.dumps({"settled": stakes, "wins": wins, "losses": sum(r["status"] == "LOST" for r in settled), "profit_units": round(profit, 4), "roi_percent": round(profit / stakes * 100, 2) if stakes else None}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("daily", help="получить ближайшие матчи, отобрать варианты и создать отчёт")
    daily.add_argument("--output", type=Path)
    settle_cmd = sub.add_parser("settle", help="заполнить результаты из JSON и вывести ROI")
    settle_cmd.add_argument("--results", type=Path, required=True, help='JSON: [{"fixture_id":"...","home_goals":2,"away_goals":1}]')
    args = parser.parse_args()
    if args.command == "settle": settle(args.results); return
    odds_key, football_key = os.getenv("ODDS_API_KEY"), os.getenv("API_FOOTBALL_KEY")
    if not odds_key or not football_key: fail("для daily нужны ODDS_API_KEY и API_FOOTBALL_KEY")
    now = datetime.now(UTC); end = now + timedelta(hours=WINDOW_HOURS)
    odds = OddsApi(odds_key); football = FootballApi(football_key)
    events = [e for e in odds.upcoming() if now <= e.commence_time <= end]
    all_picks = [p for e in events for p in candidates(e, football)]
    all_picks.sort(key=lambda p: p.estimated_probability - 1 / p.odds, reverse=True)
    selected: list[Pick] = []
    used_events: set[str] = set()
    for pick in all_picks:
        if pick.event.event_id not in used_events:
            selected.append(pick); used_events.add(pick.event.event_id)
        if len(selected) == MAX_PICKS: break
    output = args.output or ROOT / f"daily_football_report_{now.astimezone(MSK):%Y-%m-%d}.md"
    write_report(selected, now, output)
    print(f"Создан отчёт: {output}")
    print(f"Кандидатов: {len(events)}, отобрано: {len(selected)}")


if __name__ == "__main__":
    main()
