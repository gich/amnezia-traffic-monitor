#!/usr/bin/env python3
"""Rebuild the peer_daily rollup from existing peer_samples.

Normally unnecessary: the collector populates peer_daily incrementally on every
tick, and init_schema does a one-shot backfill the first time the table is empty
(i.e. right after deploying this feature). Use this script to *force* a full
recompute — e.g. after manually editing peer_samples, or to re-derive the rollup.

    .venv/bin/python scripts/backfill_daily.py            # uses config.toml
    .venv/bin/python scripts/backfill_daily.py --config /path/to/config.toml

Day boundaries use the server's local timezone, matching the collector.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbmod
from app.config import load_config


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.toml")
    args = p.parse_args()

    cfg = load_config(args.config)
    conn = dbmod.connect(cfg.db.path)
    dbmod.init_schema(conn)
    rows = dbmod.backfill_daily(conn)
    print(f"peer_daily rebuilt from peer_samples: {rows} day-rows written")


if __name__ == "__main__":
    main()
