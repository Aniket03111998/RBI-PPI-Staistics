"""MoM/3M/YoY deltas, rankings, market share, movers, and templated insights.

`get_metric_wide()` is the single entry point for turning either a METRIC_GROUPS key
(summed raw XLSX-leaf metrics) or a DERIVED_METRICS key (a ratio of two other metrics)
into an entity x month wide table. Everything else — rankings, movers, time series,
rank trend, scorecard — is built on top of that one function. Deltas use pandas'
pivot + pct_change so we never hand-roll date arithmetic.
"""
from __future__ import annotations

import re
import sqlite3

import pandas as pd

METRIC_GROUPS: dict[str, list[str]] = {
    "total_txn_value": ["purchase_txn_value", "fund_transfer_txn_value", "cash_withdrawal_atm_txn_value", "cash_withdrawal_pos_txn_value"],
    "total_txn_volume": ["purchase_txn_volume", "fund_transfer_txn_volume", "cash_withdrawal_atm_txn_volume", "cash_withdrawal_pos_txn_volume"],
    "purchase_txn_value": ["purchase_txn_value"],
    "purchase_txn_volume": ["purchase_txn_volume"],
    "fund_transfer_txn_value": ["fund_transfer_txn_value"],
    "fund_transfer_txn_volume": ["fund_transfer_txn_volume"],
    "cash_withdrawal_txn_value": ["cash_withdrawal_atm_txn_value", "cash_withdrawal_pos_txn_value"],
    "cash_withdrawal_txn_volume": ["cash_withdrawal_atm_txn_volume", "cash_withdrawal_pos_txn_volume"],
    "outstanding_count": ["outstanding_count"],
    "active_count": ["active_count"],
}

METRIC_LABELS = {
    "total_txn_value": "Total Transaction Value (₹'000)",
    "total_txn_volume": "Total Transaction Volume",
    "purchase_txn_value": "Purchase Value (₹'000)",
    "purchase_txn_volume": "Purchase Volume",
    "fund_transfer_txn_value": "Fund Transfer Value (₹'000)",
    "fund_transfer_txn_volume": "Fund Transfer Volume",
    "cash_withdrawal_txn_value": "Cash Withdrawal Value (₹'000)",
    "cash_withdrawal_txn_volume": "Cash Withdrawal Volume",
    "outstanding_count": "PPIs Outstanding",
    "active_count": "PPIs Active",
}

SHORT_LABELS = {
    "total_txn_value": "Total Value",
    "total_txn_volume": "Total Volume",
    "purchase_txn_value": "Purchase Value",
    "purchase_txn_volume": "Purchase Volume",
    "fund_transfer_txn_value": "Fund Transfer Value",
    "fund_transfer_txn_volume": "Fund Transfer Volume",
    "cash_withdrawal_txn_value": "Cash Withdrawal Value",
    "cash_withdrawal_txn_volume": "Cash Withdrawal Volume",
    "outstanding_count": "PPIs Outstanding",
    "active_count": "PPIs Active",
    "active_ratio_pct": "Active Ratio",
    "avg_txn_value_total": "Avg Value/Txn",
    "avg_txn_value_purchase": "Avg Value/Txn (Purchase)",
    "avg_txn_value_fund_transfer": "Avg Value/Txn (Fund Transfer)",
    "avg_txn_value_cash_withdrawal": "Avg Value/Txn (Cash W'draw)",
    "txns_per_ppi": "Txns per PPI",
    "wallet_share_pct": "Wallet Share",
    "cash_reliance_pct": "Cash Reliance",
    "p2m_share_pct": "P2M Share",
    "p2p_share_pct": "P2P Share",
}

# Each derived metric is (numerator_key, numerator_instrument), (denominator_key, denominator_instrument), scale.
# numerator/denominator keys may themselves be METRIC_GROUPS or other DERIVED_METRICS entries.
DERIVED_METRICS: dict[str, dict] = {
    "avg_txn_value_total": {"num": ("total_txn_value", "all"), "den": ("total_txn_volume", "all"), "scale": 1000},
    "avg_txn_value_purchase": {"num": ("purchase_txn_value", "all"), "den": ("purchase_txn_volume", "all"), "scale": 1000},
    "avg_txn_value_fund_transfer": {"num": ("fund_transfer_txn_value", "all"), "den": ("fund_transfer_txn_volume", "all"), "scale": 1000},
    "avg_txn_value_cash_withdrawal": {"num": ("cash_withdrawal_txn_value", "all"), "den": ("cash_withdrawal_txn_volume", "all"), "scale": 1000},
    "active_ratio_pct": {"num": ("active_count", "all"), "den": ("outstanding_count", "all"), "scale": 100},
    "txns_per_ppi": {"num": ("total_txn_volume", "all"), "den": ("outstanding_count", "all"), "scale": 1},
    "cash_reliance_pct": {"num": ("cash_withdrawal_txn_value", "all"), "den": ("total_txn_value", "all"), "scale": 100},
    "wallet_share_pct": {"num": ("total_txn_value", "wallet"), "den": ("total_txn_value", "all"), "scale": 100},
    "p2m_share_pct": {"num": ("purchase_txn_value", "all"), "den": ("total_txn_value", "all"), "scale": 100},
    "p2p_share_pct": {"num": ("fund_transfer_txn_value", "all"), "den": ("total_txn_value", "all"), "scale": 100},
}

DERIVED_METRIC_LABELS = {
    "avg_txn_value_total": "Avg Value per Transaction — Overall (₹)",
    "avg_txn_value_purchase": "Avg Value per Transaction — Purchase (₹)",
    "avg_txn_value_fund_transfer": "Avg Value per Transaction — Fund Transfer (₹)",
    "avg_txn_value_cash_withdrawal": "Avg Value per Transaction — Cash Withdrawal (₹)",
    "active_ratio_pct": "Active Instrument Ratio (%)",
    "txns_per_ppi": "Transactions per PPI (engagement)",
    "cash_reliance_pct": "Cash Reliance — Cash Withdrawal Share of Value (%)",
    "wallet_share_pct": "Wallet Share of Transaction Value (%)",
    "p2m_share_pct": "P2M Share — Merchant Payments as % of Value",
    "p2p_share_pct": "P2P Share — Fund Transfers as % of Value",
}

ALL_METRIC_LABELS = {**METRIC_LABELS, **DERIVED_METRIC_LABELS}

# outstanding_count/active_count are point-in-time snapshots (resample via "last");
# every other METRIC_GROUPS key is a flow that accumulates over the period (resample via "sum").
STOCK_METRICS = {"outstanding_count", "active_count"}

# P2M / P2P split. RBI doesn't use those words, but its own column footnotes define the
# buckets precisely enough to map them:
#   - "purchase of Goods and Services (both at PoS terminal and at online)"  -> paying a
#     merchant, i.e. P2M.
#   - "fund transfer (e.g., from own PPI to other PPIs issued by same or other PPI issuers,
#     PPI to Bank account, etc.)"  -> P2P. Note this bucket also contains PPI-to-own-bank
#     transfers, which are really self-transfers, so P2P here is an upper bound rather than
#     a pure person-to-person figure. The UI says so rather than implying false precision.
#   - cash withdrawal at ATM/PoS -> cash-out, neither P2M nor P2P.
TXN_TYPES: dict[str, tuple[str, list[str]]] = {
    "all": ("All transaction types", []),
    "p2m": ("P2M — merchant payments", ["purchase_txn_value", "purchase_txn_volume"]),
    "p2p": ("P2P — fund transfers", ["fund_transfer_txn_value", "fund_transfer_txn_volume"]),
    "cash": ("Cash withdrawals", ["cash_withdrawal_atm_txn_value", "cash_withdrawal_atm_txn_volume",
                                   "cash_withdrawal_pos_txn_value", "cash_withdrawal_pos_txn_volume"]),
}

# Metrics that only make sense across the full transaction mix. Under a P2M filter the cash
# rows are gone, so "cash reliance" would read 0% — technically true of the filtered slice,
# but it invites the reader to conclude the issuer does no cash business at all.
MIX_ONLY_METRICS = {"cash_reliance_pct", "p2m_share_pct", "p2p_share_pct"}


def filter_txn_type(df: pd.DataFrame, txn_type: str) -> pd.DataFrame:
    """Restrict to one transaction purpose, keeping the instrument-count rows.

    Filtering the fact table once here means every downstream view — rankings, movers, HHI,
    insights, exports — inherits the filter without threading a parameter through all of them.
    Stock metrics survive because a PPI count isn't specific to a transaction purpose; dropping
    them would break outstanding/active and every ratio derived from them.
    """
    if txn_type == "all":
        return df
    keep = set(TXN_TYPES[txn_type][1]) | STOCK_METRICS
    return df[df["metric"].isin(keep)]


def selectable_metrics(txn_type: str) -> list[str]:
    """Metric keys worth offering for the chosen transaction type. Under a type filter the
    per-type breakdowns are either a duplicate of the total (P2M -> Purchase Value) or empty
    (P2M -> Fund Transfer Value), so only the totals and instrument metrics are offered."""
    everything = list(METRIC_GROUPS) + list(DERIVED_METRICS)
    if txn_type == "all":
        return everything
    per_type = {m for _, metrics in TXN_TYPES.values() for m in metrics} | {
        "cash_withdrawal_txn_value", "cash_withdrawal_txn_volume",
        "avg_txn_value_purchase", "avg_txn_value_fund_transfer", "avg_txn_value_cash_withdrawal",
    }
    return [m for m in everything if m not in per_type and m not in MIX_ONLY_METRICS]

# code -> (display label, pandas resample rule or None for "no resampling, raw months",
#          periods-back for a "same period last year" comparison or None if not meaningful,
#          short label used for the "vs previous period" delta column)
#
# The "F" variants are the Indian financial year (April-March) rather than the calendar year:
#   - Quarterly: FY and calendar quarters land on the *same* boundaries (Mar/Jun/Sep/Dec),
#     because the Indian FY starts on a calendar quarter edge. Only the naming differs —
#     the quarter ending Jun 2026 is calendar "Q2 2026" but fiscal "Q1 FY27".
#   - Annual: genuinely different windows. YE = Jan-Dec, YE-MAR = Apr-Mar.
#   - Semi-annual has no calendar variant: pandas' 2QE lands on Mar/Sep whatever anchor you
#     give it, i.e. Apr-Sep and Oct-Mar, which *are* the FY halves Indian issuers report as
#     H1/H2. It's listed as fiscal-only rather than mislabelled as calendar halves.
PERIODS: dict[str, tuple[str, str | None, int | None, str]] = {
    "M": ("Monthly", None, 12, "MoM"),
    "Q": ("Quarterly (calendar)", "QE", 4, "QoQ"),
    "QF": ("Quarterly (FY Apr-Mar)", "QE-MAR", 4, "QoQ"),
    "H": ("Semi-Annual (FY H1/H2)", "2QE", 2, "HoH"),
    "A": ("Annual (calendar)", "YE", None, "YoY"),
    "AF": ("Annual (FY Apr-Mar)", "YE-MAR", None, "YoY"),
}

PERIOD_NOUN = {"M": "month", "Q": "quarter", "QF": "quarter",
                "H": "half-year", "A": "year", "AF": "financial year"}


def fiscal_year(ts: pd.Timestamp) -> int:
    """Indian FY label year for a period-end date. Apr 2025-Mar 2026 is FY26, so any
    month from April onward belongs to the FY named for the following calendar year."""
    return ts.year + 1 if ts.month >= 4 else ts.year


def period_label(ts: pd.Timestamp, period: str) -> str:
    """Human label for a period-end timestamp, e.g. 'Q1 FY27', 'FY26', 'Q2 2026', 'Jun 2026'."""
    if period == "Q":
        return f"Q{(ts.month - 1) // 3 + 1} {ts.year}"
    if period == "QF":
        return f"Q{((ts.month - 4) // 3) % 4 + 1} FY{fiscal_year(ts) % 100:02d}"
    if period == "H":
        return f"H{((ts.month - 4) // 6) % 2 + 1} FY{fiscal_year(ts) % 100:02d}"
    if period == "A":
        return str(ts.year)
    if period == "AF":
        return f"FY{fiscal_year(ts) % 100:02d}"
    return ts.strftime("%b %Y")


MONTHS_PER_PERIOD = {"M": 1, "Q": 3, "QF": 3, "H": 6, "A": 12, "AF": 12}


def period_coverage(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """period_end -> how many source months the loaded data actually contributes, vs how
    many that bucket spans.

    A partially-covered bucket wrecks any comparison involving a flow metric: with data
    ending Jun 2026, the FY27 bucket holds three months while FY26 holds twelve, so a naive
    YoY reads as a ~70% collapse that is purely an artefact of the window. Callers use this
    to suppress or caveat those deltas rather than publish a confident wrong number.
    """
    months = pd.DatetimeIndex(sorted(df["month"].unique()))
    present = pd.Series(1, index=months)
    rule = PERIODS[period][1]
    if rule is not None:
        present = present.resample(rule).sum()
    expected = MONTHS_PER_PERIOD[period]
    return pd.DataFrame({
        "months_present": present,
        "months_expected": expected,
        "is_complete": present >= expected,
    })


def is_partial_period(df: pd.DataFrame, month: pd.Timestamp, period: str) -> bool:
    cov = period_coverage(df, period)
    return bool(month in cov.index and not cov.loc[month, "is_complete"])


def period_span(month: pd.Timestamp, period: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The first and last *source month* a period bucket covers, so the UI can spell out
    'Q1 FY27 (Apr-Jun 2026)' instead of leaving the reader to infer the window."""
    end = pd.Timestamp(year=month.year, month=month.month, day=1)
    start = end - pd.DateOffset(months=MONTHS_PER_PERIOD[period] - 1)
    return start, end


def span_label(month: pd.Timestamp, period: str, df: pd.DataFrame | None = None) -> str:
    """'Apr-Jun 2026'. If `df` is given, the start is clipped to the data actually loaded so
    a part-covered bucket doesn't advertise months that were never downloaded."""
    start, end = period_span(month, period)
    if df is not None:
        first = pd.Timestamp(min(df["month"]))
        start = max(start, first)
    if start == end:
        return start.strftime("%b %Y")
    if start.year == end.year:
        return f"{start.strftime('%b')}–{end.strftime('%b %Y')}"
    return f"{start.strftime('%b %Y')}–{end.strftime('%b %Y')}"


def detect_renames(names) -> dict[str, str]:
    """former_name -> current_name for issuers RBI relabelled, e.g.
    'PhonePe Limited (formerly PhonePe Private Limited)' absorbs 'Phonepe Private Limited …'.

    RBI states the predecessor inline, so this is reading a documented fact rather than
    guessing at similar-looking names. Merging them is what keeps one company's history in
    one series instead of splitting it at the month of the rename.
    """
    names = list(names)
    by_norm = {_norm_name(n): n for n in names}
    mapping: dict[str, str] = {}
    for name in names:
        for former in _former_names(name):
            match = by_norm.get(former) or next(
                (orig for norm, orig in by_norm.items()
                 if norm and orig != name and (norm.startswith(former) or former.startswith(norm))),
                None,
            )
            if match and match != name:
                mapping[match] = name
                break
    # Collapse chains (A->B, B->C  =>  A->C) so every predecessor lands on the current name.
    for former in list(mapping):
        seen = {former}
        while mapping.get(mapping[former]) and mapping[former] not in seen:
            seen.add(mapping[former])
            mapping[former] = mapping[mapping[former]]
    return mapping


def _resample_wide(wide: pd.DataFrame, rule: str, agg: str) -> pd.DataFrame:
    long = wide.T  # DatetimeIndex (month) x entity, so resample operates on the index
    resampled = long.resample(rule).sum(min_count=1) if agg == "sum" else long.resample(rule).last()
    return resampled.T


def load_facts(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql("SELECT * FROM facts", conn)
    df["month"] = pd.to_datetime(df["month"])
    return df


def aggregate(df: pd.DataFrame, metric_group: str, instrument: str = "all") -> pd.DataFrame:
    raw_metrics = METRIC_GROUPS[metric_group]
    sub = df[df["metric"].isin(raw_metrics)]
    if instrument != "all":
        sub = sub[sub["instrument"] == instrument]
    return sub.groupby(["month", "entity", "entity_type"], as_index=False)["value"].sum()


def _wide(g: pd.DataFrame) -> pd.DataFrame:
    wide = g.pivot(index="entity", columns="month", values="value").fillna(0.0)
    return wide[sorted(wide.columns)]


def _entity_type_map(g: pd.DataFrame) -> pd.Series:
    return g.drop_duplicates("entity").set_index("entity")["entity_type"]


def get_metric_wide(df: pd.DataFrame, metric_key: str, instrument: str = "all", period: str = "M") -> pd.DataFrame:
    """entity x period wide table for any METRIC_GROUPS or DERIVED_METRICS key.

    `period` is one of PERIODS' keys ("M"/"Q"/"H"/"A"). Every base-metric table is first
    built at monthly granularity and reindexed to the full month range seen anywhere in
    `df` — without this, a metric that has zero rows for a given month (e.g. `active_count`,
    which only exists in the source from 2025-11 onward — Phase 0 schema drift) simply
    has no column for that month, and `align(fill_value=0.0)` below would "helpfully"
    fill it with 0 instead of leaving it unknown — manufacturing a fake spike in
    pct_change() at that boundary. Only after that is it resampled to the requested
    period: flow metrics (transaction value/volume) sum across the period, stock
    metrics (outstanding/active counts, in STOCK_METRICS) take the period's last reading.

    Derived metrics are numerator-wide / denominator-wide (element-wise, NaN where the
    denominator is 0 or missing) — each side resampled with its own correct rule via the
    recursive call, then divided, so e.g. active_ratio_pct at "Annual" is
    (last active_count of the year) / (last outstanding_count of the year), not a sum of
    monthly ratios. A missing *entity* (e.g. a card-only issuer has no wallet-instrument
    rows at all) is a true zero for that entity, not missing data, so
    `align(fill_value=0.0)` is the right call on the entity axis — just not the month axis.
    """
    if metric_key in METRIC_GROUPS:
        wide = _wide(aggregate(df, metric_key, instrument))
        wide = wide.reindex(columns=sorted(df["month"].unique()))
        rule = PERIODS[period][1]
        if rule is not None:
            agg = "last" if metric_key in STOCK_METRICS else "sum"
            wide = _resample_wide(wide, rule, agg)
        return wide
    if metric_key in DERIVED_METRICS:
        spec = DERIVED_METRICS[metric_key]
        num_key, num_instr = spec["num"]
        den_key, den_instr = spec["den"]
        num = get_metric_wide(df, num_key, num_instr, period)
        den = get_metric_wide(df, den_key, den_instr, period)
        num, den = num.align(den, fill_value=0.0)
        return (num / den.replace(0, float("nan"))) * spec["scale"]
    raise KeyError(f"Unknown metric key: {metric_key!r}")


def industry_total(df: pd.DataFrame, metric_key: str, month: pd.Timestamp, instrument: str = "all", period: str = "M") -> float:
    """Industry-wide figure for a period (month/quarter/half/year, per `period`). For
    METRIC_GROUPS this is a plain sum across entities. For DERIVED_METRICS it recomputes
    numerator-total / denominator-total — summing per-entity ratios (e.g. active_ratio_pct)
    would be meaningless."""
    if metric_key in METRIC_GROUPS:
        w = get_metric_wide(df, metric_key, instrument, period)
        return w[month].sum() if month in w.columns else float("nan")
    if metric_key in DERIVED_METRICS:
        spec = DERIVED_METRICS[metric_key]
        num_key, num_instr = spec["num"]
        den_key, den_instr = spec["den"]
        num_total = industry_total(df, num_key, month, num_instr, period)
        den_total = industry_total(df, den_key, month, den_instr, period)
        return (num_total / den_total) * spec["scale"] if den_total else float("nan")
    raise KeyError(f"Unknown metric key: {metric_key!r}")


def rankings(df: pd.DataFrame, metric_group: str, month: pd.Timestamp, entity_type: str = "all",
             instrument: str = "all", top_n: int = 10, period: str = "M") -> pd.DataFrame:
    wide = get_metric_wide(df, metric_group, instrument, period)
    if month not in wide.columns:
        return pd.DataFrame(columns=["entity", "value", "mom_pct", "share_pct", "share_delta_pp"])
    mom = wide.pct_change(axis=1) * 100
    etmap = _entity_type_map(df)

    res = pd.DataFrame({"value": wide[month], "mom_pct": mom[month]})
    res["entity_type"] = res.index.map(etmap)
    if entity_type != "all":
        res = res[res["entity_type"] == entity_type]

    segment_total = res["value"].sum()
    res["share_pct"] = (res["value"] / segment_total * 100) if segment_total else 0.0

    prev_cols = wide.columns[wide.columns < month]
    if len(prev_cols):
        prev_month = prev_cols.max()
        prev = pd.DataFrame({"value": wide[prev_month]})
        prev["entity_type"] = prev.index.map(etmap)
        if entity_type != "all":
            prev = prev[prev["entity_type"] == entity_type]
        prev_total = prev["value"].sum()
        prev_share = (prev["value"] / prev_total * 100) if prev_total else prev["value"] * 0
        mapped_prev_share = pd.Series(res.index.map(prev_share), index=res.index).fillna(0)
        res["share_delta_pp"] = res["share_pct"] - mapped_prev_share
    else:
        res["share_delta_pp"] = float("nan")

    return res.sort_values("value", ascending=False).head(top_n).reset_index().rename(columns={"index": "entity"})


def movers(df: pd.DataFrame, metric_group: str, month: pd.Timestamp, instrument: str = "all",
           materiality: float = 10.0, top_n: int = 10, period: str = "M",
           entity_type: str = "all") -> dict[str, pd.DataFrame]:
    wide = get_metric_wide(df, metric_group, instrument, period)
    wide = _segment_wide(df, wide, entity_type)
    empty = pd.DataFrame(columns=["value", "prev_value", "delta_abs", "delta_pct"])
    if month not in wide.columns:
        return {"gainers_pct": empty, "losers_pct": empty, "gainers_abs": empty, "losers_abs": empty}
    prev_cols = wide.columns[wide.columns < month]
    if not len(prev_cols):
        return {"gainers_pct": empty, "losers_pct": empty, "gainers_abs": empty, "losers_abs": empty}
    prev_month = prev_cols.max()

    cur, prev = wide[month], wide[prev_month]
    delta_abs = cur - prev
    delta_pct = (cur - prev) / prev.replace(0, float("nan")) * 100
    res = pd.DataFrame({"value": cur, "prev_value": prev, "delta_abs": delta_abs, "delta_pct": delta_pct}).dropna(subset=["delta_pct"], how="all")

    material = res[res["prev_value"] >= materiality]
    return {
        "gainers_pct": material.sort_values("delta_pct", ascending=False).head(top_n).reset_index().rename(columns={"index": "entity"}),
        "losers_pct": material.sort_values("delta_pct", ascending=True).head(top_n).reset_index().rename(columns={"index": "entity"}),
        "gainers_abs": res.sort_values("delta_abs", ascending=False).head(top_n).reset_index().rename(columns={"index": "entity"}),
        "losers_abs": res.sort_values("delta_abs", ascending=True).head(top_n).reset_index().rename(columns={"index": "entity"}),
    }


def entity_timeseries(df: pd.DataFrame, entity: str, period: str = "M") -> pd.DataFrame:
    """One row per period with a column per metric (base + derived), summed across instrument."""
    sub = df[df["entity"] == entity]
    cols = {}
    for key in list(METRIC_GROUPS) + list(DERIVED_METRICS):
        w = get_metric_wide(sub, key, period=period)
        cols[key] = w.loc[entity] if entity in w.index else pd.Series(dtype=float)
    out = pd.DataFrame(cols).sort_index()
    return out


def entity_instrument_split(df: pd.DataFrame, entity: str, metric_group: str, period: str = "M") -> pd.DataFrame:
    sub = df[(df["entity"] == entity) & (df["metric"].isin(METRIC_GROUPS[metric_group]))]
    wide = sub.groupby(["month", "instrument"], as_index=False)["value"].sum().pivot(index="month", columns="instrument", values="value").fillna(0.0).sort_index()
    rule = PERIODS[period][1]
    if rule is not None:
        wide = wide.resample(rule).sum(min_count=1)
    return wide


def rank_series(df: pd.DataFrame, metric_group: str, entity_type: str, instrument: str = "all", period: str = "M") -> pd.DataFrame:
    wide = get_metric_wide(df, metric_group, instrument, period)
    if entity_type != "all":
        etmap = _entity_type_map(df)
        wide = wide[wide.index.map(etmap) == entity_type]
    return wide.rank(ascending=False, method="min", axis=0)


def instrument_mix(df: pd.DataFrame) -> pd.Series:
    """entity -> 'card' | 'wallet' | 'both' | 'neither', based on all transaction value history."""
    sub = df[df["metric"].isin(METRIC_GROUPS["total_txn_value"])]
    piv = sub.groupby(["entity", "instrument"])["value"].sum().unstack(fill_value=0.0)
    has_card = piv.get("card", 0.0) > 0
    has_wallet = piv.get("wallet", 0.0) > 0
    return pd.Series(
        [("both" if c and w else "card" if c else "wallet" if w else "neither") for c, w in zip(has_card, has_wallet)],
        index=piv.index,
    )


def scorecard(df: pd.DataFrame, entity: str, period: str = "M",
               month: pd.Timestamp | None = None) -> pd.DataFrame:
    """Up/down/flat verdict per metric: vs the previous period, and (when meaningful for
    the chosen period) vs the same period a year ago.

    `month` pins which period is "latest". Without it the scorecard silently reports the
    newest bucket regardless of what the caller selected — which goes wrong the moment the
    newest bucket is a part-period stub the rest of the page is deliberately not showing.
    """
    ts = entity_timeseries(df, entity, period=period)
    if month is not None:
        ts = ts.loc[ts.index <= month]
    _, _, yoy_periods, delta_label = PERIODS[period]
    cols = ["metric", "label", "latest", "delta_pct", "delta_verdict", "delta_label"]
    if yoy_periods is not None:
        cols += ["yoy_pct", "yoy_verdict"]
    if ts.empty or len(ts) < 2:
        return pd.DataFrame(columns=cols)
    delta = ts.pct_change(periods=1) * 100
    yoy = ts.pct_change(periods=yoy_periods) * 100 if yoy_periods is not None else None
    rows = []
    for m in list(METRIC_GROUPS) + list(DERIVED_METRICS):
        if m not in ts.columns:
            continue
        latest = ts[m].iloc[-1]
        delta_v = delta[m].iloc[-1] if m in delta.columns else float("nan")
        row = {
            "metric": m, "label": ALL_METRIC_LABELS[m], "latest": latest,
            "delta_pct": delta_v, "delta_verdict": _verdict(delta_v), "delta_label": delta_label,
        }
        if yoy is not None:
            yoy_v = yoy[m].iloc[-1] if m in yoy.columns else float("nan")
            row["yoy_pct"] = yoy_v
            row["yoy_verdict"] = _verdict(yoy_v)
        rows.append(row)
    return pd.DataFrame(rows)


def _verdict(pct: float, flat_band: float = 1.0) -> str:
    if pd.isna(pct):
        return "n/a"
    if pct > flat_band:
        return "improving"
    if pct < -flat_band:
        return "deteriorating"
    return "flat"


# --- market-structure / competitive-dynamics views ---------------------------------
# Everything below answers "how is the market shaped and who is winning", as opposed to
# the per-entity views above. All of it is built on get_metric_wide() so it inherits
# period resampling and the NaN-vs-0 handling for free.
#
# Market share is only meaningful for metrics that sum across entities. A ratio metric
# (avg value per txn, active %) has no "share of market", so HHI and growth attribution
# reject DERIVED_METRICS rather than returning a confident-looking wrong number.


def is_additive(metric_key: str) -> bool:
    """True for metrics where summing across entities is meaningful (so market share,
    HHI and growth attribution are defined). False for ratio/derived metrics."""
    return metric_key in METRIC_GROUPS


def _segment_wide(df: pd.DataFrame, wide: pd.DataFrame, entity_type: str) -> pd.DataFrame:
    if entity_type == "all":
        return wide
    return wide[wide.index.map(_entity_type_map(df)) == entity_type]


def hhi(df: pd.DataFrame, metric_key: str, instrument: str = "all", period: str = "M") -> pd.DataFrame:
    """Herfindahl-Hirschman Index per period, per segment (0-10000 scale).

    Sum of squared percentage market shares. The US DOJ/FTC reading of the scale:
    below 1500 = unconcentrated, 1500-2500 = moderately concentrated, above 2500 =
    concentrated. Rising = consolidating toward a few winners; falling = fragmenting.
    """
    if not is_additive(metric_key):
        raise ValueError(f"HHI needs an additive metric, got {metric_key!r}")
    wide = get_metric_wide(df, metric_key, instrument, period)
    out = {}
    for seg in ("all", "bank", "non_bank"):
        sub = _segment_wide(df, wide, seg)
        totals = sub.sum(axis=0).replace(0, float("nan"))
        shares = sub.div(totals, axis=1) * 100
        out[seg] = (shares ** 2).sum(axis=0, min_count=1)
    return pd.DataFrame(out)


def trailing_growth(df: pd.DataFrame, metric_key: str, month: pd.Timestamp, periods_back: int,
                     entity_type: str = "all", instrument: str = "all", top_n: int = 10,
                     materiality: float = 0.0, period: str = "M") -> pd.DataFrame:
    """Growth over the trailing N periods (not just vs the previous one) — the screening
    view for "who has compounded fastest", which a single-period MoM can't show."""
    wide = get_metric_wide(df, metric_key, instrument, period)
    wide = _segment_wide(df, wide, entity_type)
    cols = [c for c in wide.columns if c <= month]
    if len(cols) <= periods_back:
        return pd.DataFrame(columns=["entity", "value", "base_value", "growth_pct"])
    cur, base = wide[cols[-1]], wide[cols[-1 - periods_back]]
    res = pd.DataFrame({"value": cur, "base_value": base})
    res["growth_pct"] = (cur - base) / base.replace(0, float("nan")) * 100
    res = res[res["base_value"] >= materiality].dropna(subset=["growth_pct"])
    return (res.sort_values("growth_pct", ascending=False).head(top_n)
               .reset_index().rename(columns={"index": "entity"}))


def share_of_growth(df: pd.DataFrame, metric_key: str, month: pd.Timestamp, entity_type: str = "all",
                     instrument: str = "all", top_n: int = 10, period: str = "M") -> pd.DataFrame:
    """Each entity's share of the segment's *net* change vs the previous period.

    Contributions sum to 100% by construction. They can exceed 100% or go negative when
    gainers and losers offset each other (an entity that grew while the market shrank
    gets a negative contribution to the decline) — that is the correct reading, not a bug.
    """
    if not is_additive(metric_key):
        raise ValueError(f"Growth attribution needs an additive metric, got {metric_key!r}")
    wide = get_metric_wide(df, metric_key, instrument, period)
    wide = _segment_wide(df, wide, entity_type)
    prev_cols = [c for c in wide.columns if c < month]
    if month not in wide.columns or not prev_cols:
        return pd.DataFrame(columns=["entity", "delta_abs", "contribution_pct"])
    delta = (wide[month] - wide[max(prev_cols)]).dropna()
    net = delta.sum()
    res = pd.DataFrame({"delta_abs": delta})
    res["contribution_pct"] = delta / net * 100 if net else float("nan")
    res = res.reindex(res["delta_abs"].abs().sort_values(ascending=False).index).head(top_n)
    return res.reset_index().rename(columns={"index": "entity"})


_PAREN_RE = re.compile(r"\([^)]*\)")
_FORMERLY_RE = re.compile(r"\(\s*[Ff]ormerly\s*(?:known as\s*)?(.*?)\)")
_FORMERLY_DISPLAY_RE = re.compile(r"\s*\([Ff]ormerly[^)]*\)")  # strips "(formerly ...)" for display only


def _norm_name(s: str) -> str:
    """Comparable form of an entity name: parentheticals dropped, non-alphanumerics
    stripped, lowercased. RBI's casing and punctuation drift between releases
    ('PhonePe' vs 'Phonepe', 'Pvt. Ltd.' vs 'Private Limited')."""
    return re.sub(r"[^a-z0-9]", "", _PAREN_RE.sub("", s).lower())


def _former_names(s: str) -> list[str]:
    """Prior names RBI annotates inline, e.g. 'X Limited (formerly Y Limited)' -> ['ylimited'].
    A 'formerly A and B' clause names two predecessors, so it splits into both."""
    out = []
    for inner in _FORMERLY_RE.findall(s):
        for part in re.split(r"\band\b", inner):
            norm = re.sub(r"[^a-z0-9]", "", part.lower())
            if norm:
                out.append(norm)
    return out


def entry_exit(df: pd.DataFrame, metric_key: str = "total_txn_value", instrument: str = "all",
                period: str = "M") -> dict[str, pd.DataFrame]:
    """Entities whose activity started after, or stopped before, the loaded window.

    Deliberately ignores entities active in the first/last loaded period — being present
    at the edge of the window says nothing about entry or exit (left/right censoring),
    only that we can't see past it.

    Renames are separated out from genuine entries/exits. RBI re-labels an issuer and
    keeps trading (PhonePe Private Limited -> PhonePe Limited, North East SFB -> Slice
    SFB), which naively reads as one company appearing and another vanishing in the same
    month. Reporting that as market entry/exit would be plainly wrong, so a new name whose
    inline '(formerly X)' clause points at a name that stopped reporting is paired off
    into `renames` instead. Merge the pair in data/entity_aliases.csv to join their history.
    """
    wide = get_metric_wide(df, metric_key, instrument, period)
    active = wide.fillna(0.0) > 0
    active, cols = active[active.any(axis=1)], list(wide.columns)
    first = active.idxmax(axis=1)
    last = active.iloc[:, ::-1].idxmax(axis=1)

    entrants = pd.DataFrame({"entity": first.index, "first_active": first.values})
    exits = pd.DataFrame({"entity": last.index, "last_active": last.values})
    entrants = entrants[entrants["first_active"] > cols[0]]
    exits = exits[exits["last_active"] < cols[-1]]

    exit_by_name = {_norm_name(e): e for e in exits["entity"]}
    renames = []
    for _, row in entrants.iterrows():
        for former in _former_names(row["entity"]):
            match = exit_by_name.get(former) or next(
                (n for k, n in exit_by_name.items() if k and (k.startswith(former) or former.startswith(k))), None
            )
            if match:
                renames.append({"former_name": match, "current_name": row["entity"], "renamed_on": row["first_active"]})
                break

    renamed_new = {r["current_name"] for r in renames}
    renamed_old = {r["former_name"] for r in renames}
    return {
        "entrants": entrants[~entrants["entity"].isin(renamed_new)].sort_values("first_active", ascending=False).reset_index(drop=True),
        "exits": exits[~exits["entity"].isin(renamed_old)].sort_values("last_active", ascending=False).reset_index(drop=True),
        "renames": pd.DataFrame(renames, columns=["former_name", "current_name", "renamed_on"]),
    }


def peer_benchmark(df: pd.DataFrame, entity: str, metric_key: str, month: pd.Timestamp,
                    instrument: str = "all", period: str = "M") -> dict:
    """One entity against its own segment: level, percentile, and — the part that
    actually matters — whether it grew faster or slower than the median peer."""
    wide = get_metric_wide(df, metric_key, instrument, period)
    etmap = _entity_type_map(df)
    if entity not in etmap.index or month not in wide.columns:
        return {}
    peers = _segment_wide(df, wide, etmap[entity])
    cur = peers[month].dropna()
    if entity not in cur.index or cur.empty:
        return {}
    value = cur[entity]
    out = {
        "value": value,
        "median": cur.median(),
        "p75": cur.quantile(0.75),
        "percentile": (cur < value).mean() * 100,
        "n_peers": int(cur.size),
        "growth_pct": float("nan"),
        "peer_median_growth_pct": float("nan"),
    }
    prev_cols = [c for c in peers.columns if c < month]
    if prev_cols:
        prev = peers[max(prev_cols)]
        growth = (peers[month] - prev) / prev.replace(0, float("nan")) * 100
        out["growth_pct"] = growth.get(entity, float("nan"))
        out["peer_median_growth_pct"] = growth.median()
    return out


def generate_insights(df: pd.DataFrame, month: pd.Timestamp, materiality: float = 10.0, max_insights: int = 8,
                       period: str = "M") -> list[dict]:
    """Deterministic, templated insight generation — no LLM in the default path."""
    insights = []
    _, _, _, delta_label = PERIODS[period]
    noun = PERIOD_NOUN[period]
    partial = is_partial_period(df, month, period)
    if partial:
        cov = period_coverage(df, period).loc[month]
        insights.append({
            "text": f"{period_label(month, period)} covers only {int(cov['months_present'])} of "
                    f"{int(cov['months_expected'])} months so far — {delta_label} comparisons below are "
                    f"suppressed because a part-period against a full one is not a like-for-like change.",
            "direction": "down", "entity": None, "series": [],
        })

    # 1. industry headline
    wide = get_metric_wide(df, "total_txn_value", period=period)
    if month in wide.columns:
        prev_cols = wide.columns[wide.columns < month]
        cur_total = wide[month].sum()
        if len(prev_cols) and not partial:
            prev_total = wide[prev_cols.max()].sum()
            pct = (cur_total - prev_total) / prev_total * 100 if prev_total else float("nan")
            insights.append({
                "text": f"Industry-wide PPI transaction value {'grew' if pct >= 0 else 'fell'} {abs(pct):.1f}% {delta_label} to ₹{cur_total/1e5:,.0f} Cr — "
                        f"implication: {'demand for prepaid instruments is expanding' if pct >= 0 else 'a broad-based slowdown, worth cross-checking against seasonality'}.",
                "direction": "up" if pct >= 0 else "down",
                "entity": None, "series": wide.sum(axis=0).tail(6).tolist(),
            })

    # 2. segment split (bank vs non-bank share of total_txn_value)
    etmap = _entity_type_map(df)
    if month in wide.columns:
        by_seg = wide[month].groupby(etmap).sum()
        seg_total = by_seg.sum()
        if seg_total and "non_bank" in by_seg.index:
            nb_share = by_seg.get("non_bank", 0) / seg_total * 100
            insights.append({
                "text": f"Non-bank (fintech) issuers hold {nb_share:.1f}% of total transaction value this {noun} — "
                        f"implication: {'fintechs continue to out-compete banks on PPI usage' if nb_share > 50 else 'banks still lead PPI transaction value despite fintech growth'}.",
                "direction": "up" if nb_share > 50 else "down", "entity": None, "series": [],
            })

    # 3/4. biggest gainer/loser by % (materiality-filtered), non-bank segment, purchase value
    mv = ({"gainers_pct": pd.DataFrame(), "losers_pct": pd.DataFrame()} if partial
          else movers(df, "purchase_txn_value", month, materiality=materiality, top_n=1, period=period))
    if not mv["gainers_pct"].empty:
        row = mv["gainers_pct"].iloc[0]
        insights.append({
            "text": f"{row['entity']} led purchase-value growth, up {row['delta_pct']:.1f}% {delta_label} to ₹{row['value']/1e5:,.1f} Cr — "
                    f"implication: watch for continued share consolidation among top issuers.",
            "direction": "up", "entity": row["entity"], "series": _entity_series(df, row["entity"], "purchase_txn_value", period),
        })
    if not mv["losers_pct"].empty:
        row = mv["losers_pct"].iloc[0]
        insights.append({
            "text": f"{row['entity']} saw purchase value drop {abs(row['delta_pct']):.1f}% {delta_label} to ₹{row['value']/1e5:,.1f} Cr — "
                    f"implication: worth checking for a one-off dip vs a sustained decline.",
            "direction": "down", "entity": row["entity"], "series": _entity_series(df, row["entity"], "purchase_txn_value", period),
        })

    # 5. outstanding_count sustained decline (2+ consecutive periods)
    ow = get_metric_wide(df, "outstanding_count", period=period)
    ow = ow[sorted(ow.columns)]
    if ow.shape[1] >= 3 and month in ow.columns:
        cols = list(ow.columns)
        idx = cols.index(month)
        if idx >= 2:
            m0, m1, m2 = ow[cols[idx - 2]], ow[cols[idx - 1]], ow[cols[idx]]
            declining = ow.index[(m2 < m1) & (m1 < m0) & (m0 > 0)]
            for entity in declining[:2]:
                insights.append({
                    "text": f"{entity} PPI outstanding fell for a second straight {noun} (now {m2[entity]:,.0f}) — "
                            f"implication: possible portfolio run-off or dormancy cleanup worth watching.",
                    "direction": "down", "entity": entity, "series": _entity_series(df, entity, "outstanding_count", period),
                })

    return insights[:max_insights]


def _entity_series(df: pd.DataFrame, entity: str, metric_group: str, period: str = "M", n: int = 6) -> list[float]:
    w = get_metric_wide(df[df["entity"] == entity], metric_group, period=period)
    if entity not in w.index:
        return []
    return w.loc[entity].sort_index().tail(n).tolist()


def _cr(value_thousands: float) -> str:
    """₹'000 -> a readable ₹ figure. Below a crore, Cr rounds to 0.0 and reads as nothing."""
    cr = value_thousands / 1e5
    return f"₹{cr:,.1f} Cr" if abs(cr) >= 1 else f"₹{value_thousands:,.0f} K"


def generate_entity_insights(df: pd.DataFrame, entity: str, month: pd.Timestamp, period: str = "M",
                              max_insights: int = 6) -> list[dict]:
    """Templated, deterministic insights for one issuer at the selected period grain.

    Every number is recomputed from the resampled series for `period`, so switching
    Monthly -> Quarterly -> FY changes both the figures and the wording, rather than
    restating a monthly fact under a quarterly heading.
    """
    ts = entity_timeseries(df, entity, period=period)
    if ts.empty:
        return []
    ts = ts.loc[ts.index <= month]
    if len(ts) < 2:
        # One bucket of history: real at this grain, just not comparable yet. Say so rather
        # than rendering an empty panel that looks broken. Annual needs ~24 months loaded.
        return [{
            "text": f"{period_label(month, period)} is the first {PERIOD_NOUN[period]} of loaded history for this "
                    f"issuer, so there is nothing to compare it against yet — implication: use a shorter period, "
                    f"or raise MONTHS_KEPT in ppi/config.py and re-run the refresh to load more history.",
            "direction": "down", "series": [],
        }]

    noun = PERIOD_NOUN[period]
    delta_label = PERIODS[period][3]
    etmap = _entity_type_map(df)
    entity_type = etmap.get(entity, "non_bank")
    seg_name = "banks" if entity_type == "bank" else "non-bank issuers"
    out: list[dict] = []

    # A part-covered bucket makes every flow delta meaningless (a 3-month stub against a
    # full year reads as a collapse). Report the level and say why, rather than the delta.
    cov = period_coverage(df, period)
    if month in cov.index and not cov.loc[month, "is_complete"]:
        have, want = int(cov.loc[month, "months_present"]), int(cov.loc[month, "months_expected"])
        value = ts["total_txn_value"].iloc[-1] if "total_txn_value" in ts.columns else float("nan")
        return [{
            "text": f"{period_label(month, period)} is only {have} of {want} months of data so far, so "
                    f"change-vs-previous figures would compare a part-period against a full one. "
                    f"Transaction value to date is {_cr(value)} — implication: wait for the period to "
                    f"complete, or switch to Monthly, before reading a trend into it.",
            "direction": "down", "series": [],
        }]

    def change(col: str) -> tuple[float, float, float]:
        if col not in ts.columns:
            return float("nan"), float("nan"), float("nan")
        cur, prev = ts[col].iloc[-1], ts[col].iloc[-2]
        pct = (cur - prev) / prev * 100 if prev and pd.notna(prev) and pd.notna(cur) else float("nan")
        return cur, prev, pct

    def add(text: str, up: bool, series_col: str | None = None) -> None:
        series = ts[series_col].tail(6).tolist() if series_col and series_col in ts.columns else []
        out.append({"text": text, "direction": "up" if up else "down",
                    "series": [v for v in series if pd.notna(v)]})

    # 1. headline throughput
    cur, _, pct = change("total_txn_value")
    if pd.notna(pct):
        add(f"Transaction value {'grew' if pct >= 0 else 'fell'} {abs(pct):.1f}% {delta_label} to {_cr(cur)} "
            f"— implication: {'throughput is expanding on this issuer' if pct >= 0 else 'throughput is contracting, worth separating seasonality from genuine attrition'}.",
            pct >= 0, "total_txn_value")

    # 2. growth vs peers — the share question, which a standalone growth rate can't answer
    pb = peer_benchmark(df, entity, "total_txn_value", month, period=period)
    if pb and pd.notna(pb.get("growth_pct")) and pd.notna(pb.get("peer_median_growth_pct")):
        gap = pb["growth_pct"] - pb["peer_median_growth_pct"]
        add(f"Grew {pb['growth_pct']:+.1f}% {delta_label} against a median {pb['peer_median_growth_pct']:+.1f}% across "
            f"{pb['n_peers'] - 1} peer {seg_name} — implication: "
            f"{'outpacing the segment, so this is share gain rather than a rising tide' if gap > 0 else 'lagging the segment, so it is ceding share even if volumes look flat'}.",
            gap > 0)

    # 3. rank movement within segment
    ranks = rank_series(df, "total_txn_value", entity_type, period=period)
    if entity in ranks.index:
        row = ranks.loc[entity].sort_index()
        row = row.loc[row.index <= month].dropna()
        if len(row) >= 2 and row.iloc[-1] != row.iloc[-2]:
            moved = int(row.iloc[-2] - row.iloc[-1])
            add(f"Moved {'up' if moved > 0 else 'down'} {abs(moved)} place{'s' if abs(moved) != 1 else ''} to rank "
                f"#{int(row.iloc[-1])} among {seg_name} — implication: "
                f"{'climbing the table, a durable move if it holds for another period' if moved > 0 else 'slipping down the table; check whether peers grew or this issuer shrank'}.",
                moved > 0)

    # 4. instrument mix shift
    cur, prev, _ = change("wallet_share_pct")
    if pd.notna(cur) and pd.notna(prev) and abs(cur - prev) >= 2:
        up = cur > prev
        add(f"Wallets now carry {cur:.1f}% of transaction value, {'up' if up else 'down'} {abs(cur - prev):.1f}pp on the prior {noun} "
            f"— implication: the mix is shifting toward {'wallets' if up else 'cards'}, which changes the economics and the fraud surface.",
            up, "wallet_share_pct")

    # 5. engagement per instrument
    cur, _, pct = change("txns_per_ppi")
    if pd.notna(pct) and abs(pct) >= 5:
        add(f"Transactions per PPI {'rose' if pct >= 0 else 'fell'} {abs(pct):.1f}% to {cur:.2f} — implication: "
            f"{'the existing base is being used harder, not just growing' if pct >= 0 else 'the instrument base is growing faster than usage, a dormancy warning'}.",
            pct >= 0, "txns_per_ppi")

    # 6. ticket size
    cur, _, pct = change("avg_txn_value_total")
    if pd.notna(pct) and abs(pct) >= 5:
        add(f"Average ticket size {'rose' if pct >= 0 else 'fell'} {abs(pct):.1f}% to ₹{cur:,.0f} — implication: "
            f"{'higher-value use cases are taking hold' if pct >= 0 else 'the mix is tilting to smaller, more frequent payments'}.",
            pct >= 0, "avg_txn_value_total")

    # 7. issuance outrunning usage
    _, _, out_pct = change("outstanding_count")
    _, _, vol_pct = change("total_txn_volume")
    if pd.notna(out_pct) and pd.notna(vol_pct) and out_pct > 2 and vol_pct < 0:
        add(f"PPIs outstanding rose {out_pct:.1f}% while transaction volume fell {abs(vol_pct):.1f}% — implication: "
            f"new instruments are being issued faster than they are being used; watch activation.",
            False, "outstanding_count")

    # 8. sustained direction
    series = ts["total_txn_value"].dropna()
    if len(series) >= 3:
        diffs = series.diff().dropna()
        streak, sign = 0, (1 if diffs.iloc[-1] > 0 else -1)
        for d in reversed(diffs.tolist()):
            if (d > 0) == (sign > 0) and d != 0:
                streak += 1
            else:
                break
        if streak >= 3:
            add(f"Transaction value has {'risen' if sign > 0 else 'fallen'} for {streak} consecutive {noun}s — "
                f"implication: {'a trend rather than a one-off, and a base effect to expect later' if sign > 0 else 'a sustained decline, not a single bad period'}.",
                sign > 0, "total_txn_value")

    return out[:max_insights]


def hhi_reading(value: float) -> str:
    """The DOJ/FTC band a given HHI falls in, in plain words."""
    if pd.isna(value):
        return "no reading"
    if value < 1500:
        return "unconcentrated — no issuer is close to dominant"
    if value < 2500:
        return "moderately concentrated — a handful of issuers carry most of the volume"
    return "highly concentrated — the segment is dominated by very few issuers"


def generate_compare_insights(df: pd.DataFrame, entities: list[str], metric_key: str,
                               month: pd.Timestamp, instrument: str = "all", period: str = "M",
                               max_insights: int = 6) -> list[dict]:
    """Insights about the selected set *relative to each other* — the question the Compare
    page is actually asking, which per-entity insights can't answer."""
    if len(entities) < 2:
        return []
    wide = get_metric_wide(df, metric_key, instrument, period)
    present = [e for e in entities if e in wide.index]
    if len(present) < 2 or month not in wide.columns:
        return []

    label = ALL_METRIC_LABELS[metric_key]
    delta_label = PERIODS[period][3]
    cur = wide.loc[present, month].dropna()
    if cur.empty:
        return []
    out: list[dict] = []

    def fmt(v: float) -> str:
        if metric_key.endswith("_pct"):
            return f"{v:.1f}%"
        if metric_key.startswith("avg_txn_value"):
            return f"₹{v:,.0f}"
        if "value" in metric_key:
            return _cr(v)
        return f"{v:,.0f}"

    # 1. who leads, and by how much
    leader, trailer = cur.idxmax(), cur.idxmin()
    if leader != trailer:
        ratio = cur[leader] / cur[trailer] if cur[trailer] else float("inf")
        gap = (f"{ratio:,.1f}x the smallest ({trailer} at {fmt(cur[trailer])})"
               if pd.notna(ratio) and ratio != float("inf") else f"against {trailer} at effectively zero")
        out.append({
            "text": f"{leader} leads this comparison on {label} at {fmt(cur[leader])} — {gap}. "
                    f"Implication: the selection spans very different scales, so read the growth rates "
                    f"below rather than the absolute lines.",
            "direction": "up", "entity": leader, "series": [],
        })

    # 2. combined share of the whole market
    total = wide[month].sum()
    if total:
        share = cur.sum() / total * 100
        out.append({
            "text": f"Together these {len(cur)} issuers account for {share:.1f}% of all-issuer {label} "
                    f"in {period_label(month, period)} — implication: "
                    f"{'this selection effectively is the market' if share > 50 else 'most of the market sits outside this selection, so treat it as a peer set rather than the whole picture'}.",
            "direction": "up" if share > 50 else "down", "entity": None, "series": [],
        })

    # 3/4. fastest and slowest growth across the set
    prev_cols = [c for c in wide.columns if c < month]
    if prev_cols and not is_partial_period(df, month, period):
        prev = wide.loc[present, max(prev_cols)]
        growth = ((wide.loc[present, month] - prev) / prev.replace(0, float("nan")) * 100).dropna()
        if len(growth) >= 2:
            fast, slow = growth.idxmax(), growth.idxmin()
            out.append({
                "text": f"{fast} grew fastest at {growth[fast]:+.1f}% {delta_label}, while {slow} managed "
                        f"{growth[slow]:+.1f}% — a {abs(growth[fast] - growth[slow]):.1f}pp spread. "
                        f"Implication: {'they are diverging, so relative position is shifting inside this set' if abs(growth[fast] - growth[slow]) > 5 else 'they are moving broadly together, suggesting a shared market driver rather than company-specific execution'}.",
                "direction": "up", "entity": fast, "series": [],
            })
            shrinking = growth[growth < 0]
            if 0 < len(shrinking) < len(growth):
                out.append({
                    "text": f"{len(shrinking)} of {len(growth)} in this set declined {delta_label} "
                            f"({', '.join(shrinking.index[:3])}{'…' if len(shrinking) > 3 else ''}) while the rest grew — "
                            f"implication: this is company-specific, not a sector-wide move.",
                    "direction": "down", "entity": None, "series": [],
                })

    # 5. concentration inside the selected set
    if is_additive(metric_key) and cur.sum() > 0:
        top_share = cur.max() / cur.sum() * 100
        out.append({
            "text": f"The largest of the selected issuers holds {top_share:.1f}% of the group's combined {label} — "
                    f"implication: {'the comparison is dominated by one name; consider charting it separately' if top_share > 60 else 'the group is reasonably balanced, so the chart is readable without rescaling'}.",
            "direction": "down" if top_share > 60 else "up", "entity": None, "series": [],
        })

    return out[:max_insights]
