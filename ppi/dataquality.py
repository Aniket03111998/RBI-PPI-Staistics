"""Data-quality reporting shared by `refresh.py` (console) and the dashboard's Data Quality page."""
from __future__ import annotations

import difflib

import pandas as pd

from ppi.parse import load_aliases


def unmerged_alias_suggestions(cutoff: float = 0.95) -> pd.DataFrame:
    """Entities still mapped to themselves (never manually reviewed) that fuzzy-match
    another canonical name in the alias file — candidates for merging in
    data/entity_aliases.csv. Never merges automatically; this is a suggestion list.

    Cutoff is deliberately high (0.95): most PPI issuer names share generic corporate
    boilerplate ("... Bank Limited", "... Private Limited"), which a lower cutoff flags
    as false-positive near-duplicates between genuinely different companies (e.g. "Fino
    Payments Bank Limited" vs "Jio Payments Bank Limited"). Renames RBI itself annotates
    — e.g. "PhonePe Limited (formerly PhonePe Private Limited)" — are already
    self-documenting in the raw name and don't need fuzzy detection here.
    """
    aliases = load_aliases()
    canonicals = sorted(set(aliases.values()))
    rows = []
    for raw, canonical in aliases.items():
        if raw != canonical:
            continue  # already reviewed/merged by a human
        pool = [c for c in canonicals if c != raw]
        matches = difflib.get_close_matches(raw, pool, n=2, cutoff=cutoff)
        if matches:
            rows.append({"entity": raw, "possible_match": ", ".join(matches)})
    return pd.DataFrame(rows, columns=["entity", "possible_match"])


def month_row_counts(df: pd.DataFrame) -> pd.DataFrame:
    counts = df.groupby("month").size().reset_index(name="rows").sort_values("month")
    counts["month"] = pd.to_datetime(counts["month"]).dt.strftime("%Y-%m")
    return counts


def revised_months(df: pd.DataFrame) -> list[str]:
    revised = df.loc[df["is_revised"].astype(bool), "month"].unique()
    return sorted(pd.to_datetime(revised).strftime("%Y-%m").tolist())
