"""Download the real datasets listed in datasets.json (all of them, or the ids you pass).

Usage:
    python scripts/download_datasets.py              # everything
    python scripts/download_datasets.py chinook sakila
    python scripts/download_datasets.py --list
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.sources import DatasetCatalog  # noqa: E402


def main(argv: list[str]) -> int:
    catalog = DatasetCatalog()
    datasets = catalog.all()
    if "--list" in argv:
        for d in datasets:
            print(f"{d.id:<14} {'ready' if d.is_ready() else '—':<6} {d.name}")
        return 0
    wanted = [a for a in argv if not a.startswith("-")] or [d.id for d in datasets]
    failed = 0
    for ds_id in wanted:
        ds = catalog.get(ds_id)
        if ds is None:
            print(f"✗ unknown dataset: {ds_id}")
            failed += 1
            continue
        try:
            print(f"… {ds.name}", end="", flush=True)
            catalog.ensure(ds_id, progress=lambda p: print(f"\r… {ds.name} {p:.0%}", end="", flush=True))
            print(f"\r✓ {ds.name} → {ds.db_path().relative_to(Path.cwd()) if ds.db_path().is_relative_to(Path.cwd()) else ds.db_path()}")
        except Exception as e:
            print(f"\r✗ {ds.name}: {e}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
