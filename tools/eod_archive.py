# -*- coding: utf-8 -*-
"""
پایان‌روز:
  ۱) ۲۰ اختیار پرمعاملهٔ آن روز → options_research_eod
  ۲) سری شاخص‌ها از قبل در index_series_archive است (دست نزن)
  ۳) پاک‌سازی options_ticks همان روز (جز آنچه آرشیو شد)
  ۴) اختیاری: خالی کردن details_json / raw_json اسنپ‌شات‌های همان روز برای کم‌حجم شدن
     (index_values و breadth و archive می‌مانند)

Usage:
  python tools/eod_archive.py --db data/market.sqlite --day 2026-09-27
  python tools/eod_archive.py --db data/market.sqlite --day 2026-09-27 --apply
  python tools/eod_archive.py --db data/market.sqlite --day 2026-09-27 --apply --strip-raw
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path
import datetime as dt
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
TOP_N = 20


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/market.sqlite")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD؛ پیش‌فرض دیروز تهران")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--strip-raw", action="store_true",
                    help="raw_json و details_json همان روز را خالی کن (شاخص‌ها می‌مانند)")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--backup-dir", default="data/backups")
    args = ap.parse_args()

    day = args.day
    if not day:
        day = (dt.datetime.now(TEHRAN).date() - dt.timedelta(days=1)).isoformat()

    db = Path(args.db)
    if not db.is_file():
        print(f"DB not found: {db}")
        return 1

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row

    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "options_ticks" not in tables:
        print("options_ticks missing — app.py جدید را یک‌بار با collector اجرا کن تا schema ساخته شود.")
        con.close()
        return 1

    rows = con.execute(
        """
        SELECT t.*
        FROM options_ticks t
        INNER JOIN (
            SELECT option_id, MAX(fetched_at) AS mf
            FROM options_ticks
            WHERE trading_date = ?
            GROUP BY option_id
        ) last ON last.option_id = t.option_id AND last.mf = t.fetched_at
        WHERE t.trading_date = ?
        ORDER BY COALESCE(t.trade_value, 0) DESC
        LIMIT ?
        """,
        (day, day, args.top),
    ).fetchall()

    n_ticks = con.execute(
        "SELECT COUNT(*) FROM options_ticks WHERE trading_date = ?", (day,)
    ).fetchone()[0]
    n_idx = con.execute(
        "SELECT COUNT(*) FROM index_series_archive WHERE trading_date = ?"
        if "index_series_archive" in tables
        else "SELECT 0",
        ((day,) if "index_series_archive" in tables else ()),
    ).fetchone()[0]

    print(f"day={day} options_ticks={n_ticks} index_series_archive={n_idx} top_selected={len(rows)}")
    for i, r in enumerate(rows[:5], 1):
        print(f"  #{i} {r['underlying']} {r['option_symbol']} val={r['trade_value']} ann={r['static_yield_annual_pct']}")

    if not args.apply:
        print("DRY-RUN. --apply برای نوشتن research و پاک‌سازی ticks.")
        con.close()
        return 0

    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(TEHRAN).strftime("%Y%m%d_%H%M%S")
    bp = backup_dir / f"market_before_eod_{day}_{stamp}.sqlite"
    shutil.copy2(db, bp)
    print(f"backup: {bp}")

    con.execute("DELETE FROM options_research_eod WHERE trading_date = ?", (day,))
    for rank, r in enumerate(rows, 1):
        con.execute(
            """
            INSERT INTO options_research_eod (
                trading_date, rank, option_id, option_symbol, option_name,
                underlying, strike, expiry, dte_days,
                stock_buy, option_sell, itm_pct,
                static_yield_pct, static_yield_annual_pct, max_yield_annual_pct,
                trade_value, on_buy_queue, sample_fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                day, rank, r["option_id"], r["option_symbol"], r["option_name"],
                r["underlying"], r["strike"], r["expiry"], r["dte_days"],
                r["stock_buy"], r["option_sell"], r["itm_pct"],
                r["static_yield_pct"], r["static_yield_annual_pct"],
                r["max_yield_annual_pct"], r["trade_value"], r["on_buy_queue"],
                r["fetched_at"],
            ),
        )

    con.execute("DELETE FROM options_ticks WHERE trading_date = ?", (day,))
    print(f"options_research_eod: {len(rows)} rows; options_ticks for {day} cleared.")

    if args.strip_raw:
        con.execute(
            """
            UPDATE snapshots
            SET raw_json = NULL, details_json = NULL, exclusions_json = NULL
            WHERE trading_date = ?
            """,
            (day,),
        )
        print("stripped raw/details/exclusions for that day (index_values kept).")

    con.commit()
    con.execute("VACUUM")
    con.close()
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
