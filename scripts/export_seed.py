"""Export the latest backtest results (one per strategy) to seed/backtests.json.

Run on the local copy that has full NSE history, after Strategy Lab -> Run backtests:
    python scripts/export_seed.py
Commit the file; fresh cloud servers import it on first start (they keep too little history to backtest).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tt import store  # noqa: E402
from tt.config import SEED_DIR  # noqa: E402


def main() -> None:
    rows = store.q("""SELECT b.run_at, b.strategy, b.params, b.metrics, b.trades FROM backtests b
                      JOIN (SELECT strategy, MAX(id) id FROM backtests GROUP BY strategy) m ON b.id = m.id
                      ORDER BY b.strategy""")
    if not rows:
        sys.exit("No backtests in the local database. Run Strategy Lab -> Run backtests first.")
    out = {"exported_at": store.now_ist().isoformat(),
           "note": "Backtests run locally on full NSE history (Jan 2020 - latest).",
           "active_strategy": store.kv_get("active_strategy"),
           "backtest_winner": store.kv_get("backtest_winner"),
           "backtests": rows}
    SEED_DIR.mkdir(exist_ok=True)
    path = SEED_DIR / "backtests.json"
    path.write_text(json.dumps(out), encoding="utf8")
    print(f"Wrote {len(rows)} strategies to {path} (active {out['active_strategy']}, winner {out['backtest_winner']})")


if __name__ == "__main__":
    main()
