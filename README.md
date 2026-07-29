# RBI Entity-wise PPI Statistics Dashboard

Local analytics tool built on top of RBI's monthly [Entity-wise PPI Statistics](https://www.rbi.org.in/Scripts/PPIStatisticsView.aspx)
releases (mobile wallets and PPI cards issued by banks and non-bank fintechs).

## Setup

```bash
pip install -r requirements.txt
playwright install chromium   # only needed if the requests-based download ever gets CAPTCHA-blocked
```

## Run

```bash
python -m ppi.refresh     # scrape -> download -> parse -> load -> data-quality report
streamlit run app.py
```

`refresh` is idempotent — safe to re-run any time. It downloads the newest 15 months
(`ppi.config.MONTHS_KEPT`), skips files already present in `data/raw/`, prefers RBI's
"(Revised)" releases when both exist, and re-parses every raw file on each run so that
edits to `data/entity_aliases.csv` take effect without re-downloading.

The dashboard caches its DB read for 1 hour — after running `refresh`, click **"🔄 Reload
data"** in the sidebar to see new data immediately instead of waiting.

## What the dashboard shows

**Overview** — three tabs sharing the sidebar's metric / instrument / period / top-N filters:
- *Rankings & Key Changes* — industry KPI, top banks and top non-banks with share and share
  change, plus templated plain-English insights.
- *Movers & Growth* — biggest single-period gainers/losers, and a trailing-window growth
  leaderboard for who has compounded rather than had one good month.
- *Market Structure* — HHI concentration trend per segment, growth attribution (whose gain
  or loss drove the segment's net change), and new entrants / issuers that stopped reporting.

**Company Explorer** — per-issuer insights ("what changed and what it implies", recomputed at
the selected period grain), scorecard (improving vs deteriorating across every metric), peer
benchmarking against the rest of its segment, metric time series, wallet-vs-card split, and
rank trend.

**Compare** — overlay any number of issuers across any number of metrics.

Every page exports CSV; the sidebar also exports the full tidy dataset in one click.

### P2M vs P2P

RBI never uses those labels, but its column footnotes define the buckets precisely enough to
map them, so **Transaction type** is a first-class filter:

| Filter | RBI source columns | Meaning |
|---|---|---|
| P2M | "purchase of Goods and Services (both at PoS terminal and at online)" | paying a merchant, in-store or online |
| P2P | "fund transfer (from own PPI to other PPIs …, PPI to Bank account, etc.)" | transfers out of the PPI |
| Cash withdrawals | cash withdrawal at ATM / at PoS | cash-out — neither of the above |

The three slices partition the total exactly (there's a test pinning that). Selecting one
filters the fact table once at source, so rankings, movers, concentration, insights and every
export are consistently scoped. PPI outstanding/active counts are deliberately *not* filtered —
an instrument isn't specific to a transaction purpose.

**Caveat worth carrying into any analysis:** RBI's fund-transfer bucket also contains
PPI-to-own-bank-account transfers, which are self-transfers rather than person-to-person. Treat
P2P here as an **upper bound** on true P2P volume. The app says so on screen when the filter is
active. There are also `P2M Share` and `P2P Share` metrics (each purpose's share of total value)
for ranking issuers by how merchant-led vs transfer-led their book is, available when no type
filter is applied.

### Periods: calendar vs financial year

The **Period** filter aggregates monthly source data into Monthly / Quarterly / Semi-Annual /
Annual buckets. Quarterly and Annual also offer a **Year basis** toggle, because the Indian
financial year runs April–March:

| Period | Calendar | Financial year |
|---|---|---|
| Quarterly | Q2 2026 = Apr–Jun 2026 | Q1 FY27 = Apr–Jun 2026 |
| Annual | 2026 = Jan–Dec 2026 | FY26 = Apr 2025 – Mar 2026 |

Quarter *boundaries* are identical either way — the Indian FY starts on a calendar quarter
edge, so only the naming differs. Annual boundaries genuinely differ. Semi-annual is
fiscal-only (H1 = Apr–Sep, H2 = Oct–Mar), which is what Indian issuers report as H1/H2.

Flow metrics (transaction value and volume) sum across the period; stock metrics (PPIs
outstanding/active) take the period's last reading, since summing a month-end snapshot would
be meaningless.

**Part-covered periods are flagged, not silently compared.** With data ending mid-year, the
newest FY bucket may hold only a few months. Comparing that stub against a full year reads as
a ~70% collapse that is purely an artefact of the window, so the period selector defaults to
the newest *complete* period, and if you select a partial one the app warns and suppresses the
change-vs-previous figures rather than publishing a misleading number.

### Reading the market-structure views

- **HHI** is the sum of squared percentage market shares (0–10000). Below 1500 is
  unconcentrated, 1500–2500 moderate, above 2500 concentrated. Rising = consolidating.
- **Growth attribution** contributions sum to 100% of the segment's *net* change. A negative
  contribution means that issuer moved against the segment (grew while it shrank, or vice
  versa) — that's meaningful, not an error.
- **Entrants/exits** are inferred from when reported transaction value starts or stops inside
  the loaded window. Issuers active in the first or last loaded period are excluded (we can't
  see past the window edge). "Stopped reporting" means activity hit zero — that can be a
  licence action or wind-down, not necessarily a shutdown.
- **Renames are merged automatically, not counted as entry + exit.** RBI relabels issuers
  mid-window (PhonePe Private Limited → PhonePe Limited, North East SFB → Slice SFB, Unimoni
  → Wizzmoni, Pine Labs Private → Pine Labs Limited). Left split, one company shows up as two
  half-length series that break every trend, rank and YoY built on top of it. `ppi.refresh`
  reads the predecessor out of RBI's own inline "(formerly X)" annotation and folds the old
  name into the current one before anything is stored, so history stays continuous; the pairs
  are written into `data/entity_aliases.csv` so the merge is visible and editable rather than
  invisible. The dashboard doesn't surface an on-screen rename list — check
  `data/entity_aliases.csv` directly if you need to see or edit the merged pairs.

## Tests

```bash
python -m unittest discover tests -v
```

Golden tests are pinned to the Phase 0 sample files in `data/raw/` (values read directly
from the raw XLSX cells) — a future RBI format change will fail these loudly instead of
silently mis-parsing.

## If the automatic download gets blocked

`rbidocs.rbi.org.in` occasionally serves a CAPTCHA page instead of the file for
requests that look automated. `download.py` already retries via a headless Playwright
browser if the plain `requests` download fails validation (ZIP magic bytes + openpyxl
can open it). If both fail, `refresh` prints the exact month → URL list — download those
manually into `data/raw/` (filename pattern `ppi_YYYY-MM.xlsx`, or `..._revised.xlsx`
for revised releases) and re-run `python -m ppi.refresh`; it picks up whatever's there.

## Entity name cleanup

RBI's issuer names drift across months (renames, "(formerly ...)" annotations, stray
whitespace/footnote characters). `data/entity_aliases.csv` maps `raw_name -> canonical_name`;
new names are auto-registered there as identity rows (`raw_name == canonical_name`) so
you can review and edit `canonical_name` to merge two rows into one entity — nothing
merges automatically. The dashboard's **Data Quality** page (and the console report from
`refresh`) surfaces likely duplicates via fuzzy name matching to make this easier to spot.

## Scheduling a monthly refresh (Windows Task Scheduler)

`scripts/run_refresh.bat` runs `python -m ppi.refresh` and logs to `refresh_log.txt`.
To register it as a monthly scheduled task, run this once (as the user who should own
the task):

```powershell
schtasks /create /tn "RBI PPI Refresh" /tr "C:\Users\Aniket\OneDrive\Desktop\Claude\rbi-ppi\scripts\run_refresh.bat" /sc monthly /d 5 /st 09:00
```

That schedules it for the 5th of each month at 9am (RBI typically publishes mid-month;
adjust `/d` if you want it later). This isn't run automatically by this repo — it's a
one-time opt-in step since it modifies your Windows Task Scheduler.

## Project structure

```
rbi-ppi/
  app.py                    Streamlit dashboard (Overview / Company Explorer / Compare / Data Quality)
  ppi/
    scrape.py                Listing-page scraper (handles the ASP.NET archive postback)
    download.py               Download + validate (ZIP magic bytes + openpyxl) + Playwright fallback
    inspect_schema.py         Phase 0 tool: dumps raw sheet structure for new sample files
    parse.py                  XLSX -> tidy long dataframe (header-text driven, not fixed columns)
    load.py                   Tidy df -> SQLite, with revision replace + diff
    analytics.py               Metrics, period resampling, rankings, movers, insights
    dataquality.py             Data-quality report shared by refresh.py and the dashboard
    refresh.py                 Orchestrates the full pipeline
    config.py                  top_n, default metric, materiality threshold, months_kept
  data/
    raw/                      Downloaded XLSX, one per month
    ppi.db                     SQLite fact store
    entity_aliases.csv         Editable entity-name canonicalization map
  tests/                       Golden parser tests + analytics unit tests
  scripts/run_refresh.bat      Windows Task Scheduler entry point
```

## Known data limitations (confirmed against the source, not assumed)

- **This is throughput, not financials.** RBI reports transaction value, volume and
  instrument counts — there is no revenue, take-rate, margin or funding data here. "Issuer X
  processed ₹Y Cr" says nothing about whether X makes money. Don't read it as business health.
- No gift-card split — RBI's "PPI Cards" category doesn't break gift cards out separately.
- No ₹ float/value-outstanding metric — only *instrument counts* outstanding/active exist.
- `active_count` (Active Instruments) only exists in the source from **2025-11 onward** —
  the pipeline handles this by leaving those months `NaN`, not `0`; see
  `get_metric_wide()`'s docstring in `analytics.py` if you're touching that code.
- The loaded window is `MONTHS_KEPT` months (default 15), so trailing/YoY views are limited
  by it. RBI's archive goes back to 2022 — raise `MONTHS_KEPT` in `ppi/config.py` and re-run
  `refresh` for a longer history.
- Entity history splits across renames until you merge the pair in `data/entity_aliases.csv`.
  The dashboard flags the pairs; it never merges them for you, because that's a judgement call.
