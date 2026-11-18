#!/usr/bin/env python3
"""Daily football screening, Markdown reports, and post-match ROI journal.

Odds require The Odds API key. Recent team form comes from ESPN's public JSON
endpoints, with football-data.org as an optional free-tier fallback.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from football_sources import FootballSources, parse_score, teams_match

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
ROOT = Path(__file__).resolve().parent
JOURNAL = ROOT / "post_match_journal.csv"
MIN_ODDS = float(os.getenv("MIN_ODDS", "1.50"))
MAX_PICKS = int(os.getenv("MAX_PICKS", "2"))
WINDOW_HOURS = int(os.getenv("WINDOW_HOURS", "24"))
APPROVED_ODDS_SPORT_KEYS = (
    "soccer_epl",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
)


def configured_sport_keys(raw: str) -> tuple[list[str], list[str]]:
    requested = list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    approved = set(APPROVED_ODDS_SPORT_KEYS)
    return ([item for item in requested if item in approved], [item for item in requested if item not in approved])


_raw_sports = os.getenv("ODDS_SPORT_KEYS") or ",".join(APPROVED_ODDS_SPORT_KEYS)
ODDS_SPORT_KEYS, IGNORED_ODDS_SPORT_KEYS = configured_sport_keys(_raw_sports)
ODDS_MARKETS = list(dict.fromkeys(
    item.strip() for item in (os.getenv("ODDS_MARKETS") or "h2h,totals").split(",") if item.strip()
))


def fail(message: str) -> None:
    raise SystemExit(f"Ошибка: {message}")


def get_json(url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None) -> Any:
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    req = Request(url, headers={"User-Agent": "football-daily-analysis/2.0", **(headers or {})})
    parts = urlsplit(url)
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"API request failed: {parts.netloc}{parts.path} (HTTP {exc.code})") from None
    except Exception as exc:
        raise RuntimeError(f"API request failed: {parts.netloc}{parts.path} ({exc.__class__.__name__})") from None


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
                params={
                    "apiKey": self.key,
                    "regions": "eu",
                    "markets": ",".join(ODDS_MARKETS),
                    "oddsFormat": "decimal",
                },
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


def form(rows: list[dict[str, Any]], team: str) -> dict[str, float]:
    wins = draws = losses = goals_for = goals_against = 0
    used = 0
    for row in rows:
        home_name, away_name = row["home"], row["away"]
        is_home = teams_match(home_name, team)
        is_away = teams_match(away_name, team)
        if not is_home and not is_away:
            continue
        gf = row["home_goals"] if is_home else row["away_goals"]
        ga = row["away_goals"] if is_home else row["home_goals"]
        goals_for += int(gf)
        goals_against += int(ga)
        used += 1
        if gf > ga:
            wins += 1
        elif gf == ga:
            draws += 1
        else:
            losses += 1
    n = max(used, 1)
    return {"wins": wins, "draws": draws, "losses": losses, "gf": goals_for / n, "ga": goals_against / n, "win_rate": wins / n}


def candidates(event: Event, football: FootballSources) -> list[Pick]:
    home_rows = football.recent(event.home, sport_key=event.sport_key, last=5)
    away_rows = football.recent(event.away, sport_key=event.sport_key, last=5)
    if not home_rows or not away_rows:
        print(f"Форма не найдена для {event.home} — {event.away}; вариант пропущен.")
        return []
    home_form = form(home_rows, event.home)
    away_form = form(away_rows, event.away)
    form_sources = ", ".join(sorted({row.get("source", "неизвестный источник") for row in home_rows + away_rows}))
    picks: list[Pick] = []
    for key, price in event.odds.items():
        if price < MIN_ODDS:
            continue
        implied = 1 / price
        if key.startswith("ML:"):
            selection = key.split(":", 1)[1]
            if teams_match(selection, event.home):
                advantage = home_form["win_rate"] - away_form["win_rate"]
            elif teams_match(selection, event.away):
                advantage = away_form["win_rate"] - home_form["win_rate"]
            else:
                # Do not misclassify a draw price as an away-team selection.
                continue
            estimated = max(0.01, min(0.95, implied + advantage * 0.30))
            if advantage >= 0.20 and estimated - implied >= 0.04:
                picks.append(Pick(event, "ML", selection, price, estimated,
                    [f"Форма последних матчей: {event.home} {home_form['wins']}-{home_form['draws']}-{home_form['losses']}; {event.away} {away_form['wins']}-{away_form['draws']}-{away_form['losses']}.",
                     f"Преимущество по доле побед в доступной выборке: {advantage:.0%}.",
                     f"Коэффициент {price:.2f}, грубая implied probability {implied:.1%}.",
                     f"Источник формы: {form_sources}."],
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
                    [f"Средний суммарный темп голов по последней выборке: {avg_goals:.2f}.", f"Коэффициент {price:.2f}, грубая implied probability {implied:.1%}.", "Сигнал основан на форме и голах, а не на одном крупном счёте.", f"Источник формы: {form_sources}."],
                    ["Нет автоматической проверки составов и тактики.", "История результатов не гарантирует будущую результативность."]))
    return picks


def pick_id(pick: Pick) -> str:
    raw = f"{pick.event.event_id}|{pick.market}|{pick.selection}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def write_report(picks: list[Pick], checked: datetime, output: Path) -> None:
    lines = [f"# Ежедневный футбольный анализ — {checked.astimezone(MSK):%d.%m.%Y}",
             f"**Время проверки:** {checked.astimezone(MSK):%d.%m.%Y %H:%M} MSK.",
             f"**Окно матчей:** ближайшие {WINDOW_HOURS} часов.",
             f"**Источники:** коэффициенты — The Odds API; форма — ESPN, резерв TheSportsDB, затем football-data.org при наличии токена.",
             f"**Рынки:** {', '.join(ODDS_MARKETS)}; **отобрано:** {len(picks)} вариант(а/ов).", "",
             "> Это автоматический статистический скрининг, а не гарантия исхода. ESPN использует публичный недокументированный интерфейс; составы и новости требуют ручной проверки. Ставки не совершаются.", ""]
    if not picks:
        lines += ["## Итог", "**Подходящих вариантов нет.** Доступные данные не дали достаточного запаса над грубой рыночной вероятностью при установленном пороге коэффициента, либо для матча не удалось получить историю формы."]
    for i, pick in enumerate(picks, 1):
        event = pick.event
        lines += [f"## Вариант {i} — {event.home} — {event.away}: {pick.selection}", "| Поле | Данные |", "|---|---|",
                  f"| Турнир | {event.league} |", f"| Начало | {event.commence_time.astimezone(MSK):%d.%m.%Y %H:%M} MSK |",
                  f"| Рынок | **{pick.market}: {pick.selection}** |", f"| Коэффициент | **{pick.odds:.2f}** |",
                  f"| Проверено | {checked.astimezone(MSK):%d.%m.%Y %H:%M} MSK |", "", "### Аргументы"]
        lines += [f"{n}. {reason}" for n, reason in enumerate(pick.reasons, 1)] + ["", "### Главные риски"]
        lines += [f"- {risk}" for risk in pick.risks] + ["", f"**ID варианта:** `{pick_id(pick)}`", ""]
    output.write_text("\n".join(lines), encoding="utf-8")
    append_journal(picks, checked)


def append_journal(picks: list[Pick], checked: datetime) -> None:
    fields = ["prediction_id", "report_date", "match_date", "fixture_id", "home", "away", "market", "selection", "odds", "checked_at_utc", "status", "home_goals", "away_goals", "profit_units", "source"]
    exists = JOURNAL.exists()
    existing_ids: set[str] = set()
    if exists:
        with JOURNAL.open(newline="", encoding="utf-8") as fh:
            existing_ids = {row.get("prediction_id", "") for row in csv.DictReader(fh) if row.get("prediction_id")}
    with JOURNAL.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for pick in picks:
            prediction_id = pick_id(pick)
            if prediction_id in existing_ids:
                continue
            writer.writerow({"prediction_id": pick_id(pick), "report_date": checked.astimezone(MSK).date().isoformat(), "match_date": pick.event.commence_time.astimezone(MSK).date().isoformat(), "fixture_id": pick.event.event_id, "home": pick.event.home, "away": pick.event.away, "market": pick.market, "selection": pick.selection, "odds": f"{pick.odds:.4f}", "checked_at_utc": checked.astimezone(UTC).isoformat(), "status": "PENDING", "home_goals": "", "away_goals": "", "profit_units": "", "source": pick.event.odds_source})
            existing_ids.add(prediction_id)


def settlement_outcome(market: str, selection: str, home: str, away: str, home_goals: int, away_goals: int) -> bool | None:
    normalized_market = market.strip().casefold().replace(",", ".")
    if market == "ML":
        if teams_match(selection, home):
            return home_goals > away_goals
        if teams_match(selection, away):
            return away_goals > home_goals
        return None
    if normalized_market in {"тб 2.5", "over 2.5"}:
        return home_goals + away_goals >= 3
    if normalized_market in {"тм 3.5", "under 3.5"}:
        return home_goals + away_goals <= 3
    if market == "ОЗ: Да":
        return home_goals > 0 and away_goals > 0
    if market == "ОЗ: Нет":
        return home_goals == 0 or away_goals == 0
    return None


def settle(results_file: Path) -> None:
    if not JOURNAL.exists():
        fail("post_match_journal.csv не найден")
    results = {str(item["fixture_id"]): item for item in json.loads(results_file.read_text(encoding="utf-8"))}
    with JOURNAL.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        if row["status"] != "PENDING" or row["fixture_id"] not in results:
            continue
        result = results[row["fixture_id"]]
        home_goals = parse_score(result.get("home_goals"))
        away_goals = parse_score(result.get("away_goals"))
        if home_goals is None or away_goals is None:
            print(f"Некорректный счёт для fixture {row['fixture_id']}; строка остаётся PENDING.")
            continue
        won = settlement_outcome(row["market"], row["selection"], row["home"], row["away"], home_goals, away_goals)
        if won is None:
            print(f"Неизвестный рынок/выбор для fixture {row['fixture_id']}; строка остаётся PENDING.")
            continue
        row["home_goals"], row["away_goals"] = str(home_goals), str(away_goals)
        row["status"] = "WON" if won else "LOST"
        row["profit_units"] = f"{float(row['odds']) - 1:.4f}" if won else "-1.0000"
    with JOURNAL.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    settled_by_id = {}
    for row in rows:
        if row["status"] not in {"WON", "LOST", "VOID"}:
            continue
        key = row.get("prediction_id") or "|".join((row.get("fixture_id", ""), row.get("market", ""), row.get("selection", ""), row.get("report_date", "")))
        settled_by_id.setdefault(key, row)
    settled = list(settled_by_id.values())
    profit = sum(float(row["profit_units"]) for row in settled)
    stakes = len(settled)
    wins = sum(row["status"] == "WON" for row in settled)
    print(json.dumps({"settled": stakes, "wins": wins, "losses": sum(row["status"] == "LOST" for row in settled), "profit_units": round(profit, 4), "roi_percent": round(profit / stakes * 100, 2) if stakes else None}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("daily", help="получить ближайшие матчи, отобрать варианты и создать отчёт")
    daily.add_argument("--output", type=Path)
    settle_cmd = sub.add_parser("settle", help="заполнить результаты из JSON и вывести ROI")
    settle_cmd.add_argument("--results", type=Path, required=True, help='JSON: [{"fixture_id":"...","home_goals":2,"away_goals":1}]')
    args = parser.parse_args()
    if args.command == "settle":
        settle(args.results)
        return
    odds_key = os.getenv("ODDS_API_KEY")
    if not odds_key:
        fail("для daily нужен ODDS_API_KEY (The Odds API); API-Football для формы больше не требуется")
    if not ODDS_SPORT_KEYS:
        fail("список ODDS_SPORT_KEYS пуст")
    if IGNORED_ODDS_SPORT_KEYS:
        print("Игнорируются неразрешённые ODDS_SPORT_KEYS: " + ", ".join(IGNORED_ODDS_SPORT_KEYS))
    if not set(ODDS_MARKETS).issubset({"h2h", "totals"}):
        fail("ODDS_MARKETS поддерживает только h2h и totals")
    now = datetime.now(UTC)
    end = now + timedelta(hours=WINDOW_HOURS)
    odds = OddsApi(odds_key)
    football = FootballSources(
        football_data_token=os.getenv("FOOTBALL_DATA_TOKEN"),
        thesportsdb_key=os.getenv("THESPORTSDB_KEY"),
    )
    events = [event for event in odds.upcoming() if now <= event.commence_time <= end]
    all_picks = [pick for event in events for pick in candidates(event, football)]
    all_picks.sort(key=lambda pick: pick.estimated_probability - 1 / pick.odds, reverse=True)
    selected: list[Pick] = []
    used_events: set[str] = set()
    for pick in all_picks:
        if pick.event.event_id not in used_events:
            selected.append(pick)
            used_events.add(pick.event.event_id)
        if len(selected) == MAX_PICKS:
            break
    output = args.output or ROOT / f"daily_football_report_{now.astimezone(MSK):%Y-%m-%d}.md"
    write_report(selected, now, output)
    print(f"Создан отчёт: {output}")
    print(f"Кандидатов: {len(events)}, отобрано: {len(selected)}; запрошенные рынки: {','.join(ODDS_MARKETS)}; лиги: {len(ODDS_SPORT_KEYS)}")


if __name__ == "__main__":
    main()
