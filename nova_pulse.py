# -*- coding: utf-8 -*-
"""
نبض صفحهٔ اول: روز نمایش، دو خط شاخص حول ۱۰۰، شیب، outlier، JSON وضعیت.
سبک — فقط از ستون‌های index_values می‌خواند، raw_json لمس نمی‌شود.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
DAY_ROLLOVER = dt.time(8, 45)
SESSION_START = dt.time(8, 45)
SESSION_END = dt.time(12, 30)

BOUNDS = {
    "base_points": 100.0,
    "official_lock_pct": 3.0,
    "max_virtual_return_pct": 5.0,
    "cap_weight": 0.60,
    "max_spread_pct": 3.0,
    "day_rollover_tehran": "08:45",
    "record_window": "[08:45, 12:30)",
    "outlier_mad_k": 5.0,
    "slope_window_points": 5,
    "scopes": ["combined", "stock", "fund"],
}


def now_tehran() -> dt.datetime:
    return dt.datetime.now(TEHRAN)


def display_trading_day(now: dt.datetime | None = None) -> str:
    now = now or now_tehran()
    if now.tzinfo is None:
        now = now.replace(tzinfo=TEHRAN)
    else:
        now = now.astimezone(TEHRAN)
    day = now.date()
    if now.time() < DAY_ROLLOVER:
        day = day - dt.timedelta(days=1)
    return day.isoformat()


def resolve_display_day(available: list[str], prefer: str | None = None) -> str | None:
    if not available:
        return None
    prefer = prefer or display_trading_day()
    if prefer in available:
        return prefer
    older = [d for d in available if d <= prefer]
    if older:
        return max(older)
    return available[0]


def session_ui_state(available_days: list[str], now: dt.datetime | None = None) -> dict:
    now = now or now_tehran()
    if now.tzinfo is None:
        now = now.replace(tzinfo=TEHRAN)
    else:
        now = now.astimezone(TEHRAN)
    prefer = display_trading_day(now)
    day = resolve_display_day(available_days, prefer)
    tm = now.time()
    in_record = SESSION_START <= tm < SESSION_END
    has_prefer = prefer in (available_days or [])
    if tm >= SESSION_START and not has_prefer and prefer == now.date().isoformat():
        status, label_fa = "waiting", "در انتظار دادهٔ جلسه"
    elif day and day < prefer:
        status, label_fa = "closed", "بازار بسته — آخرین جلسه"
    elif not in_record and has_prefer:
        status, label_fa = "closed", "بازار بسته — آخرین جلسه"
    elif in_record and has_prefer:
        status, label_fa = "live", "جلسه جاری"
    elif day:
        status, label_fa = "closed", "بازار بسته — آخرین جلسه"
    else:
        status, label_fa = "waiting", "در انتظار داده"
    return {"status": status, "label_fa": label_fa, "prefer_day": prefer, "display_day": day, "in_record_window": in_record}


def traded_points(last_return_pct: float | None, base: float = 100.0) -> float | None:
    if last_return_pct is None or not math.isfinite(float(last_return_pct)):
        return None
    return base * (1.0 + float(last_return_pct) / 100.0)


def model_points(return_pct: float | None, base: float = 100.0) -> float | None:
    if return_pct is None or not math.isfinite(float(return_pct)):
        return None
    return base * (1.0 + float(return_pct) / 100.0)


def filter_outliers_series(values: list[float | None], mad_k: float = 5.0) -> tuple[list[float | None], list[int]]:
    finite = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(finite) < 5:
        return list(values), []
    finite_sorted = sorted(finite)
    mid = len(finite_sorted) // 2
    median = finite_sorted[mid] if len(finite_sorted) % 2 else 0.5 * (finite_sorted[mid - 1] + finite_sorted[mid])
    abs_dev = sorted(abs(x - median) for x in finite)
    mad_mid = len(abs_dev) // 2
    mad = abs_dev[mad_mid] if len(abs_dev) % 2 else 0.5 * (abs_dev[mad_mid - 1] + abs_dev[mad_mid])
    if mad < 1e-9:
        return list(values), []
    threshold = mad_k * 1.4826 * mad
    out: list[float | None] = []
    dropped: list[int] = []
    for i, v in enumerate(values):
        if v is None or not math.isfinite(float(v)):
            out.append(None)
            continue
        if abs(float(v) - median) > threshold:
            out.append(None)
            dropped.append(i)
        else:
            out.append(float(v))
    return out, dropped


def slope_sign(values: list[float | None], window: int = 5) -> str:
    pts = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(pts) < 3:
        return "unknown"
    tail = pts[-window:] if len(pts) >= window else pts
    n = len(tail)
    x_mean = (n - 1) / 2.0
    y_mean = sum(tail) / n
    num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(tail))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den < 1e-12:
        return "flat"
    slope = num / den
    if slope > 0.002:
        return "up"
    if slope < -0.002:
        return "down"
    return "flat"


def to_tehran_label(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        t = t.astimezone(TEHRAN)
        return t.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ts)


def to_tehran_dt(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(TEHRAN)
    except (TypeError, ValueError):
        return None


def tehran_clock_banner(now: dt.datetime | None = None) -> str:
    now = now or now_tehran()
    if now.tzinfo is None:
        now = now.replace(tzinfo=TEHRAN)
    else:
        now = now.astimezone(TEHRAN)
    mil = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        import jdatetime
        j = jdatetime.datetime.fromgregorian(datetime=now.replace(tzinfo=None))
        jalali = f"{j.year:04d}/{j.month:02d}/{j.day:02d} {j.hour:02d}:{j.minute:02d}"
    except Exception:
        jalali = "—"
    return f"تهران {mil} · شمسی {jalali}"


def build_pulse_frame(history_rows: list[dict]) -> dict[str, Any]:
    times = []
    traded = []
    model = []
    for r in history_rows:
        times.append(to_tehran_label(r.get("fetched_at")))
        model.append(r.get("points"))
        traded.append(traded_points(r.get("last_return_pct")))
    traded_s, drop_t = filter_outliers_series(traded)
    model_s, drop_m = filter_outliers_series(model)
    return {
        "times": times,
        "traded": traded_s,
        "model": model_s,
        "traded_raw": traded,
        "model_raw": model,
        "outlier_idx_traded": drop_t,
        "outlier_idx_model": drop_m,
        "slope_traded": slope_sign(traded_s),
        "slope_model": slope_sign(model_s),
        "last_traded": next((v for v in reversed(traded_s) if v is not None), None),
        "last_model": next((v for v in reversed(model_s) if v is not None), None),
    }


def export_runtime_json(path: str | Path, extra: dict | None = None) -> str:
    payload = {
        "generated_at_tehran": now_tehran().isoformat(timespec="seconds"),
        "display_trading_day": display_trading_day(),
        "bounds": BOUNDS,
        "logic_fa": {
            "traded_index": "میانگین وزنی بازده last نسبت به دیروز → نقاط از ۱۰۰",
            "model_index": "همان وزن‌ها روی قیمت دفتر/صف (سقف مجازی ±۵٪) → نقاط از ۱۰۰",
            "weights": "۰٫۶ مارکت‌کپ + ۰٫۴ ارزش معامله؛ اگر ارزش صفر فقط کپ",
            "day_window": "صفحهٔ اول: از ۸:۴۵ تا ۸:۴۵ فردا همان روز معاملاتی",
            "outliers": "در نمودار با MAD حذف بصری؛ در DB می‌مانند",
            "bullets": "شیب مثبت آخرین نقاط = سبز؛ منفی = قرمز",
        },
    }
    if extra:
        payload["runtime"] = extra
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path.resolve())
