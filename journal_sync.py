#!/usr/bin/env python3
"""Keep the small bets_journal.csv synchronized and settle finished matches.

Results are matched conservatively: ambiguous or incomplete fixtures remain
PENDING. ESPN is the primary public source, TheSportsDB is the no-cost
fallback, and football-data.org is an optional fallback when its free token is
configured.
"""
from __future__ import annotations

import csv
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from football_sources import FootballSources, find_result, teams_match

ROOT = Path(__file__).resolve().parent
SIMPLE = ROOT / "bets_journal.csv"
POST = ROOT / "post_match_journal.csv"
FIELDS = ["date", "match", "tournament", "market", "result"]
MSK = ZoneInfo("Europe/Moscow")


def norm(value: str) -> str:
    from football_sources import normalize_team_name
    return normalize_team_name(value)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _fixture_date_from_block(block: str, report_date: str) -> str:
    kickoff = re.search(r"^\s*\|\s*(?:Начало|Kickoff)\s*\|\s*([^|]+)", block, re.MULTILINE | re.IGNORECASE)
    if not kickoff:
        kickoff = re.search(r"^\s*-\s*\*\*(?:Начало|Kickoff):\*\*\s*(.+?)\s*$", block, re.MULTILINE | re.IGNORECASE)
    if not kickoff:
        return report_date
    value = re.sub(r"[*_]", "", kickoff.group(1)).strip()
    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", value)
    if iso:
        try:
            return date.fromisoformat(iso.group(1)).isoformat()
        except ValueError:
            return report_date
    numeric = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", value)
    if numeric:
        try:
            return date(int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1))).isoformat()
        except ValueError:
            return report_date
    months = {
        "января": 1, "январь": 1, "февраля": 2, "февраль": 2, "марта": 3, "март": 3,
        "апреля": 4, "апрель": 4, "мая": 5, "май": 5, "июня": 6, "июнь": 6,
        "июля": 7, "июль": 7, "августа": 8, "август": 8, "сентября": 9, "сентябрь": 9,
        "октября": 10, "октябрь": 10, "ноября": 11, "ноябрь": 11, "декабря": 12, "декабрь": 12,
    }
    russian = re.search(r"\b(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?\b", value, re.IGNORECASE)
    if not russian:
        return report_date
    month = months.get(russian.group(2).casefold())
    if not month:
        return report_date
    report_day = date.fromisoformat(report_date)
    year = int(russian.group(3)) if russian.group(3) else report_day.year
    if not russian.group(3):
        if month < report_day.month - 6:
            year += 1
        elif month > report_day.month + 6:
            year -= 1
    try:
        return date(year, month, int(russian.group(1))).isoformat()
    except ValueError:
        return report_date


def parse_reports() -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for report in sorted(ROOT.glob("daily_football_report_*.md")):
        date_match = re.search(r"daily_football_report_(\d{4}-\d{2}-\d{2})\.md$", report.name)
        if not date_match:
            continue
        report_date = date_match.group(1)
        text = report.read_text(encoding="utf-8")
        blocks = re.split(r"(?=^#{2,3} )", text, flags=re.MULTILINE)
        for block in blocks:
            if "Почему" in block[:100] or "ставок нет" in block.lower():
                continue
            match = re.search(r"^\| Матч \|\s*(.*?)\s*\|$", block, re.MULTILINE)
            tournament = re.search(r"^\| Турнир \|\s*(.*?)\s*\|$", block, re.MULTILINE)
            market = re.search(r"^\| Рынок \|\s*\*\*(.*?)\*\*\s*\|$", block, re.MULTILINE)
            head = re.search(r"^#{1,3}\s+(.+?)\s*$", block, re.MULTILINE)
            tournament_b = re.search(r"^\s*-\s*\*\*Турнир:\*\*\s*(.+?)\s*$", block, re.MULTILINE)
            market_b = re.search(r"^\s*-\s*\*\*Рынок:\*\*\s*(.+?)\s*$", block, re.MULTILINE)
            if tournament:
                tournament_text = tournament.group(1).strip()
            elif tournament_b:
                tournament_text = tournament_b.group(1).strip().rstrip(".")
            else:
                continue
            if market:
                market_text = market.group(1).strip()
            elif market_b:
                market_text = market_b.group(1).strip().rstrip(".")
            else:
                continue
            if match:
                match_text = match.group(1).strip()
            elif head:
                title = head.group(1).strip()
                title = re.sub(r"^Вариант\s+\d+\s+[—–-]\s*", "", title, flags=re.I).strip()
                selection = market_text.split(":", 1)[1].strip() if ":" in market_text else ""
                suffix = ": " + selection if selection else ""
                if suffix and title.endswith(suffix):
                    title = title[:-len(suffix)].strip()
                title = re.sub(r"\s*:\s*(ML|ТБ|ТМ|ОЗ).*$", "", title).strip()
                match_text = title
            else:
                continue
            found.append({
                "date": _fixture_date_from_block(block, report_date),
                "_report_date": report_date,
                "match": match_text,
                "tournament": tournament_text,
                "market": market_text,
                "result": "PENDING",
            })
    return found


def canonical_market(value: str) -> str:
    value = value.replace("—", ":").replace("–", ":")
    value = re.sub(r"\s*:\s*", ": ", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    low = value.lower().replace(",", ".")
    if "тб 2.5" in low or "over 2.5" in low:
        return "Over 2.5"
    if "тм 3.5" in low or "under 3.5" in low:
        return "Under 3.5"
    if "оз: да" in low or "btts: yes" in low or "btts yes" in low:
        return "BTTS: Yes"
    if "оз: нет" in low or "btts: no" in low or "btts no" in low:
        return "BTTS: No"
    if low.startswith("ml"):
        selection = value.split(":", 1)[1].strip() if ":" in value else value
        selection = re.sub(r"^победа\s+", "", selection, flags=re.I)
        return "ML: " + selection
    return value


def row_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (row.get("date", ""), norm(row.get("match", "")), canonical_market(row.get("market", "")))


def sync_reports() -> int:
    rows = read_rows(SIMPLE)
    report_rows = parse_reports()
    unique: list[dict[str, str]] = []
    keys: set[tuple[str, str, str]] = set()
    existing_source_keys: set[tuple[str, str, str]] = set()
    for row in rows:
        key = row_key(row)
        if key in keys:
            continue
        row["market"] = canonical_market(row.get("market", ""))
        unique.append(row)
        keys.add(key)
        existing_source_keys.add((row.get("date", ""), norm(row.get("match", "")), row["market"]))
    rows = unique
    added = 0
    for report_row in report_rows:
        source_key = (report_row["_report_date"], norm(report_row["match"]), canonical_market(report_row["market"]))
        if source_key in existing_source_keys:
            continue
        row = {field: report_row.get(field, "") for field in FIELDS}
        row["market"] = canonical_market(row["market"])
        key = row_key(row)
        if key not in keys:
            rows.append(row)
            keys.add(key)
            added += 1
        existing_source_keys.add(source_key)
    rows.sort(key=lambda row: (row["date"], row["match"]))
    write_rows(SIMPLE, rows, FIELDS)
    print(f"Журнал: добавлено новых прогнозов: {added}")
    return added


def correct_pending_dates(rows: list[dict[str, str]], reports: list[dict[str, str]]) -> bool:
    report_dates: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for report in reports:
        report_date = report.get("_report_date")
        if not report_date:
            continue
        key = (report_date, norm(report.get("match", "")), canonical_market(report.get("market", "")))
        report_dates[key].add(report.get("date", report_date))
    corrected = False
    for row in rows:
        if row.get("result") != "PENDING":
            continue
        key = (row.get("date", ""), norm(row.get("match", "")), canonical_market(row.get("market", "")))
        candidates = report_dates.get(key, set())
        if len(candidates) == 1:
            actual_date = next(iter(candidates))
            if actual_date != row.get("date"):
                row["date"] = actual_date
                corrected = True
    return corrected


def split_match(value: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+[—–-]\s+", value)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else None


def outcome(market: str, match: str, home_goals: int, away_goals: int) -> bool | None:
    total = home_goals + away_goals
    low = market.lower().replace(",", ".")
    if low.startswith("ml:"):
        selection = market.split(":", 1)[1].strip()
        pair = split_match(match)
        if not pair:
            return None
        if teams_match(selection, pair[0]):
            return home_goals > away_goals
        if teams_match(selection, pair[1]):
            return away_goals > home_goals
        # Do not treat a draw or an unknown outcome as an away-team selection.
        return None
    if "over 2.5" in low or "тб 2.5" in low:
        return total >= 3
    if "under 3.5" in low or "тм 3.5" in low:
        return total <= 3
    if "btts: yes" in low or "оз: да" in low:
        return home_goals > 0 and away_goals > 0
    if "btts: no" in low or "оз: нет" in low:
        return home_goals == 0 or away_goals == 0
    return None


def apply_results(rows: list[dict[str, str]], records: list[dict], source_counts: Counter, expected_date: str | None = None) -> int:
    changed = 0
    for row in rows:
        if row.get("result") != "PENDING":
            continue
        result = find_result(records, row.get("match", ""), expected_date=expected_date)
        if not result:
            continue
        won = outcome(row.get("market", ""), row.get("match", ""), result["home_goals"], result["away_goals"])
        if won is None:
            continue
        row["result"] = "WON" if won else "LOST"
        changed += 1
        source_counts[result.get("source", "unknown")] += 1
    return changed


def _week_monday(date_text: str) -> date:
    value = date.fromisoformat(date_text)
    return value - timedelta(days=value.weekday())


def settle() -> int:
    rows = read_rows(SIMPLE)
    dates_corrected = correct_pending_dates(rows, parse_reports())
    today = datetime.now(MSK).date().isoformat()
    pending = [row for row in rows if row.get("result") == "PENDING" and row.get("date", "") < today]
    if not pending:
        if dates_corrected:
            write_rows(SIMPLE, rows, FIELDS)
        print("Журнал: завершённых матчей со статусом PENDING нет.")
        return 0

    sources = FootballSources(
        football_data_token=os.getenv("FOOTBALL_DATA_TOKEN"),
        thesportsdb_key=os.getenv("THESPORTSDB_KEY"),
    )
    source_counts: Counter = Counter()
    changed = 0
    by_date: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in pending:
        by_date[row["date"]].append(row)

    for match_date in sorted(by_date):
        date_rows = by_date[match_date]
        try:
            changed += apply_results(date_rows, sources.espn_scores_for_date(match_date), source_counts, expected_date=match_date)
        except Exception as exc:
            print(f"ESPN results unavailable for {match_date}: {exc}")
        unresolved = [row for row in date_rows if row.get("result") == "PENDING"]
        if unresolved:
            target = date.fromisoformat(match_date)
            for offset in (-1, 1):
                neighbor_date = (target + timedelta(days=offset)).isoformat()
                try:
                    neighbor = sources.espn_scores_for_date(neighbor_date)
                    changed += apply_results(unresolved, neighbor, source_counts, expected_date=match_date)
                except Exception as exc:
                    print(f"ESPN adjacent-date lookup unavailable for {neighbor_date}: {exc}")
                unresolved = [row for row in date_rows if row.get("result") == "PENDING"]
                if not unresolved:
                    break
        if unresolved:
            try:
                changed += apply_results(unresolved, sources.thesportsdb_scores_for_date(match_date), source_counts, expected_date=match_date)
            except Exception as exc:
                print(f"TheSportsDB results unavailable for {match_date}: {exc}")

    unresolved_all = [row for row in pending if row.get("result") == "PENDING"]
    if unresolved_all and sources.football_data_token:
        grouped_by_week: dict[date, list[dict[str, str]]] = defaultdict(list)
        for row in unresolved_all:
            grouped_by_week[_week_monday(row["date"])].append(row)
        for monday, week_rows in sorted(grouped_by_week.items()):
            # A one-week UTC window plus a day on each side covers Moscow dates
            # without querying the free API one fixture at a time.
            date_from = (monday - timedelta(days=1)).isoformat()
            date_to = (monday + timedelta(days=7)).isoformat()
            try:
                records = sources.football_data_scores_for_range(date_from, date_to)
                for row in week_rows:
                    changed += apply_results([row], records, source_counts, expected_date=row["date"])
            except Exception as exc:
                print(f"football-data.org results unavailable for {date_from}..{date_to}: {exc}")
    elif unresolved_all:
        print("FOOTBALL_DATA_TOKEN не задан: резерв football-data.org отключён; нераспознанные строки остаются PENDING.")

    if changed or dates_corrected:
        write_rows(SIMPLE, rows, FIELDS)
    remaining = sum(row.get("result") == "PENDING" for row in pending)
    print(f"Журнал: автоматически рассчитано: {changed}; источники: {dict(source_counts)}; осталось PENDING: {remaining}")
    return changed


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["sync", "settle", "all"])
    args = parser.parse_args()
    if args.command in {"sync", "all"}:
        sync_reports()
    if args.command in {"settle", "all"}:
        settle()


if __name__ == "__main__":
    main()
