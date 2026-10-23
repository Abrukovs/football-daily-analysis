#!/usr/bin/env python3
"""Keep the small bets_journal.csv synchronized and settle finished matches.

The script is deliberately conservative: if a fixture or score cannot be matched
unambiguously, the row remains PENDING instead of being guessed.
"""
from __future__ import annotations

import csv
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SIMPLE = ROOT / "bets_journal.csv"
POST = ROOT / "post_match_journal.csv"
FIELDS = ["date", "match", "tournament", "market", "result"]
MSK = ZoneInfo("Europe/Moscow")


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


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


def parse_reports() -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for report in sorted(ROOT.glob("daily_football_report_*.md")):
        date_match = re.search(r"daily_football_report_(\d{4}-\d{2}-\d{2})\.md$", report.name)
        if not date_match:
            continue
        date = date_match.group(1)
        text = report.read_text(encoding="utf-8")
        blocks = re.split(r"(?=^#{2,3} )", text, flags=re.MULTILINE)
        for block in blocks:
            if "Почему" in block[:100] or "ставок нет" in block.lower():
                continue
            match = re.search(r"^\| Матч \|\s*(.*?)\s*\|$", block, re.MULTILINE)
            tournament = re.search(r"^\| Турнир \|\s*(.*?)\s*\|$", block, re.MULTILINE)
            market = re.search(r"^\| Рынок \|\s*\*\*(.*?)\*\*\s*\|$", block, re.MULTILINE)
            if not (match and tournament and market):
                head = re.search(r"^###?\s+(.+?)\s*$", block, re.MULTILINE)
                tournament_b = re.search(r"^- \*\*Турнир:\*\*\s*(.+?)\s*$", block, re.MULTILINE)
                market_b = re.search(r"^- \*\*Рынок:\*\*\s*(.+?)\s*$", block, re.MULTILINE)
                if head and tournament_b and market_b:
                    title = head.group(1).strip()
                    title = re.sub(r"\s*:\s*(ML|ТБ|ТМ|ОЗ).*$", "", title).strip()
                    match_text = title
                    tournament_text = tournament_b.group(1).strip().rstrip(".")
                    market_text = market_b.group(1).strip().rstrip(".")
                else:
                    continue
            else:
                match_text = match.group(1).strip()
                tournament_text = tournament.group(1).strip()
                market_text = market.group(1).strip()
            found.append({"date": date, "match": match_text, "tournament": tournament_text, "market": market_text, "result": "PENDING"})
    return found


def canonical_market(value: str) -> str:
    value = value.replace("—", ":").replace("–", ":")
    value = re.sub(r"\s*:\s*", ": ", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    low = value.lower()
    if "тб 2,5" in low or "over 2.5" in low:
        return "Over 2.5"
    if "тм 3,5" in low or "under 3.5" in low:
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
    unique: list[dict[str, str]] = []
    keys: set[tuple[str, str, str]] = set()
    for row in rows:
        key = row_key(row)
        if key in keys:
            continue
        row["market"] = canonical_market(row.get("market", ""))
        unique.append(row)
        keys.add(key)
    rows = unique
    added = 0
    for row in parse_reports():
        row["market"] = canonical_market(row["market"])
        key = row_key(row)
        if key not in keys:
            rows.append(row)
            keys.add(key)
            added += 1
    rows.sort(key=lambda r: (r["date"], r["match"]))
    write_rows(SIMPLE, rows, FIELDS)
    print(f"Журнал: добавлено новых прогнозов: {added}")
    return added


def api_json(path: str, key: str, params: dict[str, str]) -> dict:
    url = "https://v3.football.api-sports.io" + path + "?" + urlencode(params)
    req = Request(url, headers={"x-apisports-key": key, "User-Agent": "football-daily-analysis/1.0"})
    with urlopen(req, timeout=30) as response:
        return __import__("json").load(response)


def split_match(value: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+[—–-]\s+", value)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else None


def find_fixture(fixtures: list[dict], match: str) -> tuple[int, int, int] | None:
    pair = split_match(match)
    if not pair:
        return None
    home, away = map(norm, pair)
    candidates = []
    for item in fixtures:
        teams = item.get("teams", {})
        hn, an = norm(teams.get("home", {}).get("name", "")), norm(teams.get("away", {}).get("name", ""))
        if (home in hn or hn in home) and (away in an or an in away):
            score = item.get("goals", {})
            if score.get("home") is not None and score.get("away") is not None:
                candidates.append((item["fixture"]["id"], int(score["home"]), int(score["away"])))
    return candidates[0] if len(candidates) == 1 else None


def outcome(market: str, match: str, hg: int, ag: int) -> bool | None:
    total = hg + ag
    m = market.lower()
    if m.startswith("ml:"):
        selection = norm(market.split(":", 1)[1])
        pair = split_match(match)
        if not pair:
            return None
        return hg > ag if selection in norm(pair[0]) or norm(pair[0]) in selection else ag > hg
    if "over 2.5" in m or "тб 2,5" in m:
        return total >= 3
    if "under 3.5" in m or "тм 3,5" in m:
        return total <= 3
    if "btts: yes" in m or "оз: да" in m:
        return hg > 0 and ag > 0
    if "btts: no" in m or "оз: нет" in m:
        return hg == 0 or ag == 0
    return None


def settle() -> int:
    key = os.getenv("API_FOOTBALL_KEY")
    if not key:
        print("API_FOOTBALL_KEY не задан: расчёт результатов пропущен.")
        return 0
    rows = read_rows(SIMPLE)
    today = datetime.now(MSK).date().isoformat()
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("result") == "PENDING" and row.get("date", "") < today:
            by_date.setdefault(row["date"], [])
    changed = 0
    for date in by_date:
        data = api_json("/fixtures", key, {"date": date})
        fixtures = data.get("response", [])
        for row in rows:
            if row.get("date") != date or row.get("result") != "PENDING":
                continue
            found = find_fixture(fixtures, row.get("match", ""))
            if not found:
                continue
            _, hg, ag = found
            won = outcome(row.get("market", ""), row.get("match", ""), hg, ag)
            if won is not None:
                row["result"] = "WON" if won else "LOST"
                changed += 1
    write_rows(SIMPLE, rows, FIELDS)
    print(f"Журнал: автоматически рассчитано результатов: {changed}")
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
