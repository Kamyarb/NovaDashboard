from __future__ import annotations

import base64
import datetime as dt
import html
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo


_COLLECTOR_MODE = "--collect" in sys.argv

import numpy as np
import pandas as pd
import requests
from dataclasses import dataclass
from scipy.optimize import brentq
from scipy.stats import norm
import hashlib

if _COLLECTOR_MODE:
    class _StMock:
        """جایگزین سبک streamlit برای اجرای CLI بدون ScriptRunContext."""
        def __init__(self):
            self.session_state = {}

        def cache_data(self, *args, **kwargs):
            def decorator(fn):
                return fn
            if args and callable(args[0]) and not kwargs:
                return args[0]
            return decorator

        def cache_resource(self, *args, **kwargs):
            return self.cache_data(*args, **kwargs)

        def set_page_config(self, *args, **kwargs):
            return None

        def markdown(self, *args, **kwargs):
            return None

        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                return None
            return _noop

    st = _StMock()
    # plotly فقط برای UI لازم است؛ در collector لود نکن
    px = None
    go = None
    make_subplots = None
else:
    import plotly.express as px
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import streamlit as st

# --- اثرانگشت توسعه‌دهنده (تغییر این بلوک امضا را می‌شکند) ---
_NOVA_DEV = {
    "name_fa": "کامیار بقا",
    "name_en": "Kamyar Bagha",
    "tagline": "پلتفرم آزاد رصد بازار — بدون همکاری با هرمس",
    "linkedin": "https://www.linkedin.com/in/kamyarbagha/",
}
_NOVA_SIG = hashlib.sha256(
    (
        _NOVA_DEV["name_fa"]
        + "|"
        + _NOVA_DEV["name_en"]
        + "|"
        + _NOVA_DEV["tagline"]
        + "|"
        + _NOVA_DEV["linkedin"]
        + "|nova-free-2026"
    ).encode("utf-8")
).hexdigest()
_NOVA_SIG_EXPECT = (
    "631c8b4f583b3d42dd045adcb5abb7bde56a32b8484d7dd80d65506bd3c5492d"
)





# ============================================================
# STORAGE (embedded SQLite for pressure / market index)
# ============================================================

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path as _Path

def _resolve_market_db() -> str:
    """
    هم‌راستا با data_collector / storage:
    MARKET_INDEX_DB یا NOVA_MARKET_DB یا ./data/market.sqlite
    """
    import os
    env = (
        os.getenv("MARKET_INDEX_DB")
        or os.getenv("NOVA_MARKET_DB")
        or os.getenv("MARKET_DB")
        or ""
    ).strip()
    here = _Path(__file__).resolve().parent
    cwd = _Path.cwd()
    candidates = []
    if env:
        candidates.append(_Path(env).expanduser())
    candidates.extend([
        cwd / "data" / "market.sqlite",
        here / "data" / "market.sqlite",
        cwd / "market.sqlite",
        here / "market.sqlite",
    ])
    for p in candidates:
        try:
            if p.is_file():
                return str(p.resolve())
        except OSError:
            continue
    return str((cwd / "data" / "market.sqlite").resolve())


DB_PATH = _resolve_market_db()


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def decode(text):
    return json.loads(text) if text else None


@contextmanager
def connection():
    global DB_PATH
    DB_PATH = _resolve_market_db()
    _Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with connection() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_key TEXT UNIQUE,
                fetched_at TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                source_url TEXT,
                source_hash TEXT,
                formula_version TEXT,
                config_json TEXT,
                raw_json TEXT,
                details_json TEXT,
                exclusions_json TEXT
            );
            CREATE TABLE IF NOT EXISTS index_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                scope TEXT NOT NULL,
                points REAL,
                return_pct REAL,
                last_return_pct REAL,
                book_gap_pp REAL,
                pressure_valid REAL,
                book_coverage_pct REAL,
                eligible_count INTEGER,
                valid_book_count INTEGER,
                summary_json TEXT,
                FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_day
                ON snapshots(trading_date, fetched_at);
            CREATE INDEX IF NOT EXISTS idx_index_values_snap
                ON index_values(snapshot_id, scope);
            """
        )


def available_dates():
    init_db()
    with connection() as con:
        rows = con.execute(
            """
            SELECT DISTINCT trading_date
            FROM snapshots
            ORDER BY trading_date DESC
            """
        ).fetchall()
    return [r["trading_date"] for r in rows]


def read_history(day, scope):
    init_db()
    with connection() as con:
        rows = con.execute(
            """
            SELECT
                s.id AS snapshot_id,
                s.fetched_at,
                s.trading_date,
                s.formula_version,
                s.config_json,
                s.source_hash,
                v.points,
                v.return_pct,
                v.last_return_pct,
                v.book_gap_pp,
                v.pressure_valid,
                v.book_coverage_pct,
                v.eligible_count,
                v.valid_book_count,
                v.summary_json
            FROM snapshots s
            JOIN index_values v ON v.snapshot_id = s.id
            WHERE s.trading_date = ?
              AND v.scope = ?
            ORDER BY s.fetched_at ASC, s.id ASC
            """,
            (day, scope),
        ).fetchall()
    return [dict(r) for r in rows]


def read_snapshot(snapshot_id: int):
    init_db()
    with connection() as con:
        row = con.execute(
            "SELECT * FROM snapshots WHERE id = ?",
            (int(snapshot_id),),
        ).fetchone()
        if row is None:
            return None
        values = con.execute(
            "SELECT * FROM index_values WHERE snapshot_id = ?",
            (int(snapshot_id),),
        ).fetchall()

    summaries = {}
    for v in values:
        item = decode(v["summary_json"]) or {}
        item.update({
            "scope": v["scope"],
            "points": v["points"],
            "return_pct": v["return_pct"],
            "last_return_pct": v["last_return_pct"],
            "book_gap_pp": v["book_gap_pp"],
            "pressure_valid": v["pressure_valid"],
            "book_coverage_pct": v["book_coverage_pct"],
            "eligible_count": v["eligible_count"],
            "valid_book_count": v["valid_book_count"],
        })
        if "excluded_count" not in item:
            item["excluded_count"] = 0
        summaries[v["scope"]] = item

    return {
        "id": row["id"],
        "fetched_at": row["fetched_at"],
        "trading_date": row["trading_date"],
        "formula_version": row["formula_version"],
        "config": decode(row["config_json"]) or {},
        "details": decode(row["details_json"]) or [],
        "exclusions": decode(row["exclusions_json"]) or [],
        "summaries": summaries,
        "source_hash": row["source_hash"],
        "source_url": row["source_url"],
    }



def collector_market_to_app_frames(raw: dict, fetched_at: str):
    """تبدیل raw_json کلکتور به DataFrameهای سازگار با داشبورد + غنی‌سازی دفتر سفارش."""
    market_rows = raw.get("market") or []
    book_rows = raw.get("book") or []
    kind_map = {
        "stock": "سهام",
        "fund": "صندوق",
        "call": "اختیار خرید",
        "put": "اختیار فروش",
        "option_call": "اختیار خرید",
        "option_put": "اختیار فروش",
        "سهام": "سهام",
        "صندوق": "صندوق",
        "اختیار خرید": "اختیار خرید",
        "اختیار فروش": "اختیار فروش",
    }
    m = pd.DataFrame(market_rows)
    if m.empty:
        m = pd.DataFrame(columns=["id", "symbol", "name", "kind"])
    else:
        if "kind" in m.columns:
            m["kind"] = m["kind"].map(lambda x: kind_map.get(str(x), str(x)))
        # fallback از asset_code / section_code
        code_col = "asset_code" if "asset_code" in m.columns else (
            "section_code" if "section_code" in m.columns else None
        )
        if code_col is not None:
            ac = m[code_col].astype(str)
            m.loc[ac.isin({"300", "303", "309"}), "kind"] = "سهام"
            m.loc[ac.isin({"305", "380"}), "kind"] = m.loc[
                ac.isin({"305", "380"}), "kind"
            ].where(
                m.loc[ac.isin({"305", "380"}), "kind"].isin(
                    ["سهام", "صندوق", "اختیار خرید", "اختیار فروش"]
                ),
                "صندوق",
            )
            m.loc[ac.isin({"311", "320"}), "kind"] = "اختیار خرید"
            m.loc[ac.isin({"312", "321"}), "kind"] = "اختیار فروش"
        for col in (
            "last_trade", "close_price", "yesterday_price", "volume", "value",
            "number_trades", "number_shares", "first_price", "low_price", "high_price",
        ):
            if col in m.columns:
                m[col] = pd.to_numeric(m[col], errors="coerce")
        if "return_pct" not in m.columns and "close_price" in m.columns and "yesterday_price" in m.columns:
            yp = m["yesterday_price"].where(m["yesterday_price"] > 0)
            m["return_pct"] = (m["close_price"] / yp - 1.0) * 100.0
        if "id" in m.columns:
            m["id"] = m["id"].astype(str)

    b = pd.DataFrame(book_rows)
    if not b.empty and "id" in b.columns:
        b["id"] = b["id"].astype(str)
        for col in ("bid_price", "ask_price", "bid_vol", "ask_vol", "level"):
            if col in b.columns:
                b[col] = pd.to_numeric(b[col], errors="coerce")
        # همان منطق parse_market_response برای best_bid/ask/pressure/spread
        for side in ["bid", "ask"]:
            price = b[f"{side}_price"] if f"{side}_price" in b.columns else pd.Series(np.nan, index=b.index)
            volume = b[f"{side}_vol"] if f"{side}_vol" in b.columns else pd.Series(np.nan, index=b.index)
            known = (
                price.notna()
                & volume.notna()
                & price.ge(0)
                & volume.ge(0)
                & ~(volume.gt(0) & price.le(0))
            )
            active = known & price.gt(0) & volume.gt(0)
            b[f"{side}_known"] = known
            b[f"{side}_value"] = (price * volume).where(known)
            b[f"{side}_valid_price"] = price.where(active)
        groups = b.groupby("id")
        aggregate = groups.agg(
            best_bid=("bid_valid_price", "max"),
            best_ask=("ask_valid_price", "min"),
            bid_complete=("bid_known", "all"),
            ask_complete=("ask_known", "all"),
            visible_levels=("level", "count"),
        )
        for side in ["bid", "ask"]:
            aggregate[f"{side}_value"] = (
                groups[f"{side}_value"].sum(min_count=1).where(aggregate[f"{side}_complete"])
            )
        aggregate["book_complete"] = aggregate["bid_complete"] & aggregate["ask_complete"]
        # drop prior book cols then join
        drop_cols = [
            c for c in (
                "best_bid", "best_ask", "bid_value", "ask_value",
                "visible_levels", "book_complete", "crossed_book",
                "pressure", "spread_pct",
            ) if c in m.columns
        ]
        if drop_cols:
            m = m.drop(columns=drop_cols)
        m = m.join(aggregate, on="id")
    else:
        for col in (
            "best_bid", "best_ask", "bid_value", "ask_value", "visible_levels",
        ):
            if col not in m.columns:
                m[col] = np.nan
        m["book_complete"] = False

    m["book_complete"] = m["book_complete"].eq(True) if "book_complete" in m.columns else False
    m["crossed_book"] = (
        m["best_bid"].notna()
        & m["best_ask"].notna()
        & m["best_bid"].gt(m["best_ask"])
    )
    depth = m.get("bid_value", pd.Series(np.nan, index=m.index)).fillna(0) + m.get(
        "ask_value", pd.Series(np.nan, index=m.index)
    ).fillna(0)
    # careful with fillna for depth of valid only
    bid_v = m["bid_value"] if "bid_value" in m.columns else pd.Series(np.nan, index=m.index)
    ask_v = m["ask_value"] if "ask_value" in m.columns else pd.Series(np.nan, index=m.index)
    depth = bid_v + ask_v
    valid_pressure = m["book_complete"].eq(True) & depth.gt(0) & ~m["crossed_book"]
    m["pressure"] = (100 * (bid_v - ask_v) / depth).where(valid_pressure)
    midpoint = (m["best_bid"] + m["best_ask"]) / 2
    valid_spread = m["best_bid"].gt(0) & m["best_ask"].ge(m["best_bid"])
    m["spread_pct"] = (100 * (m["best_ask"] - m["best_bid"]) / midpoint).where(valid_spread)

    received = fetched_at
    try:
        ts = pd.Timestamp(fetched_at)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        received = ts.tz_convert("Asia/Tehran").isoformat()
    except Exception:
        received = fetched_at

    return {
        "market": m.reset_index(drop=True),
        "book": b.reset_index(drop=True) if not b.empty else b,
        "received_at": received,
        "source": "collector_sqlite",
        "diagnostics": {
            "market_rows": len(m),
            "book_rows": len(b) if not b.empty else 0,
            "from_collector": True,
        },
    }



def load_collector_snapshot(max_age_minutes: float = 10.0):
    """
    آخرین اسنپ‌شات collect_pressure از market.sqlite.
    برمی‌گرداند (snapshot_dict|None, age_minutes|None, path).
    """
    path = _resolve_market_db()
    if not _Path(path).is_file():
        return None, None, path, None
    try:
        with connection() as con:
            row = con.execute(
                """
                SELECT id, fetched_at, trading_date, raw_json, formula_version
                FROM snapshots
                ORDER BY fetched_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None, None, path, None
        raw = decode(row["raw_json"])
        if not isinstance(raw, dict):
            return None, None, path, None
        # پشتیبانی از فشرده‌سازی احتمالی
        if raw.get("_gzip_b64"):
            import gzip, base64
            raw = json.loads(gzip.decompress(base64.b64decode(raw["_gzip_b64"])))
        snap = collector_market_to_app_frames(raw, row["fetched_at"])
        snap["collector_id"] = row["id"]
        snap["formula_version"] = row["formula_version"]
        snap["trading_date"] = row["trading_date"]
        ts = pd.Timestamp(row["fetched_at"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        age_min = (pd.Timestamp.now(tz="UTC") - ts.tz_convert("UTC")).total_seconds() / 60.0
        return snap, age_min, path, row["trading_date"]
    except Exception:
        return None, None, path, None


def get_market_snapshot(prefer_collector_minutes: float = 20.0):
    """
    منطق دریافت:

    • داخل جلسه معاملاتی (شنبه–چهارشنبه ۹:۰۰–۱۲:۳۰، غیرتعطیل):
        — اگر sqlite تازه‌تر از prefer_collector_minutes باشد → همان
        — وگرنه → دریافت زنده TSETMC

    • خارج از جلسه:
        — اگر دیتای sqlite از «امروز» باشد → همان را بخوان، درخواست نزن
        — اگر دیتا مال امروز نباشد (یا DB خالی) → فقط یک‌بار بگیر و در DB بنویس
        — اگر همان یک‌بار هم شکست خورد → آخرین DB (اگر هست)
    """
    coll, age, path, trading_date = load_collector_snapshot(prefer_collector_minutes)
    today = now_tehran().date().isoformat()
    in_session = False
    try:
        in_session = market_session_open(
            now_tehran(),
            start=dt.time(9, 0),
            end=dt.time(12, 30),
            weekdays_only=True,
        )
    except Exception:
        # اگر تابع هنوز تعریف نشده / خطا — محافظه‌کارانه خارج جلسه فرض کن
        in_session = False

    def _tag_db(snap, age_m):
        snap = dict(snap) if snap else snap
        snap["stale"] = False
        snap["age_minutes"] = age_m
        snap["db_path"] = path
        snap["source"] = snap.get("source") or "collector_sqlite"
        return snap

    def _one_shot_fetch_and_store():
        """یک دریافت کامل + ذخیره در sqlite (فرمت collector)."""
        snapshot = fetch_snapshot()
        config = IndexConfig()
        result = build_indices(snapshot["market"], snapshot["book"], config)
        if not result.get("details"):
            raise ValueError("هیچ نماد واجد شرایط برای ذخیره نیست")
        import hashlib
        import json as _json
        sig = hashlib.sha256(
            _json.dumps(
                {"v": result["formula_version"], "c": result["config"]},
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]
        now_utc = dt.datetime.now(dt.timezone.utc)
        bucket = int(now_utc.timestamp() // 60)
        key = f"offhours:60:{bucket}:{sig}"
        fetched_at = now_utc.isoformat(timespec="seconds")
        day = now_tehran().date().isoformat()
        try:
            collector_save_snapshot(snapshot, result, fetched_at, day, key)
        except Exception:
            pass  # حتی اگر ذخیره نشد، داده را برگردان
        snap = collector_market_to_app_frames(
            {"market": snapshot["market"], "book": snapshot["book"]},
            fetched_at,
        )
        snap["source"] = "live_offhours_once"
        snap["stale"] = False
        snap["age_minutes"] = 0.0
        snap["db_path"] = path
        snap["trading_date"] = day
        return snap

    # ----- خارج از جلسه -----
    if not in_session:
        is_today = (trading_date == today) if trading_date else False
        if coll is not None and is_today:
            return _tag_db(coll, age)
        # دیتا مال امروز نیست یا DB خالی → یک‌بار تلاش
        try:
            return _one_shot_fetch_and_store()
        except Exception as exc:
            if coll is not None:
                out = _tag_db(coll, age)
                out["stale"] = True
                out["offhours_fetch_error"] = str(exc)
                return out
            raise RuntimeError(
                f"خارج از ساعت بازار و دیتابیس خالی/نامعتبر است: {exc}"
            ) from exc

    # ----- داخل جلسه -----
    if coll is not None and age is not None and age <= prefer_collector_minutes:
        return _tag_db(coll, age)

    # زنده
    try:
        live = fetch_market()
    except Exception as exc:
        if coll is not None:
            out = _tag_db(coll, age)
            out["stale"] = True
            out["live_fetch_error"] = str(exc)
            return out
        raise

    if isinstance(live, dict):
        live["source"] = "live_tsetmc"
        live["stale"] = False
        live["age_minutes"] = 0.0
        live["db_path"] = path
        live["collector_age_minutes"] = age
        return live
    return live


# ============================================================
# PRESSURE ENGINE (embedded)
# ============================================================

VERSION = "pressure-fixed-day-v2.0"

CONFIG = {
    "base_points": 100.0,
    "cap_weight": 0.60,
    "max_levels": 5,
    "level_decay": 2.0,
    "max_spread_pct": 3.0,

    # حداکثر اثر فشار روی قیمت مدل: نیم درصد
    "pressure_alpha": 0.005,

    # فقط در نبود تاریخچه عمق:
    # مقیاس اولیه برابر ۰٫۰۱ درصد اندازه بازار نماد
    # این عدد کالیبره نشده و آزمایشی است.
    "fallback_depth_cap_fraction": 0.0001,

    "exclude_numeric_suffix": True,
}

SCOPES = ("combined", "stock", "fund")


def positive(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def nonnegative(value):
    try:
        return math.isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError):
        return False


def eligible(row):
    if row.get("kind") not in {"stock", "fund"}:
        return False

    if CONFIG["exclude_numeric_suffix"]:
        if re.search(r"\d$", str(row.get("symbol", "")).strip()):
            return False

    return all(
        positive(row.get(key))
        for key in ("last_trade", "yesterday_price", "number_shares")
    )


def init_pressure_db():
    init_db()

    with connection() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pressure_daily_states (
                trading_date TEXT PRIMARY KEY,
                state_json TEXT NOT NULL
            )
        """)


def load_state(day):
    with connection() as con:
        row = con.execute(
            """
            SELECT state_json
            FROM pressure_daily_states
            WHERE trading_date = ?
            """,
            (day,),
        ).fetchone()

    return json.loads(row["state_json"]) if row else None


def historical_inputs(day):
    """
    آخرین روز ثبت‌شده قبل از امروز.

    این روز الزاماً جلسه معاملاتی قبلی نیست:
    اگر برنامه چند روز خاموش بوده باشد، داده قدیمی‌تر است.
    """
    with connection() as con:
        previous = con.execute(
            """
            SELECT MAX(trading_date) AS day
            FROM snapshots
            WHERE trading_date < ?
            """,
            (day,),
        ).fetchone()["day"]

        if previous is None:
            return None, {}, {}

        latest = con.execute(
            """
            SELECT raw_json
            FROM snapshots
            WHERE trading_date = ?
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """,
            (previous,),
        ).fetchone()

        history = con.execute(
            """
            SELECT details_json
            FROM snapshots
            WHERE trading_date = ?
            ORDER BY fetched_at DESC
            LIMIT 600
            """,
            (previous,),
        ).fetchall()

    raw = json.loads(latest["raw_json"])

    previous_values = {}

    for row in raw.get("market", []):
        value = row.get("value")
        if nonnegative(value):
            previous_values[str(row["id"])] = float(value)

    depths = defaultdict(list)

    for snapshot in history:
        for row in json.loads(snapshot["details_json"]):
            if not row.get("book_valid"):
                continue

            bid = row.get("bid_depth", 0)
            ask = row.get("ask_depth", 0)

            if nonnegative(bid) and nonnegative(ask):
                total = float(bid) + float(ask)
                if positive(total):
                    depths[str(row["id"])].append(total)

    scales = {
        instrument_id: statistics.median(values)
        for instrument_id, values in depths.items()
        if values
    }

    return previous, previous_values, scales


def valid_order_price(price, row):
    if not positive(price):
        return False

    low = row.get("min_allowed_price")
    high = row.get("max_allowed_price")

    if positive(low) and positive(high) and low > high:
        return False

    if positive(low) and price < low:
        return False

    if positive(high) and price > high:
        return False

    return True


def estimate(row, orders, scale):
    """
    دفتر دوطرفه: قیمت پایه = midpoint
    صف خرید تأییدشده در سقف: قیمت پایه = سقف
    صف فروش تأییدشده در کف: قیمت پایه = کف

    فشار فقط یک بار و جداگانه روی قیمت پایه اعمال می‌شود.
    """
    bids = []
    asks = []
    bid_depth = 0.0
    ask_depth = 0.0

    for order in orders:
        level = order.get("level")

        if not positive(level):
            continue

        level = float(level)

        if not level.is_integer() or not 1 <= level <= CONFIG["max_levels"]:
            continue

        discount = level ** CONFIG["level_decay"]

        bid = order.get("bid_price")
        ask = order.get("ask_price")
        bv = order.get("bid_vol")
        av = order.get("ask_vol")

        if positive(bv) and valid_order_price(bid, row):
            bids.append(float(bid))
            bid_depth += float(bid) * float(bv) / discount

        if positive(av) and valid_order_price(ask, row):
            asks.append(float(ask))
            ask_depth += float(ask) * float(av) / discount

    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None

    anchor = float(row["last_trade"])
    method = "last_no_book"
    book_valid = False
    spread = None

    if bids and asks:
        if best_bid > best_ask:
            method = "last_crossed"
        else:
            midpoint = (best_bid + best_ask) / 2
            spread = (best_ask - best_bid) / midpoint * 100

            if spread <= CONFIG["max_spread_pct"]:
                anchor = midpoint
                method = "two_sided"
                book_valid = True
            else:
                method = "last_wide_spread"

    elif bids:
        high = row.get("max_allowed_price")

        # تلورانس عددی؛ نه یک فاصله درصدی دلخواه از سقف
        if positive(high) and math.isclose(
            best_bid, float(high), rel_tol=1e-9, abs_tol=0.01
        ):
            anchor = float(high)
            method = "buy_queue"
            book_valid = True
        else:
            method = "last_one_sided"

    elif asks:
        low = row.get("min_allowed_price")

        if positive(low) and math.isclose(
            best_ask, float(low), rel_tol=1e-9, abs_tol=0.01
        ):
            anchor = float(low)
            method = "sell_queue"
            book_valid = True
        else:
            method = "last_one_sided"

    z = None

    if book_valid:
        z = (
            (bid_depth - ask_depth)
            / (bid_depth + ask_depth + scale)
        )

    model = anchor * (
        1 + CONFIG["pressure_alpha"] * (z if z is not None else 0)
    )

    return {
        "anchor_price": anchor,
        "model_price": model,
        "last_trade": float(row["last_trade"]),
        "book_valid": book_valid,
        "carried": False,
        "price_method": method,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": spread,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "pressure_z": z,
        "depth_scale": scale,
    }


def create_state(snapshot, day, timestamp):
    previous_day, previous_values, scales = historical_inputs(day)

    selected = [
        row for row in snapshot["market"]
        if eligible(row)
    ]

    if not selected:
        raise ValueError("نماد واجد شرایط برای ساخت مبنای روز موجود نیست.")

    instruments = {}

    for row in selected:
        instrument_id = str(row["id"])
        cap = float(row["number_shares"]) * float(row["last_trade"])

        scale = scales.get(instrument_id)
        scale_source = "historical"

        if not positive(scale):
            scale = (
                float(row["number_shares"])
                * float(row["yesterday_price"])
                * CONFIG["fallback_depth_cap_fraction"]
            )
            scale_source = "fallback"

        instruments[instrument_id] = {
            "id": instrument_id,
            "symbol": row["symbol"],
            "name": row["name"],
            "kind": row["kind"],
            "asset_code": row["asset_code"],
            "cap_at_base": cap,
            "previous_trade_value": previous_values.get(instrument_id, 0.0),
            "depth_scale": scale,
            "scale_source": scale_source,
            "weights": {},
        }

    weight_modes = {}

    for scope in SCOPES:
        members = [
            item for item in instruments.values()
            if scope == "combined" or item["kind"] == scope
        ]

        cap_total = sum(item["cap_at_base"] for item in members)
        value_total = sum(item["previous_trade_value"] for item in members)

        weight_modes[scope] = (
            "unavailable" if not members
            else "cap_value" if value_total > 0
            else "cap_only"
        )

        for item in members:
            cap_share = item["cap_at_base"] / cap_total

            if value_total > 0:
                weight = (
                    CONFIG["cap_weight"] * cap_share
                    + (1 - CONFIG["cap_weight"])
                    * item["previous_trade_value"] / value_total
                )
            else:
                weight = cap_share

            item["weights"][scope] = weight

    return {
        "version": VERSION,
        "config": dict(CONFIG),
        "day": day,
        "base_at": timestamp,
        "previous_data_day": previous_day,
        "weight_modes": weight_modes,
        "instruments": instruments,
    }


def calculate(snapshot, state):
    current = {
        str(row["id"]): row
        for row in snapshot["market"]
    }

    books = defaultdict(list)

    for order in snapshot["book"]:
        books[str(order["id"])].append(order)

    details = []

    for instrument_id, item in state["instruments"].items():
        row = current.get(instrument_id)

        if row is not None and eligible(row):
            quote = estimate(
                row,
                books.get(instrument_id, []),
                item["depth_scale"],
            )

        elif "last_quote" in item:
            # سبد و وزن ثابت می‌مانند؛ قیمت آخر حفظ می‌شود.
            quote = dict(item["last_quote"])
            quote.update({
                "book_valid": False,
                "carried": True,
                "price_method": "carried",
                "pressure_z": None,
                "bid_depth": 0.0,
                "ask_depth": 0.0,
            })

        else:
            raise ValueError(
                f"ورودی اولیه نماد {item['symbol']} معتبر نیست."
            )

        if "anchor_base" not in item:
            item["anchor_base"] = quote["anchor_price"]
            item["model_base"] = quote["model_price"]

        item["last_quote"] = quote

        details.append({
            "id": instrument_id,
            "symbol": item["symbol"],
            "name": item["name"],
            "kind": item["kind"],
            "asset_code": item["asset_code"],
            "scale_source": item["scale_source"],
            "anchor_base": item["anchor_base"],
            "model_base": item["model_base"],
            "anchor_return_pct": (
                quote["anchor_price"] / item["anchor_base"] - 1
            ) * 100,
            "model_return_pct": (
                quote["model_price"] / item["model_base"] - 1
            ) * 100,
            **{
                f"weight_{scope}": item["weights"].get(scope, 0.0)
                for scope in SCOPES
            },
            **quote,
        })

    summaries = []

    for scope in SCOPES:
        members = [
            row for row in details
            if scope == "combined" or row["kind"] == scope
        ]

        weight_key = f"weight_{scope}"

        price_return = sum(
            row[weight_key] * row["anchor_return_pct"]
            for row in members
        )

        model_return = sum(
            row[weight_key] * row["model_return_pct"]
            for row in members
        )

        valid_weight = sum(
            row[weight_key]
            for row in members
            if row["book_valid"]
        )

        carried_weight = sum(
            row[weight_key]
            for row in members
            if row["carried"]
        )

        fallback_weight = sum(
            row[weight_key]
            for row in members
            if row["scale_source"] == "fallback"
        )

        pressure = (
            100 * sum(
                row[weight_key] * row["pressure_z"]
                for row in members
                if row["book_valid"]
            ) / valid_weight
            if valid_weight > 0 else None
        )

        available = bool(members)

        summaries.append({
            "scope": scope,
            "points": 100 * (1 + model_return / 100) if available else None,
            "price_points": 100 * (1 + price_return / 100) if available else None,

            # نام ستون‌های ذخیره‌سازی قبلی حفظ شده‌اند.
            # در این نسخه last_return_pct یعنی بازده قیمت پایه مدل،
            # نه بازده Last.
            "return_pct": model_return if available else None,
            "last_return_pct": price_return if available else None,
            "book_gap_pp": model_return - price_return if available else None,

            "pressure_valid": pressure,
            "book_coverage_pct": 100 * valid_weight,
            "carried_weight_pct": 100 * carried_weight,
            "fallback_scale_weight_pct": 100 * fallback_weight,
            "eligible_count": len(members),
            "valid_book_count": sum(row["book_valid"] for row in members),
            "weight_mode": state["weight_modes"][scope],
            "base_at": state["base_at"],
            "previous_data_day": state["previous_data_day"],
        })

    return {
        "formula_version": VERSION,
        "config": dict(CONFIG),
        "summaries": summaries,
        "details": details,
        "exclusions": [],
    }


def save_atomic(snapshot, result, state, timestamp, day, bucket_key):
    """
    وضعیت روز و اسنپ‌شات در یک تراکنش ذخیره می‌شوند.
    """
    with connection() as con:
        cursor = con.execute(
            """
            INSERT OR IGNORE INTO snapshots (
                bucket_key, fetched_at, trading_date,
                source_url, source_hash, formula_version,
                config_json, raw_json, details_json, exclusions_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bucket_key,
                timestamp,
                day,
                snapshot["source_url"],
                snapshot["source_hash"],
                VERSION,
                encode(CONFIG),
                encode({
                    "market": snapshot["market"],
                    "book": snapshot["book"],
                }),
                encode(result["details"]),
                encode(result["exclusions"]),
            ),
        )

        if cursor.rowcount == 0:
            return None

        snapshot_id = cursor.lastrowid

        for item in result["summaries"]:
            con.execute(
                """
                INSERT INTO index_values (
                    snapshot_id, scope, points, return_pct,
                    last_return_pct, book_gap_pp, pressure_valid,
                    book_coverage_pct, eligible_count,
                    valid_book_count, summary_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    item["scope"],
                    item["points"],
                    item["return_pct"],
                    item["last_return_pct"],
                    item["book_gap_pp"],
                    item["pressure_valid"],
                    item["book_coverage_pct"],
                    item["eligible_count"],
                    item["valid_book_count"],
                    encode(item),
                ),
            )

        con.execute(
            """
            INSERT INTO pressure_daily_states (
                trading_date, state_json
            )
            VALUES (?, ?)
            ON CONFLICT(trading_date)
            DO UPDATE SET state_json = excluded.state_json
            """,
            (day, encode(state)),
        )

    return snapshot_id


# ============================================================
# MARKET INDEX ENGINE (embedded)
# ============================================================

FORMULA_VERSION = "live-depth-v1.1-universe-filter"


@dataclass(frozen=True)
class IndexConfig:
    cap_weight: float = 0.60
    max_levels: int = 5
    level_decay: float = 2.0

    # درصد؛ مقدار 3 یعنی سه درصد
    max_spread_pct: float = 3.0

    base_points: float = 100.0  # مبنای درون‌روز؛ نه ۱۰۰۰۰

    def __post_init__(self):
        if not 0 <= self.cap_weight <= 1:
            raise ValueError("cap_weight باید بین صفر و یک باشد.")

        if self.max_levels < 1:
            raise ValueError("max_levels باید مثبت باشد.")

        if self.level_decay < 0:
            raise ValueError("level_decay نباید منفی باشد.")

        if self.max_spread_pct <= 0:
            raise ValueError("max_spread_pct باید مثبت باشد.")

        if self.base_points <= 0:
            raise ValueError("base_points باید مثبت باشد.")


def finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def positive(value):
    return finite(value) and float(value) > 0


def nonnegative(value):
    return finite(value) and float(value) >= 0


def in_allowed_range(price, row):
    if not positive(price):
        return False

    low = row.get("min_allowed_price")
    high = row.get("max_allowed_price")

    if positive(low) and positive(high) and low > high:
        return False

    if positive(low) and price < low:
        return False

    if positive(high) and price > high:
        return False

    return True


def estimate_price(row, levels, config):
    """
    قیمت برآوردی با دفتر سفارش.

    - اگر دو طرف دفتر معتبر باشد: mid ± فشار عمق (داخل اسپرد).
    - صف خرید (Last/Bid نزدیک سقف، Ask ضعیف/غایب): بازده مجازی می‌تواند
      از +۳٪ رسمی فراتر برود — سفارش سنگین خرید هنوز معامله نشده.
    - صف فروش (نزدیک کف، Bid ضعیف): بازده مجازی می‌تواند از −۳٪ پایین‌تر برود.
    """
    last = float(row["last_trade"])
    ref = row.get("yesterday_price")
    max_p = row.get("max_allowed_price")
    min_p = row.get("min_allowed_price")

    result = {
        "estimated_price": last,
        "price_method": "last_no_book",
        "book_valid": False,
        "best_bid": None,
        "best_ask": None,
        "spread_pct": None,
        "bid_depth": 0.0,
        "ask_depth": 0.0,
        "imbalance": None,
    }

    bids = []
    asks = []
    bid_vol_total = 0.0
    ask_vol_total = 0.0

    for item in levels:
        level = item.get("level") or 0
        try:
            level = int(level)
        except (TypeError, ValueError):
            continue
        if not 1 <= level <= config.max_levels:
            continue
        discount = level ** config.level_decay
        bid = item.get("bid_price")
        ask = item.get("ask_price")
        bid_vol = item.get("bid_vol")
        ask_vol = item.get("ask_vol")

        if positive(bid_vol) and positive(bid):
            # حتی اگر خارج از محدوده مجاز باشد برای تشخیص صف نگه می‌داریم
            if in_allowed_range(bid, row) or (
                positive(max_p) and float(bid) >= float(max_p) * 0.998
            ):
                bids.append(float(bid))
                result["bid_depth"] += float(bid) * float(bid_vol) / discount
                bid_vol_total += float(bid_vol)

        if positive(ask_vol) and positive(ask):
            if in_allowed_range(ask, row) or (
                positive(min_p) and float(ask) <= float(min_p) * 1.002
            ):
                asks.append(float(ask))
                result["ask_depth"] += float(ask) * float(ask_vol) / discount
                ask_vol_total += float(ask_vol)

    result["best_bid"] = max(bids) if bids else None
    result["best_ask"] = min(asks) if asks else None

    # ---- صف خرید / فروش: تخمین فراتر از دامنه ±۳٪ رسمی ----
    queue_boost_pct = 0.0  # اضافه/کم به بازده Last نسبت به دیروز
    if positive(ref) and positive(max_p) and last >= float(max_p) * 0.997:
        # نزدیک سقف
        if not asks or (result["best_ask"] is None):
            # صف خرید یک‌طرفه
            depth_ratio = min(3.0, (result["bid_depth"] / max(last * 1e-9, 1.0)) ** 0.5)
            queue_boost_pct = 0.5 + 1.5 * min(1.0, depth_ratio / 50.0)
            result["price_method"] = "buy_queue_virtual"
        elif result["bid_depth"] > 3 * max(result["ask_depth"], 1e-12):
            queue_boost_pct = 0.3 + 1.2 * min(
                1.0,
                (result["bid_depth"] / max(result["ask_depth"], 1)) / 20.0,
            )
            result["price_method"] = "buy_pressure_virtual"

    if positive(ref) and positive(min_p) and last <= float(min_p) * 1.003:
        if not bids or result["best_bid"] is None:
            depth_ratio = min(3.0, (result["ask_depth"] / max(last * 1e-9, 1.0)) ** 0.5)
            queue_boost_pct = -(0.5 + 1.5 * min(1.0, depth_ratio / 50.0))
            result["price_method"] = "sell_queue_virtual"
        elif result["ask_depth"] > 3 * max(result["bid_depth"], 1e-12):
            queue_boost_pct = -(
                0.3
                + 1.2
                * min(1.0, (result["ask_depth"] / max(result["bid_depth"], 1)) / 20.0)
            )
            result["price_method"] = "sell_pressure_virtual"

    if abs(queue_boost_pct) > 1e-9 and positive(ref):
        # بازده مجازی = بازده last + boost صف (می‌تواند از ±۳٪ عبور کند)
        last_ret = (last / float(ref) - 1.0) * 100.0
        virtual_ret = last_ret + queue_boost_pct
        # سقف منطقی تخمین: حدود ±۸٪ از دیروز (فراتر از قفل رسمی)
        virtual_ret = max(-8.0, min(8.0, virtual_ret))
        estimated = float(ref) * (1.0 + virtual_ret / 100.0)
        result["estimated_price"] = estimated
        result["book_valid"] = True
        depth_total = result["bid_depth"] + result["ask_depth"]
        if depth_total > 0:
            result["imbalance"] = (result["bid_depth"] - result["ask_depth"]) / depth_total
        return result

    if not bids or not asks:
        if bids or asks:
            result["price_method"] = "last_one_sided"
        return result

    best_bid = result["best_bid"]
    best_ask = result["best_ask"]

    if best_bid > best_ask:
        result["price_method"] = "last_crossed"
        return result

    midpoint = (best_bid + best_ask) / 2
    spread_pct = (best_ask - best_bid) / midpoint * 100
    result["spread_pct"] = spread_pct

    if spread_pct > config.max_spread_pct:
        result["price_method"] = "last_wide_spread"
        return result

    bid_depth = result["bid_depth"]
    ask_depth = result["ask_depth"]
    depth_total = bid_depth + ask_depth
    if not positive(depth_total):
        return result

    imbalance = (bid_depth - ask_depth) / depth_total
    estimated_price = midpoint + (best_ask - best_bid) / 2 * imbalance
    estimated_price = min(best_ask, max(best_bid, estimated_price))

    result.update({
        "estimated_price": estimated_price,
        "price_method": "depth",
        "book_valid": True,
        "imbalance": imbalance,
    })
    return result


def aggregate(rows, scope, candidate_count, config):
    if not rows:
        return {
            "scope": scope,
            "candidate_count": candidate_count,
            "eligible_count": 0,
            "excluded_count": candidate_count,
            "valid_book_count": 0,
            "weight_mode": "unavailable",
            "return_pct": None,
            "last_return_pct": None,
            "book_gap_pp": None,
            "points": None,
            "pressure_valid": None,
            "book_coverage_pct": 0.0,
            "market_cap_total": 0.0,
            "trade_value_total": 0.0,
            "unknown_value_count": 0,
        }

    cap_total = sum(row["market_cap"] for row in rows)
    value_total = sum(row["trade_value"] for row in rows)

    has_value = value_total > 0
    weight_key = f"weight_{scope}"

    weighted_return = 0.0
    weighted_last_return = 0.0

    book_weight = 0.0
    weighted_pressure = 0.0

    for row in rows:
        cap_share = row["market_cap"] / cap_total

        if has_value:
            value_share = row["trade_value"] / value_total

            weight = (
                config.cap_weight * cap_share
                + (1 - config.cap_weight) * value_share
            )
        else:
            # اول بازار یا زمانی که کل ارزش معاملات صفر است:
            # وزن‌ها فقط بر اساس اندازه نماد محاسبه می‌شوند.
            weight = cap_share

        row[weight_key] = weight

        weighted_return += weight * row["return_pct"]
        weighted_last_return += weight * row["last_return_pct"]

        if row["book_valid"]:
            book_weight += weight
            weighted_pressure += weight * row["imbalance"]

    # فشار فقط روی بخش دارای دفتر معتبر نرمال می‌شود.
    # درصد پوشش همیشه کنار آن گزارش می‌شود.
    pressure_valid = (
        100 * weighted_pressure / book_weight
        if book_weight > 0
        else None
    )

    return {
        "scope": scope,
        "candidate_count": candidate_count,
        "eligible_count": len(rows),
        "excluded_count": candidate_count - len(rows),
        "valid_book_count": sum(
            row["book_valid"] for row in rows
        ),
        "weight_mode": (
            "cap_value" if has_value else "cap_only"
        ),
        "return_pct": weighted_return,
        "last_return_pct": weighted_last_return,
        "book_gap_pp": (
            weighted_return - weighted_last_return
        ),
        "points": (
            config.base_points * (1 + weighted_return / 100)
        ),
        "pressure_valid": pressure_valid,
        "book_coverage_pct": book_weight * 100,
        "market_cap_total": cap_total,
        "trade_value_total": value_total,
        "unknown_value_count": sum(
            row["value_missing"] for row in rows
        ),
    }

def universe_exclusion_reason(row):
    """
    فیلتر موقت و محافظه‌کارانه سبد.

    پسوند عددی به‌تنهایی نوع تابلو را اثبات نمی‌کند؛
    این نمادها تا بررسی مشخصات رسمی قرنطینه می‌شوند.
    """
    symbol = str(row.get("symbol", "")).strip()

    # \d شامل ارقام فارسی و عربی هم می‌شود.
    if re.search(r"\d$", symbol):
        return "numeric_suffix_review"

    return None

def build_indices(market, book, config=None):
    config = config or IndexConfig()

    books = defaultdict(list)

    for item in book:
        books[item["id"]].append(item)

    candidates = [
        row for row in market
        if row.get("kind") in {"stock", "fund"}
    ]

    details = []
    exclusions = []

    for row in candidates:
        reference = row.get("yesterday_price")
        last = row.get("last_trade")
        shares = row.get("number_shares")

        reason = universe_exclusion_reason(row)

        if reason is None:
            if not positive(reference):
                reason = "invalid_reference"
            elif not positive(last):
                reason = "invalid_last"
            elif not positive(shares):
                reason = "invalid_shares"

        if reason:
            exclusions.append({
                "id": row["id"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "reason": reason,
            })
            continue

        market_cap = float(shares) * float(last)

        if not positive(market_cap):
            exclusions.append({
                "id": row["id"],
                "symbol": row["symbol"],
                "kind": row["kind"],
                "reason": "invalid_market_cap",
            })
            continue

        estimate = estimate_price(
            row,
            books.get(row["id"], []),
            config,
        )

        raw_value = row.get("value")
        value_missing = not nonnegative(raw_value)

        trade_value = (
            0.0 if value_missing else float(raw_value)
        )

        estimated_price = estimate["estimated_price"]

        details.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "name": row["name"],
            "kind": row["kind"],
            "asset_code": row["asset_code"],
            "industry_code": row["section_code"],
            "source_time": row["time"],
            "reference_price": float(reference),
            "last_trade": float(last),
            "number_shares": float(shares),
            "market_cap": market_cap,
            "trade_value": trade_value,
            "value_missing": value_missing,
            "last_return_pct": (
                float(last) / float(reference) - 1
            ) * 100,
            "return_pct": (
                estimated_price / float(reference) - 1
            ) * 100,
            **estimate,
        })

    summaries = []

    for scope in ("combined", "stock", "fund"):
        scoped_rows = [
            row for row in details
            if scope == "combined" or row["kind"] == scope
        ]

        candidate_count = sum(
            scope == "combined" or row["kind"] == scope
            for row in candidates
        )

        summaries.append(
            aggregate(
                scoped_rows,
                scope,
                candidate_count,
                config,
            )
        )

    return {
        "formula_version": FORMULA_VERSION,
        "config": asdict(config),
        "summaries": summaries,
        "details": details,
        "exclusions": exclusions,
    }


# ============================================================
# PRESSURE VIEW (embedded)
# ============================================================

PRESSURE_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

PRESSURE_MONTHS = [
    "",
    "فروردین", "اردیبهشت", "خرداد",
    "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر",
    "دی", "بهمن", "اسفند",
]

PRESSURE_SCOPES = {
    "سهام": "stock",
    "صندوق‌ها": "fund",
    "ترکیبی": "combined",
}

PRESSURE_METHODS = {
    "two_sided": "دفتر دوطرفه",
    "buy_queue": "صف خرید در سقف",
    "sell_queue": "صف فروش در کف",
    "last_no_book": "Last؛ بدون دفتر",
    "last_crossed": "Last؛ دفتر متقاطع",
    "last_wide_spread": "Last؛ اسپرد زیاد",
    "last_one_sided": "Last؛ یک‌طرفه، بدون تأیید صف",
    "carried": "حفظ مقدار قبلی؛ ورودی نامعتبر/غایب",
}


def fa(value):
    return str(value).translate(FA)


def pressure_fmt(value, digits=2):
    if value is None or pd.isna(value):
        return "—"
    return fa(f"{value:,.{digits}f}")


def day_label(value):
    date = dt.date.fromisoformat(str(value)[:10])
    jd = jdatetime.date.fromgregorian(date=date)
    return fa(f"{jd.day} {PRESSURE_MONTHS[jd.month]} {jd.year}")


def timestamp_label(value):
    t = pd.Timestamp(value)
    if t.tzinfo is not None:
        t = t.tz_convert("Asia/Tehran")
    return day_label(t.date().isoformat()) + " · " + fa(t.strftime("%H:%M:%S"))


def dates():
    with connection() as con:
        rows = con.execute(
            """
            SELECT DISTINCT trading_date
            FROM snapshots
            WHERE formula_version = ?
            ORDER BY trading_date DESC
            """,
            (VERSION,),
        ).fetchall()
        if not rows:
            # اگر نسخه فرمول فرق داشت، همه تاریخ‌ها را نشان بده
            rows = con.execute(
                """
                SELECT DISTINCT trading_date
                FROM snapshots
                ORDER BY trading_date DESC
                """
            ).fetchall()
    return [row["trading_date"] for row in rows]


def render_pressure_index(key_prefix="pressure"):
    init_pressure_db()

    st.subheader("◈ شاخص قیمت و فشار سفارش")
    st.caption(
        "مبنای روز ۱۰۰ · وزن ثابت روزانه · "
        "اثر فشار سفارش، مدل آزمایشی است نه قیمت اجرای معامله"
    )
    st.caption(f"منبع اسنپ‌شات: `{DB_PATH}`")

    available = dates()

    if not available:
        st.info(
            "هنوز اسنپ‌شاتی در دیتابیس نیست. "
            f"مسیر فعلی: `{DB_PATH}` — "
            "collect_pressure باید به همین فایل بنویسد "
            "(یا NOVA_MARKET_DB را روی مسیر market.sqlite بگذار)."
        )
        return

    a, b, c = st.columns([2, 2, 1])

    day = a.selectbox(
        "روز",
        available,
        format_func=day_label,
        key=f"{key_prefix}_day",
    )

    scope_name = b.selectbox(
        "سبد",
        list(PRESSURE_SCOPES),
        key=f"{key_prefix}_scope",
    )

    c.button("به‌روزرسانی", key=f"{key_prefix}_refresh")

    scope = PRESSURE_SCOPES[scope_name]

    records = list(read_history(day, scope))
    if not records:
        st.info(
            "داده‌ای برای این انتخاب وجود ندارد. "
            f"مسیر دیتابیس: `{DB_PATH}` — "
            "مطمئن شو collect_pressure به همین فایل می‌نویسد."
        )
        return
    # اگر چند نسخه فرمول مخلوط است، آخرین نسخه غالب را نگه دار
    versions = [r.get("formula_version") for r in records if r.get("formula_version")]
    if versions:
        prefer = VERSION if VERSION in versions else versions[-1]
        filtered = [r for r in records if r.get("formula_version") == prefer]
        if filtered:
            records = filtered

    frame = pd.DataFrame(records)

    frame["time"] = pd.to_datetime(
        frame["fetched_at"], utc=True
    ).dt.tz_convert("Asia/Tehran")

    frame["price_points"] = 100 * (
        1 + frame["last_return_pct"] / 100
    )

    packet = read_snapshot(int(frame.iloc[-1]["snapshot_id"]))
    summary = packet["summaries"][scope]

    st.caption(
        "مبنای این روز: "
        + timestamp_label(summary["base_at"])
        + " | آخرین دریافت: "
        + timestamp_label(frame.iloc[-1]["time"])
    )

    previous_day = summary["previous_data_day"]

    st.caption(
        "روز داده مرجع وزن معاملات: "
        + (day_label(previous_day) if previous_day else "بدون سابقه")
    )

    if not summary.get("eligible_count"):
        st.warning("این سبد در مبنای روز عضو واجد شرایط ندارد.")
        return

    if summary.get("weight_mode") == "cap_only":
        st.warning(
            "ارزش معاملات مرجع قابل‌استفاده وجود نداشته؛ "
            "وزن‌های امروز فقط بر اساس اندازه بازار هستند."
        )

    cols = st.columns(4)

    cols[0].metric("شاخص قیمت", fmt(summary["price_points"], 3))
    cols[1].metric("شاخص با اثر فشار", fmt(summary["points"], 3))
    cols[2].metric("فشار بخش معتبر", fmt(summary["pressure_valid"], 1))
    cols[3].metric(
        "پوشش دفتر سفارش",
        fmt(summary["book_coverage_pct"], 1) + "٪",
    )

    st.caption(
        "وزن دارای قیمت حفظ‌شده: "
        + fmt(summary["carried_weight_pct"], 1)
        + "٪ | وزن با مقیاس فشار آزمایشی: "
        + fmt(summary["fallback_scale_weight_pct"], 1)
        + "٪ | اعضای ثابت سبد: "
        + fmt(summary["eligible_count"], 0)
    )

    if (summary.get("book_coverage_pct") or 0) < 70:
        st.warning(
            "پوشش دفتر سفارش پایین است؛ فشار بخش معتبر "
            "را به تمام سبد تعمیم نده."
        )

    if summary["carried_weight_pct"] > 0:
        st.warning(
            "برای بخشی از سبد، به علت ورودی غایب یا نامعتبر، "
            "آخرین قیمت مدل حفظ شده است."
        )

    fig = go.Figure()

    hover_dates = [
        timestamp_label(value)
        for value in frame["time"]
    ]

    for column, title, color in [
        ("price_points", "شاخص قیمت", "#A78BFA"),
        ("points", "شاخص با اثر فشار", "#2DD4BF"),
    ]:
        fig.add_trace(go.Scatter(
            x=frame["time"],
            y=frame[column],
            name=title,
            mode="lines+markers",
            marker=dict(size=3),
            line=dict(width=2.5, color=color),
            text=hover_dates,
            hovertemplate=(
                "%{text}<br>" + title + ": %{y:.3f}<extra></extra>"
            ),
            connectgaps=False,
        ))

    count = min(7, len(frame))
    positions = sorted({
        round(i * (len(frame) - 1) / max(1, count - 1))
        for i in range(count)
    })

    tick_values = [frame["time"].iloc[i] for i in positions]

    fig.add_hline(
        y=100,
        line_dash="dot",
        line_color="#94A3B8",
    )

    fig.update_layout(
        height=420,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Vazirmatn, Tahoma, sans-serif"),
        margin=dict(l=20, r=20, t=45, b=25),
        legend=dict(orientation="h"),
        yaxis_title="مبنای ابتدای ثبت روز = ۱۰۰",
        xaxis=dict(
            title="ساعت تهران",
            tickmode="array",
            tickvals=tick_values,
            ticktext=[fa(t.strftime("%H:%M")) for t in tick_values],
        ),
    )

    st.plotly_chart(
        fig,
        width="stretch",
        key=f"{key_prefix}_chart",
    )

    details = pd.DataFrame(packet["details"])

    if scope != "combined":
        details = details[details["kind"] == scope].copy()

    query = st.text_input(
        "جست‌وجوی نماد",
        key=f"{key_prefix}_search",
    ).strip()

    if query:
        details = details[
            details["symbol"].str.contains(query, regex=False, na=False)
        ].copy()

    details["weight_pct"] = details[f"weight_{scope}"] * 100
    details["pressure_pct"] = details["pressure_z"] * 100
    details["method"] = details["price_method"].map(PRESSURE_METHODS)

    details = details.sort_values("weight_pct", ascending=False)

    columns = {
        "symbol": "نماد",
        "weight_pct": "وزن ثابت ٪",
        "anchor_price": "قیمت پایه · ریال",
        "model_price": "قیمت مدل · ریال",
        "anchor_return_pct": "تغییر قیمت از مبنا ٪",
        "model_return_pct": "تغییر مدل از مبنا ٪",
        "pressure_pct": "فشار",
        "bid_depth": "عمق خرید تنزیل‌شده · ریال",
        "ask_depth": "عمق فروش تنزیل‌شده · ریال",
        "depth_scale": "مقیاس فشار · ریال",
        "method": "روش",
    }

    view = details[list(columns)].rename(columns=columns)

    numeric = view.select_dtypes(include="number").columns

    st.dataframe(
        view.style.format(
            {column: "{:,.3f}" for column in numeric},
            na_rep="—",
        ),
        hide_index=True,
        width="stretch",
        height=450,
    )

    st.download_button(
        "دانلود جزئیات",
        data=details.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"pressure_{day}_{scope}.csv",
        mime="text/csv",
        key=f"{key_prefix}_download",
    )

    with st.expander("توضیح مدل و محدودیت‌ها"):
        st.markdown("""
- شاخص قیمت از قیمت پایه مدل استفاده می‌کند:
  وسط بهترین خریدوفروش، سقف/کف صف تأییدشده، یا Last در نبود دفتر معتبر.
  بنابراین شاخص رسمی معاملات یا پرتفوی قابل اجرای تضمین‌شده نیست.
- هر دو منحنی نسبت به اولین ثبت روز مبناگذاری شده‌اند.
  اختلاف آن‌ها تغییر اثر فشار نسبت به مبناست، نه صرفاً شدت فعلی صف.
- وزن‌ها و مقیاس عمق در طول روز ثابت‌اند.
- در نبود سابقه عمق، مقیاس فشار آزمایشی است.
- حذف پسوند عددی یک فیلتر موقت است و ممکن است ابزار معتبر را هم حذف کند.
- سبد ترکیبی ممکن است مواجهه صندوق و سهام زیرمجموعه را دوباره بشمارد.
- شاخص‌ها روزانه مستقل‌اند و بین روزها زنجیره نشده‌اند.
- سفارش‌ها قابل لغو هستند؛ این مدل هنوز با معاملات بعدی کالیبره نشده است.
- زمان دریافت، تضمین‌کننده زمان تولید یا تازگی سفارش‌ها نیست.
        """)


# ============================================================
# INDEX VIEW (embedded)
# ============================================================

INDEX_FA_DIGITS = str.maketrans(
    "0123456789",
    "۰۱۲۳۴۵۶۷۸۹",
)

INDEX_MONTHS = [
    "",
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
]

INDEX_SCOPES = {
    "ترکیبی؛ سهام و صندوق": "combined",
    "فقط سهام": "stock",
    "فقط صندوق‌ها": "fund",
}

INDEX_METHODS = {
    "depth": "دفتر سفارش معتبر",
    "last_no_book": "آخرین؛ بدون دفتر معتبر",
    "last_one_sided": "آخرین؛ دفتر یک‌طرفه",
    "last_crossed": "آخرین؛ دفتر متقاطع",
    "last_wide_spread": "آخرین؛ اسپرد زیاد",
}

INDEX_EXCLUSION_LABELS = {
    "invalid_reference": "قیمت مبنای نامعتبر",
    "invalid_last": "آخرین قیمت نامعتبر",
    "invalid_shares": "تعداد سهام/واحد نامعتبر",
    "invalid_market_cap": "ارزش بازار نامعتبر",
    "numeric_suffix_review": (
        "قرنطینه موقت؛ پسوند عددی و نیاز به بررسی تابلو"
    ),
}


def fa(value):
    return str(value).translate(INDEX_FA_DIGITS)


def index_fmt(value, digits=2, suffix=""):
    if value is None or pd.isna(value):
        return "—"
    return fa(f"{value:,.{digits}f}") + suffix


def jalali_day(value):
    date = dt.date.fromisoformat(str(value)[:10])
    jd = jdatetime.date.fromgregorian(date=date)

    return fa(
        f"{jd.day} {INDEX_MONTHS[jd.month]} {jd.year}"
    )


def jalali_timestamp(value):
    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("Asia/Tehran")

    return (
        jalali_day(timestamp.date().isoformat())
        + " · "
        + fa(timestamp.strftime("%H:%M:%S"))
    )


def line_chart(frame, series, key, y_title):
    x_values = frame["fetched_at"]
    hover_dates = [
        jalali_timestamp(value)
        for value in x_values
    ]

    fig = go.Figure()

    for column, label, color in series:
        if column not in frame.columns:
            continue
        fig.add_trace(
            go.Scatter(
                x=x_values,
                y=frame[column],
                name=label,
                mode="lines",
                line=dict(
                    color=color,
                    width=2.5,
                ),
                text=hover_dates,
                hovertemplate=(
                    "%{text}<br>"
                    + label
                    + ": %{y:,.3f}"
                    + "<extra></extra>"
                ),
                connectgaps=False,
            )
        )

    tick_count = min(7, len(frame))

    positions = sorted({
        round(
            index * (len(frame) - 1)
            / max(1, tick_count - 1)
        )
        for index in range(tick_count)
    })

    ticks = [x_values.iloc[index] for index in positions]

    fig.update_layout(
        template="plotly_dark",
        height=360,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(17,27,45,.55)",
        font=dict(
            family="Vazirmatn, Tahoma, sans-serif",
            size=12,
        ),
        margin=dict(l=25, r=25, t=40, b=35),
        legend=dict(
            orientation="h",
            x=1,
            xanchor="right",
            y=1.12,
        ),
        hovermode="closest",
        yaxis_title=y_title,
        xaxis=dict(
            type="date",
            tickmode="array",
            tickvals=ticks,
            ticktext=[
                fa(value.strftime("%H:%M:%S"))
                for value in ticks
            ],
            title="ساعت تهران",
        ),
    )

    st.plotly_chart(
        fig,
        width='stretch',
        key=key,
        config={"displaylogo": False},
    )


def return_color(value):
    if pd.isna(value):
        return ""

    color = "#2DD4BF" if value >= 0 else "#FB7185"
    return f"color: {color}; font-weight: 600;"


def render_market_index(key_prefix="market_index"):
    init_db()

    st.subheader("شاخص فشار قیمت بازار")

    st.caption(
        "نمای لحظه‌ای با وزن متغیر؛ "
        "قیمت برآوردی دفتر سفارش، قیمت اجرای تضمین‌شده نیست."
    )
    st.caption(f"منبع اسنپ‌شات: `{DB_PATH}`")

    dates = available_dates()

    if not dates:
        st.info(
            "هنوز اسنپ‌شاتی ذخیره نشده است. "
            f"مسیر: `{DB_PATH}`"
        )
        st.code("python collect_pressure.py --once\n# یا: export NOVA_MARKET_DB=/path/to/market.sqlite")
        return

    left, middle, right = st.columns([2, 2, 1])

    selected_date = left.selectbox(
        "روز ثبت اسنپ‌شات",
        dates,
        format_func=jalali_day,
        key=f"{key_prefix}_date",
    )

    scope_label = middle.selectbox(
        "سبد",
        list(INDEX_SCOPES),
        key=f"{key_prefix}_scope",
    )

    # کلیک، برنامه را rerun می‌کند و داده مجدداً خوانده می‌شود.
    right.button(
        "به‌روزرسانی",
        key=f"{key_prefix}_refresh",
    )

    scope = INDEX_SCOPES[scope_label]

    records = read_history(selected_date, scope)

    if not records:
        st.warning("برای این انتخاب داده‌ای موجود نیست.")
        return

    history = pd.DataFrame(records)

    # فرمول‌های متفاوت را در یک منحنی به هم وصل نکن.
    history["definition"] = (
        history["formula_version"]
        + "|"
        + history["config_json"]
    )

    latest_definition = history.iloc[-1]["definition"]

    if history["definition"].nunique() > 1:
        st.warning(
            "تنظیمات محاسبه در این روز تغییر کرده است. "
            "فقط اسنپ‌شات‌های هم‌تعریف با آخرین ثبت نمایش داده می‌شوند."
        )

    history = history[
        history["definition"] == latest_definition
    ].copy()

    history["fetched_at"] = pd.to_datetime(
        history["fetched_at"],
        utc=True,
    ).dt.tz_convert("Asia/Tehran")

    latest = history.iloc[-1]
    packet = read_snapshot(int(latest["snapshot_id"]))

    if packet is None:
        st.warning("اسنپ‌شات انتخاب‌شده دیگر موجود نیست.")
        return

    summary = packet["summaries"][scope]
    config = packet["config"]

    st.caption(
        "آخرین دریافت: "
        + jalali_timestamp(latest["fetched_at"])
        + " | نسخه: "
        + packet["formula_version"]
    )

    cap_weight = config["cap_weight"]

    st.caption(
        f"وزن اندازه بازار: {fmt(cap_weight * 100, 0)}٪"
        f" · وزن ارزش معاملات: {fmt((1 - cap_weight) * 100, 0)}٪"
        f" · سقف اسپرد: {fmt(config['max_spread_pct'], 1)}٪"
    )

    if summary.get("weight_mode") == "cap_only":
        st.warning(
            "ارزش معاملات کل سبد صفر است؛ "
            "در این اسنپ‌شات وزن‌ها فقط بر اساس اندازه نماد هستند."
        )

    if not summary.get("eligible_count"):
        st.warning("این سبد نماد واجد شرایط محاسبه ندارد.")
    else:
        columns = st.columns(4)

        columns[0].metric(
            "عدد نمای روزانه",
            fmt(summary.get("points"), 1),
        )

        columns[1].metric(
            "تغییر قیمت برآوردی",
            fmt(summary.get("return_pct"), 2, "٪"),
        )

        columns[2].metric(
            "فشار بخش معتبر",
            fmt(summary.get("pressure_valid"), 1),
        )

        columns[3].metric(
            "پوشش وزنی دفتر سفارش",
            fmt(summary.get("book_coverage_pct"), 1, "٪"),
        )

        st.caption(
            "فاصله از بازده Last با همان وزن‌ها: "
            + fmt(summary.get("book_gap_pp"), 3)
            + " واحد درصد"
            + " | نمادهای محاسبه‌شده: "
            + fmt(summary.get("eligible_count"), 0)
            + " | حذف‌شده: "
            + fmt(summary.get("excluded_count"), 0)
        )

        if (summary.get("book_coverage_pct") or 0) < 70:
            st.warning(
                "پوشش دفتر سفارش کمتر از ۷۰٪ است. "
                "بخش بزرگی از خروجی به Last متکی است؛ "
                "فشار نمایش‌داده‌شده نماینده همه سبد نیست."
            )

    if (
        len(history) >= 2
        and history.iloc[-1]["source_hash"]
        == history.iloc[-2]["source_hash"]
    ):
        st.info(
            "پاسخ خام دو دریافت آخر یکسان بوده است؛ "
            "ممکن است بازار بی‌تغییر، بسته یا داده منبع به‌روزنشده باشد."
        )

    st.markdown("#### مسیر تغییرات در همان روز")

    line_chart(
        history,
        [
            (
                "return_pct",
                "قیمت برآوردی",
                "#2DD4BF",
            ),
            (
                "last_return_pct",
                "آخرین معامله با همان وزن‌ها",
                "#A78BFA",
            ),
        ],
        key=f"{key_prefix}_returns",
        y_title="درصد تغییر نسبت به مبنای روز",
    )

    line_chart(
        history,
        [
            (
                "pressure_valid",
                "فشار بخش معتبر",
                "#60A5FA",
            ),
            (
                "book_coverage_pct",
                "پوشش دفتر سفارش ٪",
                "#FBBF24",
            ),
        ],
        key=f"{key_prefix}_pressure",
        y_title="فشار / درصد پوشش",
    )

    st.markdown("#### اجزای آخرین اسنپ‌شات")

    details = pd.DataFrame(packet["details"] or [])

    if not details.empty:
        if scope != "combined" and "kind" in details.columns:
            details = details[details["kind"] == scope].copy()

        if not details.empty:
            # وزن: weight_combined / weight_stock / weight_fund یا weight
            weight_column = None
            for cand in (f"weight_{scope}", "weight_combined", "weight", "weight_stock"):
                if cand in details.columns:
                    weight_column = cand
                    break
            if weight_column is None:
                details["_w"] = 1.0 / max(len(details), 1)
                weight_column = "_w"

            details["weight_pct"] = pd.to_numeric(details[weight_column], errors="coerce") * 100

            # بازده: return_pct یا model_return_pct یا anchor_return_pct
            ret_col = None
            for cand in ("return_pct", "model_return_pct", "anchor_return_pct", "last_return_pct"):
                if cand in details.columns:
                    ret_col = cand
                    break
            if ret_col is None:
                details["return_pct"] = 0.0
                ret_col = "return_pct"
            elif ret_col != "return_pct":
                details["return_pct"] = pd.to_numeric(details[ret_col], errors="coerce")

            if "market_cap" in details.columns:
                details["market_cap"] = pd.to_numeric(details["market_cap"], errors="coerce") / 1e13
            if "trade_value" in details.columns:
                details["trade_value"] = pd.to_numeric(details["trade_value"], errors="coerce") / 1e10

            details["contribution_pp"] = (
                pd.to_numeric(details[weight_column], errors="coerce").fillna(0)
                * pd.to_numeric(details["return_pct"], errors="coerce").fillna(0)
            )

            if "imbalance" in details.columns:
                details["pressure"] = pd.to_numeric(details["imbalance"], errors="coerce") * 100
            elif "pressure" not in details.columns:
                details["pressure"] = np.nan

            if "price_method" in details.columns:
                details["method"] = details["price_method"].map(INDEX_METHODS)
            else:
                details["method"] = "—"

            details["asset_kind"] = details["kind"].map({
                "stock": "سهام",
                "fund": "صندوق",
                "call": "اختیار خرید",
                "put": "اختیار فروش",
            }) if "kind" in details.columns else "—"

            query = st.text_input(
                "جست‌وجوی نماد یا نام",
                key=f"{key_prefix}_search",
            ).strip()

            if query:
                mask = (
                    details["symbol"].str.contains(
                        query,
                        regex=False,
                        na=False,
                    )
                    | details["name"].str.contains(
                        query,
                        regex=False,
                        na=False,
                    )
                )

                details = details[mask].copy()

            details = details.sort_values(
                "weight_pct",
                ascending=False,
            )

            labels = {
                "symbol": "نماد",
                "asset_kind": "نوع",
                "last_trade": "آخرین · ریال",
                "estimated_price": "برآوردی · ریال",
                "return_pct": "تغییر برآوردی ٪",
                "last_return_pct": "تغییر Last ٪",
                "model_return_pct": "تغییر مدل ٪",
                "anchor_return_pct": "تغییر از مبنا ٪",
                "weight_pct": "وزن ٪",
                "contribution_pp": "اثر · واحد درصد",
                "pressure": "فشار",
                "spread_pct": "اسپرد ٪",
                "market_cap": "ارزش بازار · همت",
                "trade_value": "ارزش معاملات · میلیارد تومان",
                "method": "روش قیمت‌گذاری",
            }

            present = [c for c in labels if c in details.columns]
            if not present:
                st.info("جزئیات قابل‌نمایش در این اسنپ‌شات نیست.")
                present = []
            view = details[present].rename(columns={c: labels[c] for c in present})

            formats = {
                "آخرین · ریال": "{:,.0f}",
                "برآوردی · ریال": "{:,.2f}",
                "تغییر برآوردی ٪": "{:+.2f}",
                "تغییر Last ٪": "{:+.2f}",
                "وزن ٪": "{:.3f}",
                "اثر · واحد درصد": "{:+.4f}",
                "فشار": "{:+.1f}",
                "اسپرد ٪": "{:.2f}",
                "ارزش بازار · همت": "{:,.3f}",
                "ارزش معاملات · میلیارد تومان": "{:,.2f}",
            }

            formats = {k: v for k, v in formats.items() if k in view.columns}
            styled = view.style.format(formats, na_rep="—")
            color_cols = [
                c for c in (
                    "تغییر برآوردی ٪", "تغییر Last ٪", "تغییر مدل ٪",
                    "تغییر از مبنا ٪", "اثر · واحد درصد",
                ) if c in view.columns
            ]
            if color_cols:
                styled = styled.apply(
                    lambda series: [return_color(value) for value in series],
                    subset=color_cols,
                )

            st.dataframe(
                styled,
                hide_index=True,
                width='stretch',
                height=480,
            )

            st.download_button(
                "دانلود جزئیات همین اسنپ‌شات",
                data=details.to_csv(
                    index=False,
                ).encode("utf-8-sig"),
                file_name=(
                    f"market_index_{selected_date}_{scope}.csv"
                ),
                mime="text/csv",
                key=f"{key_prefix}_download",
            )

    with st.expander("نمادهای حذف‌شده از محاسبات"):
        exclusions = pd.DataFrame(packet["exclusions"])

        if exclusions.empty:
            st.success("نمادی به دلیل ورودی نامعتبر حذف نشده است.")
        else:
            if scope != "combined":
                exclusions = exclusions[
                    exclusions["kind"] == scope
                ].copy()

            exclusions["reason"] = exclusions["reason"].map(
                INDEX_EXCLUSION_LABELS
            )

            st.dataframe(
                exclusions.rename(columns={
                    "id": "شناسه",
                    "symbol": "نماد",
                    "kind": "نوع",
                    "reason": "دلیل حذف",
                }),
                hide_index=True,
                width='stretch',
            )

    with st.expander("روش محاسبه و محدودیت‌ها"):
        st.markdown("""
- عدد نمای روزانه برابر است با مبنای امتیاز ضربدر
  یک به‌علاوه میانگین وزنی تغییر قیمت‌ها.
- وزن‌ها با هر اسنپ‌شات تغییر می‌کنند؛ بنابراین تغییر عدد،
  فقط ناشی از تغییر قیمت نیست.
- فشار بخش معتبر بین منفی ۱۰۰ و مثبت ۱۰۰ است و فقط از
  نمادهای دارای دفتر سفارش قابل‌قبول محاسبه می‌شود.
- معتبر بودن دفتر در این نسخه به معنی اعتبار ساختاری است؛
  تازگی سفارش‌ها و وضعیت توقف نماد تأیید نشده‌اند.
- زمان نمودار، زمان دریافت توسط collector است،
  نه زمان قطعی تولید داده در سامانه معاملات.
- قیمت مبنا از `yesterday_price` گرفته می‌شود؛ تعدیلات
  اقدامات شرکتی در این نسخه مستقلاً راستی‌آزمایی نمی‌شوند.
- اندازه صندوق از تعداد واحد × Last محاسبه شده و NAV نیست.
- سبد ترکیبی ممکن است مواجهه اقتصادی مشترک صندوق و سهام
  زیرمجموعه آن را دوباره بشمارد.
- تعطیلات رسمی و ساعات متفاوت انواع صندوق خودکار تشخیص
  داده نمی‌شوند.
- سفارش‌های قابل‌لغو الزاماً به معامله تبدیل نمی‌شوند.
        """)

        st.caption(f"مسیر دیتابیس: {DB_PATH}")


# ============================================================
# APP CONFIGURATION
# ============================================================

if not _COLLECTOR_MODE:
    st.set_page_config(
        page_title="NOVA | رصدخانه بازار",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

TEHRAN = ZoneInfo("Asia/Tehran")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
}

COLORS = {
    "bg": "#080D19",
    "panel": "#111B2D",
    "text": "#E8EFFB",
    "muted": "#94A5C0",
    "green": "#2DD4BF",
    "red": "#FB7185",
    "purple": "#A78BFA",
    "blue": "#60A5FA",
    "gold": "#FBBF24",
}

# Explicit market universe.
# Other classifications are not silently treated as ordinary stocks.
STOCK_CODES = {"300", "303", "309"}
CALL_CODES = {"311", "320"}
PUT_CODES = {"312", "321"}


HISTORY_YEARS = 40  # حداکثر داده موجود؛ فیلتر سخت ۵ساله برداشته شد
FX_CHUNK_DAYS = 90
MAX_FX_AGE_DAYS = 3
FX_HISTORY_DAYS = int(np.ceil(40 * 365.25))  # پوشش هم‌تراز با تاریخچه سهم

# نگاشت نام پایه در قرارداد اختیار → نماد تابلو
# (صندوق‌ها و اختصارهای رایج نام قرارداد)
BASE_ALIASES = {
    "جوانهک": "جوانه کوچک",
    "جوانه.ک": "جوانه کوچک",
    "جوانه‌ک": "جوانه کوچک",
    "جوانه کوچک": "جوانه کوچک",
    "جوانهکوچک": "جوانه کوچک",
    "اهرم": "اهرم",
    "طلا": "طلا",
    "عیار": "عیار",
    "کهربا": "کهربا",
    "گندم": "گندم",
}

MARKET_COLUMNS = [
    "id",
    "isin",
    "symbol",
    "name",
    "time",
    "first_price",
    "close_price",
    "last_trade",
    "number_trades",
    "volume",
    "value",
    "low_price",
    "high_price",
    "yesterday_price",
    "eps",
    "base_volume",
    "table_id",
    "industry_id",
    "section_code",
    "max_allowed_price",
    "min_allowed_price",
    "number_shares",
    "asset_code",
]

BOOK_COLUMNS = [
    "id",
    "level",
    "sell_count",
    "buy_count",
    "bid_price",
    "ask_price",
    "bid_vol",
    "ask_vol",
]

DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

# ============================================================
# نمایش فارسی تاریخ در نمودارهای Plotly
# ============================================================

import numpy as np
import pandas as pd
try:
    import jdatetime
except ImportError:
    jdatetime = None  # type: ignore


FA_DIGITS = str.maketrans(
    "0123456789",
    "۰۱۲۳۴۵۶۷۸۹",
)

JALALI_MONTHS = [
    "",
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
]

# datetime.weekday(): Monday = 0
FA_WEEKDAYS = [
    "دوشنبه",
    "سه‌شنبه",
    "چهارشنبه",
    "پنجشنبه",
    "جمعه",
    "شنبه",
    "یکشنبه",
]


def fa_digits(value):
    return str(value).translate(FA_DIGITS)


def jalali_label(value, full=False, with_time=False):
    """
    ورودی: تاریخ میلادی
    خروجی: متن فارسی تاریخ شمسی

    برای داده‌های لحظه‌ای، زمان ورودی باید زمان تهران باشد
    یا timezone مشخص داشته باشد.
    """
    timestamp = pd.Timestamp(value)

    if pd.isna(timestamp):
        return "—"

    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("Asia/Tehran")

    jalali = jdatetime.date.fromgregorian(
        date=timestamp.date()
    )

    text = (
        f"{jalali.day} "
        f"{JALALI_MONTHS[jalali.month]} "
        f"{jalali.year}"
    )

    if full:
        weekday = FA_WEEKDAYS[timestamp.weekday()]
        text = f"{weekday}، {text}"

    if with_time:
        text += f" · ساعت {timestamp:%H:%M}"

    return fa_digits(text)


def persianize_chart_dates(
    fig,
    date_axes=("x",),
    max_ticks=7,
    intraday=False,
):
    """
    فارسی‌سازی محورهای زمانی مشخص‌شده، بدون تغییر داده‌های x.

    date_axes:
        نمودار معمولی: ("x",)
        نمودار سه‌ردیفه زمانی: ("x", "x2", "x3")

    فقط محورهای واقعاً زمانی را معرفی کن.
    محور قیمت در نمودار P&L کاوردکال، محور زمانی نیست.
    """

    for axis_ref in date_axes:
        axis_dates = []

        for trace in fig.data:
            trace_axis = getattr(trace, "xaxis", None) or "x"

            if trace_axis != axis_ref:
                continue

            raw_x = getattr(trace, "x", None)

            if raw_x is None or len(raw_x) == 0:
                continue

            # جلوگیری از تفسیر قیمت یا شماره ردیف به‌عنوان تاریخ
            raw_series = pd.Series(list(raw_x))

            if pd.api.types.is_numeric_dtype(raw_series.dtype):
                raise ValueError(
                    f"محور {axis_ref} عددی است؛ "
                    "فقط محور تاریخ را فارسی‌سازی کن."
                )

            dates = pd.to_datetime(
                raw_series,
                errors="coerce",
            )

            if dates.isna().any():
                raise ValueError(
                    f"بخشی از تاریخ‌های محور {axis_ref} نامعتبر است."
                )

            axis_dates.extend(dates.tolist())

            # جایگزین متن hover، بدون دست‌زدن به customdata
            hover_dates = [
                jalali_label(
                    value,
                    full=True,
                    with_time=intraday,
                )
                for value in dates
            ]

            if trace.type in ("scatter", "scattergl", "bar"):
                trace.hovertext = hover_dates

                current_template = trace.hovertemplate

                if (
                    isinstance(current_template, str)
                    and current_template
                ):
                    # حفظ قالب قبلی قیمت و customdata؛
                    # فقط جایگزینی نمایش تاریخ x
                    import re

                    trace.hovertemplate = re.sub(
                        r"%\{x(?:[^}]*)\}",
                        "%{hovertext}",
                        current_template,
                    )
                else:
                    trace.hovertemplate = (
                        "<b>%{hovertext}</b><br>"
                        "مقدار: %{y:,.2f}"
                        "<extra>%{fullData.name}</extra>"
                    )

            elif trace.type in ("candlestick", "ohlc"):
                # سازگار با نسخه‌هایی که hovertemplate کندل ندارند
                trace.hovertext = [
                    (
                        f"<b>{date_text}</b><br>"
                        f"بازشدن: {fa_digits(f'{o:,.0f}')}<br>"
                        f"بیشترین: {fa_digits(f'{h:,.0f}')}<br>"
                        f"کمترین: {fa_digits(f'{l:,.0f}')}<br>"
                        f"بسته‌شدن: {fa_digits(f'{c:,.0f}')}"
                    )
                    for date_text, o, h, l, c in zip(
                        hover_dates,
                        trace.open,
                        trace.high,
                        trace.low,
                        trace.close,
                    )
                ]
                trace.hoverinfo = "text"

        if not axis_dates:
            continue

        unique_dates = pd.DatetimeIndex(
            axis_dates
        ).unique().sort_values()

        count = min(max(2, int(max_ticks)), len(unique_dates))

        positions = np.unique(
            np.linspace(
                0,
                len(unique_dates) - 1,
                count,
            ).astype(int)
        )

        ticks = unique_dates[positions]

        labels = [
            jalali_label(value, with_time=intraday)
            for value in ticks
        ]

        # x -> xaxis / x2 -> xaxis2
        layout_axis = "xaxis" + axis_ref[1:]

        fig.update_layout(**{
            layout_axis: dict(
                type="date",
                tickmode="array",
                tickvals=ticks.tolist(),
                ticktext=labels,
                tickangle=0,
                automargin=True,
                title=dict(text="تاریخ"),
                tickfont=dict(
                    family="Vazirmatn, Tahoma, sans-serif",
                    size=12,
                ),
            )
        })

    fig.update_layout(
        font=dict(
            family="Vazirmatn, Tahoma, sans-serif",
            size=13,
        ),
        hoverlabel=dict(
            font=dict(
                family="Vazirmatn, Tahoma, sans-serif",
                size=13,
            ),
            align="right",
        ),

        # جلوگیری از عنوان تاریخ میلادی در hover یکپارچه
        hovermode="closest",
    )

    return fig
# ============================================================
# FONT / STYLE
# ============================================================

font_path = Path(__file__).resolve().parent / "assets" / "Vazirmatn-Regular.woff2"

if font_path.exists():
    font_data = base64.b64encode(font_path.read_bytes()).decode()
    font_css = f"""
    @font-face {{
        font-family: Vazirmatn;
        src: url(data:font/woff2;base64,{font_data}) format("woff2");
        font-style: normal;
        font-weight: 400;
        font-display: swap;
    }}
    """
else:
    font_css = """
    @import url(
        'https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap'
    );
    """

CSS = """
<style>
__FONT__

html, body, button, input, textarea,
[data-testid="stAppViewContainer"],
[data-testid="stMarkdownContainer"],
[data-testid="stMetric"],
[data-testid="stCaptionContainer"] {
    font-family: Vazirmatn, Tahoma, sans-serif !important;
}

.stApp {
    background:
        radial-gradient(
            ellipse at 92% 0%,
            rgba(45,212,191,.10),
            transparent 38%
        ),
        radial-gradient(
            ellipse at 0% 30%,
            rgba(167,139,250,.075),
            transparent 40%
        ),
        #080D19;
    color: #E8EFFB;
}

.block-container {
    max-width: 1650px;
    padding-top: 1.3rem;
    padding-bottom: 3rem;
}

[data-testid="stMarkdownContainer"],
[data-testid="stCaptionContainer"],
[data-testid="stAlert"],
[data-testid="stMetricLabel"] {
    direction: rtl;
    text-align: right;
}

[data-testid="stSidebar"] {
    display: none;
}

[data-testid="stHeader"] {
    background: rgba(8,13,25,.65);
}

[data-testid="stMetric"] {
    background:
        linear-gradient(145deg, rgba(22,35,58,.97), rgba(13,22,39,.97));
    border: 1px solid rgba(148,165,192,.16);
    border-radius: 20px;
    padding: 18px;
    min-height: 125px;
    box-shadow:
        0 12px 28px rgba(0,0,0,.16),
        inset 0 1px 0 rgba(255,255,255,.04);
    transition: border-color .2s ease, transform .2s ease;
}

[data-testid="stMetric"]:hover {
    border-color: rgba(45,212,191,.35);
    transform: translateY(-1px);
}

[data-testid="stMetricLabel"] {
    color: #94A5C0;
}

[data-testid="stMetricValue"] {
    color: #E8EFFB;
    direction: ltr;
    text-align: right;
    font-size: clamp(1.1rem, 1.8vw, 1.9rem);
}

.hero {
    direction: rtl;
    position: relative;
    overflow: hidden;
    padding: 28px 32px;
    border-radius: 26px;
    border: 1px solid rgba(45,212,191,.22);
    background:
        linear-gradient(125deg, rgba(19,44,57,.96), rgba(28,25,51,.97));
    box-shadow:
        0 18px 40px rgba(0,0,0,.22),
        inset 0 1px 0 rgba(94,234,212,.08);
    margin-bottom: 22px;
}

.hero:after {
    content: "◈";
    position: absolute;
    left: 22px;
    top: -55px;
    font-size: 220px;
    color: rgba(45,212,191,.07);
    transform: rotate(18deg);
    pointer-events: none;
}

.hero h1 {
    font-size: clamp(26px, 3vw, 40px);
    color: #F3F7FF;
    font-weight: 800;
    margin: 9px 0 12px;
}

.hero p {
    color: #A8B8CF;
    line-height: 2;
    font-size: 13px;
    margin: 0;
}

.eyebrow {
    color: #5EEAD4;
    font-size: 11px;
    letter-spacing: 3px;
    direction: ltr;
    text-align: right;
}

.badge {
    display: inline-block;
    padding: 5px 11px;
    margin: 14px 0 0 7px;
    border-radius: 99px;
    border: 1px solid rgba(45,212,191,.20);
    color: #8DEADD;
    background: rgba(45,212,191,.06);
    font-size: 11px;
}

.section-title {
    direction: rtl;
    border-right: 3px solid #2DD4BF;
    padding-right: 12px;
    margin: 23px 0 16px;
    color: #DEE8F8;
    font-weight: 700;
    font-size: 19px;
}

[data-baseweb="tab-list"] {
    gap: 7px;
    background: rgba(17,27,45,.8);
    border-radius: 16px;
    padding: 7px;
}

[data-baseweb="tab"] {
    color: #A6B6CE !important;
    border-radius: 11px;
    padding: 12px 17px;
    font-family: Vazirmatn, Tahoma, sans-serif !important;
}

[aria-selected="true"][data-baseweb="tab"] {
    background: rgba(45,212,191,.12);
    color: #5EEAD4 !important;
}

[data-testid="stDataFrame"] {
    border: 1px solid rgba(148,165,192,.13);
    border-radius: 15px;
    overflow: hidden;
}

[data-testid="stDownloadButton"] button {
    border-radius: 12px;
    background: #132139;
    color: #E8EFFB;
    border: 1px solid rgba(96,165,250,.25);
}

code, pre {
    direction: ltr !important;
    text-align: left !important;
}

@media(max-width: 700px) {
    .hero {
        padding: 22px;
    }
    .block-container {
        padding-left: 1rem;
        padding-right: 1rem;
    }
}
</style>
"""

if not _COLLECTOR_MODE:
    st.markdown(CSS.replace("__FONT__", font_css), unsafe_allow_html=True)


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_tehran():
    return dt.datetime.now(TEHRAN)


def normalize_text(value):
    if value is None or pd.isna(value):
        return ""

    text = str(value).translate(DIGITS)
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"[\u200c\u200e\u200f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def numeric_series(series):
    cleaned = (
        series.astype("string")
        .str.translate(DIGITS)
        .str.replace(",", "", regex=False)
        .str.replace("٬", "", regex=False)
        .str.strip()
    )

    result = pd.to_numeric(cleaned, errors="coerce")
    return result.astype(float).replace([np.inf, -np.inf], np.nan)


def fmt(value, digits=0, suffix=""):
    """digits اعشار؛ suffix اختیاری مثل ٪"""
    try:
        value = float(value)
        if not np.isfinite(value):
            return "—"
        return f"{value:,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def pct(value, digits=1):
    text = fmt(value, digits)
    return "—" if text == "—" else f"{text}٪"


def safe_ratio(a, b):
    if pd.notna(a) and pd.notna(b) and b > 0:
        return a / b
    return np.nan


def total(series):
    return pd.to_numeric(series, errors="coerce").sum(min_count=1)


def money_smart(rial, fx_rial=None, digits=2):
    """
    نمایش مبالغ بزرگ بدون ریال خام:
    ≥ ۱ همت (۱۰¹² تومان) → همت
    ≥ ۱ میلیارد تومان → میلیارد تومان
    در غیر این صورت اگر نرخ تتر باشد → USDT
    وگرنه میلیون تومان.
    """
    try:
        r = float(rial)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(r):
        return "—"
    toman = abs(r) / 10.0
    sign = "-" if r < 0 else ""
    if toman >= 1e12:
        return f"{sign}{toman / 1e12:,.{digits}f} همت"
    if toman >= 1e9:
        return f"{sign}{toman / 1e9:,.{digits}f} میلیارد تومان"
    if fx_rial is not None:
        try:
            fx = float(fx_rial)
            if np.isfinite(fx) and fx > 0:
                return f"{sign}{abs(r) / fx:,.0f} USDT"
        except (TypeError, ValueError):
            pass
    if toman >= 1e6:
        return f"{sign}{toman / 1e6:,.1f} م‌تومان"
    return f"{sign}{toman:,.0f} تومان"


def verify_nova_signature() -> bool:
    payload = (
        _NOVA_DEV["name_fa"]
        + "|"
        + _NOVA_DEV["name_en"]
        + "|"
        + _NOVA_DEV["tagline"]
        + "|"
        + _NOVA_DEV["linkedin"]
        + "|nova-free-2026"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() == _NOVA_SIG_EXPECT


def render_developer_credit():
    """امضای پنهان/کم‌رنگ در پایین صفحه؛ با لینک لینکدین."""
    if not verify_nova_signature():
        st.caption("⚠ اثرانگشت توسعه‌دهنده تغییر کرده است.")
    st.markdown(
        f"""
        <div class="nova-dev-sig" style="
            margin-top:2.2rem;padding:10px 14px;
            border-top:1px solid rgba(148,163,184,.18);
            font-size:11px;opacity:.42;text-align:center;
            font-family:Vazirmatn,Tahoma,sans-serif;
            user-select:none;pointer-events:auto;
        ">
            توسعه:
            <a href="{_NOVA_DEV['linkedin']}" target="_blank" rel="noopener"
               style="color:#7DD3FC;text-decoration:none;font-weight:600;">
               {_NOVA_DEV['name_fa']} · {_NOVA_DEV['name_en']}
            </a>
            — {_NOVA_DEV['tagline']}
        </div>
        """,
        unsafe_allow_html=True,
    )



def section(title):
    st.markdown(
        f'<div class="section-title">{html.escape(title)}</div>',
        unsafe_allow_html=True,
    )


# نام‌های رایج صندوق درآمد ثابت / اوراق — از تب سهم حذف می‌شوند
_FIXED_INCOME_RE = re.compile(
    r"(درآمد\s*ثابت|ثابت\s*درآمد|صندوق\s*اوراق|اوراق\s*مشارکت|"
    r"نوع\s*درآمد|آرام\b|کارین|یارا|نامی|دیوان|هامرز|"
    r"پارند|نهال|گنجینه|سپر|پاداش|کمند|آسان|یاقوت)",
    re.IGNORECASE,
)


def is_fixed_income_fund(row) -> bool:
    """صندوق‌های غیرسهامی که برای تعدیل ارزی/تحلیل سهم مفید نیستند."""
    if str(row.get("kind", "")) != "صندوق":
        return False
    blob = f"{row.get('symbol', '')} {row.get('name', '')}"
    blob = normalize_text(blob)
    if _FIXED_INCOME_RE.search(blob):
        return True
    # اگر در نام صریحاً «سهام» / اهرم / طلا / کالایی باشد نگه می‌داریم
    if re.search(r"(سهام|اهرم|طلا|کالایی|جوانه|مثبت|فیروزه)", blob):
        return False
    # صندوق‌های مبهم: اگر ارزش معاملات خیلی کم و بدون نشانه سهامی
    return False


def is_energy3(symbol: str) -> bool:
    """استثناء: انرژی۳ (یا انرژی3) در دراپ‌داون می‌ماند."""
    s = re.sub(r"[\s\u200c\u200e\u200f]", "", normalize_text(symbol))
    return s in {"انرژی۳", "انرژی3", "انرژي۳", "انرژي3"}


def has_digit_in_symbol(symbol: str) -> bool:
    return bool(re.search(r"\d", normalize_text(symbol)))


def is_block_or_major_symbol(symbol: str) -> bool:
    """True = از دراپ‌داون سهم حذف شود (به‌جز انرژی۳)."""
    if is_energy3(symbol):
        return False
    return has_digit_in_symbol(symbol)


def filter_equity_universe(market: pd.DataFrame) -> pd.DataFrame:
    """
    جهان تب سهم: سهام عادی + صندوق سهامی/اهرم/کالایی.
    بدون درآمد ثابت و بدون نمادهای عمده/بلوکی با رقم در نماد
    (استثناء: انرژی۳).
    """
    if market is None or market.empty:
        return market.iloc[0:0].copy() if market is not None else pd.DataFrame()

    m = market.copy()
    stocks = m["kind"].eq("سهام")
    funds = m["kind"].eq("صندوق")
    if funds.any():
        drop_fi = m.apply(is_fixed_income_fund, axis=1)
        funds = funds & ~drop_fi
    out = m.loc[stocks | funds].copy()
    mask = ~out["symbol"].map(is_block_or_major_symbol)
    out = out.loc[mask].copy()
    return out.drop_duplicates("id", keep="last")


def load_fx_memo():
    """نرخ تتر را حداکثر هر ساعت یک‌بار تازه می‌کند؛ بدون اسپینر تکراری."""
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    payload = st.session_state.get("_fx_memo")
    ts = st.session_state.get("_fx_memo_ts", 0)
    if payload is not None and now - ts < 3600:
        return payload, None
    try:
        result = fetch_fx_history()
        st.session_state["_fx_memo"] = result
        st.session_state["_fx_memo_ts"] = now
        return result, None
    except Exception as exc:
        if payload is not None:
            return payload, str(exc)
        return None, str(exc)


def load_cpi_memo():
    """CPI یک‌بار از کش پایدار؛ بدون درخواست تکراری."""
    if "_cpi_memo" in st.session_state:
        return st.session_state["_cpi_memo"], st.session_state.get("_cpi_memo_err")
    try:
        cpi = fetch_cpi_history()
        st.session_state["_cpi_memo"] = cpi
        st.session_state["_cpi_memo_err"] = None
        return cpi, None
    except Exception as exc:
        st.session_state["_cpi_memo"] = pd.DataFrame()
        st.session_state["_cpi_memo_err"] = str(exc)
        return st.session_state["_cpi_memo"], str(exc)


def chart(fig, key, height=420, date_axes=None, intraday=False, max_ticks=7):
    """رسم نمودار داشبورد؛ date_axes مثلاً ("x",) برای تاریخ شمسی."""
    fig.update_layout(
        template="plotly_dark",
        font=dict(
            family="Vazirmatn, Tahoma, sans-serif",
            color=COLORS["text"],
            size=12,
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(17,27,45,.55)",
        margin=dict(l=25, r=25, t=50, b=35),
        height=height,
        colorway=[
            COLORS["green"],
            COLORS["purple"],
            COLORS["blue"],
            COLORS["red"],
            COLORS["gold"],
        ],
        legend=dict(
            orientation="h",
            x=1,
            xanchor="right",
            y=1.03,
            yanchor="bottom",
        ),
        hoverlabel=dict(
            bgcolor=COLORS["panel"],
            font=dict(family="Vazirmatn, Tahoma", size=13),
        ),
    )
    fig.update_xaxes(gridcolor="rgba(148,165,192,.07)", zeroline=False)
    fig.update_yaxes(gridcolor="rgba(148,165,192,.07)", zeroline=False)

    if date_axes:
        try:
            persianize_chart_dates(
                fig,
                date_axes=date_axes,
                max_ticks=max_ticks,
                intraday=intraday,
            )
        except Exception as exc:
            st.caption(f"فارسی‌سازی تاریخ ممکن نشد: {exc}")

    st.plotly_chart(
        fig,
        key=key,
        use_container_width=True,
        config={
            "displaylogo": False,
            "scrollZoom": False,
            "toImageButtonOptions": {"format": "png", "scale": 2},
        },
    )


LABELS = {
    "symbol": "نماد",
    "name": "نام در منبع",
    "kind": "نوع ابزار",
    "time": "زمان خام منبع",
    "close_price": "پایانی · ریال",
    "last_trade": "آخرین · ریال",
    "return_pct": "بازده پایانی ٪",
    "volume": "حجم گزارش‌شده",
    "value": "ارزش معاملات · ریال",
    "number_trades": "تعداد معاملات",
    "best_bid": "بهترین خرید",
    "best_ask": "بهترین فروش",
    "spread_pct": "اسپرد ٪",
    "pressure": "فشار ارزش سفارش",
    "bid_value": "عمق خرید · ریال",
    "ask_value": "عمق فروش · ریال",
    "price_usdt": "قیمت · تتر با نرخ مرجع",
    "industry_id": "کد صنعت",
    "crossed_book": "دفتر متقاطع",
    "book_complete": "ردیف‌های دو طرف قابل محاسبه",
}


def table(frame, columns=None, height=450):
    view = frame.copy() if columns is None else frame[columns].copy()

    numeric_columns = view.select_dtypes(include="number").columns
    view[numeric_columns] = view[numeric_columns].round(6)

    st.dataframe(
        view.rename(columns=LABELS),
        hide_index=True,
        use_container_width=True,
        height=height,
    )


def download(frame, label, filename, key):
    st.download_button(
        label,
        data=frame.to_csv(index=False).encode("utf-8-sig"),
        file_name=filename,
        mime="text/csv",
        key=key,
    )



# ============================================================
# BS-IR ENGINE (embedded)
# ============================================================

@dataclass(frozen=True)
class Config:
    # نرخ مؤثر سالانه؛ داخل مدل به نرخ پیوسته تبدیل می‌شود.
    annual_rate: float = 0.30

    # بازده نقدی پیوسته؛ برای سود نقدی گسسته باید مدل جدا توسعه یابد.
    dividend_yield: float = 0.0

    # فرض قابل تنظیم؛ بهتر است با تقویم داده خودت تعیین شود.
    trading_days: int = 245

    min_dtm: float = 7.0
    max_spread: float = 0.25
    max_spot_spread: float = 0.02
    max_age_seconds: float = 180.0
    min_peers: int = 3

    # همسایه‌ها باید از نظر سررسید و فاصله اعمال نزدیک باشند.
    max_moneyness_distance: float = 0.25
    max_tenor_ratio: float = 3.0

    # حد جست‌وجوی عددی IV، نه حکم درباره معقول‌بودن اقتصادی آن.
    max_iv: float = 5.0

    # نیم‌پهنای حداقلی باند در مقیاس log(IV).
    min_log_iv_band: float = 0.12

    # باند گسترده‌تر در صورت اتکا به HV.
    hv_log_band: float = 0.30

    # حاشیه هزینه تخمینی، به ازای یک واحد اختیار، به ریال.
    # این مقدار جایگزین محاسبه دقیق کارمزد و لغزش نیست.
    cost_per_unit: float = 0.0

    # —— ضریب کمبود نقدینگی / فوریت نزدیک سررسید ——
    # مدل ساده: ترم دوم کاهنده روی قیمت BS
    #   P_adj = P_BS × (1 − λ(τ))
    #   λ(τ) = λ_max × exp(−DTM / τ_half) × (call_boost اگر call)
    # نزدیک سررسید λ بزرگ‌تر می‌شود (فشار خروج از قرارداد، به‌ویژه کال).
    # دور از سررسید λ به صفر میل می‌کند؛ با τ_half می‌توان اثر را
    # در افق بلندتر هم نگه داشت.
    liquidity_discount_max: float = 0.12
    liquidity_half_life_days: float = 14.0
    liquidity_call_boost: float = 1.35
    iran_tight: float = 0.10


def finite(x):
    try:
        return bool(np.isfinite(float(x)))
    except (TypeError, ValueError):
        return False


def positive(x):
    return finite(x) and float(x) > 0


def normalize_type(x):
    text = str(x).strip().lower()
    return {
        "c": "call",
        "call": "call",
        "اختیار خرید": "call",
        "p": "put",
        "put": "put",
        "اختیار فروش": "put",
    }.get(text)


def bs_price(kind, s, k, t, r, q, sigma):
    """قیمت به ازای یک واحد دارایی پایه، نه کل قرارداد."""
    if kind not in {"call", "put"}:
        return np.nan

    if not positive(s) or not positive(k):
        return np.nan

    if not all(finite(x) for x in (t, r, q, sigma)):
        return np.nan

    if t <= 0:
        return max(s - k, 0.0) if kind == "call" else max(k - s, 0.0)

    a = s * np.exp(-q * t)
    b = k * np.exp(-r * t)

    if sigma <= 0:
        return max(a - b, 0.0) if kind == "call" else max(b - a, 0.0)

    v = sigma * np.sqrt(t)
    d1 = (np.log(s / k) + (r - q) * t) / v + v / 2
    d2 = d1 - v

    if kind == "call":
        return float(a * norm.cdf(d1) - b * norm.cdf(d2))

    return float(b * norm.cdf(-d2) - a * norm.cdf(-d1))


def bounds(kind, s, k, t, r, q):
    a = s * np.exp(-q * t)
    b = k * np.exp(-r * t)

    if kind == "call":
        return max(a - b, 0.0), a

    return max(b - a, 0.0), b


def implied_vol(kind, price, s, k, t, r, q, max_iv=5.0):
    if not positive(price) or not positive(t):
        return np.nan

    low, high = bounds(kind, s, k, t, r, q)

    # در مرزها IV محدود و پایدار لزوماً قابل استخراج نیست.
    if not low < price < high:
        return np.nan

    def objective(sigma):
        return bs_price(kind, s, k, t, r, q, sigma) - price

    try:
        return float(brentq(objective, 1e-6, max_iv))
    except (ValueError, RuntimeError):
        return np.nan


def vega(s, k, t, r, q, sigma):
    """حساسیت قیمت به تغییر یک واحد کامل sigma."""
    if not positive(t) or not positive(sigma):
        return np.nan

    d1 = (
        np.log(s / k) + (r - q + sigma**2 / 2) * t
    ) / (sigma * np.sqrt(t))

    return float(s * np.exp(-q * t) * norm.pdf(d1) * np.sqrt(t))


def weighted_quantile(values, weights, quantiles):
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)

    valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    values = values[valid]
    weights = weights[valid]

    if not len(values):
        return np.full(len(quantiles), np.nan)

    order = np.argsort(values)
    values = values[order]
    weights = weights[order]

    cdf = (np.cumsum(weights) - weights / 2) / weights.sum()

    return np.interp(
        quantiles,
        cdf,
        values,
        left=values[0],
        right=values[-1],
    )


def hv_table(history, trading_days=245, asof=None):
    """
    history:
        underlying_id, date, adjusted_close

    فقط روزهای قبل از تاریخ ارزش‌گذاری استفاده می‌شوند.
    داده مفقود را forward-fill نمی‌کنیم.
    """
    required = {"underlying_id", "date", "adjusted_close"}

    if not required.issubset(history.columns):
        raise ValueError(
            "ستون‌های تاریخچه باید شامل این‌ها باشند: "
            + ", ".join(sorted(required))
        )

    h = history.copy()
    h["underlying_id"] = h["underlying_id"].astype(str)
    h["date"] = pd.to_datetime(h["date"], errors="coerce").dt.normalize()
    h["adjusted_close"] = pd.to_numeric(
        h["adjusted_close"], errors="coerce"
    )

    h = h.dropna(subset=["date", "adjusted_close"])
    h = h[h["adjusted_close"] > 0]

    if asof is not None:
        day = (
            pd.Timestamp(asof)
            .tz_convert("Asia/Tehran")
            .tz_localize(None)
            .normalize()
        )
        h = h[h["date"] < day]

    output = []

    for uid, group in h.groupby("underlying_id"):
        group = (
            group.sort_values("date")
            .drop_duplicates("date", keep="last")
        )

        returns = np.log(group["adjusted_close"]).diff().dropna()

        row = {
            "underlying_id": uid,
            "hv_last_date": group["date"].max(),
        }

        for window in (20, 60, 120):
            row[f"hv{window}"] = (
                float(returns.tail(window).std(ddof=1) * np.sqrt(trading_days))
                if len(returns) >= window else np.nan
            )

        # در این نسخه HV60 اولویت دارد؛ سپس HV120 و HV20.
        row["hv_ref"] = next(
            (
                row[key]
                for key in ("hv60", "hv120", "hv20")
                if positive(row[key])
            ),
            np.nan,
        )

        output.append(row)

    return pd.DataFrame(
        output,
        columns=[
            "underlying_id", "hv_last_date",
            "hv20", "hv60", "hv120", "hv_ref",
        ],
    )


def is_fresh(timestamp, now, seconds):
    if pd.isna(timestamp):
        return False

    age = (now - timestamp).total_seconds()
    return -5 <= age <= seconds


def prepare(options, config, asof):
    required = {
        "option_id",
        "symbol",
        "underlying_id",
        "option_type",
        "strike",
        "expiry",
        "underlying_last",
    }

    missing = required - set(options.columns)

    if missing:
        raise ValueError("ستون‌های ضروری موجود نیستند: " + ", ".join(sorted(missing)))

    d = options.copy()

    if d["option_id"].isna().any() or d["underlying_id"].isna().any():
        raise ValueError("شناسه قرارداد و دارایی پایه نباید خالی باشند.")

    d["option_id"] = d["option_id"].astype(str)
    d["underlying_id"] = d["underlying_id"].astype(str)

    if d["option_id"].duplicated().any():
        raise ValueError("برای هر option_id فقط یک ردیف اسنپ‌شات ارسال کن.")

    d["option_type"] = d["option_type"].map(normalize_type)

    numeric_columns = [
        "strike", "underlying_last",
        "underlying_bid", "underlying_ask",
        "underlying_bid_volume", "underlying_ask_volume",
        "bid", "ask", "bid_volume", "ask_volume",
        "trade_value", "multiplier",
    ]

    for column in numeric_columns:
        if column not in d:
            d[column] = np.nan

        d[column] = pd.to_numeric(d[column], errors="coerce")

    for column in ("expiry", "quote_time", "underlying_quote_time"):
        if column not in d:
            d[column] = pd.NaT

        d[column] = pd.to_datetime(d[column], utc=True, errors="coerce")

    r = float(np.log1p(config.annual_rate))
    q = config.dividend_yield

    output = []

    for _, row in d.iterrows():
        x = row.to_dict()

        t = (
            (row["expiry"] - asof).total_seconds() / (365 * 86400)
            if pd.notna(row["expiry"]) else np.nan
        )

        spot_book = (
            positive(row["underlying_bid"])
            and positive(row["underlying_ask"])
            and row["underlying_ask"] >= row["underlying_bid"]
            and positive(row["underlying_bid_volume"])
            and positive(row["underlying_ask_volume"])
        )

        if spot_book:
            smid = (row["underlying_bid"] + row["underlying_ask"]) / 2
            spot_book = (
                (row["underlying_ask"] - row["underlying_bid"]) / smid
                <= config.max_spot_spread
            )

        if spot_book:
            s = smid
            s_low = row["underlying_bid"]
            s_high = row["underlying_ask"]
        else:
            s = row["underlying_last"]
            s_low = s
            s_high = s

        option_fresh = is_fresh(
            row["quote_time"], asof, config.max_age_seconds
        )
        spot_fresh = is_fresh(
            row["underlying_quote_time"], asof, config.max_age_seconds
        )

        valid = (
            row["option_type"] in {"call", "put"}
            and positive(s)
            and positive(row["strike"])
            and positive(t)
        )

        book = (
            positive(row["bid"])
            and positive(row["ask"])
            and row["ask"] >= row["bid"]
            and positive(row["bid_volume"])
            and positive(row["ask_volume"])
        )

        mid = (row["bid"] + row["ask"]) / 2 if book else np.nan
        spread = (row["ask"] - row["bid"]) / mid if book else np.nan

        x.update({
            "T": t,
            "dtm": t * 365,
            "spot": s,
            "spot_low": s_low,
            "spot_high": s_high,
            "spot_method": "mid" if spot_book else "last_fallback",
            "spot_book_ok": bool(spot_book),
            "fresh": bool(option_fresh and spot_fresh),
            "valid": bool(valid),
            "book_ok": bool(book),
            "mid": mid,
            "spread_pct": spread * 100,
            "iv_bid": np.nan,
            "iv_mid": np.nan,
            "iv_ask": np.nan,
            "log_moneyness": np.nan,
            "bounds_ok": False,
            "vega_ok": False,
        })

        if valid:
            kind = row["option_type"]
            k = row["strike"]

            lo, hi = bounds(kind, s, k, t, r, q)

            for label, price in (
                ("bid", row["bid"]),
                ("mid", mid),
                ("ask", row["ask"]),
            ):
                x[f"iv_{label}"] = implied_vol(
                    kind, price, s, k, t, r, q, config.max_iv
                )

            x["bounds_ok"] = bool(
                book and lo < row["bid"] <= row["ask"] < hi
            )

            forward = s * np.exp((r - q) * t)
            x["log_moneyness"] = np.log(k / forward)

            if positive(x["iv_mid"]) and positive(mid):
                # حذف قراردادهایی که IV آنها به تغییرات کوچک قیمت
                # بسیار حساس و از نظر عددی ناپایدار است.
                x["vega_ok"] = bool(
                    vega(s, k, t, r, q, x["iv_mid"]) * 0.01 / mid
                    >= 0.001
                )

        x["calibration_ok"] = bool(
            x["valid"]
            and x["fresh"]
            and x["spot_book_ok"]
            and x["book_ok"]
            and x["bounds_ok"]
            and x["vega_ok"]
            and x["dtm"] >= config.min_dtm
            and finite(spread)
            and spread <= config.max_spread
            and positive(row["trade_value"])
            and positive(x["iv_mid"])
            and positive(x["iv_bid"])
            and positive(x["iv_ask"])
        )

        output.append(x)

    return pd.DataFrame(output)


def peer_iv(row, prepared, config):
    """
    مرجع محلی با حذف خود قرارداد:
    همان پایه و همان نوع اختیار، نزدیک در سررسید و moneyness.

    این یک سطح SVI یا سطح تضمین‌شده بدون آربیتراژ نیست.
    """
    peers = prepared[
        prepared["calibration_ok"]
        & (prepared["underlying_id"] == row["underlying_id"])
        & (prepared["option_type"] == row["option_type"])
        & (prepared["option_id"] != row["option_id"])
    ].copy()

    if peers.empty or not positive(row["T"]):
        return None

    distance = (
        peers["log_moneyness"] - row["log_moneyness"]
    ).abs()

    tenor_distance = np.abs(np.log(peers["T"] / row["T"]))

    keep = (
        (distance <= config.max_moneyness_distance)
        & (tenor_distance <= np.log(config.max_tenor_ratio))
    )

    peers = peers[keep].copy()

    if len(peers) < config.min_peers:
        return None

    dm = (
        peers["log_moneyness"] - row["log_moneyness"]
    ).to_numpy()

    dt = np.log(peers["T"].to_numpy() / row["T"])

    liquidity = np.log1p(peers["trade_value"].to_numpy())
    liquidity = liquidity / max(np.median(liquidity), 1e-12)
    liquidity = np.clip(liquidity, 0.25, 4.0)

    spread = peers["spread_pct"].to_numpy() / 100

    weights = (
        liquidity
        * np.exp(-0.5 * (dm / 0.12)**2 - 0.5 * (dt / 0.7)**2)
        / (1 + 10 * spread)
    )

    effective_n = weights.sum()**2 / np.square(weights).sum()

    if effective_n < 2.0:
        return None

    center = weighted_quantile(
        np.log(peers["iv_mid"]), weights, [0.5]
    )[0]

    lower = weighted_quantile(
        np.log(peers["iv_bid"]), weights, [0.2]
    )[0]

    upper = weighted_quantile(
        np.log(peers["iv_ask"]), weights, [0.8]
    )[0]

    lower = min(lower, center - config.min_log_iv_band)
    upper = max(upper, center + config.min_log_iv_band)

    return {
        "iv_model": float(np.exp(center)),
        "iv_low": float(np.exp(lower)),
        "iv_high": float(np.exp(upper)),
        "model_source": "market_peers",
        "peer_count": len(peers),
        "effective_peers": float(effective_n),
    }


def fair_range(kind, s_low, s_mid, s_high, k, t, r, q, iv_low, iv_mid, iv_high, iran_tight=0.10):
    """
    بازه fair از گوشه‌های S×IV؛ سپس با iran_tight فشرده می‌شود
    تا پیشنهادها بیش از حد عریض نباشند (پیش‌فرض کل بازه ≈ ۱۰٪ حول مرکزی).
    """
    corners = [
        bs_price(kind, s, k, t, r, q, sigma)
        for s in (s_low, s_high)
        for sigma in (iv_low, iv_high)
    ]
    central = bs_price(kind, s_mid, k, t, r, q, iv_mid)
    lo = float(min(corners + [central]))
    hi = float(max(corners + [central]))
    mid = float(central)
    tight = float(iran_tight) if iran_tight is not None else 0.10
    tight = max(0.02, min(0.50, tight))
    if mid > 0 and (hi - lo) > mid * tight:
        half = mid * tight / 2.0
        lo = max(0.0, mid - half)
        hi = mid + half
    return lo, mid, hi


def liquidity_urgency_discount(dtm_days, kind, config: Config) -> float:
    """
    ضریب کمبود نقدینگی نزدیک سررسید ∈ [0, 1).

    λ = λ_max × e^(−DTM / τ½) × boost_call

    - DTM→0: نزدیک λ_max (فشار خلاص‌شدن از قرارداد)
    - DTM≫τ½: λ≈0 (اثر ناچیز در افق دور)
    - کال معمولاً boost > 1 می‌گیرد چون فشار بستن موقعیت خرید
      اختیار نزدیک اعمال در بازار ایران رایج‌تر گزارش می‌شود.
    """
    lam_max = float(config.liquidity_discount_max)
    half = float(config.liquidity_half_life_days)
    boost = float(config.liquidity_call_boost)

    if lam_max <= 0 or not finite(dtm_days) or dtm_days < 0:
        return 0.0

    half = max(half, 1e-6)
    decay = float(np.exp(-float(dtm_days) / half))
    factor = boost if kind == "call" else 1.0
    return float(min(0.95, max(0.0, lam_max * decay * factor)))


def run_bs_ir(options, history=None, config=None, asof=None):
    config = config or Config()

    if config.annual_rate <= -1:
        raise ValueError("نرخ مؤثر سالانه باید بزرگ‌تر از منفی ۱۰۰٪ باشد.")

    now = pd.Timestamp.now(tz="UTC") if asof is None else pd.Timestamp(asof)

    if now.tzinfo is None:
        raise ValueError("asof باید timezone داشته باشد.")

    now = now.tz_convert("UTC")

    prepared = prepare(options, config, now)

    if prepared.empty:
        return prepared

    if history is not None and not history.empty:
        hv = hv_table(history, config.trading_days, now)
        prepared = prepared.merge(
            hv, on="underlying_id", how="left", validate="many_to_one"
        )
    else:
        for column in ("hv20", "hv60", "hv120", "hv_ref"):
            prepared[column] = np.nan
        prepared["hv_last_date"] = pd.NaT

    r = np.log1p(config.annual_rate)
    q = config.dividend_yield
    results = []

    for _, row in prepared.iterrows():
        out = row.to_dict()

        out.update({
            "iv_model": np.nan,
            "iv_low": np.nan,
            "iv_high": np.nan,
            "fair_bs_low": np.nan,
            "fair_bs_central": np.nan,
            "fair_bs_high": np.nan,
            "liquidity_discount": np.nan,
            "liquidity_discount_pct": np.nan,
            "fair_low": np.nan,
            "fair_central": np.nan,
            "fair_high": np.nan,
            "buy_gap_net": np.nan,
            "sell_gap_net": np.nan,
            "buy_edge_pct": np.nan,
            "sell_edge_pct": np.nan,
            "peer_count": 0,
            "effective_peers": 0.0,
            "model_source": "insufficient_data",
            "signal": "داده ناکافی",
            "quality": "ناکافی",
        })

        if not row["valid"]:
            out["signal"] = "مشخصات نامعتبر یا سررسید گذشته"
            results.append(out)
            continue

        reference = peer_iv(row, prepared, config)

        if reference is None and positive(row["hv_ref"]):
            reference = {
                "iv_model": row["hv_ref"],
                "iv_low": row["hv_ref"] * np.exp(-config.hv_log_band),
                "iv_high": row["hv_ref"] * np.exp(config.hv_log_band),
                "model_source": "hv_reference",
                "peer_count": 0,
                "effective_peers": 0.0,
            }

        if reference is None:
            results.append(out)
            continue

        out.update(reference)

        lo, central, hi = fair_range(
            row["option_type"],
            row["spot_low"],
            row["spot"],
            row["spot_high"],
            row["strike"],
            row["T"],
            r,
            q,
            out["iv_low"],
            out["iv_model"],
            out["iv_high"],
            iran_tight=float(getattr(config, "iran_tight", 0.10)),
        )

        # ترم دوم: دیسکانت فوریت/کمبود نقدینگی نزدیک سررسید
        # P_adj = P_BS × (1 − λ(τ))
        lam = liquidity_urgency_discount(
            row["dtm"], row["option_type"], config
        )
        scale = 1.0 - lam
        lo_adj = lo * scale
        central_adj = central * scale
        hi_adj = hi * scale

        out.update({
            "fair_bs_low": lo,
            "fair_bs_central": central,
            "fair_bs_high": hi,
            "liquidity_discount": lam,
            "liquidity_discount_pct": lam * 100,
            "fair_low": lo_adj,
            "fair_central": central_adj,
            "fair_high": hi_adj,
        })

        # شکاف نسبت به fair مرکزی (±۲٪) تا سیگنال‌های بیشتری برای call و put تولید شود
        edge_tol = 0.02  # ۲٪ زیر/بالای fair مرکزی
        if positive(row["ask"]) and positive(central_adj):
            out["buy_gap_net"] = central_adj * (1 - edge_tol) - row["ask"] - config.cost_per_unit
            out["buy_edge_pct"] = (
                (central_adj - row["ask"] - config.cost_per_unit)
                / row["ask"] * 100
            )
        if positive(row["bid"]) and positive(central_adj):
            out["sell_gap_net"] = row["bid"] - central_adj * (1 + edge_tol) - config.cost_per_unit
            out["sell_edge_pct"] = (
                (row["bid"] - central_adj - config.cost_per_unit)
                / central_adj * 100
            )

        execution_quality = bool(
            row["fresh"]
            and row["spot_book_ok"]
            and row["book_ok"]
            and row["bounds_ok"]
            and row["dtm"] >= config.min_dtm
            and row["spread_pct"] <= config.max_spread * 100
        )

        # برچسب ارزان/گران برای call و put یکسان است (Ask پایین = ارزان، Bid بالا = گران)
        cheap = finite(out.get("buy_gap_net")) and out["buy_gap_net"] > 0
        rich = finite(out.get("sell_gap_net")) and out["sell_gap_net"] > 0

        if out["model_source"] == "hv_reference":
            out["quality"] = "پایین"
            if cheap and not rich:
                out["signal"] = "Ask زیر محدوده مدل"
            elif rich and not cheap:
                out["signal"] = "Bid بالای محدوده مدل"
            else:
                out["signal"] = "مرجع HV؛ بدون برچسب فرصت"
        else:
            out["quality"] = "متوسط" if execution_quality else "پایین"
            if cheap and (not rich or (out.get("buy_gap_net") or 0) >= (out.get("sell_gap_net") or 0)):
                out["signal"] = "Ask زیر محدوده مدل"
            elif rich:
                out["signal"] = "Bid بالای محدوده مدل"
            elif not execution_quality:
                out["signal"] = "مدل موجود؛ کیفیت اجرا/داده ناکافی"
            else:
                out["signal"] = "برتری اجرایی آشکار نسبت به مدل ندارد"

        results.append(out)

    return pd.DataFrame(results)

# ============================================================
# BS-IR VIEW (embedded)
# ============================================================

TEHRAN = ZoneInfo("Asia/Tehran")

DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

BASE_ALIASES = {
    "جوانهک": "جوانه کوچک",
    "جوانه.ک": "جوانه کوچک",
    "جوانه‌ک": "جوانه کوچک",
    "جوانه کوچک": "جوانه کوچک",
    "جوانهکوچک": "جوانه کوچک",
    "اهرم": "اهرم",
    "طلا": "طلا",
    "عیار": "عیار",
    "کهربا": "کهربا",
    "گندم": "گندم",
}


def clean(value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    text = (
        str(value)
        .translate(DIGITS)
        .replace("ي", "ی")
        .replace("ك", "ک")
        .replace("\u200c", " ")
        .replace("\u200d", "")
        .strip()
    )
    return re.sub(r"\s+", " ", text)


def symbol_key(value: str) -> str:
    return re.sub(r"[\s_]+", "", clean(value))


def num(value) -> float:
    try:
        return float(
            clean(value)
            .replace(",", "")
            .replace("٬", "")
            .replace("٫", ".")
        )
    except (TypeError, ValueError):
        return np.nan


def positive(value) -> bool:
    return np.isfinite(value) and value > 0


def parse_contract(name: str):
    text = clean(name)
    pattern = (
        r"^اختیار\s*(?:خرید|خ|فروش|ف)\s*"
        r"(?P<base>.+?)\s*[-–—]\s*"
        r"(?P<strike>[\d,٬]+)\s*[-–—]\s*"
        r"(?P<expiry>"
        r"(?:\d{4}|\d{2})[/.\-]\d{1,2}[/.\-]\d{1,2}"
        r"|\d{8}|\d{6}"
        r")\s*$"
    )
    match = re.fullmatch(pattern, text)
    if match is None:
        raise ValueError("نام قرارداد قابل تفسیر نیست.")

    strike = num(match.group("strike"))
    if not positive(strike):
        raise ValueError("قیمت اعمال نامعتبر است.")

    expiry_text = match.group("expiry")
    if expiry_text.isdigit():
        year_size = len(expiry_text) - 4
        year = int(expiry_text[:year_size])
        month = int(expiry_text[year_size:year_size + 2])
        day = int(expiry_text[-2:])
        short_year = year_size == 2
    else:
        parts = re.split(r"[/.\-]", expiry_text)
        year, month, day = map(int, parts)
        short_year = len(parts[0]) == 2

    if short_year:
        year += 1400

    if 1300 <= year <= 1599:
        expiry = jdatetime.date(year, month, day).togregorian()
    elif 1900 <= year <= 2200:
        expiry = dt.date(year, month, day)
    else:
        raise ValueError("سال سررسید نامعتبر است.")

    if re.search(r"اختیار\s*(?:فروش|ف(?=\s))", text):
        option_type = "put"
    elif re.search(r"اختیار\s*(?:خرید|خ(?=\s))", text):
        option_type = "call"
    else:
        option_type = "call"

    return match.group("base").strip(), strike, expiry, option_type


def resolve_base_key(name: str) -> str:
    key = symbol_key(name)
    mapped = BASE_ALIASES.get(key)
    return symbol_key(mapped) if mapped else key


def build_options_frame(market: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """
    از snapshot بازار، جدول ورودی موتور BS-IR را می‌سازد.
    زمان مظنه = زمان snapshot (فرض همزمانی کل تابلو).
    """
    data = market.copy()
    data["id"] = data["id"].astype(str)
    data["_name"] = data["name"].map(clean)
    data["_symbol_key"] = data["symbol"].map(symbol_key)

    by_name_call = data["_name"].str.match(
        r"^اختیار\s*(?:خرید|خ(?=\s))", na=False
    )
    by_name_put = data["_name"].str.match(
        r"^اختیار\s*(?:فروش|ف(?=\s))", na=False
    )
    by_type_call = pd.Series(False, index=data.index)
    by_type_put = pd.Series(False, index=data.index)
    if "kind" in data.columns:
        by_type_call |= data["kind"].eq("اختیار خرید")
        by_type_put |= data["kind"].eq("اختیار فروش")

    is_option = by_name_call | by_name_put | by_type_call | by_type_put
    is_call = (by_name_call | by_type_call) & ~by_name_put

    options = data.loc[is_option].drop_duplicates("id").copy()
    bases = data.loc[~is_option].drop_duplicates("id").copy()
    bases["_alias_key"] = bases["_symbol_key"].map(
        lambda k: symbol_key(BASE_ALIASES.get(k, k))
    )

    now = pd.Timestamp.now(tz="UTC")
    rows = []
    rejected = []

    for _, opt in options.iterrows():
        try:
            base_name, strike, expiry, parsed_type = parse_contract(opt["name"])
            target = resolve_base_key(base_name)
            matches = bases.loc[
                bases["_symbol_key"].eq(target)
                | bases["_alias_key"].eq(target)
                | bases["_symbol_key"].eq(symbol_key(base_name))
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"پایه «{base_name}» به یک نماد یکتا نرسید ({len(matches)})."
                )

            base = matches.iloc[0]
            stock_last = num(base.get("last_trade"))
            if not positive(stock_last):
                stock_last = num(base.get("close_price"))
            if not positive(stock_last):
                raise ValueError("آخرین قیمت پایه نامعتبر است.")

            kind = parsed_type
            if "kind" in opt.index:
                if opt["kind"] == "اختیار خرید":
                    kind = "call"
                elif opt["kind"] == "اختیار فروش":
                    kind = "put"

            bid = num(opt.get("best_bid"))
            ask = num(opt.get("best_ask"))
            last = num(opt.get("last_trade"))
            value = num(opt.get("value"))

            # حجم سطح اول از دفتر در market aggregate نیست؛ از مقدار مثبت فرضی
            # برای عبور فیلتر book فقط وقتی bid/ask معتبرند استفاده می‌شود.
            bid_vol = 1.0 if positive(bid) else np.nan
            ask_vol = 1.0 if positive(ask) else np.nan

            rows.append({
                "option_id": str(opt["id"]),
                "symbol": opt["symbol"],
                "name": opt["name"],
                "underlying_id": str(base["id"]),
                "underlying_symbol": base["symbol"],
                "option_type": kind,
                "strike": strike,
                "expiry": pd.Timestamp(expiry, tz="UTC") + pd.Timedelta(hours=12),
                "underlying_last": stock_last,
                "underlying_bid": num(base.get("best_bid")),
                "underlying_ask": num(base.get("best_ask")),
                "underlying_bid_volume": (
                    1.0 if positive(num(base.get("best_bid"))) else np.nan
                ),
                "underlying_ask_volume": (
                    1.0 if positive(num(base.get("best_ask"))) else np.nan
                ),
                "bid": bid,
                "ask": ask,
                "bid_volume": bid_vol,
                "ask_volume": ask_vol,
                "last_trade": last,
                "trade_value": value if positive(value) else np.nan,
                "multiplier": 1.0,
                "quote_time": now,
                "underlying_quote_time": now,
            })
        except Exception as exc:
            rejected.append({
                "نماد": opt.get("symbol"),
                "نام": opt.get("name"),
                "علت": str(exc),
            })

    frame = pd.DataFrame(rows)
    return frame, rejected


def valuation_label(row) -> str:
    signal = str(row.get("signal", ""))
    if "Ask زیر" in signal:
        return "ارزان (Ask زیر مدل)"
    if "Bid بالای" in signal:
        return "گران (Bid بالای مدل)"
    if "برتری اجرایی" in signal:
        return "منصفانه"
    if "HV" in signal:
        return "مرجع HV"
    if "ناکافی" in signal or "نامعتبر" in signal:
        return "داده ناکافی"
    return signal or "—"


def style_valuation(val: str) -> str:
    if "ارزان" in val:
        return "background-color: rgba(16,185,129,0.45); color: #ecfdf5; font-weight: 800;"
    if "گران" in val:
        return "background-color: rgba(239,68,68,0.45); color: #fef2f2; font-weight: 800;"
    if "منصفانه" in val:
        return "background-color: rgba(96,165,250,0.18); color: #93c5fd;"
    if "HV" in val:
        return "background-color: rgba(251,191,36,0.15); color: #fcd34d;"
    return "color: #94a3b8;"


def render_bs_ir(market: pd.DataFrame, key_prefix: str = "bsir"):
    st.markdown("### ◈ ارزش‌گذاری بلک‌شولز ایرانی (BS-IR)")
    st.caption(
        "از اختیارهای نقدشونده (هم‌پایه و هم‌نوع) IV مرجع ساخته می‌شود؛ "
        "سپس برای بقیه قراردادها بازهٔ fair price محاسبه می‌گردد. "
        "این خروجی توصیه خرید/فروش نیست و هزینه معامله را کامل مدل نمی‌کند."
    )

    if market is None or market.empty:
        st.info("داده بازار برای BS-IR موجود نیست.")
        return

    with st.expander("⚙ پارامترهای مدل", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        annual_rate = c1.number_input(
            "نرخ بهره مؤثر سالانه",
            min_value=0.0,
            max_value=2.0,
            value=0.30,
            step=0.01,
            key=f"{key_prefix}_rate",
        )
        min_dtm = c2.number_input(
            "حداقل DTM (روز)",
            min_value=1.0,
            max_value=90.0,
            value=7.0,
            step=1.0,
            key=f"{key_prefix}_mindtm",
        )
        min_peers = int(c3.number_input(
            "حداقل peer برای کالیبراسیون",
            min_value=2,
            max_value=20,
            value=3,
            key=f"{key_prefix}_peers",
        ))
        min_trade_m = c4.number_input(
            "حداقل ارزش معاملات peer · م‌تومان",
            min_value=0.0,
            value=50.0,
            step=10.0,
            key=f"{key_prefix}_minval",
        )

        st.markdown("**ضریب کمبود نقدینگی نزدیک سررسید (ترم دوم)**")
        d1, d2, d3 = st.columns(3)
        liq_max = d1.number_input(
            "λ_max · حداکثر دیسکانت (۰ تا ۱)",
            min_value=0.0,
            max_value=0.5,
            value=0.12,
            step=0.01,
            key=f"{key_prefix}_liq_max",
            help="در سررسید، حداکثر کسر از قیمت BS که به‌عنوان فشار خروج کم می‌شود.",
        )
        liq_half = d2.number_input(
            "نیمه‌عمر دیسکانت · روز",
            min_value=1.0,
            max_value=120.0,
            value=14.0,
            step=1.0,
            key=f"{key_prefix}_liq_half",
            help="هر این تعداد روز دورتر از سررسید، اثر λ تقریباً نصف می‌شود. "
                 "عدد بزرگ‌تر = اثر در افق دورتر هم باقی می‌ماند.",
        )
        liq_call = d3.number_input(
            "تقویت کال (call boost)",
            min_value=1.0,
            max_value=3.0,
            value=1.35,
            step=0.05,
            key=f"{key_prefix}_liq_call",
            help="ضریب اضافه برای اختیار خرید؛ فشار خلاص‌شدن نزدیک اعمال روی کال قوی‌تر فرض می‌شود.",
        )
        st.caption(
            "P_adj = P_BS × (1 − λ) · λ = λ_max × exp(−DTM / τ½) × boost_call. "
            "نرخ ۳۰٪ پیش‌فرض تقریبی بازار پول است. "
            "زمان مظنه‌ها = زمان snapshot (هم‌زمانی تابلو)."
        )
        iran_tight_pct = st.slider(
            "ایرانیزه‌سازی بازه fair (حداکثر عرض ٪ حول مرکزی)",
            min_value=4,
            max_value=40,
            value=10,
            step=1,
            key=f"{key_prefix}_iran_tight",
            help="۱۰٪ یعنی کل بازه Fair حداکثر حدود ۱۰٪ حول قیمت مرکزی فشرده می‌شود.",
        )
        iran_tight = float(iran_tight_pct) / 100.0

    # کش نتیجه موتور: فقط وقتی بازار یا پارامترها عوض شوند دوباره محاسبه
    cfg_tuple = (
        float(annual_rate),
        float(min_dtm),
        int(min_peers),
        float(min_trade_m),
        float(liq_max),
        float(liq_half),
        float(liq_call),
        float(iran_tight),
    )
    try:
        m_sig = (
            int(len(market)),
            int(pd.to_numeric(market.get("value"), errors="coerce").fillna(0).sum()),
            str(market["id"].iloc[0]) if len(market) else "",
            str(market["id"].iloc[-1]) if len(market) else "",
        )
    except Exception:
        m_sig = (id(market),)

    cache_key = ("bsir_v2", key_prefix, cfg_tuple, m_sig)
    cached = st.session_state.get("_bsir_cache")

    if cached and cached.get("key") == cache_key:
        result = cached["result"]
        rejected = cached.get("rejected") or []
    else:
        with st.spinner("ساخت قراردادها و اجرای BS-IR…"):
            frame, rejected = build_options_frame(market)

        if frame.empty:
            st.warning("هیچ قرارداد قابل‌تحلیل برای BS-IR ساخته نشد.")
            if rejected:
                with st.expander(f"ردشده‌ها · {len(rejected)}"):
                    st.dataframe(
                        pd.DataFrame(rejected),
                        hide_index=True,
                        use_container_width=True,
                    )
            return

        min_rial = min_trade_m * 1e7
        # برای کالیبراسیون: peerهای کم‌معامله trade_value پایین دارند
        frame = frame.copy()
        if "trade_value" in frame.columns:
            frame.loc[
                frame["trade_value"].isna() | (frame["trade_value"] < min_rial),
                "trade_value",
            ] = 0.0

        config = Config(
            annual_rate=float(annual_rate),
            min_dtm=float(min_dtm),
            min_peers=min_peers,
            max_age_seconds=3600.0,
            max_spread=0.35,
            max_spot_spread=0.05,
            liquidity_discount_max=float(liq_max),
            liquidity_half_life_days=float(liq_half),
            liquidity_call_boost=float(liq_call),
            iran_tight=float(iran_tight),
        )

        try:
            result = run_bs_ir(frame, history=None, config=config)
        except Exception as exc:
            st.error(f"اجرای موتور BS-IR ناموفق بود: {exc}")
            return

        if result.empty:
            st.info("خروجی موتور خالی است.")
            return

        result = result.copy()
        # برچسب ارزش‌گذاری برداری (سریع‌تر از apply ردیفی)
        buy = result.get("buy_gap_net")
        sell = result.get("sell_gap_net")
        fair = result.get("fair_central")
        val = pd.Series("بدون مدل", index=result.index, dtype=object)
        if fair is not None:
            has_model = fair.notna()
            val = val.where(~has_model, "منصفانه")
        if buy is not None:
            val = val.mask(buy.fillna(0) > 0, "ارزان نسبت به مدل")
        if sell is not None:
            val = val.mask(sell.fillna(0) > 0, "گران نسبت به مدل")
        # اگر هر دو، اولویت با فاصله بزرگ‌تر
        if buy is not None and sell is not None:
            both = (buy.fillna(0) > 0) & (sell.fillna(0) > 0)
            val = val.mask(both & (buy.abs() >= sell.abs()), "ارزان نسبت به مدل")
            val = val.mask(both & (sell.abs() > buy.abs()), "گران نسبت به مدل")
        result["valuation"] = val
        result["mid_display"] = result["mid"]
        result["fair_mid"] = result["fair_central"]
        result["iv_mid_pct"] = result["iv_mid"] * 100
        result["iv_model_pct"] = result["iv_model"] * 100
        result["trade_m"] = result["trade_value"] / 1e7

        st.session_state["_bsir_cache"] = {
            "key": cache_key,
            "result": result,
            "rejected": rejected,
        }

    # خلاصه (روی کل نتیجه کش‌شده)
    n_cal = int(result["calibration_ok"].fillna(False).sum()) if "calibration_ok" in result else 0
    n_cheap = int(result["valuation"].astype(str).str.contains("ارزان", na=False).sum())
    n_rich = int(result["valuation"].astype(str).str.contains("گران", na=False).sum())
    n_fair = int(result["valuation"].astype(str).str.contains("منصفانه", na=False).sum())

    filter_key = f"{key_prefix}_filter"
    if filter_key not in st.session_state:
        st.session_state[filter_key] = "همه"

    a, b, c, d = st.columns(4)
    a.metric("قرارداد تحلیل‌شده", f"{len(result):,}")
    b.metric("کالیبره‌شده (peer)", f"{n_cal:,}")

    # دکمه‌ها فقط state را عوض می‌کنند — بدون st.rerun و بدون محاسبه مجدد موتور
    if c.button(
        f"ارزان · {n_cheap}",
        key=f"{key_prefix}_btn_cheap",
        use_container_width=True,
        type="primary" if st.session_state[filter_key] == "فقط ارزان" else "secondary",
    ):
        st.session_state[filter_key] = "فقط ارزان"
    if d.button(
        f"گران · {n_rich}",
        key=f"{key_prefix}_btn_rich",
        use_container_width=True,
        type="primary" if st.session_state[filter_key] == "فقط گران" else "secondary",
    ):
        st.session_state[filter_key] = "فقط گران"

    st.caption(
        f"منصفانه: {n_fair} · فیلتر فقط روی جدول اعمال می‌شود (موتور دوباره اجرا نمی‌شود). "
        "ارزان = Ask زیر مدل · گران = Bid بالای مدل."
    )

    # فیلتر نمایش
    f1, f2, f3 = st.columns([2, 1.4, 1.2])
    search = f1.text_input(
        "جست‌وجوی نماد اختیار یا پایه",
        key=f"{key_prefix}_search",
        placeholder="مثلاً ضفملی یا فملی",
    )
    only_signal = f2.selectbox(
        "فیلتر ارزش‌گذاری",
        [
            "همه",
            "فقط ارزان",
            "فقط گران",
            "منصفانه",
            "دارای مدل",
        ],
        key=filter_key,
    )
    sort_by = f3.selectbox(
        "مرتب‌سازی",
        [
            "ارزش معاملات",
            "فاصله خرید (buy_gap)",
            "فاصله فروش (sell_gap)",
            "IV مدل",
        ],
        key=f"{key_prefix}_sort",
    )

    view = result.copy()
    if search.strip():
        q = symbol_key(search)
        view = view[
            view["symbol"].map(symbol_key).str.contains(q, regex=False)
            | view["underlying_symbol"].map(symbol_key).str.contains(
                q, regex=False
            )
        ]

    if only_signal == "فقط ارزان":
        view = view[view["valuation"].str.contains("ارزان", na=False)]
    elif only_signal == "فقط گران":
        view = view[view["valuation"].str.contains("گران", na=False)]
    elif only_signal == "منصفانه":
        view = view[view["valuation"].str.contains("منصفانه", na=False)]
    elif only_signal == "دارای مدل":
        view = view[view["fair_central"].notna()]

    sort_map = {
        "ارزش معاملات": ("trade_value", False),
        "فاصله خرید (buy_gap)": ("buy_gap_net", False),
        "فاصله فروش (sell_gap)": ("sell_gap_net", False),
        "IV مدل": ("iv_model", False),
    }
    col, asc = sort_map[sort_by]
    if col in view.columns:
        view = view.sort_values(col, ascending=asc, na_position="last")

    if view.empty:
        st.info("با فیلتر فعلی قراردادی نیست.")
    else:
        display_cols = {
            "symbol": "اختیار",
            "underlying_symbol": "پایه",
            "option_type": "نوع",
            "strike": "اعمال",
            "dtm": "DTM",
            "spot": "پایه · ریال",
            "bid": "Bid",
            "ask": "Ask",
            "mid_display": "Mid",
            "fair_low": "Fair پایین",
            "fair_mid": "Fair مرکزی",
            "fair_high": "Fair بالا",
            "liquidity_discount_pct": "دیسکانت نقدینگی ٪",
            "iv_mid_pct": "IV بازار ٪",
            "iv_model_pct": "IV مدل ٪",
            "buy_gap_net": "شکاف خرید",
            "sell_gap_net": "شکاف فروش",
            "trade_m": "ارزش · م‌تومان",
            "peer_count": "Peer",
            "valuation": "ارزش‌گذاری",
            "quality": "کیفیت",
            "model_source": "منبع مدل",
        }
        present = [c for c in display_cols if c in view.columns]
        table = view[present].rename(columns=display_cols)

        def type_fa(x):
            return {"call": "خرید", "put": "فروش"}.get(x, x)

        if "نوع" in table.columns:
            table["نوع"] = table["نوع"].map(type_fa)

        formats = {
            "اعمال": "{:,.0f}",
            "DTM": "{:.0f}",
            "پایه · ریال": "{:,.0f}",
            "Bid": "{:,.0f}",
            "Ask": "{:,.0f}",
            "Mid": "{:,.1f}",
            "Fair پایین": "{:,.1f}",
            "Fair مرکزی": "{:,.1f}",
            "Fair بالا": "{:,.1f}",
            "دیسکانت نقدینگی ٪": "{:.2f}",
            "IV بازار ٪": "{:.1f}",
            "IV مدل ٪": "{:.1f}",
            "شکاف خرید": "{:+.1f}",
            "شکاف فروش": "{:+.1f}",
            "ارزش · م‌تومان": "{:,.1f}",
            "Peer": "{:.0f}",
        }

        styled = table.style.format(formats, na_rep="—")
        if "ارزش‌گذاری" in table.columns:
            styled = styled.apply(
                lambda s: [style_valuation(str(v)) for v in s],
                subset=["ارزش‌گذاری"],
            )

        st.dataframe(
            styled,
            hide_index=True,
            use_container_width=True,
            height=min(560, 40 + 34 * len(table)),
        )

        st.download_button(
            "↓ دانلود خروجی BS-IR",
            data=view.to_csv(index=False).encode("utf-8-sig"),
            file_name="bs_ir_valuation.csv",
            mime="text/csv",
            key=f"{key_prefix}_dl",
        )

        # —— درون‌روز اختیار + سهم پایه + نرخ کاوردکال ——
        st.markdown("#### معاملات درون‌روز و کاوردکال لحظه‌ای")
        id_col = next(
            (c for c in ("id", "option_id", "inscode") if c in view.columns),
            None,
        )
        und_col = next(
            (c for c in ("underlying_id", "underlying", "base_id") if c in view.columns),
            None,
        )
        sym_col = next((c for c in ("symbol", "name") if c in view.columns), None)
        if id_col and len(view):
            labels = []
            for _, r in view.head(80).iterrows():
                lab = str(r.get(sym_col) or r.get(id_col))
                labels.append(f"{lab} · {r.get(id_col)}")
            pick = st.selectbox(
                "نماد اختیار برای نمودار درون‌روز",
                options=labels,
                key=f"{key_prefix}_intra_pick",
            )
            if pick:
                oid = pick.split("·")[-1].strip()
                row0 = view[view[id_col].astype(str) == oid]
                und_id = None
                strike = np.nan
                if not row0.empty:
                    if und_col:
                        und_id = str(row0.iloc[0][und_col])
                    for sc in ("strike", "اعمال", "K"):
                        if sc in row0.columns:
                            strike = float(pd.to_numeric(row0.iloc[0][sc], errors="coerce"))
                            break
                with st.spinner("دریافت معاملات درون‌روز…"):
                    opt_tr = fetch_intraday_trades(oid, max_days=1)
                    und_tr = pd.DataFrame()
                    if und_id and und_id not in ("None", "nan", ""):
                        und_tr = fetch_intraday_trades(und_id, max_days=1)
                plot_intraday_combined(
                    opt_tr,
                    und_tr,
                    strike if np.isfinite(strike) else None,
                    f"درون‌روز · {pick.split('·')[0].strip()} + پایه + کاوردکال",
                    f"{key_prefix}_combo_intra",
                )
        else:
            st.caption("ستون شناسه قرارداد برای نمودار درون‌روز در دسترس نیست.")

    with st.expander("روش کار و محدودیت‌ها"):
        st.markdown(
            """
- **کالیبراسیون:** فقط قراردادهای تازه، با دفتر دوطرفه معتبر، اسپرد محدود،
  DTM کافی و ارزش معاملات مثبت وارد ساخت IV مرجع می‌شوند.
- **Peer:** همان دارایی پایه و همان نوع اختیار، نزدیک از نظر moneyness و سررسید.
- **ترم دوم — کمبود نقدینگی:**  
  `P_adj = P_BS × (1 − λ)`  
  `λ = λ_max × exp(−DTM / τ½) × boost_call`  
  نزدیک سررسید λ بزرگ می‌شود (فشار خروج از قرارداد، به‌ویژه کال).  
  با بزرگ‌کردن τ½ می‌توان اثر را در روزهای دورتر هم نگه داشت.
- **ارزان / گران:** نسبت به بازهٔ **تعدیل‌شده** با λ سنجیده می‌شود.
- **ایرانیزه‌سازی:** عرض بازه fair حداکثر حدود `iran_tight` (پیش‌فرض ۱۰٪) حول قیمت مرکزی فشرده می‌شود.
- بدون peer کافی، مدل به HV نمی‌رود مگر تاریخچه قیمت وصل باشد.
- مشخصات قرارداد از **نام** استخراج می‌شود.
- اندازه قرارداد / ضریب رسمی در این نسخه ۱ فرض شده است.
            """
        )

    if rejected:
        with st.expander(f"قراردادهای کنارگذاشته‌شده · {len(rejected)}"):
            st.dataframe(
                pd.DataFrame(rejected),
                hide_index=True,
                use_container_width=True,
            )

# ============================================================
# MARKET / ORDER BOOK
# ============================================================

def parse_market_response(text):
    parts = text.split("@")

    if len(parts) < 4:
        raise ValueError("ساختار پاسخ، snapshot کامل MarketWatch نیست.")

    raw_rows = [
        row.split(",")
        for row in parts[2].split(";")
        if row.strip()
    ]

    rows = [
        row[:len(MARKET_COLUMNS)]
        for row in raw_rows
        if len(row) >= len(MARKET_COLUMNS)
        and row[0].strip().isdigit()
    ]

    if not rows:
        raise ValueError("رکورد کامل بازار در پاسخ موجود نیست.")

    market = pd.DataFrame(rows, columns=MARKET_COLUMNS)

    text_columns = {
        "id", "isin", "symbol", "name", "time",
        "table_id", "industry_id", "section_code", "asset_code",
    }

    for col in text_columns:
        market[col] = market[col].astype(str).str.strip()

    for col in set(MARKET_COLUMNS) - text_columns:
        market[col] = numeric_series(market[col])

    market["symbol"] = market["symbol"].map(normalize_text)
    market["name"] = market["name"].map(normalize_text)

    market = market.drop_duplicates("id", keep="last").copy()

    # Negative prices, sizes and values are not market observations.
    for col in [
        "close_price", "last_trade", "yesterday_price",
        "volume", "value", "number_trades",
    ]:
        market.loc[market[col] < 0, col] = np.nan
        market["kind"] = "سایر"

    market.loc[
        market["asset_code"].isin(STOCK_CODES),
        "kind",
    ] = "سهام"

    # صندوق‌ها نباید با اختیارها اشتباه شوند.
    market.loc[
        market["asset_code"].isin({"305", "380"}),
        "kind",
    ] = "صندوق"

    # کد رسمی نوع ابزار، معیار اولیه است.
    market.loc[
        market["asset_code"].isin(CALL_CODES),
        "kind",
    ] = "اختیار خرید"

    market.loc[
        market["asset_code"].isin(PUT_CODES),
        "kind",
    ] = "اختیار فروش"

    # نام کامل و اختصارات رایج منبع:
    # اختیار خرید / اختیارخ
    # اختیار فروش / اختیارف
    instrument_names = market["name"].map(normalize_text)

    name_is_call = instrument_names.str.contains(
        r"اختیار\s*(?:خرید|خ(?=\s))",
        regex=True,
        na=False,
    )

    name_is_put = instrument_names.str.contains(
        r"اختیار\s*(?:فروش|ف(?=\s))",
        regex=True,
        na=False,
    )

    market.loc[
        name_is_call & ~name_is_put,
        "kind",
    ] = "اختیار خرید"

    market.loc[
        name_is_put & ~name_is_call,
        "kind",
    ] = "اختیار فروش"

    # نام متناقض را وارد تحلیل اختیار نمی‌کنیم.
    ambiguous_name = name_is_call & name_is_put

    market.loc[
        ambiguous_name,
        "kind",
    ] = "نامشخص"

    # عمداً هیچ قانونی بر اساس حرف اول نماد وجود ندارد.

    market["return_pct"] = (
        market["close_price"].where(market["close_price"] > 0)
        / market["yesterday_price"].where(market["yesterday_price"] > 0)
        - 1
    ) * 100

    raw_book = [
        row.split(",")
        for row in parts[3].split(";")
        if row.strip()
    ]

    valid_book = [row for row in raw_book if len(row) == len(BOOK_COLUMNS)]
    book = pd.DataFrame(valid_book, columns=BOOK_COLUMNS)

    book["id"] = book["id"].astype(str).str.strip()

    for col in BOOK_COLUMNS[1:]:
        book[col] = numeric_series(book[col])

    book = book[
        book["id"].isin(market["id"])
        & book["level"].gt(0)
    ].drop_duplicates(["id", "level"], keep="last").copy()

    for side in ["bid", "ask"]:
        price = book[f"{side}_price"]
        volume = book[f"{side}_vol"]

        # Zero volume is an observed empty level, not missing data.
        known = (
            price.notna()
            & volume.notna()
            & price.ge(0)
            & volume.ge(0)
            & ~(volume.gt(0) & price.le(0))
        )

        active = known & price.gt(0) & volume.gt(0)

        book[f"{side}_known"] = known
        book[f"{side}_value"] = (price * volume).where(known)
        book[f"{side}_valid_price"] = price.where(active)

    if not book.empty:
        groups = book.groupby("id")

        aggregate = groups.agg(
            best_bid=("bid_valid_price", "max"),
            best_ask=("ask_valid_price", "min"),
            bid_complete=("bid_known", "all"),
            ask_complete=("ask_known", "all"),
            visible_levels=("level", "count"),
        )

        for side in ["bid", "ask"]:
            aggregate[f"{side}_value"] = (
                groups[f"{side}_value"]
                .sum(min_count=1)
                .where(aggregate[f"{side}_complete"])
            )

        aggregate["book_complete"] = (
            aggregate["bid_complete"] & aggregate["ask_complete"]
        )

        market = market.join(aggregate, on="id")

    else:
        for col in [
            "best_bid", "best_ask", "bid_value", "ask_value",
            "visible_levels",
        ]:
            market[col] = np.nan

        market["book_complete"] = False

    market["book_complete"] = market["book_complete"].eq(True)

    market["crossed_book"] = (
        market["best_bid"].notna()
        & market["best_ask"].notna()
        & market["best_bid"].gt(market["best_ask"])
    )

    depth = market["bid_value"] + market["ask_value"]

    valid_pressure = (
        market["book_complete"]
        & depth.gt(0)
        & ~market["crossed_book"]
    )

    market["pressure"] = (
        100 * (market["bid_value"] - market["ask_value"]) / depth
    ).where(valid_pressure)

    midpoint = (market["best_bid"] + market["best_ask"]) / 2

    valid_spread = (
        market["best_bid"].gt(0)
        & market["best_ask"].ge(market["best_bid"])
    )

    market["spread_pct"] = (
        100 * (market["best_ask"] - market["best_bid"]) / midpoint
    ).where(valid_spread)

    diagnostics = {
        "market_rows": len(market),
        "book_rows": len(book),
        "rejected_market_rows": len(raw_rows) - len(rows),
        "malformed_book_rows": len(raw_book) - len(valid_book),
        "crossed_books": int(market["crossed_book"].sum()),
    }

    return market.reset_index(drop=True), book, diagnostics


@st.cache_data(ttl=45, show_spinner=False)
def fetch_market():
    urls = [
        "https://old.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0",
        "https://www.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0",
    ]

    errors = []

    for url in urls:
        try:
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=(5, 15),
            )
            response.raise_for_status()

            market, book, diagnostics = parse_market_response(
                response.content.decode("utf-8-sig", errors="replace")
            )

            return {
                "market": market,
                "book": book,
                "diagnostics": diagnostics,
                "received_at": now_tehran().isoformat(),
                "source": url,
            }

        except Exception as exc:
            errors.append(str(exc))

    raise RuntimeError(" | ".join(errors))


def pressure_summary(frame):
    if frame is None or frame.empty or "pressure" not in frame.columns:
        return frame.iloc[0:0].copy() if frame is not None else pd.DataFrame(), np.nan, np.nan, np.nan, 0.0
    eligible = frame[frame["pressure"].notna()].copy()
    bid = total(eligible["bid_value"]) if "bid_value" in eligible.columns else np.nan
    ask = total(eligible["ask_value"]) if "ask_value" in eligible.columns else np.nan
    value = 100 * safe_ratio(bid - ask, bid + ask)
    coverage = 100 * safe_ratio(len(eligible), len(frame))
    return eligible, bid, ask, value, coverage


# ============================================================
# HISTORICAL STOCK DATA
# ============================================================

@st.cache_data(ttl=86400, show_spinner=False, max_entries=256)
def fetch_stock_history(inscode):
    """
    تاریخچه تعدیل‌شده سهم از Financial.aspx با a=1.

    ترتیب ستون‌های پاسخ:
    date, h, l, o, last, vol, close_price

    خروجی سازگار با render_stock_detail و adjust_currency:
    date, open, high, low, last, close, volume
    """

    stock_id = str(inscode).strip()

    if not re.fullmatch(r"\d+", stock_id):
        raise ValueError("شناسه سهم معتبر نیست.")

    url = (
        "https://members.tsetmc.com/tsev2/chart/data/Financial.aspx"
        f"?i={stock_id}&t=ph&a=1"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/114.0.0.0 Safari/537.36"
        )
    }

    r = requests.get(
        url,
        headers=headers,
        timeout=(5, 30),
    )
    r.raise_for_status()

    adjusted_close = r.content.decode("utf-8-sig").strip()

    if not adjusted_close:
        raise ValueError("پاسخ تاریخچه تعدیل‌شده خالی است.")

    if (
        "<html" in adjusted_close.lower()
        or "<!doctype" in adjusted_close.lower()
    ):
        raise ValueError(
            "منبع به‌جای تاریخچه سهم، صفحه HTML برگردانده است."
        )

    # حذف رکورد خالی احتمالی بعد از آخرین ;
    raw = [
        record.strip()
        for record in adjusted_close.split(";")
        if record.strip()
    ]

    cols = [
        "date",
        "h",
        "l",
        "o",
        "last",
        "vol",
        "close_price",
    ]

    rows = [
        [field.strip() for field in record.split(",")]
        for record in raw
    ]

    if not rows:
        raise ValueError("هیچ رکورد تاریخی دریافت نشد.")

    if any(len(row) != len(cols) for row in rows):
        raise ValueError(
            "ساختار پاسخ با قالب هفت‌ستونی مورد انتظار سازگار نیست."
        )

    df_stock = pd.DataFrame(rows, columns=cols)

    # تاریخ‌های پاسخ، مطابق روش ارسالی شما
    df_stock["date"] = pd.to_datetime(
        df_stock["date"].str.translate(DIGITS),
        errors="coerce",
    )

    if df_stock["date"].isna().any():
        raise ValueError(
            "بخشی از تاریخ‌های دریافتی قابل تبدیل نیست."
        )

    df_stock["date"] = df_stock["date"].dt.normalize()

    for column in ["h", "l", "o", "last", "vol", "close_price"]:
        df_stock[column] = numeric_series(df_stock[column])

    # تغییر نام‌ها صرفاً برای سازگاری با داشبورد فعلی است.
    history = df_stock.rename(
        columns={
            "h": "high",
            "l": "low",
            "o": "open",
            "vol": "volume",
            "close_price": "close",
        }
    )

    for column in ["open", "high", "low", "last", "close"]:
        history.loc[
            history[column].le(0),
            column,
        ] = np.nan

    history.loc[
        history["volume"].lt(0),
        "volume",
    ] = np.nan

    # روز جاری ممکن است هنوز کندل تکمیل‌شده نداشته باشد.
    today = pd.Timestamp(now_tehran().date())

    history = history.loc[
        history["date"].lt(today)
        & history["close"].notna()
    ].copy()

    # مرتب‌سازی ضروری برای تطبیق زمانی با نرخ ارز
    history = (
        history
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )

    if history.empty:
        raise ValueError(
            "تاریخچه تعدیل‌شده معتبر و تکمیل‌شده موجود نیست."
        )

    # کل تاریخچه دریافت و کش می‌شود.
    # بازه پنج‌ساله در render_stock_detail فیلتر می‌شود.
    return history[
        [
            "date",
            "open",
            "high",
            "low",
            "last",
            "close",
            "volume",
        ]
    ]
# ============================================================
# REAL FX DATA: USDT/TOMAN
# ============================================================

@st.cache_data(show_spinner=False, max_entries=512)
def fetch_fx_chunk(start_ts: int, end_ts: int):
    """
    Cache successful fixed historical intervals without a TTL.

    Exceptions are not converted into empty successful results.
    Therefore a failed request can be retried later.
    """
    response = requests.get(
        "https://api.wallex.ir/v1/udf/history",
        headers=HEADERS,
        params={
            "symbol": "USDTTMN",
            "resolution": "1D",
            "from": int(start_ts),
            "to": int(end_ts),
        },
        timeout=(5, 20),
    )
    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("ساختار پاسخ والکس معتبر نیست.")

    data = payload.get("result", payload)

    if not isinstance(data, dict):
        raise ValueError("بدنه تاریخچه ارز معتبر نیست.")

    if data.get("s") == "no_data":
        return []

    timestamps = data.get("t")
    closes = data.get("c")

    if (
        not isinstance(timestamps, list)
        or not isinstance(closes, list)
        or len(timestamps) != len(closes)
    ):
        raise ValueError("آرایه‌های تاریخ و نرخ معتبر نیستند.")

    return list(zip(timestamps, closes))


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fx_history():
    """
    Historical chunks are reused from memory.

    Only a new/extended recent interval needs a new request
    when another completed daily candle becomes available.
    """
    now = pd.Timestamp.now(tz="UTC")
    today_utc = now.normalize()

    requested_start = (
        today_utc
        - pd.DateOffset(years=HISTORY_YEARS)
        - pd.Timedelta(days=MAX_FX_AGE_DAYS + 3)
    )

    chunk_seconds = FX_CHUNK_DAYS * 86400

    # Align historical chunks to fixed boundaries so they do not
    # all shift and get downloaded again each day.
    cursor = (
        int(requested_start.timestamp()) // chunk_seconds
    ) * chunk_seconds

    # Exclude the currently open UTC day.
    end_ts = int(today_utc.timestamp()) - 1

    rows = []
    failed_chunks = 0

    while cursor <= end_ts:
        stop = min(
            cursor + chunk_seconds - 1,
            end_ts,
        )

        try:
            rows.extend(fetch_fx_chunk(cursor, stop))
        except Exception:
            failed_chunks += 1

        cursor = stop + 1

    if not rows:
        raise RuntimeError(
            "هیچ تاریخچه معتبر USDTTMN دریافت نشد."
        )

    fx = pd.DataFrame(
        rows,
        columns=["timestamp", "fx_toman"],
    )

    fx["timestamp"] = numeric_series(fx["timestamp"])
    fx["fx_toman"] = numeric_series(fx["fx_toman"])

    fx["candle_start"] = pd.to_datetime(
        fx["timestamp"],
        unit="s",
        utc=True,
        errors="coerce",
    )

    fx["available_at"] = (
        fx["candle_start"] + pd.Timedelta(days=1)
    )

    fx = fx[
        fx["fx_toman"].gt(0)
        & fx["candle_start"].notna()
        & fx["candle_start"].ge(requested_start)
        & fx["available_at"].le(now)
    ].copy()

    fx["fx_rial"] = fx["fx_toman"] * 10

    fx = (
        fx.sort_values("available_at")
        .drop_duplicates("available_at", keep="last")
        .reset_index(drop=True)
    )

    if fx.empty:
        raise RuntimeError(
            "کندل تکمیل‌شده و معتبر ارز موجود نیست."
        )

    return {
        "data": fx,
        "failed_chunks": failed_chunks,
    }

def adjust_currency(history, fx):
    stock = history.sort_values("date").copy()

    # Use FX already available at the START of the Tehran stock day.
    # This avoids using a later crypto close from the same date.
    stock["stock_cutoff"] = (
        stock["date"]
        .dt.tz_localize(TEHRAN)
        .dt.tz_convert("UTC")
    )

    result = pd.merge_asof(
        stock.sort_values("stock_cutoff"),
        fx[
            ["candle_start", "available_at", "fx_toman", "fx_rial"]
        ].sort_values("available_at"),
        left_on="stock_cutoff",
        right_on="available_at",
        direction="backward",
        tolerance=pd.Timedelta(days=MAX_FX_AGE_DAYS),
    )

    result["close_usdt"] = result["close"] / result["fx_rial"]

    result["fx_age_days"] = (
        result["stock_cutoff"] - result["available_at"]
    ).dt.total_seconds() / 86400

    return result


@st.cache_data(show_spinner=False, ttl=2_592_000)
def fetch_cpi_history():
    """
    شاخص CPI سالانه ایران از World Bank (FP.CPI.TOTL، پایه ۲۰۱۰=۱۰۰).
    یک‌بار گرفته و در کش Streamlit نگه داشته می‌شود (بدون انقضای TTL).
    """
    response = requests.get(
        "https://api.worldbank.org/v2/country/IRN/indicator/FP.CPI.TOTL",
        params={
            "format": "json",
            "per_page": 80,
            "date": "2000:2030",
        },
        headers=HEADERS,
        timeout=(5, 20),
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError("پاسخ CPI بانک جهانی معتبر نیست.")

    rows = []
    for item in payload[1] or []:
        if not isinstance(item, dict):
            continue
        year = item.get("date")
        value = item.get("value")
        if year is None or value is None:
            continue
        try:
            y = int(year)
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        # شاخص سال Y از ابتدای سال بعد قابل اتکا فرض می‌شود
        available = pd.Timestamp(year=y + 1, month=1, day=1, tz="UTC")
        rows.append({
            "cpi_year": y,
            "cpi": v,
            "available_at": available,
        })

    if not rows:
        raise RuntimeError("هیچ مشاهده CPI معتبری دریافت نشد.")

    cpi = (
        pd.DataFrame(rows)
        .sort_values("available_at")
        .drop_duplicates("available_at", keep="last")
        .reset_index(drop=True)
    )
    return cpi



@st.cache_data(show_spinner=False, ttl=2_592_000)
def fetch_us_cpi_history():
    """CPI سالانه آمریکا (World Bank FP.CPI.TOTL) — یک‌بار کش می‌شود."""
    response = requests.get(
        "https://api.worldbank.org/v2/country/USA/indicator/FP.CPI.TOTL",
        params={"format": "json", "per_page": 80, "date": "2000:2030"},
        headers=HEADERS,
        timeout=(5, 20),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError("پاسخ CPI آمریکا معتبر نیست.")
    rows = []
    for item in payload[1] or []:
        if not isinstance(item, dict):
            continue
        year, value = item.get("date"), item.get("value")
        if year is None or value is None:
            continue
        try:
            y, v = int(year), float(value)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        available = pd.Timestamp(year=y + 1, month=1, day=1, tz="UTC")
        rows.append({"cpi_year": y, "cpi_us": v, "available_at": available})
    if not rows:
        raise RuntimeError("هیچ مشاهده CPI آمریکا دریافت نشد.")
    return (
        pd.DataFrame(rows)
        .sort_values("available_at")
        .drop_duplicates("available_at", keep="last")
        .reset_index(drop=True)
    )


def load_us_cpi_memo():
    if "_us_cpi_memo" in st.session_state:
        return st.session_state["_us_cpi_memo"], st.session_state.get("_us_cpi_memo_err")
    try:
        data = fetch_us_cpi_history()
        st.session_state["_us_cpi_memo"] = data
        st.session_state["_us_cpi_memo_err"] = None
        return data, None
    except Exception as exc:
        st.session_state["_us_cpi_memo"] = pd.DataFrame()
        st.session_state["_us_cpi_memo_err"] = str(exc)
        return st.session_state["_us_cpi_memo"], str(exc)



TEDPIX_CODE = "32097828799138957"


def load_tedpix_memo(index_code: str = TEDPIX_CODE):
    """یک‌بار در هر نشست؛ تاریخچه شاخص کل (TEDPIX)."""
    key = f"_tedpix_memo_{index_code}"
    if key in st.session_state and st.session_state[key] is not None:
        return st.session_state[key], None
    try:
        df = _fetch_index_history_raw(index_code)
        st.session_state[key] = df
        return df, None
    except Exception as exc:
        st.session_state[key] = None
        return None, str(exc)


@st.cache_data(show_spinner=False, ttl=6 * 3600, max_entries=4)
def _fetch_index_history_raw(index_code: str) -> pd.DataFrame:
    """
    تاریخچه شاخص از CDN تی‌اس‌ای‌تی‌ام‌سی.
    ستون‌های رایج: dEven، xNivInuClMresIbs (پایانی)
    """
    url = f"https://cdn.tsetmc.com/api/Index/GetIndexB2History/{index_code}"
    r = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        },
        timeout=45,
    )
    r.raise_for_status()
    raw = r.json().get("indexB2") or r.json().get("indexB2History") or []
    if not raw and isinstance(r.json(), list):
        raw = r.json()
    df = pd.DataFrame(raw)
    if df.empty:
        return pd.DataFrame(columns=["date", "close"])

    # تاریخ
    date_col = next(
        (c for c in ("dEven", "date", "DEven", "evenDate") if c in df.columns),
        None,
    )
    if date_col is None:
        raise ValueError(f"ستون تاریخ شاخص یافت نشد: {list(df.columns)}")

    # پایانی شاخص
    close_col = next(
        (
            c
            for c in (
                "xNivInuClMresIbs",
                "xNivInuClMresIbs ",
                "close",
                "pClosing",
                "XNivInuClMresIbs",
                "indexValue",
                "value",
            )
            if c in df.columns
        ),
        None,
    )
    if close_col is None:
        # عددی‌ترین ستون به‌جز تاریخ
        nums = [
            c
            for c in df.columns
            if c != date_col and pd.api.types.is_numeric_dtype(df[c])
        ]
        if not nums:
            raise ValueError(f"ستون پایانی شاخص یافت نشد: {list(df.columns)}")
        close_col = nums[-1]

    out = pd.DataFrame()
    # dEven مثل 20240922
    dates = df[date_col]
    if pd.api.types.is_numeric_dtype(dates) or dates.astype(str).str.fullmatch(r"\d{8}").all():
        out["date"] = pd.to_datetime(dates.astype(int).astype(str), format="%Y%m%d", errors="coerce")
    else:
        out["date"] = pd.to_datetime(dates, errors="coerce")
    out["close"] = pd.to_numeric(df[close_col], errors="coerce")
    out = out.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date")
    out = out.reset_index(drop=True)
    return out





def parse_trade_overview_xml(raw_text: str, date=None) -> pd.DataFrame:
    """
    پارس خروجی TradeOverview.aspx (XML با row/cell).
    هر ردیف: start_time, end_time, volume, price — بازه‌های تجمیعی درون‌روز.
    """
    import html as _html
    if not raw_text:
        return pd.DataFrame()
    raw_text = (
        raw_text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\r\n", "\n")
    )
    raw_text = _html.unescape(raw_text)
    row_blocks = re.findall(
        r"<row>\s*(.*?)\s*</row>",
        raw_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    records = []
    for block in row_blocks:
        cells = re.findall(
            r"<cell>\s*(.*?)\s*</cell>",
            block,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if len(cells) != 4:
            continue
        start_time, end_time, volume, price = [x.strip() for x in cells]
        try:
            records.append({
                "start_time": start_time,
                "end_time": end_time,
                "vol": int(float(volume.replace(",", ""))),
                "price": float(price.replace(",", "")),
            })
        except ValueError:
            continue
    df = pd.DataFrame(records)
    if df.empty:
        return df
    df["start_td"] = pd.to_timedelta(df["start_time"], errors="coerce")
    df["end_td"] = pd.to_timedelta(df["end_time"], errors="coerce")
    df["duration_sec"] = (df["end_td"] - df["start_td"]).dt.total_seconds()
    df["value"] = df["vol"] * df["price"]
    day = pd.Timestamp(date).normalize() if date is not None else pd.Timestamp(now_tehran().date())
    df["datetime"] = day + df["start_td"]
    df["end_datetime"] = day + df["end_td"]
    df = df.dropna(subset=["datetime", "price"]).sort_values("datetime").reset_index(drop=True)
    return df


def fetch_intraday_trades(symbol_id, dates=None, max_days=1):
    """
    معاملات / بازه‌های درون‌روز.
    ۱) اولویت: old.tsetmc TradeOverview.aspx (پایدارتر)
    ۲) پشتیبان: cdn GetTradeHistory JSON
    خروجی یکدست: datetime, price, vol, symbol_id
    """
    import time as _time
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }
    if dates is None:
        today = now_tehran().strftime("%Y%m%d")
        dates = [today]
    dates = list(dates)[: max(1, int(max_days))]
    frames = []
    sid = str(symbol_id).strip()

    # --- منبع ۱: TradeOverview ---
    try:
        url = f"https://old.tsetmc.com/tsev2/data/TradeOverview.aspx?i={sid}"
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        raw_txt = r.content.decode("utf-8", errors="replace")
        day = dates[0]
        day_ts = pd.Timestamp(str(day) if len(str(day)) == 8 else now_tehran().date())
        df = parse_trade_overview_xml(raw_txt, date=day_ts)
        if not df.empty:
            df = df.rename(columns={"vol": "vol"})
            out = df[["datetime", "price", "vol"]].copy()
            out["symbol_id"] = sid
            frames.append(out)
    except Exception:
        pass

    # --- منبع ۲: GetTradeHistory (در صورت خالی بودن) ---
    if not frames:
        for date in dates:
            try:
                url = f"https://cdn.tsetmc.com/api/Trade/GetTradeHistory/{sid}/{date}/true"
                r = requests.get(url, headers=headers, timeout=12)
                r.raise_for_status()
                raw = r.json().get("tradeHistory") or []
                if not raw:
                    continue
                df = pd.DataFrame(raw)
                cols = {
                    "nTran": "num",
                    "hEven": "time_raw",
                    "qTitTran": "vol",
                    "pTran": "price",
                }
                use = [c for c in cols if c in df.columns]
                if not use:
                    continue
                df = df[use].rename(columns={c: cols[c] for c in use})
                df["price"] = pd.to_numeric(df["price"], errors="coerce")
                df["vol"] = pd.to_numeric(df.get("vol", 0), errors="coerce")

                def _parse_t(x):
                    try:
                        s = f"{int(x):06d}"
                        return dt.time(int(s[:2]), int(s[2:4]), int(s[4:6]))
                    except Exception:
                        return None

                df["time"] = df["time_raw"].map(_parse_t)
                day = dt.datetime.strptime(str(date), "%Y%m%d").date()
                df["datetime"] = [
                    dt.datetime.combine(day, t) if t is not None else pd.NaT
                    for t in df["time"]
                ]
                df = df.dropna(subset=["datetime", "price"])
                df["symbol_id"] = sid
                frames.append(df[["datetime", "price", "vol", "symbol_id"]])
                _time.sleep(0.35)
            except Exception:
                continue

    if not frames:
        return pd.DataFrame(columns=["datetime", "price", "vol", "symbol_id"])
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values("datetime")
        .drop_duplicates(subset=["datetime", "price"], keep="last")
        .reset_index(drop=True)
    )


def _session_end_today(day=None):
    """پایان جلسه معاملات تهران ۱۲:۳۰."""
    day = pd.Timestamp(day or now_tehran().date()).normalize()
    return day + pd.Timedelta(hours=12, minutes=30)


def forward_fill_intraday(
    df,
    min_price=None,
    max_price=None,
    session_end=None,
    freq="1min",
):
    """
    اگر معامله‌ای نباشد، آخرین قیمت تا ۱۲:۳۰ ادامه داده می‌شود.
    نزدیک کف مجاز → صف فروش؛ نزدیک سقف → صف خرید.
    """
    empty = pd.DataFrame(columns=["datetime", "price", "vol", "is_fill", "queue"])
    if df is None or df.empty or "datetime" not in df.columns:
        return empty

    d = df.dropna(subset=["datetime", "price"]).copy()
    d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
    d = d.dropna(subset=["datetime"]).sort_values("datetime")
    # برچسب‌های تکراری محور را حذف کن (علت ValueError reindex)
    d = d.groupby("datetime", as_index=False).agg(
        price=("price", "last"),
        vol=("vol", "sum") if "vol" in d.columns else ("price", "size"),
    )
    if d.empty:
        return empty

    day0 = d["datetime"].iloc[0].normalize()
    end = session_end or _session_end_today(day0)
    try:
        now = pd.Timestamp(now_tehran().replace(tzinfo=None))
    except Exception:
        now = pd.Timestamp.utcnow()
    end_eff = min(pd.Timestamp(end), max(now, d["datetime"].iloc[-1]))
    if end_eff <= d["datetime"].iloc[0]:
        end_eff = d["datetime"].iloc[-1]

    idx = pd.date_range(d["datetime"].iloc[0], end_eff, freq=freq)
    base = (
        d.drop_duplicates(subset=["datetime"], keep="last")
        .set_index("datetime")[["price", "vol"]]
        .sort_index()
    )
    # اگر هنوز تکراری ماند
    base = base[~base.index.duplicated(keep="last")]
    grid = base.reindex(idx)
    grid["is_fill"] = grid["price"].isna()
    grid["price"] = grid["price"].ffill()
    grid["vol"] = grid["vol"].fillna(0)
    grid = grid.dropna(subset=["price"]).reset_index().rename(columns={"index": "datetime"})

    last_px = float(grid["price"].iloc[-1])
    queue = None
    try:
        if min_price is not None and float(min_price) > 0 and last_px <= float(min_price) * 1.002:
            queue = "sell"
        elif max_price is not None and float(max_price) > 0 and last_px >= float(max_price) * 0.998:
            queue = "buy"
    except (TypeError, ValueError):
        queue = None
    grid["queue"] = queue
    return grid


def plot_intraday_series(
    df,
    title,
    key,
    color="#60A5FA",
    min_price=None,
    max_price=None,
):
    if df is None or df.empty:
        st.info("معامله درون‌روزی برای این نماد یافت نشد.")
        return
    filled = forward_fill_intraday(df, min_price=min_price, max_price=max_price)
    if filled.empty:
        st.info("پس از پر کردن بازه‌ها داده معتبری نماند.")
        return

    fig = go.Figure()
    real = filled.loc[~filled["is_fill"].fillna(False)]
    fill = filled.loc[filled["is_fill"].fillna(False)]
    # خط کامل: داده واقعی
    if not real.empty:
        fig.add_trace(
            go.Scatter(
                x=real["datetime"],
                y=real["price"],
                mode="lines",
                name=f"{title} · معامله",
                line=dict(color=color, width=2, dash="solid"),
            )
        )
    # خط‌چین: پرشده از آخرین قیمت
    if not fill.empty:
        # برای پیوستگی بصری: آخرین نقطه واقعی + fill
        bridge = filled.copy()
        fig.add_trace(
            go.Scatter(
                x=bridge["datetime"],
                y=bridge["price"].where(bridge["is_fill"] | (~bridge["is_fill"].shift(-1, fill_value=False))),
                mode="lines",
                name="بدون معامله (ffill)",
                line=dict(color=color, width=1.6, dash="dash"),
                connectgaps=False,
            )
        )
        # ساده‌تر: کل مسیر ffill به‌صورت خط‌چین نیمه‌شفاف زیر واقعی
        fig.add_trace(
            go.Scatter(
                x=filled["datetime"],
                y=filled["price"],
                mode="lines",
                name="مسیر با ادامه قیمت",
                line=dict(color=color, width=1.2, dash="dot"),
                opacity=0.45,
            )
        )

    q = filled["queue"].iloc[-1] if "queue" in filled.columns else None
    if q == "sell":
        fig.add_hline(
            y=float(filled["price"].iloc[-1]),
            line_dash="dot",
            line_color="#EF4444",
            annotation_text="احتمال صف فروش / کف مجاز — معامله کم یا متوقف",
            annotation_position="bottom right",
        )
        st.warning(
            "آخرین قیمت نزدیک **کف مجاز** است و بازه‌های بدون معامله با همان قیمت "
            "تا پایان جلسه (یا الان) ادامه داده شده‌اند → عملاً صف فروش / قفل منفی."
        )
    elif q == "buy":
        fig.add_hline(
            y=float(filled["price"].iloc[-1]),
            line_dash="dot",
            line_color="#10B981",
            annotation_text="احتمال صف خرید / سقف مجاز — معامله کم یا متوقف",
            annotation_position="top right",
        )
        st.success(
            "آخرین قیمت نزدیک **سقف مجاز** است؛ نبود معامله با ادامهٔ همان قیمت "
            "نشان‌دهندهٔ صف خرید / قفل مثبت است."
        )
    elif fill["is_fill"].any() if "is_fill" in fill.columns else filled["is_fill"].any():
        st.caption(
            "بازه‌های بدون معامله تا ۱۲:۳۰ (یا لحظهٔ فعلی) با **آخرین قیمت معامله‌شده** پر شده‌اند."
        )

    fig.update_layout(
        title=title,
        height=340,
        margin=dict(l=20, r=20, t=40, b=20),
        hovermode="x unified",
    )
    chart(fig, key, 340)


def plot_intraday_combined(opt_df, und_df, strike, title, key):
    """یک نمودار: سهم پایه + پریمیوم + کاوردکال؛ solid=معامله، dash=ffill."""
    if (opt_df is None or opt_df.empty) and (und_df is None or und_df.empty):
        st.info("دادهٔ درون‌روز اختیار یا سهم پایه موجود نیست.")
        return

    opt_f = (
        forward_fill_intraday(opt_df)
        if opt_df is not None and not opt_df.empty
        else pd.DataFrame()
    )
    und_f = (
        forward_fill_intraday(und_df)
        if und_df is not None and not und_df.empty
        else pd.DataFrame()
    )

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    def _add_price_traces(frame, name, color, secondary=False):
        if frame is None or frame.empty:
            return
        real = frame.loc[~frame["is_fill"].fillna(False)]
        if not real.empty:
            fig.add_trace(
                go.Scatter(
                    x=real["datetime"],
                    y=real["price"],
                    name=f"{name} · معامله",
                    line=dict(color=color, width=2, dash="solid"),
                    mode="lines",
                ),
                secondary_y=secondary,
            )
        # مسیر کامل با ffill — خط‌چین
        fig.add_trace(
            go.Scatter(
                x=frame["datetime"],
                y=frame["price"],
                name=f"{name} · ادامه قیمت",
                line=dict(color=color, width=1.4, dash="dash"),
                mode="lines",
                opacity=0.55,
            ),
            secondary_y=secondary,
        )

    _add_price_traces(und_f, "سهم پایه", "#60A5FA", False)
    _add_price_traces(opt_f, "پریمیوم اختیار", "#A78BFA", False)

    # outer join زمانی برای نرخ کاوردکال
    if not opt_f.empty and not und_f.empty:
        o = (
            opt_f[["datetime", "price", "is_fill"]]
            .rename(columns={"price": "prem", "is_fill": "opt_fill"})
            .drop_duplicates("datetime")
            .sort_values("datetime")
        )
        u = (
            und_f[["datetime", "price", "is_fill"]]
            .rename(columns={"price": "spot", "is_fill": "und_fill"})
            .drop_duplicates("datetime")
            .sort_values("datetime")
        )
        merged = pd.merge(o, u, on="datetime", how="outer").sort_values("datetime")
        merged["prem"] = merged["prem"].ffill()
        merged["spot"] = merged["spot"].ffill()
        merged = merged.dropna(subset=["prem", "spot"])
        merged = merged[merged["spot"] > 0]
        if not merged.empty:
            merged["cc_yield_pct"] = merged["prem"] / merged["spot"] * 100.0
            both_real = ~(
                merged.get("opt_fill", False).fillna(True)
                | merged.get("und_fill", False).fillna(True)
            )
            if both_real.any():
                fig.add_trace(
                    go.Scatter(
                        x=merged.loc[both_real, "datetime"],
                        y=merged.loc[both_real, "cc_yield_pct"],
                        name="کاوردکال ٪ · معامله",
                        line=dict(color="#10B981", width=2, dash="solid"),
                        mode="lines",
                    ),
                    secondary_y=True,
                )
            fig.add_trace(
                go.Scatter(
                    x=merged["datetime"],
                    y=merged["cc_yield_pct"],
                    name="کاوردکال ٪ · ادامه",
                    line=dict(color="#10B981", width=1.4, dash="dash"),
                    mode="lines",
                    opacity=0.55,
                ),
                secondary_y=True,
            )
            st.caption(
                f"میانگین نرخ کاوردکال: {merged['cc_yield_pct'].mean():.2f}% · "
                f"آخر: {merged['cc_yield_pct'].iloc[-1]:.2f}%"
                + (
                    f" · اعمال {strike:,.0f}"
                    if strike is not None and np.isfinite(strike)
                    else ""
                )
                + " · خط‌چین = بدون معامله (آخرین قیمت)"
            )

    fig.update_layout(
        title=title,
        height=420,
        hovermode="x unified",
        legend=dict(orientation="h", y=1.14),
        margin=dict(l=20, r=20, t=50, b=20),
    )
    fig.update_yaxes(title_text="قیمت · ریال", secondary_y=False)
    fig.update_yaxes(title_text="کاوردکال ٪", secondary_y=True)
    chart(fig, key, 420)


def plot_intraday_cc_yield(opt_df, und_df, strike, title, key):
    """سازگاری عقب‌رو — به نمودار ترکیبی هدایت می‌شود."""
    plot_intraday_combined(opt_df, und_df, strike, title, key)


def apply_bubble_envelope(plot_df, series_col, intensity: float, last_anchor: float):
    """
    منحنی هجوم دی۹۸–مهر۹۹ با شانه‌ها.
    intensity>=1 ؛ 1=بدون اثر.
    """
    if intensity is None or intensity <= 1.0 + 1e-9:
        return plot_df
    shoulder_l = pd.Timestamp("2019-12-22")
    peak_l = pd.Timestamp("2020-01-21")
    peak_r = pd.Timestamp("2020-09-21")
    shoulder_r = pd.Timestamp("2020-11-20")
    t = plot_df["date"]
    env = pd.Series(0.0, index=plot_df.index)
    m_left = (t >= shoulder_l) & (t < peak_l)
    if m_left.any():
        span_l = (peak_l - shoulder_l).total_seconds() or 1.0
        x = ((t - shoulder_l).dt.total_seconds() / span_l).clip(0, 1)
        env = env.where(~m_left, np.sin(0.5 * np.pi * x) ** 2)
    m_peak = (t >= peak_l) & (t <= peak_r)
    env = env.where(~m_peak, 1.0)
    m_right = (t > peak_r) & (t <= shoulder_r)
    if m_right.any():
        span_r = (shoulder_r - peak_r).total_seconds() or 1.0
        x = ((t - peak_r).dt.total_seconds() / span_r).clip(0, 1)
        env = env.where(~m_right, np.cos(0.5 * np.pi * x) ** 2)
    factor = 1.0 + (float(intensity) - 1.0) * env
    out = plot_df.copy()
    out[series_col] = out[series_col] / factor
    last = float(out[series_col].iloc[-1])
    if last != 0:
        out[series_col] = out[series_col] / last * float(last_anchor)
    return out



def apply_liquidity_envelope(
    plot_df,
    series_col,
    intensity: float,
    last_anchor: float,
    start=None,
    ramp_days: int = 40,
):
    """تعدیل نقدینگی اخیر (فراتر از CPI). intensity=1 بدون اثر."""
    if intensity is None or intensity <= 1.0 + 1e-9:
        return plot_df
    if start is None:
        start = pd.Timestamp("2025-06-13")
    start = pd.Timestamp(start)
    ramp_end = start + pd.Timedelta(days=max(1, int(ramp_days)))
    t = plot_df["date"]
    env = pd.Series(0.0, index=plot_df.index)
    m_ramp = (t >= start) & (t < ramp_end)
    if m_ramp.any():
        span = (ramp_end - start).total_seconds() or 1.0
        x = ((t - start).dt.total_seconds() / span).clip(0, 1)
        env = env.where(~m_ramp, np.sin(0.5 * np.pi * x) ** 2)
    m_flat = t >= ramp_end
    env = env.where(~m_flat, 1.0)
    factor = 1.0 + (float(intensity) - 1.0) * env
    out = plot_df.copy()
    out[series_col] = out[series_col] / factor
    last = float(out[series_col].iloc[-1])
    if last != 0 and last_anchor is not None:
        out[series_col] = out[series_col] / last * float(last_anchor)
    return out


def render_tedpix_adjusted(fx, cpi, cpi_us=None, key_prefix="tedpix"):
    """نمودار شاخص کل با همان بازبیان تتر×CPI_US + وزن تورم ایران."""
    st.markdown("#### شاخص کل (TEDPIX) · بازبیان قدرت خرید")
    st.caption(
        "یک‌بار در نشست دریافت می‌شود · همان منطق سهم: "
        "تتر تعدیلی با CPI آمریکا + وزن تورم ایران · لنگر آخرین مقدار شاخص."
    )

    idx_df, err = load_tedpix_memo()
    if err:
        st.warning(f"دریافت شاخص کل ناموفق: {err}")
        return
    if idx_df is None or idx_df.empty:
        st.info("تاریخچه شاخص خالی است.")
        return

    if fx is None or fx.empty:
        st.info("برای بازبیان شاخص به داده تتر نیاز است.")
        # نمودار خام
        fig = go.Figure(
            go.Scatter(
                x=idx_df["date"],
                y=idx_df["close"],
                name="TEDPIX",
                line=dict(color="#60A5FA", width=2),
            )
        )
        fig.update_layout(height=380, title="شاخص کل (خام)")
        chart(fig, f"{key_prefix}-raw", 380, date_axes=("x",), max_ticks=8)
        return

    s1, s2 = st.columns(2)
    w_ir_pct = s1.slider(
        "وزن تورم ایران برای شاخص (٪)",
        min_value=0,
        max_value=100,
        value=45,
        step=5,
        key=f"{key_prefix}_w_ir",
    )
    cpi_boost = s2.slider(
        "ضریب کم‌اظهاری CPI (شاخص)",
        min_value=1.0,
        max_value=1.8,
        value=1.15,
        step=0.05,
        key=f"{key_prefix}_boost",
    )

    # history شبیه سهم
    history = idx_df.rename(columns={"close": "close"}).copy()
    # columns needed by adjust: date, close
    try:
        us_cpi = cpi_us
        if us_cpi is None:
            us_cpi, _ = load_us_cpi_memo()
        pp = adjust_purchasing_power(
            history,
            fx,
            cpi if cpi is not None and not getattr(cpi, "empty", True) else None,
            w_ir_pct / 100.0,
            cpi_us=us_cpi if us_cpi is not None and not getattr(us_cpi, "empty", True) else None,
            cpi_boost=float(cpi_boost),
        )
    except Exception as exc:
        st.error(f"بازبیان شاخص ناموفق: {exc}")
        return

    if pp is None or pp.empty or "close_real" not in pp.columns:
        st.info("هم‌پوشانی شاخص و تتر کافی نیست.")
        return

    plot_df = pp.dropna(subset=["close_real"]).copy()
    if plot_df.empty:
        st.info("سری بازبیان‌شده شاخص خالی است.")
        return

    last_raw = float(plot_df["close"].iloc[-1])
    last_adj = float(plot_df["close_real"].iloc[-1])
    # لنگر
    if abs(last_adj - last_raw) / max(abs(last_raw), 1) > 0.01:
        plot_df.iloc[-1, plot_df.columns.get_loc("close_real")] = last_raw
        last_adj = last_raw

    # تعدیل حباب برای شاخص
    bub_on = st.checkbox(
        "تعدیل هجوم بورس روی شاخص (دی۹۸–مهر۹۹)",
        value=False,
        key=f"{key_prefix}_bub_on",
    )
    bub_div = st.slider(
        "شدت حباب شاخص",
        1.0, 3.0, 1.35, 0.05,
        key=f"{key_prefix}_bub_div",
        disabled=not bub_on,
    )
    if bub_on:
        plot_df = apply_bubble_envelope(plot_df, "close_real", bub_div, last_raw)
        last_adj = float(plot_df["close_real"].iloc[-1])

    liq_on = st.checkbox(
        "تعدیل نقدینگی اخیر (فراتر از CPI — اثر چاپ پول / پس از شوک)",
        value=False,
        key=f"{key_prefix}_liq_on",
        help=(
            "نقدینگی ≠ CPI: بخشی از پول قبل از کالا در دارایی‌ها می‌نشیند. "
            "این تعدیل رشد اسمی اخیر را کمی خنثی می‌کند."
        ),
    )
    l1, l2, l3 = st.columns(3)
    liq_div = l1.slider(
        "شدت نقدینگی",
        1.0, 2.0, 1.25, 0.05,
        key=f"{key_prefix}_liq_div",
        disabled=not liq_on,
    )
    liq_start = l2.date_input(
        "شروع اثر",
        value=dt.date(2025, 6, 13),
        key=f"{key_prefix}_liq_start",
        disabled=not liq_on,
    )
    liq_ramp = l3.slider(
        "روزهای شانه (مثلاً ۴۰روزه)",
        10, 90, 40, 5,
        key=f"{key_prefix}_liq_ramp",
        disabled=not liq_on,
    )
    if liq_on:
        plot_df = apply_liquidity_envelope(
            plot_df,
            "close_real",
            liq_div,
            last_raw,
            start=pd.Timestamp(liq_start),
            ramp_days=int(liq_ramp),
        )
        last_adj = float(plot_df["close_real"].iloc[-1])

    st.caption(
        "ترتیب: ۱) بازبیان تتر+CPI  ۲) حباب ۹۸–۹۹  ۳) نقدینگی اخیر  ۴) باند و سیگنال."
    )
    appetite = st.slider(
        "اقبال به بورس (ذهنیت کاربر)",
        min_value=-50,
        max_value=50,
        value=0,
        step=5,
        key=f"{key_prefix}_appetite",
        help=(
            "مثبت = بازار گرم/ارزش معاملات بالا → فروش سخت‌گیرانه‌تر، خرید آسان‌تر. "
            "منفی = فضای فروش → فروش آسان‌تر، خرید سخت‌گیرانه‌تر."
        ),
    )

    z1, z2, z3, z4 = st.columns(4)
    bb_win = z1.slider(
        "پنجره میانگین (روز)",
        min_value=20,
        max_value=250,
        value=90,
        step=5,
        key=f"{key_prefix}_bb_win",
    )
    z_mult = z2.slider(
        "ضریب باند (σ)",
        min_value=0.8,
        max_value=3.0,
        value=2.0,
        step=0.1,
        key=f"{key_prefix}_z_mult",
    )
    z_buy = z3.slider(
        "آستانه خرید پایه (z≤−)",
        min_value=0.5,
        max_value=3.0,
        value=1.6,
        step=0.1,
        key=f"{key_prefix}_z_buy",
    )
    z_sell = z4.slider(
        "آستانه فروش پایه (z≥+)",
        min_value=0.5,
        max_value=3.5,
        value=2.3,
        step=0.1,
        key=f"{key_prefix}_z_sell",
    )

    # اقبال: مثبت → z_sell↑ و |z_buy|↓ ؛ منفی برعکس
    ap = float(appetite) / 50.0  # -1..+1
    z_buy_eff = float(z_buy) - 0.6 * ap   # اقبال مثبت: خرید آسان‌تر
    z_sell_eff = float(z_sell) + 0.8 * ap  # اقبال مثبت: فروش دیرتر
    z_buy_eff = max(0.4, min(3.5, z_buy_eff))
    z_sell_eff = max(0.5, min(4.0, z_sell_eff))
    st.caption(
        f"آستانه‌های مؤثر با اقبال: خرید z≤−{z_buy_eff:.2f} · فروش z≥+{z_sell_eff:.2f}"
    )

    # محاسبات فقط بعد از تعدیل‌ها
    series = plot_df["close_real"].astype(float)
    ma = series.rolling(int(bb_win), min_periods=max(10, int(bb_win) // 3)).mean()
    sd = series.rolling(int(bb_win), min_periods=max(10, int(bb_win) // 3)).std(ddof=1)
    upper = ma + float(z_mult) * sd
    lower = ma - float(z_mult) * sd
    zscore = (series - ma) / sd.replace(0, np.nan)
    plot_df = plot_df.copy()
    plot_df["bb_ma"] = ma
    plot_df["bb_up"] = upper
    plot_df["bb_lo"] = lower
    plot_df["zscore"] = zscore

    # بازه حباب: سیگنال خرید را سرکوب کن (کف مصنوعی تعدیل)
    bubble_mask = (
        (plot_df["date"] >= pd.Timestamp("2019-12-22"))
        & (plot_df["date"] <= pd.Timestamp("2020-11-20"))
    )
    buy_m = plot_df["zscore"] <= -float(z_buy_eff)
    sell_m = plot_df["zscore"] >= float(z_sell_eff)
    if bub_on:
        buy_m = buy_m & ~bubble_mask
        # فروش در اوج حباب را هم کمی سخت‌گیرتر کن
        sell_m = sell_m & ~(
            bubble_mask
            & (plot_df["zscore"] < float(z_sell_eff) + 0.4)
        )

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=plot_df["date"], y=plot_df["bb_up"],
            name=f"باند بالا (+{z_mult}σ)",
            line=dict(color="rgba(239,68,68,0.35)", width=1),
            mode="lines",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["date"], y=plot_df["bb_lo"],
            name=f"باند پایین (−{z_mult}σ)",
            line=dict(color="rgba(16,185,129,0.35)", width=1),
            fill="tonexty",
            fillcolor="rgba(148,163,184,0.08)",
            mode="lines",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["date"], y=plot_df["bb_ma"],
            name=f"میانگین {bb_win}روز",
            line=dict(color="#FBBF24", width=1.6, dash="dot"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["date"],
            y=plot_df["close_real"],
            name="TEDPIX بازبیان",
            line=dict(color="#60A5FA", width=2.4),
        )
    )
    if buy_m.any():
        fig.add_trace(go.Scatter(
            x=plot_df.loc[buy_m, "date"],
            y=plot_df.loc[buy_m, "close_real"],
            mode="markers",
            name="ناحیه خرید",
            marker=dict(color="#10B981", size=7, symbol="triangle-up"),
        ))
    if sell_m.any():
        fig.add_trace(go.Scatter(
            x=plot_df.loc[sell_m, "date"],
            y=plot_df.loc[sell_m, "close_real"],
            mode="markers",
            name="ناحیه فروش",
            marker=dict(color="#EF4444", size=7, symbol="triangle-down"),
        ))

    fig.update_layout(
        title="شاخص کل TEDPIX · بازبیان + باند σ و سیگنال",
        yaxis_title="واحد شاخص (بازبیان؛ آخر = امروز)",
        height=460,
        hovermode="x unified",
        legend=dict(orientation="h", y=1.14),
    )
    chart(
        fig,
        f"{key_prefix}-adj-{w_ir_pct}-{cpi_boost}-{bb_win}-{z_mult}",
        460,
        date_axes=("x",),
        max_ticks=8,
    )

    z_now = float(plot_df["zscore"].iloc[-1]) if plot_df["zscore"].notna().any() else np.nan
    a, b, c, d = st.columns(4)
    a.metric("آخرین شاخص (لنگر)", f"{last_raw:,.0f}")
    b.metric("آخرین بازبیان", f"{last_adj:,.0f}")
    c.metric("z فعلی", f"{z_now:+.2f}" if np.isfinite(z_now) else "—")
    if np.isfinite(z_now):
        if z_now <= -float(z_buy_eff):
            d.success("سیگنال خرید آزمایشی")
        elif z_now >= float(z_sell_eff):
            d.warning("سیگنال فروش آزمایشی")
        else:
            d.info("باند میانی")
    st.caption(
        f"روزهای معتبر: {len(plot_df):,} · وزن ایران {w_ir_pct}٪ · "
        f"ضریب CPI×{cpi_boost:.2f} · پنجره {bb_win} · σ×{z_mult} · کد {TEDPIX_CODE}"
    )

    # —— همان سیگنال‌ها روی شاخص خام (بدون تعدیل) ——
    st.markdown("#### شاخص خام (ریال/نقاط اسمی) + سیگنال‌های سری بازبیان")
    st.caption(
        "مثلث‌ها از روی سری تعدیلی استخراج شده‌اند؛ اینجا روی سطح اسمی TEDPIX "
        "می‌بینید بعد از سیگنال، رفتار ریالی شاخص چگونه بوده است."
    )
    fig_raw = go.Figure()
    fig_raw.add_trace(
        go.Scatter(
            x=plot_df["date"],
            y=plot_df["close"],
            name="TEDPIX خام",
            line=dict(color="#94A3B8", width=2),
        )
    )
    if buy_m.any():
        fig_raw.add_trace(go.Scatter(
            x=plot_df.loc[buy_m, "date"],
            y=plot_df.loc[buy_m, "close"],
            mode="markers",
            name="سیگنال خرید (از بازبیان)",
            marker=dict(color="#10B981", size=8, symbol="triangle-up"),
        ))
    if sell_m.any():
        fig_raw.add_trace(go.Scatter(
            x=plot_df.loc[sell_m, "date"],
            y=plot_df.loc[sell_m, "close"],
            mode="markers",
            name="سیگنال فروش (از بازبیان)",
            marker=dict(color="#EF4444", size=8, symbol="triangle-down"),
        ))
    fig_raw.update_layout(
        title="TEDPIX اسمی · اعتبارسنجی سیگنال‌های قدرت خرید",
        yaxis_title="سطح شاخص (خام)",
        height=400,
        hovermode="x unified",
        legend=dict(orientation="h", y=1.12),
    )
    chart(
        fig_raw,
        f"{key_prefix}-raw-signals-{bb_win}-{z_mult}",
        400,
        date_axes=("x",),
        max_ticks=8,
    )



def adjust_purchasing_power(
    history,
    fx,
    cpi,
    weight_cpi: float,
    cpi_us=None,
    cpi_boost: float = 1.0,
):
    """
    بازسازی مسیر قیمت با لنگرِ آخرین قیمت واقعی سهم.

    گام ۱ — تتر تعدیلی با CPI آمریکا (نسبت به امروز):
        fx_adj_t = fx_t × (CPI_US_today / CPI_US_t)
        یعنی نرخ گذشته را با تورم دلار «به قدرت خرید امروز» می‌رسانیم.

    گام ۲ — ضریب ترکیبی (جمع وزن‌ها = ۱):
        w_ir = weight_cpi ∈ [0,1]
        w_fx = 1 − w_ir
        # کم‌اظهاری رسمی CPI: رشد شاخص ایران × cpi_boost (≥۱)
        ratio_fx_t = fx_adj_t / fx_adj_today
        ratio_ir_t = (CPI_IR_t / CPI_IR_today) ** cpi_boost
        deflator_t = ratio_fx_t ** w_fx × ratio_ir_t ** w_ir

    گام ۳ — لنگر امروز:
        S_adj_t = S_t / deflator_t
        ⇒ S_adj_today = S_today  (چون deflator_today = ۱)
        گذشته‌ها طوری جابه‌جا می‌شوند که با قیمت امروز قابل‌مقایسه باشند.
    """
    w_ir = float(np.clip(weight_cpi, 0.0, 1.0))
    w_fx = 1.0 - w_ir
    boost = float(max(1.0, cpi_boost))

    stock = history.sort_values("date").copy()
    stock["stock_cutoff"] = (
        stock["date"].dt.tz_localize(TEHRAN).dt.tz_convert("UTC")
    )

    merged = pd.merge_asof(
        stock.sort_values("stock_cutoff"),
        fx[["available_at", "fx_rial", "fx_toman"]].sort_values("available_at"),
        left_on="stock_cutoff",
        right_on="available_at",
        direction="backward",
        tolerance=pd.Timedelta(days=MAX_FX_AGE_DAYS),
    )
    merged = merged.rename(columns={"available_at": "fx_available_at"})

    if cpi is not None and not cpi.empty:
        merged = pd.merge_asof(
            merged.sort_values("stock_cutoff"),
            cpi[["available_at", "cpi", "cpi_year"]].sort_values("available_at"),
            left_on="stock_cutoff",
            right_on="available_at",
            direction="backward",
        )
        merged = merged.rename(columns={"available_at": "cpi_available_at"})
    else:
        merged["cpi"] = np.nan

    if cpi_us is not None and not cpi_us.empty:
        us = cpi_us.copy()
        if "cpi_us" not in us.columns and "cpi" in us.columns:
            us = us.rename(columns={"cpi": "cpi_us"})
        cols = [c for c in ("available_at", "cpi_us") if c in us.columns]
        merged = pd.merge_asof(
            merged.sort_values("stock_cutoff"),
            us[cols].sort_values("available_at"),
            left_on="stock_cutoff",
            right_on="available_at",
            direction="backward",
        )
        merged = merged.rename(columns={"available_at": "cpi_us_available_at"})
    else:
        merged["cpi_us"] = np.nan

    usable = merged.dropna(subset=["close", "fx_rial"]).copy()
    if usable.empty:
        return merged

    ref = usable.iloc[-1]
    fx_today = float(ref["fx_rial"])
    cpi_us_today = float(ref["cpi_us"]) if pd.notna(ref.get("cpi_us")) else np.nan
    cpi_ir_today = float(ref["cpi"]) if pd.notna(ref.get("cpi")) else np.nan
    s_today = float(ref["close"])

    # ۱) تتر تعدیلی با تورم دلار (به قدرت خرید امروز)
    if pd.notna(cpi_us_today) and cpi_us_today > 0 and usable["cpi_us"].notna().any():
        usable["fx_adj"] = usable["fx_rial"] * (cpi_us_today / usable["cpi_us"])
    else:
        usable["fx_adj"] = usable["fx_rial"]

    fx_adj_today = float(usable["fx_adj"].iloc[-1])
    if not (fx_adj_today > 0):
        usable["close_real"] = usable["close"]
        usable["deflator"] = 1.0
        return usable

    ratio_fx = usable["fx_adj"] / fx_adj_today

    # ۲) نسبت تورم ایران (با ضریب کم‌اظهاری)
    if pd.notna(cpi_ir_today) and cpi_ir_today > 0 and usable["cpi"].notna().any():
        ratio_ir = (usable["cpi"] / cpi_ir_today).clip(lower=1e-12) ** boost
    else:
        ratio_ir = pd.Series(1.0, index=usable.index)
        w_ir, w_fx = 0.0, 1.0

    ratio_fx = ratio_fx.clip(lower=1e-12)
    deflator = (ratio_fx ** w_fx) * (ratio_ir ** w_ir)
    # نرمال: آخرین نقطه دقیقاً ۱
    deflator = deflator / float(deflator.iloc[-1])

    usable["deflator"] = deflator
    usable["close_usdt"] = usable["close"] / usable["fx_rial"]
    # ۳) لنگر امروز: S_adj_today = S_today
    usable["close_real"] = usable["close"] / deflator
    # تضمین عددی آخرین نقطه
    usable.iloc[-1, usable.columns.get_loc("close_real")] = s_today

    usable["weight_cpi"] = w_ir
    usable["weight_fx"] = w_fx
    usable["cpi_boost"] = boost
    usable["fx_adj"] = usable["fx_adj"]

    # میانگین‌های چندمقیاسی روی کل سری (قبل از فیلتر بازهٔ نمایش)
    series = usable["close_real"].dropna()
    n = len(series)
    usable["ma_60"] = usable["close_real"].rolling(60, min_periods=30).mean()
    usable["ma_120"] = usable["close_real"].rolling(120, min_periods=60).mean()
    usable["ma_252"] = usable["close_real"].rolling(252, min_periods=120).mean()
    if n >= 20:
        usable["real_mean_static"] = float(series.mean())
        usable["real_mean_roll"] = usable["ma_120"]  # سازگاری عقب‌رو
        sd = float(series.std(ddof=1)) or np.nan
        usable["real_std"] = sd
        if sd and sd > 0:
            usable["real_z"] = (usable["close_real"] - series.mean()) / sd
        else:
            usable["real_z"] = np.nan
    else:
        usable["real_mean_static"] = np.nan
        usable["real_mean_roll"] = np.nan
        usable["real_std"] = np.nan
        usable["real_z"] = np.nan

    return usable



# ============================================================
# MARKET PANEL
# ============================================================

def render_market(stocks, pressure_history):
    eligible, bid, ask, pressure, coverage = pressure_summary(stocks)

    left, right = st.columns([1, 1.5])

    with left:
        section("قطب‌نمای سفارش‌ها")

        if pd.notna(pressure):
            fig = go.Figure(
                go.Indicator(
                    mode="gauge+number",
                    value=float(pressure),
                    number=dict(
                        valueformat=".1f",
                        font=dict(size=46, color=COLORS["text"]),
                    ),
                    gauge=dict(
                        axis=dict(
                            range=[-100, 100],
                            tickvals=[-100, -50, 0, 50, 100],
                        ),
                        bar=dict(
                            color=COLORS["blue"],
                            thickness=.25,
                        ),
                        borderwidth=0,
                        bgcolor=COLORS["panel"],
                        steps=[
                            dict(
                                range=[-100, 0],
                                color="rgba(251,113,133,.25)",
                            ),
                            dict(
                                range=[0, 100],
                                color="rgba(45,212,191,.25)",
                            ),
                        ],
                        threshold=dict(
                            value=float(pressure),
                            line=dict(color=COLORS["text"], width=3),
                            thickness=.8,
                        ),
                    ),
                )
            )

            chart(fig, "pressure-gauge", 300)

        else:
            st.info("عمق معتبر کافی برای شاخص موجود نیست.")

        st.caption(
            "مثبت: غلبه ارزش سفارش خرید؛ منفی: غلبه ارزش سفارش فروش. "
            "سفارش‌ها قابل لغو هستند و معادل معامله انجام‌شده نیستند."
        )

    with right:
        section("توازن ارزش سفارش‌های قابل مشاهده")

        a, b, c = st.columns(3)
        a.metric("خرید · میلیارد تومان", fmt(bid / 1e10, 2))
        b.metric("فروش · میلیارد تومان", fmt(ask / 1e10, 2))
        c.metric("پوشش شاخص", pct(coverage))

        if pd.notna(bid) and pd.notna(ask):
            fig = go.Figure(
                go.Bar(
                    x=[bid / 1e10, ask / 1e10],
                    y=["خرید", "فروش"],
                    orientation="h",
                    marker_color=[COLORS["green"], COLORS["red"]],
                    text=[fmt(bid / 1e10, 2), fmt(ask / 1e10, 2)],
                    textposition="auto",
                )
            )
            fig.update_layout(
                xaxis_title="میلیارد تومان",
                showlegend=False,
            )
            chart(fig, "book-balance", 220)

    section("مسیر شاخص در نشست جاری")

    if len(pressure_history) >= 2:
        history = pd.DataFrame(pressure_history)
        history["time"] = pd.to_datetime(history["time"], utc=True)
        history["time"] = history["time"].dt.tz_convert(TEHRAN)

        fig = go.Figure(
            go.Scatter(
                x=history["time"],
                y=history["pressure"],
                mode="lines+markers",
                line=dict(color=COLORS["green"], width=2),
                marker=dict(size=4),
                name="فشار سفارش",
            )
        )
        fig.add_hline(
            y=0,
            line_dash="dot",
            line_color=COLORS["muted"],
        )
        fig.update_layout(
            yaxis_title="شاخص فشار",
            hovermode="x unified",
        )
        chart(
            fig,
            "pressure-session-history",
            290,
            date_axes=("x",),
            intraday=True,
            max_ticks=8,
        )

    else:
        st.info(
            "روند شاخص از دریافت‌های واقعی همین نشست ساخته می‌شود؛ "
            "بعد از دریافت نمونه بعدی ظاهر خواهد شد."
        )

    st.caption(
        "این نمودار تاریخچه کل روز نیست؛ فقط دریافت‌های نشست باز فعلی "
        "را نگه می‌دارد. تازگی پاسخ، تازگی تک‌تک مظنه‌ها را تضمین نمی‌کند."
    )

    section("نقشه حرارتی نقدشوندگی")

    heat = stocks[
        stocks["value"].gt(0)
        & stocks["return_pct"].notna()
    ].nlargest(100, "value").copy()

    if not heat.empty:
        color_limit = max(
            1.0,
            float(heat["return_pct"].abs().quantile(.95)),
        )

        fig = px.treemap(
            heat,
            path=["industry_id", "symbol"],
            values="value",
            color="return_pct",
            color_continuous_scale=[
                COLORS["red"],
                COLORS["panel"],
                COLORS["green"],
            ],
            range_color=(-color_limit, color_limit),
            color_continuous_midpoint=0,
        )

        fig.update_traces(
            textinfo="label",
            marker=dict(
                line=dict(width=2, color=COLORS["bg"]),
            ),
            hovertemplate=(
                "<b>%{label}</b><br>"
                "ارزش معاملات: %{value:,.0f} ریال"
                "<extra></extra>"
            ),
        )

        fig.update_layout(coloraxis_colorbar_title="بازده ٪")
        chart(fig, "market-heatmap", 500)

        st.caption(
            "۱۰۰ نماد با بیشترین ارزش معاملات؛ "
            "مساحت = ارزش معاملات، رنگ = بازده پایانی. "
            "گروه‌بندی بر اساس کد صنعت منبع است. "
            "شدت رنگ در صدک ۹۵ قدرمطلق بازده محدود شده است."
        )

    section("تابلوی سهام")

    table(
        stocks.sort_values("value", ascending=False),
        [
            "symbol", "name", "time", "close_price",
            "return_pct", "price_usdt", "value",
            "best_bid", "best_ask", "spread_pct", "pressure",
        ],
        height=540,
    )

    st.caption(
        "ستون تتر، تبدیل پایانی snapshot با نرخ مرجع روزانه است؛ "
        "نرخ هم‌زمان لحظه‌ای سهم و ارز نیست. مقدار خالی یعنی داده "
        "معتبر برای محاسبه موجود نیست."
    )

    download(
        stocks,
        "دریافت تابلوی سهام",
        "nova_stocks.csv",
        "download-stocks",
    )


# ============================================================
# OPTIONS PANEL
# ============================================================

def render_options(options, market=None):
    section("نبض اختیار معامله")
    with st.expander("اختیار خرید و فروش یعنی چه؟", expanded=False):
        st.markdown(
            """
**اختیار خرید (Call · نمادهای ض…)**  
حق می‌خردی که سهم پایه را تا سررسید با قیمت اعمال بخری.  
- اگر **بخری**: وقتی سهم بالای اعمال برود سود می‌کنی؛ حداکثر زیان = پریمیوم پرداختی.  
- اگر **بفروشی (کال‌نویس)**: پریمیوم می‌گیری؛ اگر سهم خیلی بالا برود زیان می‌تواند بزرگ شود.

**اختیار فروش (Put · نمادهای ط…)**  
حق می‌خردی که سهم را با قیمت اعمال بفروشی.  
- اگر **بخری**: از افت سهم سود می‌بری؛ حداکثر زیان = پریمیوم.  
- اگر **بفروشی**: پریمیوم می‌گیری؛ افت شدید سهم زیان ایجاد می‌کند.

**ارزان/گران در BS-IR** نسبت به مدل است نه توصیه قطعی خرید/فروش.
            """
        )
    st.caption(
        "نمادهای «ض»: اختیار خرید · «ط»: اختیار فروش · "
        "کد نوع ابزار نیز برای سایر نمادها بررسی می‌شود."
    )

    if options.empty:
        st.info("اختیار معامله‌ای در پاسخ فعلی شناسایی نشد.")
        if market is not None and render_bs_ir is not None:
            section("ارزش‌گذاری بلک‌شولز ایرانی")
            render_bs_ir(market, key_prefix="app8_bsir_empty")
        return

    calls = options[options["kind"].eq("اختیار خرید")]
    puts = options[options["kind"].eq("اختیار فروش")]

    call_value = total(calls["value"])
    put_value = total(puts["value"])

    a, b, c, d = st.columns(4)

    a.metric("تعداد نماد اختیار خرید", fmt(len(calls)))
    b.metric("تعداد نماد اختیار فروش", fmt(len(puts)))
    c.metric(
        "ارزش معاملات · میلیارد تومان",
        fmt(total(options["value"]) / 1e10, 2),
    )
    d.metric(
        "نسبت ارزش فروش به خرید",
        fmt(safe_ratio(put_value, call_value), 3),
    )

    st.caption(
        "نسبت بالا از ارزش معاملات اختیار فروش به اختیار خرید ساخته شده؛ "
        "نسبت موقعیت باز، نسبت تعداد قراردادها یا جهت قطعی بازار نیست."
    )

    left, right = st.columns(2)

    with left:
        summary = (
            options.groupby("kind")["value"]
            .sum(min_count=1)
            .reset_index()
            .dropna()
        )

        if not summary.empty and summary["value"].sum() > 0:
            fig = px.pie(
                summary,
                names="kind",
                values="value",
                color="kind",
                hole=.72,
                color_discrete_map={
                    "اختیار خرید": COLORS["green"],
                    "اختیار فروش": COLORS["red"],
                },
            )

            fig.update_traces(
                textinfo="percent+label",
                textposition="outside",
            )

            fig.update_layout(
                title="ترکیب ارزش معاملات",
                showlegend=False,
            )
            chart(fig, "options-donut", 370)

    with right:
        leaders = (
            options[options["value"].gt(0)]
            .nlargest(14, "value")
            .sort_values("value")
            .copy()
        )

        if not leaders.empty:
            leaders["billion_toman"] = leaders["value"] / 1e10

            fig = px.bar(
                leaders,
                x="billion_toman",
                y="symbol",
                color="kind",
                orientation="h",
                color_discrete_map={
                    "اختیار خرید": COLORS["green"],
                    "اختیار فروش": COLORS["red"],
                },
                labels={
                    "billion_toman": "میلیارد تومان",
                    "symbol": "",
                    "kind": "نوع اختیار",
                },
            )
            fig.update_layout(title="کانون معاملات اختیار")
            chart(fig, "options-leaders", 370)

    section("فشار سفارش و کیفیت مظنه")

    a, b, c, d = st.columns(4)

    for container, frame, label in [
        (a, calls, "خرید"),
        (b, puts, "فروش"),
    ]:
        _, _, _, pressure, coverage = pressure_summary(frame)
        container.metric(f"فشار سفارش اختیار {label}", fmt(pressure, 1))
        container.caption(f"پوشش: {pct(coverage)}")

    c.metric(
        "میانه اسپرد مظنه‌های معتبر",
        pct(options["spread_pct"].median(), 2),
    )
    d.metric(
        "قراردادهای دارای معامله",
        fmt(options["volume"].gt(0).sum()),
    )

    section("تابلوی قراردادها")

    table(
        options.sort_values("value", ascending=False),
        [
            "symbol", "name", "kind", "time",
            "last_trade", "return_pct", "volume",
            "value", "number_trades", "best_bid",
            "best_ask", "spread_pct", "pressure",
        ],
        height=560,
    )

    st.info(
        "مشخصات ساختاریافته رسمی (اندازه قرارداد، موقعیت باز) در این "
        "endpoint نیست. اعمال و سررسید از نام قرارداد استخراج می‌شوند."
    )

    download(
        options,
        "دریافت تابلوی اختیار معامله",
        "nova_options.csv",
        "download-options",
    )

    # —— BS-IR: تخمین از پرمعامله‌ها + fair range برای بقیه ——
    if market is not None and render_bs_ir is not None:
        st.divider()
        render_bs_ir(market, key_prefix="app8_bsir")
    elif render_bs_ir is None:
        st.warning(
            "ماژول bs_ir_view در دسترس نیست؛ ارزش‌گذاری BS-IR نمایش داده نشد."
        )


# ============================================================
# AUTOMATIC STOCK DETAIL / FX PANEL
# ============================================================


# ============================================================
# FUNDAMENTALS · Codal + TSETMC (no reinventing the wheel)
# ============================================================

def fetch_codal_letters(symbol: str, page: int = 1, category: int = -1, length: int = -1):
    """
    جستجوی اطلاعیه‌های کدال برای نماد.
    منبع پایدار رسمی: https://search.codal.ir/api/search/v2/q
    category: 1=صورت‌های مالی ، -1=همه
    length: طول دوره مالی ماه (۱۲=سالانه) یا -1 همه
    """
    import urllib.parse
    params = {
        "Audited": "true",
        "AuditorRef": "-1",
        "Category": str(category),
        "Childs": "false",
        "CompanyState": "-1",
        "CompanyType": "-1",
        "Consolidatable": "true",
        "IsNotAudited": "false",
        "Length": str(length),
        "LetterType": "-1",
        "Mains": "true",
        "NotAudited": "true",
        "NotPublished": "false",
        "PageNumber": str(page),
        "Publisher": "false",
        "Symbol": symbol,
        "TracingNo": "-1",
        "search": "true",
    }
    url = "https://search.codal.ir/api/search/v2/q?" + urllib.parse.urlencode(params)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    data = r.json()
    letters = data.get("Letters") or []
    rows = []
    for L in letters:
        pdf = L.get("PdfUrl") or ""
        excel = L.get("ExcelUrl") or ""
        html_url = L.get("Url") or ""
        if pdf and not str(pdf).startswith("http"):
            pdf = "https://www.codal.ir/" + str(pdf).lstrip("/")
        if html_url and not str(html_url).startswith("http"):
            html_url = "https://www.codal.ir" + str(html_url)
        rows.append({
            "title": L.get("Title"),
            "symbol": L.get("Symbol"),
            "company": L.get("CompanyName"),
            "code": L.get("LetterCode"),
            "published": L.get("PublishDateTime") or L.get("SentDateTime"),
            "tracing": L.get("TracingNo"),
            "has_html": L.get("HasHtml"),
            "has_pdf": L.get("HasPdf"),
            "has_excel": L.get("HasExcel"),
            "pdf_url": pdf,
            "excel_url": excel,
            "html_url": html_url,
        })
    return pd.DataFrame(rows), int(data.get("Total") or 0)


def fetch_tsetmc_instrument_info(inscode: str) -> dict:
    """اطلاعات هویتی / بنیادی تابلو از CDN تی‌اس‌ای‌تی‌ام‌سی."""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    out = {}
    for path in (
        f"https://cdn.tsetmc.com/api/Instrument/GetInstrumentInfo/{inscode}",
        f"https://cdn.tsetmc.com/api/Instrument/GetInstrumentIdentity/{inscode}",
    ):
        try:
            r = requests.get(path, headers=headers, timeout=12)
            if r.ok:
                j = r.json()
                if isinstance(j, dict):
                    # اغلب داخل instrumentInstrument یا مشابه
                    out.update(j)
                    for k in ("instrumentInstrument", "instrumentIdentity", "instrument"):
                        if isinstance(j.get(k), dict):
                            out.update(j[k])
        except Exception:
            continue
    return out


def fetch_tsetmc_shareholders(inscode: str) -> pd.DataFrame:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        r = requests.get(
            f"https://cdn.tsetmc.com/api/Shareholder/GetShareholder/{inscode}/0",
            headers=headers,
            timeout=12,
        )
        if not r.ok:
            return pd.DataFrame()
        j = r.json()
        rows = j.get("shareHolder") or j.get("shareholder") or j
        if isinstance(rows, dict):
            rows = rows.get("shareHolder") or rows.get("data") or []
        if not isinstance(rows, list):
            return pd.DataFrame()
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


def render_fundamentals_panel(symbol: str, inscode: str, asset=None):
    """
    پنل بنیادی خلاقانه: کدال + هویت TSETMC + سهامداران.
    داده از منابع رسمی خوانده می‌شود؛ در session کش می‌شود.
    """
    st.markdown("#### ◈ بنیادی و افشا · کدال + تابلو")
    st.caption(
        "منبع: search.codal.ir (افشا) · cdn.tsetmc.com (هویت/سهامدار) — "
        "بدون بازنویسی پارسر صورت‌های مالی؛ لینک PDF/HTML رسمی."
    )

    cache_key = f"_fund_{symbol}_{inscode}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = {}

    memo = st.session_state[cache_key]

    c1, c2, c3 = st.columns([1, 1, 1])
    load_codal = c1.button("دریافت اطلاعیه‌های کدال", key=f"btn_codal_{inscode}")
    load_id = c2.button("هویت نماد (TSETMC)", key=f"btn_id_{inscode}")
    load_sh = c3.button("سهامداران عمده", key=f"btn_sh_{inscode}")

    # ---- Codal ----
    if load_codal or memo.get("codal_df") is not None:
        if load_codal or memo.get("codal_df") is None:
            with st.spinner("جستجوی کدال…"):
                try:
                    df_all, total = fetch_codal_letters(symbol, category=-1)
                    df_fs, total_fs = fetch_codal_letters(symbol, category=1, length=12)
                    memo["codal_df"] = df_all
                    memo["codal_total"] = total
                    memo["codal_fs"] = df_fs
                    memo["codal_fs_total"] = total_fs
                    memo["codal_err"] = None
                except Exception as exc:
                    memo["codal_err"] = str(exc)
                    memo["codal_df"] = pd.DataFrame()
                    memo["codal_fs"] = pd.DataFrame()

        if memo.get("codal_err"):
            st.warning(f"کدال در دسترس نیست: {memo['codal_err']}")
        else:
            fs = memo.get("codal_fs")
            all_l = memo.get("codal_df")
            a, b, c = st.columns(3)
            a.metric("کل آگهی‌های صفحه", len(all_l) if all_l is not None else 0)
            b.metric("صورت مالی سالانه (نمونه)", len(fs) if fs is not None else 0)
            c.metric("Total کدال", memo.get("codal_total") or "—")

            tabs = st.tabs(["صورت‌های مالی", "همه اطلاعیه‌ها", "خط زمان"])
            with tabs[0]:
                if fs is not None and not fs.empty:
                    show = fs.copy()
                    # لینک‌ها
                    def _link(row, col, label):
                        u = row.get(col)
                        if u:
                            return f"[{label}]({u})"
                        return "—"
                    show["PDF"] = show.apply(lambda r: _link(r, "pdf_url", "PDF"), axis=1)
                    show["HTML"] = show.apply(lambda r: _link(r, "html_url", "گزارش"), axis=1)
                    st.dataframe(
                        show[["published", "title", "code", "PDF", "HTML"]],
                        hide_index=True,
                        use_container_width=True,
                        height=280,
                    )
                else:
                    st.info("صورت مالی سالانه‌ای در این صفحه یافت نشد.")
            with tabs[1]:
                if all_l is not None and not all_l.empty:
                    show = all_l.copy()
                    show["PDF"] = show.apply(
                        lambda r: f"[PDF]({r['pdf_url']})" if r.get("pdf_url") else "—",
                        axis=1,
                    )
                    st.dataframe(
                        show[["published", "title", "code", "PDF"]],
                        hide_index=True,
                        use_container_width=True,
                        height=280,
                    )
            with tabs[2]:
                src = fs if fs is not None and not fs.empty else all_l
                if src is not None and not src.empty and "published" in src.columns:
                    # timeline-ish table
                    tl = src.head(12)[["published", "title"]].copy()
                    st.markdown("**آخرین افشاها**")
                    for _, row in tl.iterrows():
                        st.markdown(
                            f"<div style='border-right:3px solid #60A5FA;padding:0.4rem 0.8rem;margin:0.35rem 0;"
                            f"background:#0f172a;border-radius:8px'>"
                            f"<span style='color:#94a3b8;font-size:0.8rem'>{row['published']}</span><br>"
                            f"<b>{row['title']}</b></div>",
                            unsafe_allow_html=True,
                        )

    # ---- Instrument identity ----
    if load_id or memo.get("ident") is not None:
        if load_id or memo.get("ident") is None:
            with st.spinner("هویت TSETMC…"):
                try:
                    memo["ident"] = fetch_tsetmc_instrument_info(str(inscode))
                    memo["ident_err"] = None
                except Exception as exc:
                    memo["ident"] = {}
                    memo["ident_err"] = str(exc)
        if memo.get("ident_err"):
            st.caption(f"هویت: {memo['ident_err']}")
        ident = memo.get("ident") or {}
        if ident:
            # pick interesting keys
            prefer = [
                "lVal18AFC", "lVal30", "sectorPe", "psr", "pSRatio",
                "eps", "pe", "bvol", "zTitad", "baseVol", "cIsin",
                "yMarNSC", "flow", "cgrValCotTitle", "cComValTitle",
            ]
            cards = []
            for k, v in ident.items():
                if v is None or v == "" or isinstance(v, (dict, list)):
                    continue
                if k in prefer or k.lower() in ("eps", "pe", "sectorpe"):
                    cards.append((k, v))
            if not cards:
                # show first few scalar
                for k, v in list(ident.items())[:12]:
                    if not isinstance(v, (dict, list)):
                        cards.append((k, v))
            if cards:
                cols = st.columns(min(4, len(cards)))
                for i, (k, v) in enumerate(cards[:8]):
                    cols[i % len(cols)].metric(str(k), str(v)[:48])
            with st.expander("JSON هویت خام", expanded=False):
                st.json(ident)

    # ---- Shareholders ----
    if load_sh or memo.get("sh_df") is not None:
        if load_sh or memo.get("sh_df") is None:
            with st.spinner("سهامداران…"):
                memo["sh_df"] = fetch_tsetmc_shareholders(str(inscode))
        sh = memo.get("sh_df")
        if sh is not None and not sh.empty:
            st.dataframe(sh.head(30), hide_index=True, use_container_width=True)
        else:
            st.info("سهامدار عمده‌ای از API برنگشت.")

    st.caption(
        "برای ریز اقلام ترازنامه داخل جدول: پکیج‌های `codalpy` / `fipiran` یا دانلود Excel از لینک‌های بالا. "
        "این پنل فهرست افشا و لینک رسمی را بدون اختراع چرخ می‌آورد."
    )



def render_stock_detail(
    stocks,
    book,
    fx,
    fx_error,
    failed_chunks,
    fx_reference,
    cpi=None,
    cpi_error=None,
):
    # ========================================================
    # 1. SELECT STOCK — SINGLE SOURCE OF TRUTH
    # ========================================================

    candidates = (
        stocks
        .drop_duplicates("id")
        .sort_values("symbol")
        .copy()
    )

    if candidates.empty:
        st.info("هیچ سهمی برای انتخاب در پاسخ فعلی موجود نیست.")
        return

    section("انتخاب سهم")

    symbol_ids = candidates["id"].tolist()

    symbol_labels = {
        row["id"]: f"{row['symbol']} | {row['name']}"
        for _, row in candidates.iterrows()
    }

    selection_key = "selected_stock_detail_id"

    # تعیین پیش‌فرض فقط در شروع یا هنگام حذف نماد از پاسخ بازار.
    # انتخاب کاربر در بازخوانی‌های بعدی تغییر نمی‌کند.
    if st.session_state.get(selection_key) not in symbol_ids:
        default_asset = candidates.sort_values(
            "value",
            ascending=False,
            na_position="last",
        ).iloc[0]

        st.session_state[selection_key] = default_asset["id"]

    selected_id = st.selectbox(
        "نام یا نماد سهم را جست‌وجو کن",
        options=symbol_ids,
        index=None,
        format_func=lambda inscode: symbol_labels[inscode],
        key=selection_key,
        placeholder="مثلاً فملی، فولاد، شپنا…",
    )
    st.caption(
        f"{len(symbol_ids):,} نماد قابل‌انتخاب "
        "(عمده/بلوکی با رقم در نماد حذف؛ استثناء انرژی۳)"
    )

    if selected_id is None:
        st.info("برای نمایش تحلیل، یک سهم انتخاب کن.")
        return

    # تنها محل تعیین asset در تمام این تابع.
    asset = candidates.loc[
        candidates["id"].eq(selected_id)
    ].iloc[0]

    symbol = str(asset["symbol"])
    inscode = str(asset["id"])

    history_years = max(
        1,
        int(globals().get("HISTORY_YEARS", 5)),
    )

    section(f"پرونده تحلیلی سهم · {symbol}")

    st.caption(
        f"تمام اطلاعات این بخش، شامل تحلیل ارزی، تاریخچه قیمت "
        f"و دفتر سفارش، مربوط به «{symbol}» با شناسه {inscode} است. "
        "انتخاب سهم هنگام بازخوانی خودکار حفظ می‌شود."
    )

    valid_fx_reference = (
        pd.notna(fx_reference)
        and np.isfinite(float(fx_reference))
        and float(fx_reference) > 0
    )

    current_usdt_price = np.nan

    if (
        valid_fx_reference
        and pd.notna(asset["close_price"])
        and asset["close_price"] > 0
    ):
        current_usdt_price = (
            float(asset["close_price"]) / float(fx_reference)
        )

    # ارزش بازار
    shares = float(asset.get("number_shares") or 0)
    close_rial = float(asset.get("close_price") or 0)
    mcap_rial = shares * close_rial if shares > 0 and close_rial > 0 else np.nan
    # ریال → تومان (/10) ؛ همت = ۱۰۰۰۰ میلیارد تومان = 1e13 ریال
    mcap_toman = mcap_rial / 10.0 if np.isfinite(mcap_rial) else np.nan
    fx_ref = float(fx_reference) if valid_fx_reference else np.nan
    # fx_reference در کد معمولاً ریال/تتر است
    mcap_usdt = (mcap_rial / fx_ref) if (np.isfinite(mcap_rial) and np.isfinite(fx_ref) and fx_ref > 0) else np.nan

    def _fmt_mcap_toman(toman):
        if not np.isfinite(toman) or toman <= 0:
            return "—"
        if toman >= 1e12:  # هزار میلیارد تومان = همت
            return f"{toman / 1e12:,.2f} همت"
        if toman >= 1e9:
            return f"{toman / 1e9:,.1f} میلیارد تومان"
        return f"{toman / 1e6:,.0f} میلیون تومان"

    a, b, c, d = st.columns(4)

    a.metric(
        f"پایانی {symbol} · ریال",
        fmt(asset["close_price"]),
    )

    b.metric(
        "بازده پایانی روز",
        pct(asset["return_pct"], 2),
    )

    c.metric(
        "ارزش بازار",
        _fmt_mcap_toman(mcap_toman),
    )

    d.metric(
        "ارزش بازار · USDT",
        f"{mcap_usdt:,.0f}" if np.isfinite(mcap_usdt) else "—",
    )

    # —— معاملات درون‌روز سهم ——
    with st.expander("معاملات درون‌روز امروز", expanded=False):
        try:
            intra = fetch_intraday_trades(str(inscode), max_days=1)
            plot_intraday_series(
                intra,
                f"درون‌روز {symbol}",
                f"stock_intra_{inscode}",
                "#10B981",
                min_price=asset.get("min_allowed_price"),
                max_price=asset.get("max_allowed_price"),
            )
        except Exception as exc:
            st.caption(f"درون‌روز در دسترس نیست: {exc}")

    with st.expander("بنیادی · کدال و هویت شرکت", expanded=True):
        try:
            render_fundamentals_panel(symbol, str(inscode), asset=asset)
        except Exception as exc:
            st.warning(f"پنل بنیادی: {exc}")

    # ========================================================
    # 2. LOAD THE SELECTED STOCK'S HISTORY
    # ========================================================

    history = pd.DataFrame()
    history_error = None

    try:
        with st.spinner(f"آماده‌سازی تاریخچه {symbol}…"):
            all_history = fetch_stock_history(inscode)

        # تمام داده موجود — بدون برش ۵ساله
        history = all_history.sort_values("date").copy()

        if history.empty:
            history_error = (
                f"تاریخچه معتبری برای {symbol} موجود نیست."
            )

    except Exception as exc:
        history_error = str(exc)

    # ========================================================
    # 3. FX ANALYSIS FIRST
    # ========================================================

    section(f"قیمت ارزی و تعدیل نرخ ارز · {symbol}")

    st.caption(
        "منبع نرخ ارز: بازار USDTTMN والکس. "
        "این خروجی بر حسب تتر است، نه دلار نقدی. "
        "تبدیل ارز با تعدیل سود نقدی و افزایش سرمایه متفاوت است."
    )

    has_fx = fx is not None and not fx.empty

    if fx_error:
        st.warning(
            f"دریافت نرخ ارز با خطا مواجه شده است: {fx_error}"
        )

    if has_fx:
        latest_fx = fx.sort_values("available_at").iloc[-1]

        a, b, c = st.columns(3)

        a.metric(
            "آخرین نرخ روزانه موجود · تومان/تتر",
            fmt(latest_fx["fx_toman"]),
        )

        b.metric(
            "تاریخ شروع کندل ارز · UTC",
            latest_fx["candle_start"].strftime("%Y-%m-%d"),
        )

        c.metric(
            "بازه تاریخچه",
            "حداکثر داده موجود",
        )

        st.caption(
            "زمان تکمیل آخرین کندل ارز: "
            f"{latest_fx['available_at'].tz_convert(TEHRAN):%Y-%m-%d %H:%M}"
            " تهران. این نرخ، نرخ لحظه‌ای ارز نیست."
        )

        if failed_chunks:
            st.warning(
                f"{failed_chunks} بازه درخواست ارز ناموفق بوده است؛ "
                "بخشی از تاریخچه ممکن است پوشش نداشته باشد."
            )

        if not valid_fx_reference:
            st.warning(
                "نرخ مرجع تازه و معتبر برای تبدیل قیمت فعلی "
                "یا بازبیان تاریخچه موجود نیست. "
                "در صورت وجود هم‌پوشانی، تحلیل تاریخی ارزی "
                "همچنان محاسبه می‌شود."
            )

    elif not fx_error:
        st.info("تاریخچه معتبر ارز فعلاً در دسترس نیست.")

    if history_error:
        st.warning(
            f"تاریخچه سهم {symbol} در دسترس نیست: {history_error}"
        )

    if has_fx and not history.empty:
        try:
            adjusted = adjust_currency(history, fx)

            usable = (
                adjusted
                .dropna(subset=["close_usdt"])
                .sort_values("date")
                .copy()
            )

        except Exception as exc:
            adjusted = pd.DataFrame()
            usable = pd.DataFrame()

            st.error(
                f"تطبیق تاریخچه {symbol} با نرخ ارز ناموفق بود: {exc}"
            )

        if not adjusted.empty and usable.empty:
            st.info(
                "بین تاریخچه سهم و نرخ‌های ارز، "
                "هم‌پوشانی معتبر با محدودیت زمانی تعیین‌شده وجود ندارد."
            )

        if not usable.empty:
            first = usable.iloc[0]
            last = usable.iloc[-1]

            coverage = 100 * len(usable) / len(adjusted)

            adjusted["rebased_rial"] = (
                adjusted["close_usdt"] * float(fx_reference)
                if valid_fx_reference
                else np.nan
            )

            a, b, c, d = st.columns(4)

            a.metric(
                f"آخرین قیمت تاریخی معتبر {symbol} · تتر",
                fmt(last["close_usdt"], 6),
            )

            b.metric(
                "تاریخ آخرین قیمت ارزی معتبر",
                last["date"].strftime("%Y-%m-%d"),
            )

            c.metric(
                "پوشش تاریخچه ارز",
                pct(coverage),
            )

            d.metric(
                "روزهای سهم دارای نرخ معتبر",
                fmt(len(usable)),
            )

            st.caption(
                f"بازه مشترک قابل محاسبه: "
                f"{first['date']:%Y-%m-%d} تا "
                f"{last['date']:%Y-%m-%d}. "
                "درخواست تاریخچه بلندتر، وجود همان مقدار داده "
                "در منبع را تضمین نمی‌کند."
            )

            # ------------------------------------------------
            # بازبیان با لنگر قیمت امروز
            # تتر×CPI_US + وزن CPI ایران (جمع=۱) + ضریب کم‌اظهاری
            # ------------------------------------------------
            us_cpi_data, us_cpi_err = load_us_cpi_memo()
            if us_cpi_err:
                st.caption(f"CPI آمریکا: {us_cpi_err}")

            s1, s2 = st.columns(2)
            w_ir_pct = s1.slider(
                "وزن تورم ایران (٪) — باقی = تتر تعدیلی",
                min_value=0,
                max_value=100,
                value=45,
                step=5,
                key=f"fx_ir_weight_{inscode}",
                help="w_ir + w_fx = ۱ · w_fx روی تتر تعدیل‌شده با CPI آمریکا است.",
            )
            cpi_boost = s2.slider(
                "ضریب کم‌اظهاری CPI ایران",
                min_value=1.0,
                max_value=1.8,
                value=1.15,
                step=0.05,
                key=f"cpi_boost_{inscode}",
                help="اگر باور داری تورم رسمی کمتر اعلام می‌شود، ضریب را بالاتر ببر (≥۱).",
            )
            st.caption(
                f"ترکیب: {w_ir_pct}٪ تورم ایران (×{cpi_boost:.2f}) + "
                f"{100 - w_ir_pct}٪ تتر تعدیلی با CPI آمریکا · "
                "آخرین نقطه نمودار = قیمت امروز سهم."
            )

            try:
                pp_fx = adjust_purchasing_power(
                    history,
                    fx,
                    cpi if cpi is not None and not cpi.empty else None,
                    w_ir_pct / 100.0,
                    cpi_us=us_cpi_data if us_cpi_data is not None and not us_cpi_data.empty else None,
                    cpi_boost=float(cpi_boost),
                )
            except Exception as _exc:
                pp_fx = pd.DataFrame()
                st.error(f"محاسبه بازبیان ناموفق: {_exc}")

            series_col = "close_real"
            if (
                not pp_fx.empty
                and series_col in pp_fx.columns
                and pp_fx[series_col].notna().any()
            ):
                plot_df = pp_fx.dropna(subset=[series_col]).copy()
                last_adj = float(plot_df[series_col].iloc[-1])
                last_raw = float(plot_df["close"].iloc[-1])
                if abs(last_adj - last_raw) / max(abs(last_raw), 1) > 0.01:
                    plot_df.iloc[-1, plot_df.columns.get_loc(series_col)] = last_raw
                    last_adj = last_raw

                # —— منحنی حباب: شانه دی۹۸ → فلات بهمن۹۸–شهریور۹۹ → شانه آبان۹۹ ——
                # دی ۱۳۹۸ ≈ ۲۰۱۹-۱۲-۲۲ · بهمن ≈ ۲۰۲۰-۰۱-۲۱ · شهریور۹۹ ≈ ۲۰۲۰-۰۹-۲۱ · آبان ≈ ۲۰۲۰-۱۱-۲۰
                shoulder_l = pd.Timestamp("2019-12-22")  # شروع شانه چپ (دی ۹۸)
                peak_l = pd.Timestamp("2020-01-21")      # شروع اوج (بهمن ۹۸)
                peak_r = pd.Timestamp("2020-09-21")      # پایان اوج (شهریور ۹۹)
                shoulder_r = pd.Timestamp("2020-11-20")  # پایان شانه راست (آبان ۹۹)
                apply_bubble = st.checkbox(
                    "تعدیل هجوم مردم به بورس (دی۹۸–مهر۹۹ با شانه‌ها)",
                    value=False,
                    key=f"bubble_on_{inscode}",
                    help="همان منحنی شانه/فلات: دی۹۸→بهمن۹۸→شهریور۹۹→آبان۹۹",
                )
                b1, b2 = st.columns(2)
                bubble_div = b1.slider(
                    "شدت اوج حباب (فعال فقط اگر تیک بالا زده شود)",
                    min_value=1.0,
                    max_value=3.0,
                    value=1.35,
                    step=0.05,
                    key=f"bubble_div_{inscode}",
                    disabled=not apply_bubble,
                )
                z_thr = b2.slider(
                    "آستانه |z| برای سیگنال",
                    min_value=0.6,
                    max_value=2.0,
                    value=1.0,
                    step=0.1,
                    key=f"z_thr_{inscode}",
                )
                if apply_bubble and bubble_div > 1.0 + 1e-9:
                    t = plot_df["date"]
                    # envelope: 0 خارج · شانه چپ 0→1 · فلات 1 · شانه راست 1→0
                    env = pd.Series(0.0, index=plot_df.index)
                    # شانه چپ
                    m_left = (t >= shoulder_l) & (t < peak_l)
                    if m_left.any():
                        span_l = (peak_l - shoulder_l).total_seconds() or 1.0
                        x = ((t - shoulder_l).dt.total_seconds() / span_l).clip(0, 1)
                        # sin ramp soft
                        env = env.where(~m_left, (np.sin(0.5 * np.pi * x) ** 2))
                    # فلات
                    m_peak = (t >= peak_l) & (t <= peak_r)
                    env = env.where(~m_peak, 1.0)
                    # شانه راست
                    m_right = (t > peak_r) & (t <= shoulder_r)
                    if m_right.any():
                        span_r = (shoulder_r - peak_r).total_seconds() or 1.0
                        x = ((t - peak_r).dt.total_seconds() / span_r).clip(0, 1)
                        env = env.where(~m_right, (np.cos(0.5 * np.pi * x) ** 2))
                    factor = 1.0 + (float(bubble_div) - 1.0) * env
                    plot_df[series_col] = plot_df[series_col] / factor
                    # لنگر دوباره: آخر = قیمت امروز
                    plot_df[series_col] = (
                        plot_df[series_col]
                        / float(plot_df[series_col].iloc[-1])
                        * last_raw
                    )
                    last_adj = last_raw
                    plot_df["ma_60"] = plot_df[series_col].rolling(60, min_periods=30).mean()
                    plot_df["ma_120"] = plot_df[series_col].rolling(120, min_periods=60).mean()
                    plot_df["ma_252"] = plot_df[series_col].rolling(252, min_periods=120).mean()
                    full_mean = float(plot_df[series_col].mean())
                    full_std = float(plot_df[series_col].std(ddof=1)) or np.nan
                    plot_df["real_mean_static"] = full_mean
                    plot_df["real_std"] = full_std
                    if full_std and full_std > 0:
                        plot_df["real_z"] = (plot_df[series_col] - full_mean) / full_std

                # —— نقدینگی اخیر (فراتر از CPI) ——
                apply_liq = st.checkbox(
                    "تعدیل نقدینگی اخیر (چاپ پول / پس از شوک — فراتر از CPI)",
                    value=False,
                    key=f"liq_on_{inscode}",
                    help=(
                        "نقدینگی ≠ CPI: بخشی از پول در دارایی می‌نشیند قبل از آنکه CPI کالا کامل نشان دهد. "
                        "این منحنی رشد اسمی اخیر را کمی خنثی می‌کند."
                    ),
                )
                lq1, lq2, lq3 = st.columns(3)
                liq_div = lq1.slider(
                    "شدت نقدینگی",
                    1.0, 2.0, 1.25, 0.05,
                    key=f"liq_div_{inscode}",
                    disabled=not apply_liq,
                )
                liq_start = lq2.date_input(
                    "شروع اثر نقدینگی",
                    value=dt.date(2025, 6, 13),
                    key=f"liq_start_{inscode}",
                    disabled=not apply_liq,
                )
                liq_ramp = lq3.slider(
                    "روز شانه",
                    10, 90, 40, 5,
                    key=f"liq_ramp_{inscode}",
                    disabled=not apply_liq,
                )
                if apply_liq and liq_div > 1.0 + 1e-9:
                    plot_df = apply_liquidity_envelope(
                        plot_df,
                        series_col,
                        float(liq_div),
                        last_raw,
                        start=pd.Timestamp(liq_start),
                        ramp_days=int(liq_ramp),
                    )
                    last_adj = last_raw
                    plot_df["ma_60"] = plot_df[series_col].rolling(60, min_periods=30).mean()
                    plot_df["ma_120"] = plot_df[series_col].rolling(120, min_periods=60).mean()
                    plot_df["ma_252"] = plot_df[series_col].rolling(252, min_periods=120).mean()
                    full_mean = float(plot_df[series_col].mean())
                    full_std = float(plot_df[series_col].std(ddof=1)) or np.nan
                    plot_df["real_mean_static"] = full_mean
                    plot_df["real_std"] = full_std
                    if full_std and full_std > 0:
                        plot_df["real_z"] = (plot_df[series_col] - full_mean) / full_std

                # فیلتر بازه نمایش (میانگین ثابت بازهٔ انتخابی دوباره محاسبه می‌شود)
                dmin = pd.Timestamp(plot_df["date"].min()).normalize()
                dmax = pd.Timestamp(plot_df["date"].max()).normalize()
                try:
                    dr = st.slider(
                        "بازه نمایش نمودار",
                        min_value=dmin.to_pydatetime(),
                        max_value=dmax.to_pydatetime(),
                        value=(dmin.to_pydatetime(), dmax.to_pydatetime()),
                        key=f"pp_date_range_{inscode}",
                        format="YYYY-MM-DD",
                    )
                    view = plot_df[
                        (plot_df["date"] >= pd.Timestamp(dr[0]))
                        & (plot_df["date"] <= pd.Timestamp(dr[1]))
                    ].copy()
                except Exception:
                    view = plot_df.copy()
                if view.empty:
                    view = plot_df.copy()

                # میانگین ثابت روی بازهٔ نمایش
                view_static = float(view[series_col].mean()) if len(view) else np.nan
                # میانگین‌های rolling از کل سری (سیگنال ساختاری)
                full_static = float(plot_df[series_col].mean()) if len(plot_df) else np.nan
                full_std = float(plot_df[series_col].std(ddof=1)) or np.nan

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=view["date"],
                    y=view[series_col],
                    name=f"{symbol} · بازبیان",
                    connectgaps=False,
                    line=dict(color=COLORS["green"], width=2.5),
                ))
                for col, label, color, dash in [
                    ("ma_60", "MA ۶۰روز", "#60A5FA", "dot"),
                    ("ma_120", "MA ۱۲۰روز", "#A78BFA", "dot"),
                    ("ma_252", "MA ۲۵۲روز", "#FBBF24", "dash"),
                ]:
                    if col in view.columns and view[col].notna().any():
                        fig.add_trace(go.Scatter(
                            x=view["date"],
                            y=view[col],
                            name=label,
                            connectgaps=False,
                            line=dict(color=color, width=1.4, dash=dash),
                        ))
                if np.isfinite(view_static):
                    fig.add_hline(
                        y=view_static,
                        line_dash="dash",
                        line_color=COLORS["gold"],
                        annotation_text=f"میانگین ثابت بازه نمایش {view_static:,.0f}",
                        annotation_position="top left",
                    )
                if np.isfinite(full_static) and abs(full_static - view_static) / max(abs(full_static), 1) > 0.01:
                    fig.add_hline(
                        y=full_static,
                        line_dash="solid",
                        line_color="rgba(251,191,36,0.35)",
                        annotation_text=f"میانگین کل سری {full_static:,.0f}",
                        annotation_position="bottom left",
                    )

                buy_mask = pd.Series(False, index=plot_df.index)
                sell_mask = pd.Series(False, index=plot_df.index)
                buy_zones, sell_zones = [], []

                # نواحی خرید/فروش بر اساس اجماع ۴ میانگین روی کل سری، نمایش روی view
                # buy: قیمت < min(MAها و static) و z < -thr
                # sell: قیمت > max(...) و z > thr
                # برای خلوت بودن: فقط باندهای پیوسته به‌صورت shape نیمه‌شفاف
                if full_std and full_std > 0 and np.isfinite(full_static):
                    z_full = (plot_df[series_col] - full_static) / full_std
                    ma_stack = plot_df[["ma_60", "ma_120", "ma_252"]].copy()
                    ma_stack["static"] = full_static
                    ma_min = ma_stack.min(axis=1)
                    ma_max = ma_stack.max(axis=1)
                    buy_mask = (plot_df[series_col] < ma_min) & (z_full <= -float(z_thr))
                    sell_mask = (plot_df[series_col] > ma_max) & (z_full >= float(z_thr))

                    def _zones(mask):
                        """لیست بازه‌های پیوسته True."""
                        zones = []
                        in_z = False
                        start = None
                        dates = plot_df["date"].tolist()
                        for i, flag in enumerate(mask.fillna(False).tolist()):
                            if flag and not in_z:
                                in_z = True
                                start = dates[i]
                            elif not flag and in_z:
                                in_z = False
                                zones.append((start, dates[i - 1]))
                        if in_z:
                            zones.append((start, dates[-1]))
                        # فقط بازه‌های حداقل ~۵ روز
                        out = []
                        for a, b in zones:
                            if (pd.Timestamp(b) - pd.Timestamp(a)).days >= 5:
                                out.append((a, b))
                        return out

                    buy_zones = _zones(buy_mask)
                    sell_zones = _zones(sell_mask)
                    y0 = float(view[series_col].min()) * 0.98
                    y1 = float(view[series_col].max()) * 1.02
                    for a, b in buy_zones:
                        fig.add_vrect(
                            x0=a, x1=b, y0=y0, y1=y1,
                            fillcolor="rgba(16,185,129,0.12)",
                            line_width=0,
                            annotation_text="خرید",
                            annotation_position="top left",
                            annotation_font_size=10,
                            annotation_font_color="#6EE7B7",
                        )
                    for a, b in sell_zones:
                        fig.add_vrect(
                            x0=a, x1=b, y0=y0, y1=y1,
                            fillcolor="rgba(239,68,68,0.12)",
                            line_width=0,
                            annotation_text="فروش",
                            annotation_position="top left",
                            annotation_font_size=10,
                            annotation_font_color="#FCA5A5",
                        )

                    z_now = float(z_full.iloc[-1]) if len(z_full) else np.nan
                    if pd.notna(z_now):
                        if z_now <= -float(z_thr):
                            st.success(
                                f"**الان ناحیه خرید (آزمایشی)** · z={z_now:.2f} "
                                f"(آستانه ±{z_thr}) · قیمت بازبیان زیر اجماع میانگین‌ها."
                            )
                        elif z_now >= float(z_thr):
                            st.warning(
                                f"**الان ناحیه فروش/سبک‌کردن (آزمایشی)** · z={z_now:.2f} "
                                f"(آستانه ±{z_thr})."
                            )
                        else:
                            st.info(
                                f"الان در باند میانی · z={z_now:.2f} — سیگنال قوی نیست."
                            )

                fig.update_layout(
                    title=f"بازبیان {symbol} · لنگر امروز · MAهای چندمقیاسی و نواحی",
                    yaxis_title="ریال بازبیان‌شده (آخر = قیمت امروز)",
                    hovermode="x unified",
                    height=520,
                    legend=dict(orientation="h", y=1.12),
                )
                chart(
                    fig,
                    f"stock-rebased-real-{inscode}-{w_ir_pct}-{cpi_boost}-{bubble_div}",
                    520,
                    date_axes=("x",),
                    max_ticks=8,
                )
                a, b, c, d = st.columns(4)
                a.metric("قیمت امروز (لنگر)", f"{last_raw:,.0f}")
                b.metric("میانگین بازه نمایش", f"{view_static:,.0f}" if np.isfinite(view_static) else "—")
                c.metric("میانگین کل سری", f"{full_static:,.0f}" if np.isfinite(full_static) else "—")
                d.metric("روز در نمایش", f"{len(view):,}")
                st.caption(
                    "سیگنال‌ها از اجماع MA۶۰/۱۲۰/۲۵۲ + میانگین کل سری و z-score می‌آیند. "
                    "بازه حباب با تقسیم‌گر ضرب می‌شود (نه تفریق ثابت). "
                    "میانگین ثابت طلایی = فقط بازهٔ نمایش انتخابی."
                )

                # —— سیگنال بازبیان روی قیمت ریالی خام ——
                if "close" in view.columns and (buy_mask.any() or sell_mask.any()):
                    st.markdown("#### قیمت ریالی خام + سیگنال‌های سری بازبیان")
                    st.caption(
                        "همان نواحی خرید/فروش که از قدرت خرید گرفته شده؛ "
                        "اینجا روی پایانی اسمی سهم می‌بینید اثر بعدی در ریال چگونه بوده."
                    )
                    fig_rial = go.Figure()
                    fig_rial.add_trace(go.Scatter(
                        x=view["date"],
                        y=view["close"],
                        name=f"{symbol} · ریال خام",
                        line=dict(color="#94A3B8", width=2),
                    ))
                    y0r = float(view["close"].min()) * 0.98
                    y1r = float(view["close"].max()) * 1.02
                    for a, b in buy_zones:
                        fig_rial.add_vrect(
                            x0=a, x1=b, y0=y0r, y1=y1r,
                            fillcolor="rgba(16,185,129,0.12)",
                            line_width=0,
                            annotation_text="خرید",
                            annotation_position="top left",
                            annotation_font_size=10,
                            annotation_font_color="#6EE7B7",
                        )
                    for a, b in sell_zones:
                        fig_rial.add_vrect(
                            x0=a, x1=b, y0=y0r, y1=y1r,
                            fillcolor="rgba(239,68,68,0.12)",
                            line_width=0,
                            annotation_text="فروش",
                            annotation_position="top left",
                            annotation_font_size=10,
                            annotation_font_color="#FCA5A5",
                        )
                    # نقاط
                    if buy_mask.any():
                        vm = view.loc[buy_mask.reindex(view.index, fill_value=False)]
                        if not vm.empty:
                            fig_rial.add_trace(go.Scatter(
                                x=vm["date"], y=vm["close"],
                                mode="markers", name="خرید",
                                marker=dict(color="#10B981", size=7, symbol="triangle-up"),
                            ))
                    if sell_mask.any():
                        vm = view.loc[sell_mask.reindex(view.index, fill_value=False)]
                        if not vm.empty:
                            fig_rial.add_trace(go.Scatter(
                                x=vm["date"], y=vm["close"],
                                mode="markers", name="فروش",
                                marker=dict(color="#EF4444", size=7, symbol="triangle-down"),
                            ))
                    fig_rial.update_layout(
                        title=f"{symbol} · پایانی ریالی + سیگنال قدرت خرید",
                        yaxis_title="ریال",
                        height=400,
                        hovermode="x unified",
                        legend=dict(orientation="h", y=1.12),
                    )
                    chart(
                        fig_rial,
                        f"stock-rial-signals-{inscode}-{w_ir_pct}",
                        400,
                        date_axes=("x",),
                        max_ticks=8,
                    )

            if not usable.empty:
                rial_change = (last["close"] / first["close"] - 1) * 100
                usdt_change = (last["close_usdt"] / first["close_usdt"] - 1) * 100
                a, b = st.columns(2)
                a.metric("تغییر ریالی اسمی بازه", pct(rial_change, 2))
                b.metric("تغییر تتری اسمی بازه", pct(usdt_change, 2))

            with st.expander(
                f"داده قابل ممیزی تحلیل ارزی {symbol}"
            ):
                audit_columns = [
                    "date",
                    "close",
                    "available_at",
                    "fx_toman",
                    "fx_age_days",
                    "close_usdt",
                    "rebased_rial",
                ]

                audit = adjusted[
                    audit_columns
                ].rename(
                    columns={
                        "date": "تاریخ سهم",
                        "close": "پایانی منبع · ریال",
                        "available_at": "دسترس‌پذیری نرخ · UTC",
                        "fx_toman": "تتر · تومان",
                        "fx_age_days": "فاصله از تکمیل نرخ · روز",
                        "close_usdt": "قیمت سهم · تتر",
                        "rebased_rial": "بازبیان · ریال",
                    }
                )

                st.dataframe(
                    audit,
                    hide_index=True,
                    use_container_width=True,
                    height=350,
                )

            download(
                adjusted,
                f"دریافت تاریخچه ارزی {symbol}",
                f"{inscode}_fx_history.csv",
                f"download-stock-fx-{inscode}",
            )

    # ========================================================
    # 3b. COMBINED CPI + FX PURCHASING POWER
    # ========================================================

    # قدرت خرید ترکیبی در بخش «بازبیان قیمت تاریخی» بالاتر ادغام شده است.

    # ========================================================
    # 4. PRICE / VOLUME OF THE SAME SELECTED STOCK
    # ========================================================

    section(f"ساختار قیمت و حجم · {symbol}")

    if not history.empty:
        st.warning(
            "این برنامه تعدیل مستقلی برای افزایش سرمایه، سود نقدی "
            "یا دیگر رویدادهای شرکتی انجام نمی‌دهد. تغییر قیمت سری "
            "دریافتی لزوماً بازده کل سرمایه‌گذاری نیست."
        )

        st.caption(
            f"بازه تاریخچه موجود سهم: "
            f"{history['date'].min():%Y-%m-%d} تا "
            f"{history['date'].max():%Y-%m-%d}؛ "
            f"{len(history):,} روز دارای داده."
        )

        valid_candles = (
            history[
                ["open", "high", "low", "last"]
            ].gt(0).all(axis=1)

            & history["high"].ge(
                history[
                    ["open", "last", "low"]
                ].max(axis=1)
            )

            & history["low"].le(
                history[
                    ["open", "last", "high"]
                ].min(axis=1)
            )
        )

        candles = history[valid_candles]

        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=.035,
            row_heights=[.75, .25],
        )

        if not candles.empty:
            fig.add_trace(
                go.Candlestick(
                    x=candles["date"],
                    open=candles["open"],
                    high=candles["high"],
                    low=candles["low"],
                    close=candles["last"],
                    name=f"کندل {symbol}",
                    increasing_line_color=COLORS["green"],
                    decreasing_line_color=COLORS["red"],
                ),
                row=1,
                col=1,
            )

        fig.add_trace(
            go.Scatter(
                x=history["date"],
                y=history["close"],
                name="پایانی",
                line=dict(
                    color=COLORS["blue"],
                    width=1.3,
                ),
            ),
            row=1,
            col=1,
        )

        for window, color in [
            (20, COLORS["gold"]),
            (60, COLORS["purple"]),
        ]:
            fig.add_trace(
                go.Scatter(
                    x=history["date"],
                    y=history["close"].rolling(window).mean(),
                    name=f"میانگین {window} روز معاملاتی",
                    line=dict(
                        color=color,
                        width=1.5,
                    ),
                ),
                row=1,
                col=1,
            )

        fig.add_trace(
            go.Bar(
                x=history["date"],
                y=history["volume"],
                name="حجم",
                marker_color="rgba(96,165,250,.45)",
            ),
            row=2,
            col=1,
        )

        fig.update_layout(
            title=f"قیمت و حجم {symbol}",
            xaxis_rangeslider_visible=False,
            hovermode="x unified",
        )

        fig.update_yaxes(
            title_text="ریال",
            row=1,
            col=1,
        )

        chart(
            fig,
            f"stock-candles-{inscode}",
            600,
            date_axes=("x",),
            max_ticks=8,
        )

        invalid_count = int((~valid_candles).sum())

        if invalid_count:
            st.caption(
                f"{invalid_count:,} ردیف OHLC نامعتبر از کندل‌ها "
                "کنار گذاشته شد؛ خط پایانی بر اساس داده معتبر خود "
                "نمایش داده می‌شود."
            )

        download(
            history,
            f"دریافت تاریخچه قیمت {symbol}",
            f"{inscode}_price_history.csv",
            f"download-stock-price-{inscode}",
        )

    else:
        st.info(
            f"نمودار قیمت {symbol} به‌دلیل نبود تاریخچه معتبر "
            "قابل نمایش نیست. دفتر سفارش مستقل از آن نمایش داده می‌شود."
        )

    # ========================================================
    # 5. ORDER BOOK OF THE SAME SELECTED STOCK
    # ========================================================

    section(f"دفتر سفارش · {symbol}")

    a, b, c, d = st.columns(4)

    a.metric(
        "بهترین خرید · ریال",
        fmt(asset["best_bid"]),
    )

    b.metric(
        "بهترین فروش · ریال",
        fmt(asset["best_ask"]),
    )

    c.metric(
        "اسپرد معتبر",
        pct(asset["spread_pct"], 2),
    )

    d.metric(
        "فشار ارزش سفارش",
        fmt(asset["pressure"], 1),
    )

    if bool(asset["crossed_book"]):
        st.warning(
            "دفتر سفارش این نماد متقاطع است؛ بهترین خرید از "
            "بهترین فروش بالاتر است. این snapshot برای تفسیر "
            "اسپرد و فشار سفارش معتبر محسوب نمی‌شود."
        )

    ob = book.loc[
        book["id"].astype(str).eq(inscode)
    ].copy()

    if ob.empty:
        st.info(
            f"در snapshot فعلی، دفتر سفارشی برای {symbol} موجود نیست."
        )
        return

    bids = (
        ob.loc[ob["bid_valid_price"].notna()]
        .groupby("bid_price", as_index=False)["bid_vol"]
        .sum()
        .sort_values("bid_price", ascending=False)
    )

    asks = (
        ob.loc[ob["ask_valid_price"].notna()]
        .groupby("ask_price", as_index=False)["ask_vol"]
        .sum()
        .sort_values("ask_price")
    )

    if not bids.empty or not asks.empty:
        fig = go.Figure()

        fig.add_trace(
            go.Scatter(
                x=bids["bid_price"],
                y=bids["bid_vol"].cumsum(),
                name="عمق خرید",
                mode="lines+markers",
                line=dict(
                    color=COLORS["green"],
                    shape="hv",
                ),
                fill="tozeroy",
                fillcolor="rgba(45,212,191,.08)",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=asks["ask_price"],
                y=asks["ask_vol"].cumsum(),
                name="عمق فروش",
                mode="lines+markers",
                line=dict(
                    color=COLORS["red"],
                    shape="hv",
                ),
                fill="tozeroy",
                fillcolor="rgba(251,113,133,.08)",
            )
        )

        fig.update_layout(
            title=f"عمق سفارش‌های قابل مشاهده {symbol}",
            xaxis_title="قیمت · ریال",
            yaxis_title="حجم تجمیعی",
        )

        chart(
            fig,
            f"stock-depth-{inscode}",
            350,
        )

    else:
        st.info(
            "ردیف دارای قیمت و حجم مثبت برای رسم منحنی عمق موجود نیست."
        )

    orderbook_view = ob.sort_values("level")[
        [
            "level",
            "buy_count",
            "bid_vol",
            "bid_price",
            "ask_price",
            "ask_vol",
            "sell_count",
        ]
    ].rename(
        columns={
            "level": "ردیف",
            "buy_count": "تعداد سفارش خرید",
            "bid_vol": "حجم خرید",
            "bid_price": "قیمت خرید · ریال",
            "ask_price": "قیمت فروش · ریال",
            "ask_vol": "حجم فروش",
            "sell_count": "تعداد سفارش فروش",
        }
    )

    st.dataframe(
        orderbook_view,
        hide_index=True,
        use_container_width=True,
    )

    a, b = st.columns(2)

    a.metric(
        "ارزش عمق خرید · میلیارد تومان",
        fmt(asset["bid_value"] / 1e10, 3),
    )

    b.metric(
        "ارزش عمق فروش · میلیارد تومان",
        fmt(asset["ask_value"] / 1e10, 3),
    )

    st.caption(
        "ارزش عمق، مجموع قیمت × حجم هر ردیف معتبر است؛ "
        "معادل ارزش صف در حد مجاز یا کل سفارش‌های پنهان بازار نیست."
    )

    download(
        ob,
        f"دریافت دفتر سفارش {symbol}",
        f"{inscode}_orderbook.csv",
        f"download-stock-book-{inscode}",
    )

    # ---- شاخص کل به‌صورت پیش‌فرض زیر جزئیات سهم ----
    st.divider()
    us_cpi_data, _us_err = load_us_cpi_memo()
    render_tedpix_adjusted(
        fx=fx,
        cpi=cpi,
        cpi_us=us_cpi_data,
        key_prefix=f"tedpix_{inscode}",
    )


# ============================================================
# METHODOLOGY / QUALITY
# ============================================================

def render_quality(snapshot, stocks, options):
    """Documentation + diagnostics (English formulas)."""
    section("روش محاسبه و کیفیت")

    st.markdown(
        """
<div style="background:linear-gradient(135deg,#0f172a,#1e293b);border:1px solid #334155;
border-radius:16px;padding:1.25rem 1.5rem;margin-bottom:1rem;color:#e2e8f0;line-height:1.7">
<h3 style="margin-top:0;color:#93c5fd">NOVA · Methodology</h3>
<p style="color:#94a3b8;margin-bottom:0.5rem">
Free market observatory · signals are <b>experimental</b>, not investment advice.
</p>
</div>
        """,
        unsafe_allow_html=True,
    )

    t1, t2, t3, t4 = st.tabs([
        "Live depth index",
        "Purchasing power",
        "Options / BS-IR",
        "Signals & quality",
    ])

    with t1:
        st.markdown("### Intraday pressure / live-depth index")
        st.markdown(
            r"""
**Universe:** equity + equity funds (exclude fixed-income, block trades with numeric suffix except special cases).

**Per-symbol estimated price** from order book depth (levels $1..L$ with decay $L^{-\gamma}$):

$$
P^{\mathrm{est}} = m + \frac{a-b}{2}\,\iota,\quad
m=\frac{b+a}{2},\quad
\iota=\frac{D^{\mathrm{bid}}-D^{\mathrm{ask}}}{D^{\mathrm{bid}}+D^{\mathrm{ask}}}
$$

**Queue virtual return** (can exceed official $\pm 3\%$): near the daily limit, one-sided heavy book adds a virtual boost so that

$$
r^{\mathrm{virt}} = r^{\mathrm{last}} + \delta_{\mathrm{queue}}\in [-8\%, +8\%].
$$

**Index points** (base $= 100$):

$$
I = 100\times\Bigl(1 + \sum_i w_i\, r_i^{\mathrm{est}}/100\Bigr),\quad
w_i = \alpha\,\frac{\mathrm{Cap}_i}{\sum\mathrm{Cap}} + (1-\alpha)\,\frac{V_i}{\sum V}.
$$

Data: prefer `market.sqlite` if age $\le 20$ min; outside session use today’s snapshot only (one fetch if not today).
            """
        )

    with t2:
        st.markdown("### Purchasing-power rebase (stock & TEDPIX)")
        st.markdown(
            r"""
**Pipeline order (important):**

1. Adjust USDT path by **US CPI** (USD purchasing power).
2. Build a deflator mixing **Iran CPI** (optional understatement boost $\beta\ge 1$) and adjusted FX.
3. Optional **bubble envelope** (Dey 98 → Mehr 99 shoulders).
4. Optional **recent liquidity envelope** (post-shock money printing; not the same as CPI).
5. **Then** rolling means / Bollinger / $z$-scores and buy–sell zones.

**Anchor:** last point of the rebased series equals **today’s nominal price** (or index level).

Deflator sketch:

$$
D_t = \bigl(w\, (\mathrm{CPI}^{\mathrm{IR}}_t/\mathrm{CPI}^{\mathrm{IR}}_T)^{\beta}
 + (1-w)\, f(\mathrm{FX}_t,\mathrm{CPI}^{\mathrm{US}})\bigr),\quad
S^{\mathrm{real}}_t = S_t / D_t.
$$

**Liquidity $\neq$ CPI:** excess M2 can lift assets before goods CPI fully reacts; the liquidity slider divides recent levels by a smooth ramp factor and re-anchors.
            """
        )

    with t3:
        st.markdown("### Covered call & BS-IR")
        st.markdown(
            r"""
**Covered-call score** blends liquidity, yield (after fees ~0.2% buy stock, ~0.85% sell), ITM% (negative if OTM), and DTM.

**Black–Scholes–IR**

$$
C = S e^{-qT}N(d_1) - K e^{-rT}N(d_2),\quad
d_{1,2}=\frac{\ln(S/K)+(r-q\pm\sigma^2/2)T}{\sigma\sqrt{T}}.
$$

Peer IV calibration on liquid contracts; **liquidity urgency discount**:

$$
P^{\mathrm{adj}} = P^{\mathrm{BS}}(1-\lambda),\quad
\lambda = \lambda_{\max}\, e^{-\mathrm{DTM}/\tau_{1/2}}\times\mathrm{boost}_{\mathrm{call}}.
$$

**Iranian tight fair band:** width of $[P_L,P_H]$ capped at about `iran_tight` (default **10%**) around the central model price so quotes are not unrealistically wide.

Cheap / expensive flags use the adjusted band (green / red).
            """
        )

    with t4:
        st.markdown("### Signals on adjusted vs nominal")
        st.markdown(
            r"""
Buy / sell markers are computed on the **rebased** series:

$$
z_t = \frac{S^{\mathrm{real}}_t - \mathrm{MA}_t}{\sigma_t}.
$$

- Buy if $z \le -z_{\mathrm{buy}}$ (appetite slider can ease this when “risk-on”).
- Sell if $z \ge +z_{\mathrm{sell}}$ (stricter when appetite is high).

A **second chart** plots the **raw rial / nominal TEDPIX** with the **same dates** marked, so you can audit what happened in nominal terms after a signal.

Bubble window (2019-12 → 2020-11): artificial buy marks on the adjusted path are suppressed.
            """
        )

    st.markdown("---")
    section("کیفیت پاسخ")

    diagnostics = snapshot.get("diagnostics") or {}
    try:
        eligible, _, _, _, coverage = pressure_summary(stocks)
    except Exception:
        eligible, coverage = [], np.nan

    a, b, c, d = st.columns(4)
    market_df = snapshot.get("market")
    a.metric("کل ابزارها", fmt(len(market_df) if market_df is not None else 0))
    b.metric("اختیارهای شناسایی‌شده", fmt(len(options) if options is not None else 0))
    c.metric("سهام واردشده به شاخص", fmt(len(eligible)))
    d.metric("پوشش شاخص", pct(coverage) if np.isfinite(coverage) else "—")

    with st.expander("Diagnostics JSON", expanded=False):
        st.json(diagnostics)

    src = snapshot.get("source") or "—"
    age = snapshot.get("age_minutes")
    st.caption(
        f"Source: `{src}`"
        + (f" · age {age:.1f} min" if isinstance(age, (int, float)) else "")
        + f" · DB: `{snapshot.get('db_path') or '—'}`"
    )

    st.info(
        "Snapshot time is not last-trade time. Auto-refresh may see unchanged "
        "payloads when the market is closed. Universe or coverage shifts also move the index."
    )

    a, b = st.columns(2)
    with a:
        market_df = snapshot.get("market")
    book_df = snapshot.get("book")
    download(
            market_df if market_df is not None else pd.DataFrame(),
            "دریافت کل بازار پردازش‌شده",
            "nova_full_market.csv",
            "download-full-market",
        )
    with b:
        download(
            book_df if book_df is not None else pd.DataFrame(),
            "دریافت ردیف‌های دفتر سفارش",
            "nova_orderbook.csv",
            "download-orderbook",
        )



def render_covered_call(market):
    import re
    import html
    import datetime as dt
    from zoneinfo import ZoneInfo

    import numpy as np
    import pandas as pd
    import jdatetime
    import streamlit as st
    import plotly.graph_objects as go

    # ========================================================
    # Helpers
    # ========================================================

    def clean(value):
        if value is None or pd.isna(value):
            return ""

        return (
            str(value)
            .translate(str.maketrans(
                "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                "01234567890123456789",
            ))
            .replace("ي", "ی")
            .replace("ك", "ک")
            .replace("\u200c", " ")
            .replace("\u200d", "")
            .strip()
        )

    def symbol_key(value):
        return re.sub(r"[\s_]+", "", clean(value))

    def num(value):
        try:
            return float(
                clean(value)
                .replace(",", "")
                .replace("٬", "")
                .replace("٫", ".")
            )
        except (ValueError, TypeError):
            return np.nan

    def positive(value):
        return np.isfinite(value) and value > 0

    def fmt(value, decimals=2):
        if not np.isfinite(value):
            return "—"
        return f"{value:,.{decimals}f}"

    def percent(value):
        return f"{fmt(value)}٪" if np.isfinite(value) else "—"

    def parse_contract(name):
        text = clean(name)

        pattern = (
            r"^اختیار\s*(?:خرید|خ)\s*"
            r"(?P<base>.+?)\s*[-–—]\s*"
            r"(?P<strike>[\d,٬]+)\s*[-–—]\s*"
            r"(?P<expiry>"
            r"(?:\d{4}|\d{2})[/.\-]\d{1,2}[/.\-]\d{1,2}"
            r"|\d{8}|\d{6}"
            r")\s*$"
        )

        match = re.fullmatch(pattern, text)

        if match is None:
            raise ValueError(
                "نام قرارداد برای استخراج پایه، اعمال و سررسید "
                "قابل تفسیر نیست."
            )

        strike = num(match.group("strike"))

        if not positive(strike):
            raise ValueError("قیمت اعمال نامعتبر است.")

        expiry_text = match.group("expiry")

        if expiry_text.isdigit():
            year_size = len(expiry_text) - 4
            year = int(expiry_text[:year_size])
            month = int(expiry_text[year_size:year_size + 2])
            day = int(expiry_text[-2:])
            short_year = year_size == 2
        else:
            parts = re.split(r"[/.\-]", expiry_text)
            year, month, day = map(int, parts)
            short_year = len(parts[0]) == 2

        if short_year:
            year += 1400

        if 1300 <= year <= 1599:
            expiry = jdatetime.date(
                year, month, day
            ).togregorian()
        elif 1900 <= year <= 2200:
            expiry = dt.date(year, month, day)
        else:
            raise ValueError("سال سررسید نامعتبر است.")

        return (
            match.group("base").strip(),
            strike,
            expiry,
            short_year,
        )

    with st.expander("کاوردکال یعنی چه؟", expanded=False):
        st.markdown(
            """
**کاوردکال** = خرید سهم پایه + فروش اختیار خرید روی همان سهم.

- **اگر سهم تا سررسید بالای اعمال بماند:** اختیار اعمال می‌شود؛ سهم را به قیمت اعمال تحویل می‌دهی و **حداکثر سود** را می‌گیری (پریمیوم + اختلاف تا اعمال، پس از کارمزد).
- **اگر سهم زیر اعمال بماند:** اختیار بی‌ارزش می‌شود؛ سهم می‌ماند و پریمیوم مال توست — ولی افت سهم زیان می‌سازد.
- **سربه‌سر** تقریباً نزدیک Last − پریمیوم (با کارمزد کمی بالاتر) است.

کارمزد تقریبی مدل: خرید سهم **۰٫۲٪** · فروش سهم **۰٫۳٪ + ۰٫۵۵٪ مالیات**.
            """
        )

    apply_fees = st.checkbox(
        "اعمال کارمزد و مالیات در بازده کاوردکال",
        value=True,
        key="cc_apply_fees",
        help="خرید سهم ≈۰٫۲٪ · فروش سهم ≈۰٫۳٪ + مالیات ≈۰٫۵۵٪",
    )
    fee_buy = 0.002
    fee_sell = 0.003 + 0.0055  # ۰٫۸۵٪ تقریبی خروج

    def calculate(stock_last, premium, strike, days):
        """
        بازده حداکثر کاوردکال وقتی در سررسید S≥K (اعمال می‌شود).
        با کارمزد: خرید سهم گران‌تر، دریافت K پس از کسر کارمزد/مالیات فروش.
        """
        result = {
            "net": np.nan,
            "profit": np.nan,
            "period": np.nan,
            "simple": np.nan,
            "compound": np.nan,
        }

        if (
            not positive(stock_last)
            or not positive(premium)
            or not positive(strike)
            or days <= 0
        ):
            return result

        if apply_fees:
            cost = stock_last * (1.0 + fee_buy) - premium
            proceeds = strike * (1.0 - fee_sell)
        else:
            cost = stock_last - premium
            proceeds = strike

        if cost <= 0:
            return result

        profit = proceeds - cost
        period_fraction = profit / cost

        with np.errstate(over="ignore", invalid="ignore"):
            compound = np.expm1(
                np.log1p(period_fraction) * 365.0 / days
            ) * 100

        return {
            "net": cost,
            "profit": profit,
            "period": period_fraction * 100,
            "simple": period_fraction * 365.0 / days * 100,
            "compound": (
                float(compound)
                if np.isfinite(compound)
                else np.nan
            ),
        }

    def relative_score(series):
        """
        امتیاز رتبه‌ای نسبی در بین قراردادهای واجد شرایط.
        برای داده‌های یکسان امتیاز خنثی می‌دهد.
        """
        result = pd.Series(np.nan, index=series.index)
        valid = series.dropna()

        if valid.empty:
            return result

        if len(valid) == 1 or valid.nunique() == 1:
            result.loc[valid.index] = 50.0
        else:
            ranks = valid.rank(method="average")
            result.loc[valid.index] = (
                (ranks - ranks.min())
                / (ranks.max() - ranks.min())
                * 100
            )

        return result

    # ========================================================
    # Appearance
    # ========================================================

    st.markdown(
        """
        <style>
        .cc-hero {
            direction: rtl;
            padding: 24px 26px;
            border: 1px solid rgba(45,212,191,.25);
            border-radius: 22px;
            background:
                radial-gradient(
                    circle at 100% 0%,
                    rgba(45,212,191,.18),
                    transparent 50%
                ),
                linear-gradient(130deg, #101827, #172339);
            margin-bottom: 18px;
            color: #e5edf8;
        }
        .cc-hero h2 {
            margin: 0 0 10px;
            color: #f8fafc;
            font-size: 26px;
        }
        .cc-hero p {
            margin: 0;
            color: #b8c9dd;
            line-height: 1.9;
        }
        .cc-card {
            direction: rtl;
            border-radius: 18px;
            border: 1px solid rgba(148,163,184,.25);
            background: #111c2e;
            padding: 18px;
            margin-bottom: 12px;
            min-height: 250px;
            color: #dce6f3;
        }
        .cc-card-title {
            font-size: 15px;
            font-weight: 700;
            margin-bottom: 12px;
        }
        .cc-card-value {
            font-size: 29px;
            font-weight: 800;
            margin: 8px 0;
        }
        .cc-card-note {
            color: #9aadc3;
            font-size: 12px;
            line-height: 1.9;
        }
        </style>

        <div class="cc-hero">
            <h2>◈ رادار کاوردکال</h2>
            <p>
                مقایسه سه سناریوی ورود · نقدشوندگی معاملات ·
                فاصله تا سررسید · بازده ناخالص
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ========================================================
    # Validate input
    # ========================================================

    required = {
        "id", "symbol", "name",
        "last_trade", "best_bid", "best_ask",
    }

    missing = sorted(required - set(market.columns))

    if missing:
        st.error(
            "ستون‌های لازم موجود نیستند: " + ", ".join(missing)
        )
        return

    data = market.copy()
    data["id"] = data["id"].astype(str)
    data["_name"] = data["name"].map(clean)
    data["_symbol_key"] = data["symbol"].map(symbol_key)

    # ارزش معاملات خود اختیار؛ نه ارزش معاملات دارایی پایه.
    # فرض: مقدار این ستون در دیتای بازار بر حسب ریال است.
    value_column = next(
        (
            column
            for column in ["value", "trade_value"]
            if column in data.columns
        ),
        None,
    )

    if value_column is None:
        st.warning(
            "ستون value یا trade_value موجود نیست؛ "
            "ارزش معاملات نمایش داده نمی‌شود و امتیاز فرصت "
            "تا زمان تأمین این داده محاسبه نخواهد شد."
        )

    by_name_call = data["_name"].str.match(
        r"^اختیار\s*(?:خرید|خ(?=\s))",
        na=False,
    )

    by_name_put = data["_name"].str.match(
        r"^اختیار\s*(?:فروش|ف(?=\s))",
        na=False,
    )

    by_type_call = pd.Series(False, index=data.index)
    by_type_put = pd.Series(False, index=data.index)

    if "kind" in data.columns:
        by_type_call |= data["kind"].eq("اختیار خرید")
        by_type_put |= data["kind"].eq("اختیار فروش")

    if "type_of_asset" in data.columns:
        by_type_call |= data["type_of_asset"].isin([
            "calloption", "IFB_calloptions", "IFB_calloption",
        ])

        by_type_put |= data["type_of_asset"].isin([
            "putoption", "IFB_putoption", "IFB_putoptions",
        ])

    is_call = (by_name_call | by_type_call) & ~by_name_put
    is_option = (
        by_name_call | by_name_put | by_type_call | by_type_put
    )

    calls = data.loc[is_call].drop_duplicates("id").copy()

    # صندوق‌ها، از جمله اهرم و جوانه کوچک، حذف نمی‌شوند.
    bases = data.loc[~is_option].drop_duplicates("id").copy()
    bases["_alias_key"] = bases["_symbol_key"].map(
        lambda k: symbol_key(BASE_ALIASES.get(k, k))
    )

    today = dt.datetime.now(
        ZoneInfo("Asia/Tehran")
    ).date()

    def resolve_base_key(name: str) -> str:
        key = symbol_key(name)
        # نگاشت مستقیم یا پس از حذف فاصله/نقطه
        mapped = BASE_ALIASES.get(key) or BASE_ALIASES.get(
            symbol_key(key)
        )
        return symbol_key(mapped) if mapped else key

    # ========================================================
    # Build three scenarios
    # ========================================================

    records = []
    rejected = []

    for _, call in calls.iterrows():
        try:
            base_name, strike, expiry, short_year = parse_contract(
                call["name"]
            )

            target_key = resolve_base_key(base_name)
            matches = bases.loc[
                bases["_symbol_key"].eq(target_key)
                | bases["_alias_key"].eq(target_key)
                | bases["_symbol_key"].eq(symbol_key(base_name))
            ]

            if len(matches) != 1:
                raise ValueError(
                    f"پایه «{base_name}» به یک نماد یکتا تطبیق نخورد."
                )

            base = matches.iloc[0]
            days = (expiry - today).days

            if days <= 0:
                raise ValueError(
                    "سررسید گذشته یا امروز است."
                )

            stock_last = num(base["last_trade"])

            if not positive(stock_last):
                raise ValueError(
                    "آخرین معامله پایه معتبر نیست؛ "
                    "طبق مدل درخواستی، پایانی جایگزین Last نمی‌شود."
                )

            premiums = {
                "ask": num(call["best_ask"]),
                "bid": num(call["best_bid"]),
                "last": num(call["last_trade"]),
            }

            crossed = (
                positive(premiums["ask"])
                and positive(premiums["bid"])
                and premiums["bid"] > premiums["ask"]
            )

            if crossed:
                raise ValueError("دفتر سفارش اختیار متقاطع است.")

            value = (
                num(call[value_column])
                if value_column is not None
                else np.nan
            )

            if np.isfinite(value) and value < 0:
                value = np.nan

            row = {
                "id": call["id"],
                "call_symbol": call["symbol"],
                "call_name": call["name"],
                "base_symbol": base["symbol"],
                "base_id": base["id"],
                "base_name": base["name"],
                "strike": strike,
                "stock_last": stock_last,
                "expiry": expiry,
                "expiry_jalali": (
                    jdatetime.date.fromgregorian(date=expiry)
                    .strftime("%Y/%m/%d")
                ),
                "short_year": short_year,
                "dtm": days,
                "value_rial": value,

                # ۱ میلیون تومان = ۱۰ میلیون ریال
                "value_million_toman": value / 1e7,

                "status": (
                    "ITM" if stock_last > strike
                    else "OTM" if stock_last < strike
                    else "ATM"
                ),
                # (S−K)/S×100 :
                # ITM > 0 = درصد ریزش لازم تا K
                # OTM < 0 = درصد رشد لازم از امروز تا رسیدن به K (منفی نمایش داده می‌شود)
                # سقف +۱۰۰٪ وقتی K→0
                "itm_pct": (
                    float(min(
                        100.0,
                        (stock_last - strike) / stock_last * 100.0,
                    ))
                    if positive(stock_last) and positive(strike)
                    else np.nan
                ),
            }

            for mode, premium in premiums.items():
                row[f"{mode}_premium"] = premium

                result = calculate(
                    stock_last, premium, strike, days
                )

                for field, value_result in result.items():
                    row[f"{mode}_{field}"] = value_result

            if all(
                pd.isna(row[f"{mode}_period"])
                for mode in premiums
            ):
                raise ValueError(
                    "هیچ سناریوی دارای پریمیوم و سرمایه خالص "
                    "معتبر موجود نیست."
                )

            records.append(row)

        except Exception as exc:
            rejected.append({
                "نماد": call["symbol"],
                "نام": call["name"],
                "علت": str(exc),
            })

    chain = pd.DataFrame(records)

    if chain.empty:
        st.info("قرارداد قابل محاسبه‌ای موجود نیست.")

        if rejected:
            st.dataframe(
                pd.DataFrame(rejected),
                hide_index=True,
                use_container_width=True,
            )
        return

    # ========================================================
    # Opportunity settings
    # ========================================================

    with st.expander("⚙ تنظیم معیار فرصت و نقدشوندگی", expanded=False):
        a, b, c = st.columns(3)

        min_days = int(a.number_input(
            "حداقل DTM برای دریافت امتیاز",
            min_value=1,
            max_value=365,
            value=21,
            key="cc_min_days_v3",
        ))

        preferred_days = b.slider(
            "بازه مطلوب سررسید · روز",
            min_value=1,
            max_value=365,
            value=(45, 120),
            key="cc_preferred_days_v3",
        )

        min_value_m = c.number_input(
            "حداقل ارزش معاملات اختیار · میلیون تومان",
            min_value=0.0,
            value=100.0,
            step=25.0,
            key="cc_min_value_v3",
        )

        st.caption(
            "آستانه‌های اولیه صرفاً تنظیم پیشنهادی دیده‌بان‌اند، "
            "نه معیار قطعی مناسب‌بودن معامله. ارزش معاملات از snapshot "
            "فعلی گرفته می‌شود؛ اگر منبع مقدار روزانه تجمعی می‌دهد، "
            "این آستانه در ابتدای بازار سخت‌گیرانه‌تر خواهد بود."
        )

    # ========================================================
    # Opportunity score
    #   30% ارزش معاملات · 25% عمق ITM · 25% DTM · 20% بازده Bid
    # ========================================================

    chain["eligible"] = (
        chain["bid_period"].notna()
        & chain["bid_period"].gt(0)
        & chain["dtm"].ge(min_days)
        & chain["value_million_toman"].notna()
        & chain["value_million_toman"].gt(0)
        & chain["value_million_toman"].ge(min_value_m)
    )

    low, high = preferred_days
    days_array = chain["dtm"].astype(float)
    chain["maturity_fit"] = np.where(
        days_array < low,
        days_array / max(low, 1) * 100,
        np.where(
            days_array > high,
            high / days_array * 100,
            100.0,
        ),
    )

    chain["score"] = np.nan
    eligible_index = chain.index[chain["eligible"]]

    if len(eligible_index):
        liquidity_score = relative_score(
            chain.loc[eligible_index, "value_million_toman"]
        )
        itm_score = relative_score(
            chain.loc[eligible_index, "itm_pct"]
        )
        dtm_score = relative_score(
            chain.loc[eligible_index, "dtm"]
        )
        yield_score = relative_score(
            chain.loc[eligible_index, "bid_simple"]
        )
        chain.loc[eligible_index, "score"] = (
            0.30 * liquidity_score
            + 0.25 * itm_score
            + 0.25 * dtm_score
            + 0.20 * yield_score
        ).round(1)

    def qualification_reason(row):
        reasons = []

        if pd.isna(row["bid_period"]):
            reasons.append("Bid نامعتبر")
        elif row["bid_period"] <= 0:
            reasons.append("سود سقف غیرمثبت")

        if row["dtm"] < min_days:
            reasons.append("سررسید نزدیک")

        if pd.isna(row["value_million_toman"]):
            reasons.append("ارزش نامشخص")
        elif (
            row["value_million_toman"] <= 0
            or row["value_million_toman"] < min_value_m
        ):
            reasons.append("کم‌معامله")

        return " · ".join(reasons) if reasons else "واجد شرایط"

    chain["qualification"] = chain.apply(
        qualification_reason,
        axis=1,
    )

    a, b, c, d = st.columns(4)

    a.metric("قرارداد قابل بررسی", len(chain))
    b.metric("واجد شرایط امتیاز", int(chain["eligible"].sum()))
    c.metric(
        "بیشترین امتیاز",
        fmt(chain["score"].max(), 1),
    )
    d.metric(
        "مجموع ارزش اختیارهای جدول · میلیارد تومان",
        fmt(chain["value_rial"].sum(min_count=1) / 1e10),
    )


    # --- برآورد نرخ بدون‌ریسک از کاوردکال‌های عمیق ITM ---
    try:
        deep = chain[
            chain["itm_pct"].notna()
            & chain["itm_pct"].ge(8.0)
            & chain["bid_simple"].notna()
            & chain["bid_simple"].gt(0)
            & chain["value_rial"].notna()
            & chain["value_rial"].gt(0)
            & chain["dtm"].notna()
            & chain["dtm"].gt(3)
        ].copy()
        if len(deep) >= 3:
            w = deep["value_rial"].astype(float)
            rf = float(np.average(deep["bid_simple"].astype(float), weights=w))
            st.metric(
                "برآورد نرخ بدون‌ریسک بازار (وزنی ارزش · ITM≥۸٪)",
                f"{rf:.1f}٪ سالانه",
            )
            st.caption(
                f"از {len(deep)} قرارداد ITM نقد · تقریب free-risk معاملاتی نه نرخ رسمی."
            )
    except Exception:
        pass

    st.caption(
        "امتیاز فرصت: ۳۰٪ ارزش معاملات + ۲۵٪ عمق ITM "
        "(مثبت‌تر بهتر) + ۲۵٪ DTM (دورتر بهتر) + ۲۰٪ بازده Bid. "
        "ITM/OTM ٪ = (Last پایه − اعمال) / Last پایه × ۱۰۰ (مثبت=ITM، منفی=OTM). "
        "این امتیاز نسبی است، نه توصیه خرید."
    )

    # ========================================================
    # Filters and ordering
    # ========================================================

    a, b, c = st.columns([2, 1.4, 1.2])

    search = a.text_input(
        "جست‌وجوی اختیار یا پایه",
        placeholder="مثلاً ضهرم یا اهرم",
        key="cc_search_v3",
    )

    sort_label = b.selectbox(
        "مرتب‌سازی",
        [
            "امتیاز فرصت",
            "ارزش معاملات",
            "عمق ITM بیشتر",
            "DTM بیشتر",
            "بازده دوره Bid",
            "سالانه ساده Bid",
            "DTM کمتر",
        ],
        key="cc_sort_v3",
    )

    only_eligible = c.checkbox(
        "فقط واجد شرایط",
        value=False,
        key="cc_only_eligible_v3",
    )

    visible = chain.copy()

    if search.strip():
        query = symbol_key(search)

        visible = visible.loc[
            visible["call_symbol"].map(symbol_key).str.contains(
                query, regex=False
            )
            | visible["base_symbol"].map(symbol_key).str.contains(
                query, regex=False
            )
        ]

    if only_eligible:
        visible = visible.loc[visible["eligible"]]

    sort_columns = {
        "امتیاز فرصت": ("score", False),
        "ارزش معاملات": ("value_million_toman", False),
        "ITM بیشتر (OTM منفی):": ("itm_pct", False),
        "DTM بیشتر": ("dtm", False),
        "بازده دوره Bid": ("bid_period", False),
        "سالانه ساده Bid": ("bid_simple", False),
        "DTM کمتر": ("dtm", True),
    }

    sort_column, ascending = sort_columns[sort_label]

    visible = visible.sort_values(
        [sort_column, "id"],
        ascending=[ascending, True],
        na_position="last",
    ).reset_index(drop=True)

    if visible.empty:
        st.info("قراردادی با فیلتر فعلی پیدا نشد.")
        return

    # ========================================================
    # Interactive styled table
    # ========================================================

    labels = {
        "call_symbol": "اختیار",
        "base_symbol": "پایه",
        "score": "امتیاز",
        "qualification": "ارزیابی",
        "value_million_toman": "ارزش · م‌تومان",
        "itm_pct": "ITM/OTM ٪ (+ITM / −OTM)",
        "dtm": "DTM",
        "status": "وضعیت",
        "bid_period": "دوره Bid ٪",
        "ask_period": "دوره Ask ٪",
        "last_period": "دوره Last ٪",
        "bid_simple": "سالانه ساده Bid ٪",
        "expiry_jalali": "سررسید",
    }

    table = visible[list(labels)].rename(columns=labels)

    def style_row(row):
        source = visible.loc[row.name]
        score = source["score"]

        if pd.isna(score):
            background = "rgba(148,163,184,0.045)"
        elif score >= 75:
            background = "rgba(16,185,129,0.23)"
        elif score >= 55:
            background = "rgba(20,184,166,0.14)"
        else:
            background = "rgba(59,130,246,0.09)"

        styles = [
            f"background-color: {background};"
            for _ in row.index
        ]

        for column in [
            "دوره Bid ٪", "دوره Ask ٪",
            "دوره Last ٪", "سالانه ساده Bid ٪",
            "ITM/OTM ٪",
        ]:
            if column not in row.index:
                continue
            index = row.index.get_loc(column)
            value = row[column]
            if pd.notna(value):
                color = "#10b981" if value > 0 else "#f97373"
                styles[index] += (
                    f"color: {color}; font-weight: 700;"
                )

        styles[row.index.get_loc("اختیار")] += (
            "font-weight: 800; color: #38bdf8;"
        )
        return styles

    styled = table.style.apply(style_row, axis=1).format(
        {
            "امتیاز": "{:.1f}",
            "ارزش · م‌تومان": "{:,.1f}",
            "ITM/OTM ٪": "{:+.2f}%",
            "DTM": "{:.0f}",
            "دوره Bid ٪": "{:.2f}",
            "دوره Ask ٪": "{:.2f}",
            "دوره Last ٪": "{:.2f}",
            "سالانه ساده Bid ٪": "{:.2f}",
        },
        na_rep="—",
    )

    # حفظ انتخاب بر اساس شناسه، نه شماره ردیف.
    selected_key = "cc_selected_contract_v3"
    table_key = "cc_interactive_table_v3"
    mapping_key = "cc_visible_ids_v3"

    visible_ids = visible["id"].tolist()

    if st.session_state.get(selected_key) not in visible_ids:
        st.session_state[selected_key] = visible_ids[0]

    def on_contract_selected():
        state = st.session_state.get(table_key, {})
        selection = state.get("selection", {})
        selected_rows = selection.get("rows", [])
        displayed_ids = st.session_state.get(mapping_key, [])

        if selected_rows:
            position = selected_rows[0]

            if 0 <= position < len(displayed_ids):
                st.session_state[selected_key] = displayed_ids[position]

    st.session_state[mapping_key] = visible_ids

    st.caption(
        "برای تغییر پرونده پایین، ردیف نماد موردنظر را انتخاب کن. "
        "م‌تومان یعنی میلیون تومان. بازده‌های جدول، حداکثر بازده "
        "ناخالص تا سررسید بر سرمایه خالص هستند."
    )

    st.dataframe(
        styled,
        hide_index=True,
        use_container_width=True,
        height=min(570, 40 + 36 * len(table)),
        selection_mode="single-row",
        on_select=on_contract_selected,
        key=table_key,
        column_config={
            "اختیار": st.column_config.TextColumn(
                "اختیار",
                width="small",
                help="ردیف این قرارداد را برای تحلیل انتخاب کن.",
            ),
            "ارزیابی": st.column_config.TextColumn(
                "ارزیابی",
                width="medium",
            ),
            "امتیاز": st.column_config.NumberColumn(
                "امتیاز",
                help="امتیاز نسبی فرصت بین صفر و صد.",
            ),
            "ارزش · م‌تومان": st.column_config.NumberColumn(
                "ارزش · م‌تومان",
                help="ارزش معاملات خود اختیار در snapshot فعلی.",
            ),
        },
    )

    # ========================================================
    # Selected contract
    # ========================================================

    selected_id = st.session_state[selected_key]

    selected = chain.loc[
        chain["id"].eq(selected_id)
    ].iloc[0]

    symbol = str(selected["call_symbol"])
    base_symbol = str(selected["base_symbol"])

    S0 = float(selected["stock_last"])
    K = float(selected["strike"])
    days = int(selected["dtm"])

    st.markdown(
        f"""
        <div class="cc-hero">
            <h2>{html.escape(symbol)} · {html.escape(base_symbol)}</h2>
            <p>
                سررسید {html.escape(selected["expiry_jalali"])}
                &nbsp; | &nbsp; {days} روز
                &nbsp; | &nbsp; {html.escape(selected["status"])}
                &nbsp; | &nbsp;
                {html.escape(selected["qualification"])}
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    a, b, c, d = st.columns(4)

    a.metric("Last پایه · ریال", fmt(S0, 0))
    b.metric("اعمال · ریال", fmt(K, 0))
    c.metric(
        "ارزش معاملات اختیار · میلیون تومان",
        fmt(selected["value_million_toman"], 1),
    )
    d.metric(
        "امتیاز فرصت",
        fmt(selected["score"], 1),
    )

    # ---- صف خرید + دستور کار شفاف ----
    buy_queue = False
    queue_note = ""
    try:
        bases_match = data.loc[
            data["_symbol_key"].eq(symbol_key(base_symbol))
        ]
        if bases_match.empty:
            bases_match = data.loc[
                data["symbol"].astype(str).str.contains(
                    str(base_symbol), regex=False, na=False
                )
            ]
        if not bases_match.empty:
            stock_row = bases_match.iloc[0]
            last_p = float(
                stock_row.get("last_trade")
                or stock_row.get("close_price")
                or S0
                or 0
            )
            max_p = float(stock_row.get("max_allowed_price") or 0)
            best_bid = float(stock_row.get("best_bid") or 0)
            best_ask = float(stock_row.get("best_ask") or 0)
            if max_p > 0 and last_p >= max_p * 0.998:
                buy_queue = True
                queue_note = (
                    f"Last پایه نزدیک سقف مجاز ({fmt(max_p, 0)} ریال) است."
                )
            if max_p > 0 and best_bid >= max_p * 0.998:
                buy_queue = True
                queue_note = (
                    queue_note
                    or f"بهترین Bid نزدیک سقف ({fmt(max_p, 0)}) — نشانه صف خرید."
                )
            if best_ask <= 0 and best_bid > 0 and max_p > 0 and best_bid >= max_p * 0.995:
                buy_queue = True
                queue_note = queue_note or "دفتر بدون Ask و Bid نزدیک سقف."
    except Exception:
        pass

    prem_bid = selected.get("bid_premium")
    prem_last = selected.get("last_premium")
    prem_ask = selected.get("ask_premium")
    sell_px = prem_bid if pd.notna(prem_bid) and float(prem_bid) > 0 else (
        prem_last if pd.notna(prem_last) else prem_ask
    )
    period_r = selected.get("bid_period")
    annual_r = selected.get("bid_simple")
    if pd.isna(period_r):
        period_r = selected.get("last_period")
        annual_r = selected.get("last_simple")
    if pd.isna(period_r):
        period_r = selected.get("ask_period")
        annual_r = selected.get("ask_simple")

    direction, slope, last_pts, trend_msg = index_intraday_trend("combined")

    st.markdown("#### دستور کار اجرای کاوردکال")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(
            f"""
            <div style="padding:14px;border-radius:14px;background:rgba(96,165,250,.14);
            border:1px solid rgba(96,165,250,.4);min-height:140px;">
            <div style="color:#93C5FD;font-size:12px;">① خرید سهم پایه</div>
            <div style="font-size:22px;font-weight:800;color:#E0F2FE;">{html.escape(base_symbol)}</div>
            <div style="font-size:20px;font-weight:700;color:#7DD3FC;">≈ {fmt(S0, 0)} ریال</div>
            <div style="color:#64748B;font-size:11px;">مرجع: Last پایه</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"""
            <div style="padding:14px;border-radius:14px;background:rgba(167,139,250,.14);
            border:1px solid rgba(167,139,250,.4);min-height:140px;">
            <div style="color:#C4B5FD;font-size:12px;">② فروش اختیار خرید</div>
            <div style="font-size:22px;font-weight:800;color:#EDE9FE;">{html.escape(symbol)}</div>
            <div style="font-size:20px;font-weight:700;color:#DDD6FE;">≈ {fmt(sell_px, 0) if pd.notna(sell_px) else "—"} ریال</div>
            <div style="color:#64748B;font-size:11px;">ترجیح: قیمت Bid اختیار</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"""
            <div style="padding:14px;border-radius:14px;background:rgba(45,212,191,.12);
            border:1px solid rgba(45,212,191,.4);min-height:140px;">
            <div style="color:#5EEAD4;font-size:12px;">③ اگر تا سررسید S ≥ K</div>
            <div style="margin-top:8px;color:#99F6E4;">بازده دوره: <b style="color:#2DD4BF;font-size:18px;">{percent(period_r)}</b></div>
            <div style="color:#FDE68A;">معادل سالانه: <b style="color:#FBBF24;font-size:18px;">{percent(annual_r)}</b></div>
            <div style="color:#64748B;font-size:11px;margin-top:6px;">اعمال {fmt(K, 0)} · {days} روز</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if buy_queue:
        st.error(
            "⚠ **صف خرید / نزدیک سقف مجاز** — " + (queue_note or "مراقب خرید پایه باش.")
        )
    if direction == "up":
        st.success(
            "🟢 شاخص لحظه‌ای **صعودی** است → **سریع سهم را بخر**، سپس فروش اختیار را تکمیل کن. "
            + trend_msg
        )
    elif direction == "down":
        st.warning(
            "🔴 شاخص لحظه‌ای **نزولی** است → **اول اختیار خرید را بفروش** و در خرید سهم عجله نکن. "
            + trend_msg
        )
    else:
        st.info("شاخص خنثی — " + trend_msg + " · ترتیب معمول: خرید پایه سپس فروش اختیار.")

    # ========================================================
    # Three scenario cards
    # ========================================================

    mode_info = {
        "ask": (
            "Ask اختیار + Last پایه",
            "#a78bfa",
            "فروش روی قیمت پیشنهادی فروش؛ نیازمند خریدار مقابل است.",
        ),
        "bid": (
            "Bid اختیار + Last پایه",
            "#2dd4bf",
            "فروش به بهترین خرید؛ حجم و تازگی مظنه باید بررسی شود.",
        ),
        "last": (
            "Last اختیار + Last پایه",
            "#60a5fa",
            "قیمت معاملات قبلی؛ ممکن است دو Last هم‌زمان نباشند.",
        ),
    }

    columns = st.columns(3)

    for column, mode in zip(columns, ["ask", "bid", "last"]):
        title, color, note = mode_info[mode]

        with column:
            st.markdown(
                f"""
                <div class="cc-card" style="border-top:3px solid {color}">
                    <div class="cc-card-title" style="color:{color}">
                        {title}
                    </div>
                    <div class="cc-card-note">حداکثر بازده دوره</div>
                    <div class="cc-card-value" style="color:{color}">
                        {percent(selected[f"{mode}_period"])}
                    </div>
                    <div>
                        سالانه ساده:
                        <b>{percent(selected[f"{mode}_simple"])}</b>
                    </div>
                    <div>
                        پریمیوم:
                        <b>{fmt(selected[f"{mode}_premium"], 0)}</b>
                        ریال
                    </div>
                    <div class="cc-card-note" style="margin-top:12px">
                        {note}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.caption(
        "در هر سه مدل، خرید پایه با Last فرض شده است؛ "
        "Last قیمت پیشنهادی فروش نیست و ممکن است خرید با آن ممکن نباشد. "
        "Ask معمولاً بازده ظاهری بالاتری می‌دهد، اما اجرای فروش روی آن "
        "تضمین‌شده نیست."
    )
    # توضیح حداکثر سود
    best_mode = "bid"
    for m in ("bid", "last", "ask"):
        if pd.notna(selected.get(f"{m}_period")):
            best_mode = m
            break
    per = selected.get(f"{best_mode}_period")
    ann = selected.get(f"{best_mode}_simple")
    if pd.notna(per):
        fee_note = "با احتساب کارمزد/مالیات" if apply_fees else "بدون کارمزد"
        st.info(
            f"اگر تا سررسید قیمت سهم **بالای اعمال ({fmt(K, 0)} ریال)** بماند، "
            f"سود حداکثری دوره حدود **{percent(per)}** است "
            f"(سالانه ساده ≈ **{percent(ann)}**) — {fee_note}. "
            "در ناحیه S≥K منحنی P&L افقی می‌شود (سقف سود)."
        )

    # ========================================================
    # Full comparison
    # ========================================================

    comparison_rows = []

    for mode in ["ask", "bid", "last"]:
        comparison_rows.append({
            "مدل": mode_info[mode][0],
            "پریمیوم · ریال": selected[f"{mode}_premium"],
            "سرمایه خالص / سربه‌سر · ریال": selected[f"{mode}_net"],
            "حداکثر سود · ریال/واحد": selected[f"{mode}_profit"],
            "حداکثر بازده دوره ٪": selected[f"{mode}_period"],
            "سالانه ساده ٪": selected[f"{mode}_simple"],
            "سالانه مرکب ٪": selected[f"{mode}_compound"],
        })

    with st.expander("مقایسه کامل بازده، سرمایه و سربه‌سر"):
        st.dataframe(
            pd.DataFrame(comparison_rows).round(2),
            hide_index=True,
            use_container_width=True,
        )

        st.caption(
            "سرمایه خالص ≈ Last×(۱+کارمزدخرید) − پریمیوم. "
            "حداکثر دریافتی ≈ اعمال×(۱−کارمزدفروش/مالیات). "
            "سالانه ساده = بازده دوره × ۳۶۵ / DTM. "
            "سالانه مرکب = (۱ + بازده دوره)^(۳۶۵ / DTM) − ۱. "
            "سالانه‌سازی مرکب در DTM کوتاه ممکن است عدد بسیار بزرگ بدهد؛ "
            "این عدد YTM تضمین‌شده نیست."
        )

    # ========================================================
    # P&L comparison chart
    # ========================================================

    st.subheader("مسیر سود و زیان کاوردکال در سررسید")

    chart_type = st.radio(
        "مقیاس نمودار",
        ["بازده معادل سالانه ٪", "سود و زیان · ریال/واحد", "بازده دوره ٪"],
        horizontal=True,
        key="cc_chart_scale_v4",
    )

    break_even_points = [
        float(selected[f"{mode}_net"])
        for mode in mode_info
        if pd.notna(selected[f"{mode}_net"])
    ]

    x_max = max(S0, K) * 1.55
    terminal_prices = np.unique(np.concatenate([
        np.linspace(0, x_max, 500),
        [S0, K],
        break_even_points,
    ]))

    fig = go.Figure()

    # ناحیه سقف سود: S ≥ K
    fig.add_vrect(
        x0=K,
        x1=x_max,
        fillcolor="rgba(45,212,191,0.10)",
        line_width=0,
        annotation_text="سقف سود (S≥K)",
        annotation_position="top left",
        annotation_font_color="#2DD4BF",
    )
    fig.add_vrect(
        x0=0,
        x1=K,
        fillcolor="rgba(148,163,184,0.06)",
        line_width=0,
        annotation_text="زیر اعمال",
        annotation_position="top left",
        annotation_font_color="#94A3B8",
    )

    for mode in ["ask", "bid", "last"]:
        net = selected[f"{mode}_net"]
        if pd.isna(net):
            continue
        # P&L واحد: min(S,K) - net  (بدون کارمزد روی محور S؛ کارمزد در net/profit لحاظ شده)
        pnl = np.minimum(terminal_prices, K) - net
        period_frac = pnl / net
        if chart_type == "بازده معادل سالانه ٪":
            y = period_frac * 365.0 / max(days, 1) * 100.0
        elif chart_type == "بازده دوره ٪":
            y = period_frac * 100.0
        else:
            y = pnl
        fig.add_trace(go.Scatter(
            x=terminal_prices,
            y=y,
            name=mode.upper(),
            mode="lines",
            line=dict(color=mode_info[mode][1], width=3.2),
            hovertemplate=(
                "قیمت پایه در سررسید: %{x:,.0f}<br>"
                "مقدار: %{y:,.2f}<extra>%{fullData.name}</extra>"
            ),
        ))

    fig.add_vline(
        x=K,
        line_dash="dash",
        line_color="#FBBF24",
        annotation_text=f"اعمال {K:,.0f}",
    )
    fig.add_vline(
        x=S0,
        line_dash="dot",
        line_color="#60A5FA",
        annotation_text=f"Last امروز {S0:,.0f}",
    )
    fig.add_hline(y=0, line_color="rgba(148,163,184,0.5)", line_width=1)

    # خط افقی حداکثر بازده Bid
    if chart_type == "بازده معادل سالانه ٪" and pd.notna(selected.get("bid_simple")):
        fig.add_hline(
            y=float(selected["bid_simple"]),
            line_dash="dot",
            line_color="#2DD4BF",
            annotation_text=f"سقف سالانه Bid {float(selected['bid_simple']):.1f}٪",
            annotation_position="bottom right",
        )
    elif chart_type == "بازده دوره ٪" and pd.notna(selected.get("bid_period")):
        fig.add_hline(
            y=float(selected["bid_period"]),
            line_dash="dot",
            line_color="#2DD4BF",
            annotation_text=f"سقف دوره Bid {float(selected['bid_period']):.1f}٪",
            annotation_position="bottom right",
        )

    fig.update_layout(
        title="P&L کاوردکال در سررسید · ناحیه سبز = حداکثر سود",
        xaxis_title="قیمت سهم پایه در سررسید · ریال",
        yaxis_title=chart_type,
        hovermode="x unified",
        height=460,
        legend=dict(orientation="h", y=1.12),
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(17,27,45,.55)",
        font=dict(family="Vazirmatn, Tahoma, sans-serif"),
        margin=dict(l=20, r=20, t=50, b=40),
    )
    chart(fig, "cc-pnl-v4", 460)

    st.caption(
        "اگر قیمت سهم در سررسید **بالای اعمال** باشد، سود در سقف می‌ماند "
        f"(بازده دوره تا حدود {percent(selected.get('bid_period'))} در مدل Bid"
        + (f" · سالانه ساده {percent(selected.get('bid_simple'))}" if pd.notna(selected.get('bid_simple')) else "")
        + "). "
        + ("کارمزد خرید ۰٫۲٪ و فروش+مالیات ۰٫۸۵٪ در سرمایه خالص لحاظ شده." if apply_fees
           else "محاسبه بدون کارمزد است.")
    )

    if rejected:
        with st.expander(
            f"قراردادهای کنارگذاشته‌شده · {len(rejected)}"
        ):
            st.dataframe(
                pd.DataFrame(rejected),
                hide_index=True,
                use_container_width=True,
            )

    st.download_button(
        "↓ دریافت دیده‌بان و هر سه مدل محاسبه",
        data=chain.to_csv(index=False).encode("utf-8-sig"),
        file_name="covered_call_radar.csv",
        mime="text/csv",
        key="cc_download_v3",
    )


def index_intraday_trend(scope: str = "combined", min_points: int = 4):
    """جهت شاخص لحظه‌ای از اسنپ‌شات‌های امروز."""
    try:
        init_db()
        day = dt.datetime.now(TEHRAN).date().isoformat()
        records = read_history(day, scope) or read_history(day, "combined")
        if not records or len(records) < min_points:
            return "flat", 0.0, None, "اسنپ‌شات کافی برای تشخیص روند نیست"
        hist = pd.DataFrame(records)
        hist["fetched_at"] = pd.to_datetime(hist["fetched_at"], utc=True)
        hist = hist.sort_values("fetched_at")
        if "points" not in hist.columns or hist["points"].notna().sum() < min_points:
            return "flat", 0.0, None, "ستون points موجود نیست"
        y = hist["points"].astype(float).values
        t = hist["fetched_at"].astype("int64").values / 1e9 / 3600.0
        t = t - t[0]
        if t[-1] <= 0:
            return "flat", 0.0, float(y[-1]), "بازه زمانی صفر"
        slope = float(np.polyfit(t, y, 1)[0])
        last = float(y[-1])
        if slope >= 0.08:
            return "up", slope, last, f"صعودی · شیب ≈ {slope:+.3f} واحد/ساعت"
        if slope <= -0.08:
            return "down", slope, last, f"نزولی · شیب ≈ {slope:+.3f} واحد/ساعت"
        return "flat", slope, last, f"خنثی · شیب ≈ {slope:+.3f} واحد/ساعت"
    except Exception as exc:
        return "flat", 0.0, None, f"خطا در روند: {exc}"



def render_snapshot_observatory(key_prefix="obs"):
    """رصد اسنپ‌شات‌های market.sqlite: شاخص، اثر نمادها، ارزش معاملات."""
    init_db()
    st.subheader("رصدخانه اسنپ‌شات‌ها")
    st.caption(f"منبع: `{DB_PATH}`")

    dates = available_dates()
    if not dates:
        st.info("اسنپ‌شاتی نیست. collect_pressure را اجرا کن.")
        return

    day = st.selectbox(
        "روز",
        dates,
        format_func=jalali_day,
        key=f"{key_prefix}_day",
    )
    scope_label = st.selectbox(
        "سبد",
        list(INDEX_SCOPES) if INDEX_SCOPES else ["ترکیبی؛ سهام و صندوق"],
        key=f"{key_prefix}_scope",
    )
    scope = INDEX_SCOPES.get(scope_label, "combined")

    records = read_history(day, scope)
    if not records:
        # fallback combined
        records = read_history(day, "combined")
    if not records:
        st.warning("برای این روز دادهٔ شاخص نیست.")
        return

    hist = pd.DataFrame(records)
    hist["fetched_at"] = pd.to_datetime(hist["fetched_at"], utc=True).dt.tz_convert("Asia/Tehran")
    hist = hist.sort_values("fetched_at")

    # ---- مسیر شاخص ----
    st.markdown("#### مسیر شاخص در طول روز")
    fig = go.Figure()
    if "points" in hist.columns:
        fig.add_trace(go.Scatter(
            x=hist["fetched_at"], y=hist["points"],
            name="شاخص فشار/قیمت",
            line=dict(color="#2DD4BF", width=2.5),
            mode="lines+markers",
            marker=dict(size=5),
        ))
    if "last_return_pct" in hist.columns and hist["last_return_pct"].notna().any():
        # تبدیل بازده به عدد نمایه تقریبی 100*(1+r/100)
        idx_last = 100 * (1 + hist["last_return_pct"].astype(float) / 100.0)
        fig.add_trace(go.Scatter(
            x=hist["fetched_at"], y=idx_last,
            name="شاخص Last (۱۰۰×)",
            line=dict(color="#A78BFA", width=1.6, dash="dot"),
        ))
    direction, slope, last_pts, trend_msg = index_intraday_trend(scope)
    if direction == "up" and "points" in hist.columns:
        fig.add_annotation(
            x=hist["fetched_at"].iloc[-1], y=float(hist["points"].iloc[-1]),
            text="● صعودی", showarrow=True, arrowhead=2,
            font=dict(color="#6EE7B7", size=14),
            bgcolor="rgba(16,185,129,0.15)",
        )
        st.success(f"مسیر شاخص **صعودی** است — {trend_msg}")
    elif direction == "down" and "points" in hist.columns:
        fig.add_annotation(
            x=hist["fetched_at"].iloc[-1], y=float(hist["points"].iloc[-1]),
            text="● نزولی", showarrow=True, arrowhead=2,
            font=dict(color="#FCA5A5", size=14),
            bgcolor="rgba(239,68,68,0.15)",
        )
        st.error(f"مسیر شاخص **نزولی** است — {trend_msg}")
    else:
        st.info(f"مسیر شاخص خنثی — {trend_msg}")

    fig.update_layout(
        height=360, hovermode="x unified",
        yaxis_title="عدد شاخص",
        legend=dict(orientation="h", y=1.12),
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(17,27,45,.55)",
    )
    st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_idx")

    # ---- ارزش معاملات از raw هر اسنپ‌شات ----
    trade_rows = []
    contrib_frames = []
    for _, rec in hist.iterrows():
        sid = int(rec.get("snapshot_id") or rec.get("id") or 0)
        if not sid:
            continue
        packet = read_snapshot(sid)
        if not packet:
            continue
        raw = packet  # has details
        details = pd.DataFrame(packet.get("details") or [])
        # trade value from details or raw_json market
        tv = np.nan
        if not details.empty and "trade_value" in details.columns:
            tv = pd.to_numeric(details["trade_value"], errors="coerce").sum()
        else:
            try:
                with connection() as con:
                    row = con.execute(
                        "SELECT raw_json FROM snapshots WHERE id=?", (sid,)
                    ).fetchone()
                if row and row["raw_json"]:
                    rawd = decode(row["raw_json"])
                    if isinstance(rawd, dict):
                        mk = rawd.get("market") or []
                        vals = []
                        for item in mk:
                            k = item.get("kind")
                            if k in ("stock", "fund", "سهام", "صندوق", None):
                                v = item.get("value")
                                try:
                                    if v is not None:
                                        vals.append(float(v))
                                except (TypeError, ValueError):
                                    pass
                        if vals:
                            tv = float(np.nansum(vals))
            except Exception:
                pass
        trade_rows.append({
            "fetched_at": rec["fetched_at"],
            "snapshot_id": sid,
            "points": rec.get("points"),
            "trade_value_rial": tv,
            "trade_value_b_toman": (tv / 1e10) if pd.notna(tv) else np.nan,
        })
        if not details.empty:
            d = details.copy()
            d["snapshot_id"] = sid
            d["fetched_at"] = rec["fetched_at"]
            # normalize return
            for cand in ("return_pct", "model_return_pct", "anchor_return_pct"):
                if cand in d.columns:
                    d["ret"] = pd.to_numeric(d[cand], errors="coerce")
                    break
            else:
                d["ret"] = np.nan
            for cand in (f"weight_{scope}", "weight_combined", "weight", "weight_stock"):
                if cand in d.columns:
                    d["w"] = pd.to_numeric(d[cand], errors="coerce")
                    break
            else:
                d["w"] = np.nan
            d["contrib"] = d["w"].fillna(0) * d["ret"].fillna(0)
            if "symbol" in d.columns:
                contrib_frames.append(d)

    trade_df = pd.DataFrame(trade_rows)
    if not trade_df.empty:
        trade_df = trade_df.sort_values("fetched_at")
        trade_df["cum_trade_b"] = trade_df["trade_value_b_toman"].fillna(0).cumsum()

        st.markdown("#### ارزش معاملات در هر اسنپ‌شات و تجمعی")
        f2 = go.Figure()
        f2.add_trace(go.Bar(
            x=trade_df["fetched_at"],
            y=trade_df["trade_value_b_toman"],
            name="ارزش اسنپ‌شات · میلیارد تومان",
            marker_color="rgba(96,165,250,0.75)",
        ))
        f2.add_trace(go.Scatter(
            x=trade_df["fetched_at"],
            y=trade_df["cum_trade_b"],
            name="تجمعی از ابتدای روز",
            yaxis="y2",
            line=dict(color="#FBBF24", width=2.4),
        ))
        f2.update_layout(
            height=340, hovermode="x unified",
            yaxis=dict(title="میلیارد تومان / اسنپ‌شات"),
            yaxis2=dict(title="تجمعی", overlaying="y", side="right"),
            legend=dict(orientation="h", y=1.14),
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(17,27,45,.55)",
        )
        st.plotly_chart(f2, use_container_width=True, key=f"{key_prefix}_tv")
        a, b, c = st.columns(3)
        a.metric("آخرین ارزش اسنپ‌شات · میلیارد تومان",
                 f"{trade_df['trade_value_b_toman'].iloc[-1]:,.1f}"
                 if pd.notna(trade_df['trade_value_b_toman'].iloc[-1]) else "—")
        b.metric("تجمعی روز · میلیارد تومان",
                 f"{trade_df['cum_trade_b'].iloc[-1]:,.1f}")
        c.metric("تعداد اسنپ‌شات", f"{len(trade_df)}")

    # ---- اثر نمادها در آخرین اسنپ‌شات ----
    if contrib_frames:
        all_c = pd.concat(contrib_frames, ignore_index=True)
        latest_sid = int(hist.iloc[-1].get("snapshot_id") or hist.iloc[-1].get("id"))
        latest_d = all_c[all_c["snapshot_id"] == latest_sid].copy()
        if not latest_d.empty and "symbol" in latest_d.columns:
            st.markdown("#### بیشترین / کمترین اثر روی شاخص (آخرین اسنپ‌شات)")
            show = latest_d[["symbol", "ret", "w", "contrib"]].copy()
            if "name" in latest_d.columns:
                show.insert(1, "name", latest_d["name"])
            show = show.rename(columns={
                "symbol": "نماد", "name": "نام",
                "ret": "بازده ٪", "w": "وزن", "contrib": "اثر (pp)",
            })
            show = show.sort_values("اثر (pp)", ascending=False)
            top = pd.concat([show.head(12), show.tail(12)]).drop_duplicates()

            def _style_contrib(val):
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    return ""
                if v > 0.02:
                    return "background-color: rgba(16,185,129,0.25); color: #6EE7B7"
                if v < -0.02:
                    return "background-color: rgba(239,68,68,0.25); color: #FCA5A5"
                return ""

            sty = top.style.format({
                "بازده ٪": "{:+.2f}",
                "وزن": "{:.4f}",
                "اثر (pp)": "{:+.4f}",
            }, na_rep="—")
            if "اثر (pp)" in top.columns:
                sty = sty.map(_style_contrib, subset=["اثر (pp)"])
                if "بازده ٪" in top.columns:
                    sty = sty.map(_style_contrib, subset=["بازده ٪"])
            st.dataframe(sty, hide_index=True, use_container_width=True, height=420)

        # ---- رنج / حرکت نمادها در طول روز ----
        st.markdown("#### نمادهایی که امروز رنج یا حرکت قوی داشتند")
        if "symbol" in all_c.columns and "ret" in all_c.columns:
            g = all_c.groupby("symbol")["ret"].agg(["min", "max", "mean", "count"])
            g["range"] = g["max"] - g["min"]
            g = g[g["count"] >= max(2, min(5, len(hist) // 3))].copy()
            g = g.sort_values("range", ascending=False).head(20)
            g = g.reset_index()
            g.columns = ["نماد", "حداقل بازده ٪", "حداکثر بازده ٪", "میانگین ٪", "تعداد اسنپ‌شات", "دامنه ٪"]
            st.caption("دامنه = max−min بازده مدل در اسنپ‌شات‌های امروز؛ رنج بالا = نوسان زیاد همان روز.")
            st.dataframe(
                g.style.format({
                    "حداقل بازده ٪": "{:+.2f}",
                    "حداکثر بازده ٪": "{:+.2f}",
                    "میانگین ٪": "{:+.2f}",
                    "دامنه ٪": "{:.2f}",
                }, na_rep="—").background_gradient(
                    subset=["دامنه ٪"], cmap="YlOrRd"
                ),
                hide_index=True,
                use_container_width=True,
                height=380,
            )

            # sparkline-like: top 5 range symbols over time
            top_syms = g["نماد"].head(5).tolist()
            if top_syms:
                st.markdown("##### مسیر بازده چند نماد پرنوسان")
                f3 = go.Figure()
                palette = ["#2DD4BF", "#60A5FA", "#FBBF24", "#A78BFA", "#F472B6"]
                for i, sym in enumerate(top_syms):
                    sub = all_c[all_c["symbol"] == sym].sort_values("fetched_at")
                    f3.add_trace(go.Scatter(
                        x=sub["fetched_at"], y=sub["ret"],
                        name=sym, line=dict(color=palette[i % len(palette)], width=2),
                        mode="lines+markers", marker=dict(size=4),
                    ))
                f3.update_layout(
                    height=320, hovermode="x unified",
                    yaxis_title="بازده مدل ٪",
                    legend=dict(orientation="h", y=1.12),
                    template="plotly_dark",
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(17,27,45,.55)",
                )
                st.plotly_chart(f3, use_container_width=True, key=f"{key_prefix}_sym")

    st.caption(
        "اثر (pp) ≈ وزن × بازده مدل. "
        "اعداد از details ذخیره‌شده در market.sqlite می‌آیند."
    )



def dashboard():
    st.markdown(
        """
        <div class="hero">
            <div class="eyebrow">NOVA / MARKET OBSERVATORY</div>
            <h1>رصدخانه هوشمند بازار سرمایه · app10</h1>
            <p>
                فشار سفارش‌ها، جریان معاملات اختیار و ارزش ارزی سهام؛
                یک نمای یکپارچه از داده‌های واقعی بازار
            </p>
            <span class="badge">داده واقعی</span>
            <span class="badge">بدون ورود دستی اطلاعات</span>
            <span class="badge">یک‌فایلی · بدون عمده/بلوکی · تب سهم پایدار</span>
            <span class="badge">اسنپ‌شات: data/market.sqlite · آستانه ۲۰دقیقه</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        _db = _resolve_market_db()
        _ok = _Path(_db).is_file()
        if _ok:
            with connection() as _con:
                _n = _con.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()[0]
            st.success(
                f"اسنپ‌شات‌های collect_pressure خوانده می‌شود · `{_db}` · **{_n}** رکورد"
            )
        else:
            st.warning(
                f"فایل دیتابیس پیدا نشد: `{_db}` — "
                "streamlit را از همان پوشه‌ای که `market.sqlite` است اجرا کن، "
                "یا `export NOVA_MARKET_DB=/path/to/market.sqlite`"
            )
    except Exception as _e:
        st.warning(f"خطا در اتصال به دیتابیس اسنپ‌شات: {_e}")

    def _load_snapshot():
        stale = False
        try:
            snapshot = get_market_snapshot(prefer_collector_minutes=20.0)
            st.session_state["last_market_snapshot"] = snapshot
            st.session_state["last_market_stale"] = False
            age = snapshot.get("age_minutes")
            src = snapshot.get("source", "")
            if src == "collector_sqlite" and age is not None:
                st.caption(
                    f"منبع: collect_pressure / market.sqlite · سن داده {age:.1f} دقیقه (آستانه ۲۰)"
                )
            elif src == "live_tsetmc":
                ca = snapshot.get("collector_age_minutes")
                extra = f" (sqlite قدیمی‌تر از ۲۰ دقیقه: {ca:.0f}دقیقه)" if ca else ""
                st.caption(f"منبع: دریافت زنده TSETMC{extra}")
        except Exception as exc:
            snapshot = st.session_state.get("last_market_snapshot")
            if snapshot is None:
                st.error(f"دریافت بازار ناموفق بود: {exc}")
                st.info(
                    "اتصال شبکه یا market.sqlite را بررسی کن."
                )
                return None, True
            stale = True
            st.session_state["last_market_stale"] = True
            st.warning(
                "دریافت جدید ناموفق؛ آخرین snapshot موفق. "
                f"خطا: {exc}"
            )
        return snapshot, stale

    def _snapshot_caption(snapshot, stale):
        received_at = dt.datetime.fromisoformat(snapshot["received_at"])
        age_minutes = (now_tehran() - received_at).total_seconds() / 60
        status = "داده ذخیره‌شده نشست" if stale else "آخرین دریافت موفق"
        st.caption(
            f"{status} | {received_at:%Y-%m-%d %H:%M:%S} تهران"
            f" | سن snapshot: {age_minutes:.1f} دقیقه"
        )

    def _record_pressure(snapshot, stocks, stale):
        eligible, _, _, pressure, coverage = pressure_summary(stocks)
        history = st.session_state.setdefault("pressure_observations", [])
        sample_id = snapshot["received_at"]
        if (
            not stale
            and pd.notna(pressure)
            and (not history or history[-1]["time"] != sample_id)
        ):
            history.append(
                {
                    "time": sample_id,
                    "pressure": float(pressure),
                    "coverage_pct": float(coverage),
                    "symbols": len(eligible),
                }
            )
            del history[:-600]
        return history, pressure, eligible, coverage

    tabs = st.tabs(
        [
            "◈ نبض بازار",
            "⌁ اختیار معامله",
            "◉ کاوردکال",
            "$ سهم و ارزش ارزی",
            "◎ فشار و شاخص (sqlite)",
            "◎ روش محاسبه و کیفیت",
        ]
    )

    with tabs[0]:
        @st.fragment(run_every="60s")
        def tab_market():
            snapshot, stale = _load_snapshot()
            if snapshot is None:
                return
            _snapshot_caption(snapshot, stale)
            market = snapshot["market"].copy()
            stocks = market[market["kind"].eq("سهام")].copy()
            history, pressure, eligible, coverage = _record_pressure(
                snapshot, stocks, stale
            )
            fx_result, _ = load_fx_memo()
            fx_reference = np.nan
            if fx_result is not None and not fx_result["data"].empty:
                latest_fx = fx_result["data"].iloc[-1]
                fx_age = (
                    pd.Timestamp.now(tz="UTC") - latest_fx["available_at"]
                )
                if fx_age <= pd.Timedelta(days=MAX_FX_AGE_DAYS):
                    fx_reference = float(latest_fx["fx_rial"])
            stocks = stocks.copy()
            stocks["price_usdt"] = (
                stocks["close_price"].where(stocks["close_price"] > 0)
                / fx_reference
            )
            traded = stocks[
                stocks["volume"].gt(0) & stocks["return_pct"].notna()
            ]
            positive_share = 100 * safe_ratio(
                int(traded["return_pct"].gt(0).sum()),
                len(traded),
            )
            a, b, c, d = st.columns(4)
            tether_toman = (
                float(fx_reference) / 10.0
                if pd.notna(fx_reference) and float(fx_reference) > 0
                else np.nan
            )
            a.metric("سهام دریافت‌شده", fmt(len(stocks)))
            b.metric(
                "ارزش معاملات سهام · میلیارد تومان",
                fmt(total(stocks["value"]) / 1e10, 1),
            )
            c.metric(
                "سهم نمادهای مثبت از معامله‌شده‌ها",
                pct(positive_share),
            )
            d.metric(
                "تتر · تومان",
                fmt(tether_toman, 0) if pd.notna(tether_toman) else "—",
            )
            st.caption(
                "شاخص فشار/قیمت رسمی از تب «فشار و شاخص (sqlite)» و collector خوانده می‌شود؛ "
                "اینجا فقط نمای تابلو و تتر است."
            )
            st.write("")
            render_market(stocks, history)

        tab_market()

    with tabs[1]:
        @st.fragment(run_every="60s")
        def tab_options():
            snapshot, stale = _load_snapshot()
            if snapshot is None:
                return
            _snapshot_caption(snapshot, stale)
            market = snapshot["market"].copy()
            options = market[
                market["kind"].isin(["اختیار خرید", "اختیار فروش"])
            ].copy()
            render_options(options, market=market)

        tab_options()

    with tabs[2]:
        @st.fragment(run_every="60s")
        def tab_cc():
            snapshot, stale = _load_snapshot()
            if snapshot is None:
                return
            _snapshot_caption(snapshot, stale)
            render_covered_call(snapshot["market"].copy())

        tab_cc()

    with tabs[3]:
        @st.fragment
        def tab_stock():
            snapshot = st.session_state.get("last_market_snapshot")
            if snapshot is None:
                # یک‌بار بدون تایمر
                snapshot, _ = _load_snapshot()
            if snapshot is None:
                st.info("snapshot بازار هنوز در دسترس نیست.")
                return

            market = snapshot["market"].copy()
            equity_like = filter_equity_universe(market)

            fx = pd.DataFrame()
            fx_error = None
            failed_chunks = 0
            fx_reference = np.nan
            fx_result, fx_err = load_fx_memo()
            if fx_result is not None:
                fx = fx_result["data"]
                failed_chunks = fx_result.get("failed_chunks", 0)
                if not fx.empty:
                    latest_fx = fx.iloc[-1]
                    fx_age = (
                        pd.Timestamp.now(tz="UTC")
                        - latest_fx["available_at"]
                    )
                    if fx_age <= pd.Timedelta(days=MAX_FX_AGE_DAYS):
                        fx_reference = float(latest_fx["fx_rial"])
            if fx_err:
                fx_error = fx_err

            cpi, cpi_error = load_cpi_memo()

            st.caption(
                f"جهان نماد: {len(equity_like):,} سهم و صندوق سهامی "
                "(بدون درآمد ثابت) · تتر حداکثر هر ساعت · "
                "CPI یک‌بار در نشست · بدون رفرش ۶۰ثانیه‌ای"
            )
            render_stock_detail(
                stocks=equity_like,
                book=snapshot["book"],
                fx=fx,
                fx_error=fx_error,
                failed_chunks=failed_chunks,
                fx_reference=fx_reference,
                cpi=cpi,
                cpi_error=cpi_error,
            )

        tab_stock()

    with tabs[4]:
        st.caption(
            "این بخش از `market.sqlite` که collect_pressure می‌نویسد خوانده می‌شود."
        )
        try:
            render_snapshot_observatory(key_prefix="app9_obs")
        except Exception as exc:
            st.error(f"رصدخانه اسنپ‌شات: {exc}")
        st.divider()
        try:
            render_pressure_index(key_prefix="app9_pressure_main")
        except Exception as exc:
            st.error(f"شاخص فشار (fixed-day): {exc}")
        st.divider()
        try:
            render_market_index(key_prefix="app9_market_index_main")
        except Exception as exc:
            st.error(f"شاخص بازار (live-depth): {exc}")

    with tabs[5]:
        @st.fragment(run_every="60s")
        def tab_quality():
            snapshot, stale = _load_snapshot()
            if snapshot is None:
                return
            _snapshot_caption(snapshot, stale)
            market = snapshot["market"].copy()
            stocks = market[market["kind"].eq("سهام")].copy()
            options = market[
                market["kind"].isin(["اختیار خرید", "اختیار فروش"])
            ].copy()
            render_quality(snapshot, stocks, options)

        tab_quality()





# ============================================================
# تعطیلات رسمی (مبتنی بر holidayapi.ir ← داده time.ir)
# ============================================================

@st.cache_data(ttl=12 * 3600, show_spinner=False)
def _holiday_api_day(year: int, month: int, day: int):
    """برمی‌گرداند is_holiday یا None در صورت خطا."""
    try:
        url = f"https://holidayapi.ir/jalali/{year}/{month}/{day}"
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        data = r.json()
        return bool(data.get("is_holiday"))
    except Exception:
        return None


def is_iran_official_holiday(when=None) -> bool:
    """تعطیل رسمی شمسی؟ (جمعه جداگانه با weekday چک می‌شود)."""
    when = when or dt.datetime.now(TEHRAN)
    try:
        if jdatetime is not None:
            jd = jdatetime.datetime.fromgregorian(datetime=when)
            y, m, d = jd.year, jd.month, jd.day
        else:
            # تقریب بدون jdatetime: API میلادی نداریم؛ رد شو
            return False
        cached = _holiday_api_day(y, m, d)
        if cached is not None:
            return cached
    except Exception:
        pass
    return False


def market_session_open(now=None, start=None, end=None, weekdays_only=True) -> bool:
    """
    شنبه تا چهارشنبه، در بازه ساعت، و نه تعطیل رسمی.
    weekday: شنبه=5 ... چهارشنبه=2 در zoneinfo/Python.
    """
    now = now or dt.datetime.now(TEHRAN)
    if weekdays_only and now.weekday() not in {5, 6, 0, 1, 2}:
        return False
    if start is not None and end is not None:
        clock = now.time().replace(tzinfo=None)
        if not (start <= clock <= end):
            return False
    if is_iran_official_holiday(now):
        return False
    return True



# --- collector pieces embedded ---
# ===== MARKET DATA =====

import hashlib
import math
import os
import re

import requests


MARKET_COLUMNS = [
    "id",
    "isin",
    "symbol",
    "name",
    "time",
    "first_price",
    "close_price",
    "last_trade",
    "number_trades",
    "volume",
    "value",
    "low_price",
    "high_price",
    "yesterday_price",
    "eps",
    "base_volume",
    "table_id",
    "industry_id",
    "section_code",
    "max_allowed_price",
    "min_allowed_price",
    "number_shares",
    "asset_code",
]

BOOK_COLUMNS = [
    "id",
    "level",
    "sell_count",
    "buy_count",
    "bid_price",
    "ask_price",
    "bid_vol",
    "ask_vol",
]

TEXT_COLUMNS = {
    "id",
    "isin",
    "symbol",
    "name",
    "time",
    "table_id",
    "industry_id",
    "section_code",
    "asset_code",
}

DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def configured_codes(name, default):
    return {
        item.strip()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    }


# طبقه‌بندی صریح؛ ابزار ناشناخته وارد سبد نمی‌شود.
STOCK_CODES = configured_codes(
    "INDEX_STOCK_CODES",
    "300,303,309",
)

FUND_CODES = configured_codes(
    "INDEX_FUND_CODES",
    "305",
)
CALL_CODES = configured_codes("INDEX_CALL_CODES", "311,320")
PUT_CODES = configured_codes("INDEX_PUT_CODES", "312,321")


def normalize_text(value):
    text = str(value or "").translate(DIGITS)
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"[\u200c\u200e\u200f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def number(value):
    try:
        text = str(value).translate(DIGITS)
        text = text.replace(",", "").replace("٬", "").strip()
        result = float(text)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def classify(section_code):
    if section_code in STOCK_CODES:
        return "stock"
    if section_code in FUND_CODES:
        return "fund"
    if section_code in CALL_CODES:
        return "call"
    if section_code in PUT_CODES:
        return "put"
    return "other"


def parse_market_response(text):
    parts = text.split("@")

    if len(parts) < 4:
        raise ValueError(
            "پاسخ منبع، ساختار کامل MarketWatchInit ندارد."
        )

    market_by_id = {}

    for raw in parts[2].split(";"):
        cells = raw.strip().split(",")

        if (
            len(cells) < len(MARKET_COLUMNS)
            or not cells[0].strip().isdigit()
        ):
            continue

        row = dict(zip(MARKET_COLUMNS, cells))
        row["source_extra_fields"] = cells[len(MARKET_COLUMNS):]

        for key in MARKET_COLUMNS:
            if key in TEXT_COLUMNS:
                row[key] = (
                    normalize_text(row[key])
                    if key in {"symbol", "name"}
                    else str(row[key]).strip()
                )
            else:
                row[key] = number(row[key])

        row["kind"] = classify(row["asset_code"])
        market_by_id[row["id"]] = row

    if not market_by_id:
        raise ValueError(
            "هیچ رکورد کامل بازار از پاسخ استخراج نشد."
        )

    book_by_key = {}

    for raw in parts[3].split(";"):
        cells = raw.strip().split(",")

        if (
            len(cells) < len(BOOK_COLUMNS)
            or not cells[0].strip().isdigit()
        ):
            continue

        row = dict(zip(BOOK_COLUMNS, cells))
        row["id"] = row["id"].strip()

        for key in BOOK_COLUMNS[1:]:
            row[key] = number(row[key])

        level = row["level"]

        if (
            level is None
            or level < 1
            or not level.is_integer()
        ):
            continue

        row["level"] = int(level)
        book_by_key[(row["id"], row["level"])] = row

    return (
        list(market_by_id.values()),
        list(book_by_key.values()),
    )


def fetch_snapshot():
    custom_url = os.getenv("MARKETWATCH_URL", "").strip()

    urls = (
        [custom_url]
        if custom_url
        else [
            (
                "https://www.tsetmc.com/tsev2/data/"
                "MarketWatchInit.aspx?h=0&r=0"
            ),
            (
                "https://old.tsetmc.com/tsev2/data/"
                "MarketWatchInit.aspx?h=0&r=0"
            ),
        ]
    )

    errors = []

    with requests.Session() as session:
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
        })

        for url in urls:
            try:
                response = session.get(
                    url,
                    timeout=(10, 35),
                )
                response.raise_for_status()

                # این خروجی متنی معمولاً UTF-8 است.
                text = response.content.decode("utf-8-sig")

                market, book = parse_market_response(text)

                if not any(
                    row["kind"] in {"stock", "fund", "call", "put"}
                    for row in market
                ):
                    raise ValueError(
                        "نماد سهام/صندوق با کدهای تنظیم‌شده پیدا نشد."
                    )

                return {
                    "market": market,
                    "book": book,
                    "source_url": url,
                    "source_hash": hashlib.sha256(
                        response.content
                    ).hexdigest(),
                }

            except (
                requests.RequestException,
                UnicodeError,
                ValueError,
            ) as exc:
                errors.append(f"{url}: {exc}")

    raise RuntimeError(
        "دریافت اسنپ‌شات ناموفق بود:\n"
        + "\n".join(errors)
    )


def collector_save_snapshot(
    snapshot,
    result,
    fetched_at,
    trading_date,
    bucket_key,
):
    with connection() as con:
        cursor = con.execute(
            """
            INSERT OR IGNORE INTO snapshots (
                bucket_key,
                fetched_at,
                trading_date,
                source_url,
                source_hash,
                formula_version,
                config_json,
                raw_json,
                details_json,
                exclusions_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bucket_key,
                fetched_at,
                trading_date,
                snapshot["source_url"],
                snapshot["source_hash"],
                result["formula_version"],
                encode(result["config"]),
                encode({
                    "market": snapshot["market"],
                    "book": snapshot["book"],
                }),
                encode(result["details"]),
                encode(result["exclusions"]),
            ),
        )

        if cursor.rowcount == 0:
            return None

        snapshot_id = cursor.lastrowid

        for item in result["summaries"]:
            con.execute(
                """
                INSERT INTO index_values (
                    snapshot_id,
                    scope,
                    points,
                    return_pct,
                    last_return_pct,
                    book_gap_pp,
                    pressure_valid,
                    book_coverage_pct,
                    eligible_count,
                    valid_book_count,
                    summary_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    item["scope"],
                    item["points"],
                    item["return_pct"],
                    item["last_return_pct"],
                    item["book_gap_pp"],
                    item["pressure_valid"],
                    item["book_coverage_pct"],
                    item["eligible_count"],
                    item["valid_book_count"],
                    encode(item),
                ),
            )

    return snapshot_id




# IndexConfig/build_indices already in app


def _collector_cli_main():
    import argparse
    import logging
    import time as _time
    from zoneinfo import ZoneInfo as _ZI

    parser = argparse.ArgumentParser(description="جمع‌آوری اسنپ‌شات بازار → market.sqlite")
    parser.add_argument("--collect", action="store_true", help="اجرای collector")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--cap-weight", type=float, default=0.60)
    parser.add_argument("--max-spread", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--start", type=str, default="09:00")
    parser.add_argument("--end", type=str, default="12:30")
    parser.add_argument("--weekdays-only", action="store_true", default=True)
    parser.add_argument("--no-weekdays-only", action="store_true")
    args, _unknown = parser.parse_known_args()

    def _clock(s):
        return dt.datetime.strptime(s, "%H:%M").time()

    start_t = _clock(args.start)
    end_t = _clock(args.end)
    weekdays_only = not args.no_weekdays_only

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    LOG = logging.getLogger("data_collector")

    init_db()
    global DB_PATH
    DB_PATH = _resolve_market_db()
    _Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    LOG.info("Database: %s", DB_PATH)

    config = IndexConfig(cap_weight=args.cap_weight, max_spread_pct=args.max_spread)

    while True:
        now = dt.datetime.now(TEHRAN)
        open_ok = args.once or market_session_open(now, start_t, end_t, weekdays_only)
        if open_ok:
            try:
                snapshot = fetch_snapshot()
                result = build_indices(snapshot["market"], snapshot["book"], config)
                if not result.get("details"):
                    raise ValueError("هیچ نماد واجد شرایط نیست")
                import hashlib, json as _json
                sig = hashlib.sha256(
                    _json.dumps({"v": result["formula_version"], "c": result["config"]}, sort_keys=True).encode()
                ).hexdigest()[:16]
                bucket = int(dt.datetime.now(dt.timezone.utc).timestamp() // args.interval)
                key = f"{args.interval}:{bucket}:{sig}"
                fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                day = now.date().isoformat()
                sid = collector_save_snapshot(snapshot, result, fetched_at, day, key)
                combined = next(x for x in result["summaries"] if x["scope"] == "combined")
                if sid is None:
                    LOG.info("بازه تکراری")
                else:
                    LOG.info(
                        "snapshot=%s | return=%+.3f%% | coverage=%.1f%% | symbols=%s",
                        sid, combined["return_pct"], combined["book_coverage_pct"], combined["eligible_count"],
                    )
            except Exception:
                LOG.exception("collect failed")
                if args.once:
                    raise
        else:
            if is_iran_official_holiday(now):
                LOG.info("تعطیل رسمی — جمع‌آوری انجام نشد")
            elif args.once:
                LOG.info("خارج از بازه جلسه")
        if args.once:
            break
        _time.sleep(max(1, args.interval))

# ============================================================
# MODULE 3: ADVANCED COVERED CALL & STRESS TESTING ENGINE
# ============================================================

class CoveredCallOptimizer:
    """
    تحلیل ساختار سود/زیان و بهینه‌سازی موقعیت کاوردکال با شبیه‌سازی سناریوها
    """
    @staticmethod
    def evaluate_strategy(
        stock_price: float,
        strike_price: float,
        option_premium: float,
        days_to_expiry: int,
        risk_free_rate: float = 0.30
    ) -> dict:
        """
        محاسبه جامع شاخص‌های مالی و نرخ‌های معادل سالانه استراتژی کاوردکال
        """
        if stock_price <= 0 or days_to_expiry <= 0:
            return {}

        net_outlay = stock_price - option_premium  # قیمت تمام‌شده خرید سهم
        break_even = net_outlay                  # نقطه سربه‌سر
        
        max_profit = (strike_price - net_outlay) if strike_price > net_outlay else option_premium
        max_return_pct = (max_profit / net_outlay) * 100.0
        
        # بازده سالانه شده (APR)
        year_fraction = days_to_expiry / 365.0
        apr_max = max_return_pct / year_fraction
        
        # بازده در صورت ثبات قیمت سهم (Unchanged Return)
        static_profit = option_premium + min(0.0, strike_price - stock_price)
        static_return_pct = (static_profit / net_outlay) * 100.0
        apr_static = static_return_pct / year_fraction
        
        # درصد پوشش ریسک افت قیمت سهم (Downside Buffer)
        downside_protection = (option_premium / stock_price) * 100.0

        return {
            "net_cost_basis": round(net_outlay, 2),
            "break_even_price": round(break_even, 2),
            "max_profit_per_share": round(max_profit, 2),
            "max_return_pct": round(max_return_pct, 2),
            "apr_max_pct": round(apr_max, 2),
            "apr_static_pct": round(apr_static, 2),
            "downside_protection_pct": round(downside_protection, 2)
        }

    @staticmethod
    def simulate_payoff_grid(stock_price: float, strike_price: float, option_premium: float) -> pd.DataFrame:
        """تولید ماتریس سناریوهای قیمت سهم در سررسید و محاسبه سود/زیان خالص"""
        price_changes = [-0.20, -0.10, -0.05, 0.0, 0.05, 0.10, 0.20]
        records = []
        net_cost = stock_price - option_premium
        
        for pct in price_changes:
            s_tf = stock_price * (1 + pct)
            payoff_stock = s_tf - stock_price
            payoff_option = -max(0.0, s_tf - strike_price) + option_premium
            total_pnl = payoff_stock + payoff_option
            total_return = (total_pnl / net_cost) * 100.0
            
            records.append({
                "تغییر قیمت سهم": f"{pct*100:+.0f}٪",
                "قیمت سهم در سررسید": round(s_tf, 1),
                "سود/زیان کل (ریال)": round(total_pnl, 1),
                "بازده کل (٪)": round(total_return, 2)
            })
            
        return pd.DataFrame(records)




# ============================================================
# MODULE 2: ADVANCED ORDER BOOK & MARKET SENTIMENT ANALYZER
# ============================================================

import pandas as pd

class AdvancedSentimentAnalyzer:
    """
    تحلیل عمیق عدم‌تقارن دفتر سفارشات و استخراج سیگنال‌های هیجان بازار
    """
    @staticmethod
    def analyze_order_book_depth(book_df: pd.DataFrame) -> dict:
        """
        بررسی عمق ۵ لایه و محاسبه عدم‌تقارن حجم/ارزش در طرفین خرید و فروش
        """
        if book_df is None or book_df.empty:
            return {"asymmetry_ratio": 0.0, "liquidity_score": 0.0, "market_state": "نامشخص"}
        
        bid_val = book_df["bid_value"].sum() if "bid_value" in book_df.columns else 0.0
        ask_val = book_df["ask_value"].sum() if "ask_value" in book_df.columns else 0.0
        
        total_depth = bid_val + ask_val
        if total_depth == 0:
            return {"asymmetry_ratio": 0.0, "liquidity_score": 0.0, "market_state": "بدون عمق"}

        # نسبت عدم تقارن (بین ۱۰۰- تا ۱۰۰+)
        asymmetry = ((bid_val - ask_val) / total_depth) * 100.0
        
        # محاسبه نقدشوندگی نسبی
        liquidity_score = min(100.0, (total_depth / 1e10) * 10) # بر اساس میزان حجم ریالی
        
        if asymmetry > 40:
            state = "تقاضای انباشته و صف خرید قوی"
        elif asymmetry > 10:
            state = "برتری نسبی خریداران"
        elif asymmetry >= -10:
            state = "تعادل عرضه و تقاضا"
        elif asymmetry >= -40:
            state = "برتری نسبی فروشندگان"
        else:
            state = "فشار عرضه و صف فروش سنگین"

        return {
            "asymmetry_ratio": round(asymmetry, 2),
            "liquidity_score": round(liquidity_score, 2),
            "bid_total_value": round(bid_val, 0),
            "ask_total_value": round(ask_val, 0),
            "market_state": state
        }

# ============================================================
# MODULE 1: BLACK-SCHOLES OPTIONS PRICING & GREEKS ENGINE
# ============================================================

import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

class OptionGreeksEngine:
    """
    موتور محاسباتی متقدم برای ارزش‌گذاری اختیار معامله و استخراج پارامترهای یونانی
    """
    def __init__(self, r: float = 0.30):
        self.r = r  # نرخ بهره بدون ریسک سالانه (مثلاً ۳۰ درصد)

    def black_scholes_call(self, S: float, K: float, T: float, sigma: float) -> float:
        """ارزش نظری اختیار خرید (Call Option) با مدل بلک-شولز"""
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return max(0.0, S - K)
        d1 = (np.log(S / K) + (self.r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        return S * norm.cdf(d1) - K * np.exp(-self.r * T) * norm.cdf(d2)

    def calculate_greeks(self, S: float, K: float, T: float, sigma: float) -> dict:
        """محاسبه کلیه پارامترهای ریسک یونانی (Greeks)"""
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
        
        sqrt_T = np.sqrt(T)
        d1 = (np.log(S / K) + (self.r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T
        
        delta = norm.cdf(d1)
        gamma = norm.pdf(d1) / (S * sigma * sqrt_T)
        vega = S * norm.pdf(d1) * sqrt_T / 100.0  # تغییر به ازای ۱٪ تغییر نوسان‌پذیری
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * sqrt_T) - self.r * K * np.exp(-self.r * T) * norm.cdf(d2)) / 365.0  # افت روزانه ارزش
        rho = (K * T * np.exp(-self.r * T) * norm.cdf(d2)) / 100.0
        
        return {
            "delta": round(float(delta), 4),
            "gamma": round(float(gamma), 6),
            "theta": round(float(theta), 4),
            "vega": round(float(vega), 4),
            "rho": round(float(rho), 4)
        }

    def implied_volatility(self, market_price: float, S: float, K: float, T: float) -> float:
        """استخراج نوسان‌پذیری ضمنی (Implied Volatility) به روش عددی Brent"""
        if T <= 0 or market_price <= 0:
            return 0.0
        intrinsic = max(0.0, S - K)
        if market_price <= intrinsic:
            return 0.001
        
        f = lambda sig: self.black_scholes_call(S, K, T, sig) - market_price
        try:
            return brentq(f, 1e-4, 5.0)
        except (ValueError, RuntimeError):
            return 0.0


import sys as _sys
if _COLLECTOR_MODE or "--collect" in _sys.argv:
    _collector_cli_main()
else:
    dashboard()
    render_developer_credit()
