#!/usr/bin/env python3
"""Publish verified IRS business rates. Standard library only; fail before writing."""
import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.request import Request, urlopen

IRS_URL = "https://www.irs.gov/tax-professionals/standard-mileage-rates"
HEADERS = ["Period", "Business use", "Charity use", "Medical or military moving", "Source"]
MONTHS = {name: i for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class RateHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self.table is not None:
                raise ValueError("Nested table")
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("th", "td") and self.row is not None:
            self.cell = []
        elif tag in ("br", "p", "li") and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def period_dates(period):
    period = " ".join(period.replace("–", "-").replace("—", "-").split())
    if re.fullmatch(r"\d{4}", period):
        year = int(period)
        return date(year, 1, 1), date(year, 12, 31)
    match = re.fullmatch(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})", period)
    if match:
        return tuple(datetime.strptime(s, "%m/%d/%Y").date() for s in match.groups())
    match = re.fullmatch(r"(\d{4})\s*\(([A-Za-z]+)\.?\s*(\d{1,2})\s*-\s*([A-Za-z]+)\.?\s*(\d{1,2})\)", period)
    if match:
        year, m1, d1, m2, d2 = match.groups()
        return (date(int(year), MONTHS[m1[:3].lower()], int(d1)),
                date(int(year), MONTHS[m2[:3].lower()], int(d2)))
    raise ValueError(f"Unrecognized period: {period}")


def parse_entries(html):
    parser = RateHTML()
    parser.feed(html)
    parser.close()
    if len(parser.tables) != 1 or not parser.tables[0] or parser.tables[0][0] != HEADERS:
        raise ValueError("Expected one IRS table with the standard mileage headers")
    entries = []
    for row in parser.tables[0][1:]:
        if len(row) != 5:
            raise ValueError(f"Invalid table row: {row}")
        period, business, _, _, notice = row
        if not re.fullmatch(r"\d+(?:\.\d)?", business):
            raise ValueError(f"Invalid business cents: {business}")
        cents = Decimal(business)
        if not 30 <= cents <= 150:
            raise ValueError(f"Business rate outside 30–150 cents: {business}")
        # Older IRS rows can carry explanatory footnotes (e.g. '2018 TCJA').
        years = [int(y) for y in re.findall(r"\b\d{4}\b", period)]
        if years and max(years) < 2024:
            continue
        start, end = period_dates(period)
        if start < date(2024, 1, 1) or start > end or start.year != end.year:
            raise ValueError(f"Invalid interval: {period}")
        if not re.fullmatch(r"IR(?:B)?-\d{4}-\d+", notice):
            raise ValueError(f"Invalid IRS source: {notice}")
        suffix = ""
        if (start.month, start.day, end.month, end.day) == (1, 1, 6, 30):
            suffix = "-h1"
        elif (start.month, start.day, end.month, end.day) == (7, 1, 12, 31):
            suffix = "-h2"
        elif (start.month, start.day, end.month, end.day) != (1, 1, 12, 31):
            suffix = f"-{start:%m%d}"
        entries.append(dict(id=f"irs-{start.year}{suffix}", start=start.isoformat(), end=end.isoformat(),
                            rate=f"{cents / 100:.3f}", source="IRS", notice=notice, verified=True))
    return sorted(entries, key=lambda e: e["start"])


def validate_entries(entries, previous, today):
    if not entries or entries[0]["start"] != "2024-01-01":
        raise ValueError("Coverage must begin on 2024-01-01")
    for entry in entries:
        if date.fromisoformat(entry["start"]) > date(today.year + 1, 12, 31):
            raise ValueError(f"Interval starts after next year: {entry['id']}")
        if not entry["verified"] or not Decimal("0.30") <= Decimal(entry["rate"]) <= Decimal("1.50"):
            raise ValueError(f"Unverified or invalid rate: {entry['id']}")
    for a, b in zip(entries, entries[1:]):
        if date.fromisoformat(a["end"]) + timedelta(days=1) != date.fromisoformat(b["start"]):
            raise ValueError(f"Gap or overlap between {a['id']} and {b['id']}")
    for old in (previous or {}).get("entries", []):
        pieces = [e for e in entries if old["start"] <= e["start"] and e["end"] <= old["end"]]
        if not pieces or pieces[0]["start"] != old["start"] or pieces[-1]["end"] != old["end"]:
            raise ValueError(f"Previous interval removed or changed bounds: {old['id']}")
        # Global continuity above also guarantees continuity of these exact-cover pieces.
        if Decimal(pieces[0]["rate"]) != Decimal(old["rate"]):
            if len(pieces) > 1 or date.fromisoformat(old["start"]) <= today:
                raise ValueError(f"Previous rate changed retroactively: {old['id']}")
        if len(pieces) > 1 or pieces[0] != old:
            print(f"warning: IRS row changed: {old['id']}", file=sys.stderr)


def next_version(previous, today):
    day, number = today.isoformat(), 1
    if previous:
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.(\d+)", previous["tableVersion"])
        if not match:
            raise ValueError("Invalid previous tableVersion")
        old_day, old_number = match.groups()
        date.fromisoformat(old_day)
        if old_day >= day:
            day, number = old_day, int(old_number) + 1
    return f"{day}.{number}"


def build_update(html, previous=None, today=None):
    today = today or datetime.now(timezone.utc).date()
    entries = parse_entries(html)
    validate_entries(entries, previous, today)
    if previous is not None and entries == previous["entries"]:
        return None
    return dict(schemaVersion=1, tableVersion=next_version(previous, today), publishedAt=today.isoformat(),
                currency="USD", verifiedAgainst=IRS_URL, verifiedOn=today.isoformat(), entries=entries)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--html", type=Path, help="Use a saved IRS page instead of fetching")
    args = parser.parse_args(argv)
    try:
        previous = json.loads(args.previous.read_text()) if args.previous.exists() else None
        if args.html:
            html = args.html.read_text(encoding="utf-8")
        else:
            request = Request(IRS_URL, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"})
            with urlopen(request, timeout=30) as response:
                html = response.read().decode("utf-8")
        update = build_update(html, previous)
        if update is None:
            print("no change")
            return 0
        data = json.dumps(update, indent=2) + "\n"
        args.out.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.out.parent, delete=False) as f:
                temporary = f.name
                f.write(data)
            os.replace(temporary, args.out)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        print(f"updated to {update['tableVersion']}")
        return 0
    except (ValueError, KeyError, OSError, ArithmeticError) as error:
        print(f"IRS rates update failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
