"""Scrape the RBI PPI Statistics listing page for month -> XLSX URL."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://www.rbi.org.in/Scripts/PPIStatisticsView.aspx"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": LISTING_URL,
}

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


@dataclass
class MonthEntry:
    month: date
    label: str
    xlsx_url: str
    is_revised: bool


def _parse_label(label: str) -> tuple[date, bool]:
    is_revised = "revised" in label.lower()
    m = re.search(r"([A-Za-z]+)\s*-\s*(\d{4})", label)
    if not m:
        raise ValueError(f"Could not parse month label: {label!r}")
    month_name, year = m.group(1), int(m.group(2))
    month_num = MONTHS[month_name.strip().capitalize()]
    return date(year, month_num, 1), is_revised


def fetch_listing_html() -> str:
    resp = requests.get(LISTING_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def fetch_year_html(session: requests.Session, base_html: str, year: str) -> str:
    """POST the ASP.NET postback form (GetYearMonth('year','0')) to list a full year."""
    soup = BeautifulSoup(base_html, "lxml")
    form = {
        inp.get("name"): inp.get("value", "")
        for inp in soup.find_all("input")
        if inp.get("name") and inp.get("type") == "hidden"
    }
    form["hdnYear"] = year
    form["hdnMonth"] = "0"
    form["UsrFontCntr$btn"] = ""
    resp = session.post(LISTING_URL, headers=HEADERS, data=form, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_listing(html: str) -> list[MonthEntry]:
    soup = BeautifulSoup(html, "lxml")
    entries: list[MonthEntry] = []
    current_label: str | None = None
    for row in soup.find_all("tr"):
        header_cell = row.find("td", class_="tableheader")
        if header_cell is not None:
            current_label = header_cell.get_text(" ", strip=True)
            continue
        link_cell = row.find("a", class_="link2")
        if link_cell is not None and current_label and "revised" in link_cell.get_text(" ", strip=True).lower():
            # header row doesn't carry the "(Revised)" marker; the link2 text does.
            current_label = current_label + " (Revised)"
        a = row.find("a", href=lambda h: h and h.upper().endswith(".XLSX"))
        if a is None or current_label is None:
            continue
        href = a["href"]
        try:
            month, is_revised = _parse_label(current_label)
        except ValueError:
            continue
        url = href if href.startswith("http") else requests.compat.urljoin(LISTING_URL, href)
        entries.append(MonthEntry(month=month, label=current_label, xlsx_url=url, is_revised=is_revised))

    # collapse duplicates per month: prefer revised
    by_month: dict[date, MonthEntry] = {}
    for e in entries:
        existing = by_month.get(e.month)
        if existing is None or (e.is_revised and not existing.is_revised):
            by_month[e.month] = e

    return sorted(by_month.values(), key=lambda e: e.month, reverse=True)


def get_months(n: int | None = None) -> list[MonthEntry]:
    """Fetch the default (current year) listing, then walk back a year at a time via
    the ASP.NET postback form until we have >= n months (or hit 2022, the archive floor)."""
    session = requests.Session()
    html = fetch_listing_html()
    by_month: dict[date, MonthEntry] = {e.month: e for e in parse_listing(html)}

    year = date.today().year
    while (n is None or len(by_month) < n) and year > 2022:
        year -= 1
        year_html = fetch_year_html(session, html, str(year))
        for e in parse_listing(year_html):
            existing = by_month.get(e.month)
            if existing is None or (e.is_revised and not existing.is_revised):
                by_month[e.month] = e

    entries = sorted(by_month.values(), key=lambda e: e.month, reverse=True)
    return entries[:n] if n else entries


if __name__ == "__main__":
    for e in get_months():
        print(e.month, "REVISED" if e.is_revised else "", e.xlsx_url)
