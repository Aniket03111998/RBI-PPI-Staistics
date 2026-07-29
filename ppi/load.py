"""Tidy dataframe -> SQLite, with revision replace-and-diff."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date

import pandas as pd

from ppi.config import DB_PATH

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    month TEXT NOT NULL,
    entity_raw TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    instrument TEXT,
    metric TEXT NOT NULL,
    unit TEXT NOT NULL,
    value REAL,
    source_file TEXT NOT NULL,
    is_revised INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_month ON facts(month);
CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def load_month(conn: sqlite3.Connection, df: pd.DataFrame, month: date, is_revised: bool) -> dict:
    """Replace all rows for `month` with `df`. Returns a diff summary if a revision replaced prior data."""
    month_str = month.isoformat()
    prior = pd.read_sql(
        "SELECT entity, metric, value FROM facts WHERE month = ?", conn, params=(month_str,)
    )
    diff = {"month": month_str, "prior_rows": len(prior), "new_rows": len(df), "changed": []}

    if not prior.empty:
        merged = prior.merge(
            df[["entity", "metric", "value"]], on=["entity", "metric"], suffixes=("_old", "_new"), how="outer"
        )
        changed = merged[(merged["value_old"] != merged["value_new"])]
        diff["changed"] = changed.to_dict("records")

    conn.execute("DELETE FROM facts WHERE month = ?", (month_str,))
    out = df.copy()
    out["month"] = out["month"].astype(str)
    out["is_revised"] = out["is_revised"].astype(int)
    out.to_sql("facts", conn, if_exists="append", index=False)
    conn.commit()
    return diff


def entities_in_db(conn: sqlite3.Connection) -> set[str]:
    return set(r[0] for r in conn.execute("SELECT DISTINCT entity FROM facts"))
