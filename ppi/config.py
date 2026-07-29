from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "ppi.db"
ALIASES_PATH = ROOT / "data" / "entity_aliases.csv"

MONTHS_KEPT = 15  # download buffer above the 12-month target
TOP_N = 10
DEFAULT_METRIC = "total_txn_value"
MATERIALITY_THRESHOLD = 10.0  # min prior-month base (in the metric's native unit) to qualify for % movers
