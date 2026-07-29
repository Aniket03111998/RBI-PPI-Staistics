"""One command: scrape -> download -> parse -> load -> data-quality report.
Run: python -m ppi.refresh
"""
from __future__ import annotations

import logging
import re
from datetime import date

from ppi.analytics import detect_renames
from ppi.config import MONTHS_KEPT
from ppi.dataquality import unmerged_alias_suggestions
from ppi.download import RAW_DIR, download_recent
from ppi.load import get_conn, load_month
from ppi.parse import parse_file, record_renames

log = logging.getLogger(__name__)
FNAME_RE = re.compile(r"ppi_(\d{4})-(\d{2})(_revised)?\.xlsx")


def month_and_revision_from_filename(name: str) -> tuple[date, bool]:
    m = FNAME_RE.match(name)
    if not m:
        raise ValueError(f"Unrecognized raw filename: {name}")
    y, mo, revised = m.groups()
    return date(int(y), int(mo), 1), bool(revised)


def run(months: int = MONTHS_KEPT) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    log.info("=== scrape + download ===")
    download_recent(months)

    log.info("=== parse ===")
    files = sorted(RAW_DIR.glob("ppi_*.xlsx"))
    all_unmatched: dict[str, list[str]] = {}
    parsed = []

    for path in files:
        month, is_revised = month_and_revision_from_filename(path.name)
        df, unmatched = parse_file(path, month, is_revised)
        if unmatched:
            all_unmatched[path.name] = unmatched
        parsed.append((month, is_revised, df))

    # Merge issuers RBI renamed mid-window before anything is stored. Left split, one company
    # appears as two half-length series that break every trend, rank and YoY on the page.
    every_name = {n for _, _, d in parsed for n in d["entity"].unique()}
    renames = detect_renames(every_name)
    if renames:
        log.info("merging %d renamed issuer(s) into their current name", len(renames))
        for _, _, d in parsed:
            d["entity"] = d["entity"].replace(renames)
        record_renames(renames)

    log.info("=== load ===")
    conn = get_conn()
    revisions_applied = []
    row_counts = {}
    for month, is_revised, df in parsed:
        diff = load_month(conn, df, month, is_revised)
        row_counts[month.isoformat()] = len(df)
        if is_revised and diff["changed"]:
            revisions_applied.append((month.isoformat(), len(diff["changed"])))

    print("\n" + "=" * 70)
    print("DATA QUALITY REPORT")
    print("=" * 70)
    print(f"Months loaded: {len(row_counts)}")
    for month, n in sorted(row_counts.items()):
        print(f"  {month}: {n} rows")

    if revisions_applied:
        print("\nRevisions applied (rows changed vs prior load):")
        for fname, n in revisions_applied:
            print(f"  {fname}: {n} rows changed")

    if all_unmatched:
        print("\nNew entity names seen for the first time (added as identity rows to data/entity_aliases.csv —")
        print("edit canonical_name there if one of these is really an existing entity under a new label):")
        for fname, names in all_unmatched.items():
            print(f"  {fname}:")
            for n in names:
                print(f"    - {n}")
    else:
        print("\nNo new entity names.")

    suggestions = unmerged_alias_suggestions()
    if not suggestions.empty:
        print("\nPossible duplicate entities worth reviewing in data/entity_aliases.csv (also visible in the")
        print("dashboard's Data Quality page):")
        for _, row in suggestions.iterrows():
            print(f"  {row['entity']}  (maybe same as: {row['possible_match']})")

    conn.close()


if __name__ == "__main__":
    run()
