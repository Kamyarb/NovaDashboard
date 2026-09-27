# -*- coding: utf-8 -*-
"""
پاک‌سازی اسنپ‌شات‌های خارج از بازهٔ [08:45, 12:30) به وقت تهران.

پیش‌فرض: dry-run (حذف نمی‌کند).
همیشه قبل از حذف واقعی backup می‌گیرد.

Usage:
  python tools/cleanup_offhours.py --db data/market.sqlite
  python tools/cleanup_offhours.py --db data/market.sqlite --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
START = dt.time(8, 45)
END = dt.time(12, 30)  # exclusive


def _parse_fetched(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(TEHRAN)
    except (TypeError, ValueError):
        return None


def in_record_window(fetched_at: str) -> bool:
    t = _parse_fetched(fetched_at)
    if t is None:
        return False
    tm = t.time()
    return START <= tm < END


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/market.sqlite")
    ap.add_argument("--apply", action="store_true", help="واقعاً حذف کن (پیش‌فرض dry-run)")
    ap.add_argument("--backup-dir", default="data/backups")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"DB not found: {db.resolve()}")
        return 1

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, fetched_at, trading_date, bucket_key FROM snapshots ORDER BY id"
    ).fetchall()

    keep, drop = [], []
    for r in rows:
        (keep if in_record_window(r["fetched_at"]) else drop).append(dict(r))

    print(f"total={len(rows)} keep={len(keep)} drop={len(drop)} window=[08:45,12:30) Asia/Tehran")
    if drop:
        print("sample drop ids:", [d["id"] for d in drop[:15]], ("..." if len(drop) > 15 else ""))
        by_day = {}
        for d in drop:
            by_day[d["trading_date"]] = by_day.get(d["trading_date"], 0) + 1
        print("drop by trading_date:", dict(sorted(by_day.items())))

    if not args.apply:
        print("DRY-RUN only. Re-run with --apply to delete after backup.")
        con.close()
        return 0

    if not drop:
        print("Nothing to delete.")
        con.close()
        return 0

    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(TEHRAN).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"market_before_offhours_cleanup_{stamp}.sqlite"
    shutil.copy2(db, backup_path)
    print(f"backup: {backup_path.resolve()}")

    ids = [d["id"] for d in drop]
    for table in ("breadth_values", "index_values"):
        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            continue
        con.executemany(
            f"DELETE FROM {table} WHERE snapshot_id = ?",
            [(i,) for i in ids],
        )
    con.executemany("DELETE FROM snapshots WHERE id = ?", [(i,) for i in ids])
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"deleted {len(ids)} snapshots (+ child rows). VACUUM done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
