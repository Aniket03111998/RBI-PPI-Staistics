"""Download + validate RBI PPI XLSX files, with Playwright and manual fallbacks."""
from __future__ import annotations

import logging
from pathlib import Path

import openpyxl
import requests

from ppi.scrape import HEADERS, LISTING_URL, MonthEntry, get_months

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
ZIP_MAGIC = b"PK\x03\x04"

log = logging.getLogger(__name__)


def dest_filename(entry: MonthEntry) -> str:
    suffix = "_revised" if entry.is_revised else ""
    return f"ppi_{entry.month:%Y-%m}{suffix}.xlsx"


def is_valid_xlsx(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            if f.read(4) != ZIP_MAGIC:
                return False
        openpyxl.load_workbook(path, read_only=True).close()
        return True
    except Exception:
        return False


def _download_requests(url: str, dest: Path) -> bool:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return is_valid_xlsx(dest)
    except requests.RequestException:
        return False


def _download_playwright(url: str, dest: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed; skipping browser fallback")
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(extra_http_headers={"Referer": LISTING_URL})
            with page.expect_download(timeout=60_000) as dl_info:
                page.goto(url)
            dl_info.value.save_as(dest)
            browser.close()
        return is_valid_xlsx(dest)
    except Exception as e:
        log.warning("playwright fallback failed for %s: %s", url, e)
        return False


def download_month(entry: MonthEntry, dest_dir: Path = RAW_DIR) -> Path | None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / dest_filename(entry)
    if dest.exists() and is_valid_xlsx(dest):
        log.info("skip (already present): %s", dest.name)
        return dest

    if _download_requests(entry.xlsx_url, dest):
        log.info("downloaded via requests: %s", dest.name)
        return dest

    if _download_playwright(entry.xlsx_url, dest):
        log.info("downloaded via playwright: %s", dest.name)
        return dest

    if dest.exists():
        dest.unlink()
    log.error("FAILED to download %s -> %s", entry.month, entry.xlsx_url)
    return None


def download_recent(n: int = 15, dest_dir: Path = RAW_DIR) -> list[Path]:
    entries = get_months(n)
    downloaded, failed = [], []
    for e in entries:
        path = download_month(e, dest_dir)
        if path:
            downloaded.append(path)
        else:
            failed.append(e)

    if failed:
        print("\nManual fallback needed for these months (download into data/raw/):")
        for e in failed:
            print(f"  {e.month:%Y-%m}  {'(revised) ' if e.is_revised else ''}{e.xlsx_url}")

    return downloaded


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for p in download_recent():
        print(p)
