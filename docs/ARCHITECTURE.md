# NOVA — معماری محصول (به‌روزشونده)

## Schema فعلی (تأییدشده از ماشین کاربر)

جداول: `snapshots`, `index_values`, `breadth_values`, `pressure_daily_states`

`index_values`: id, snapshot_id, scope, points, return_pct, last_return_pct,
book_gap_pp, pressure_valid, book_coverage_pct, eligible_count, valid_book_count, summary_json

**نتیجه:** schema فعلی کافی است. پاک‌کردن کل DB لازم نیست.
فقط رکوردهای **خارج از [08:45, 12:30)** باید پس از backup پاک شوند (`tools/cleanup_offhours.py`).

## قانون روز و ساعت

| بازه تهران | ذخیره در DB | صفحهٔ اول |
|------------|-------------|-----------|
| [08:45, 12:30) | بله (collector) | دادهٔ همان روز؛ اگر هنوز خالی → «در انتظار داده» |
| [12:30, 08:45 فردا) | خیر | آخرین جلسه با برچسب **بازار بسته** |
| قبل از اولین اسنپ‌شات بعد از 08:45 | — | **در انتظار داده** |

## سه شاخص حول ۱۰۰ — یک سبد، یک وزن

وزن ثابت روزانه: `0.60·cap + 0.40·value` (اگر value=0 فقط cap).

1. **شاخص معاملات** = Σ w · last_return → points از ۱۰۰
2. **شاخص فشار** = همان وزن روی بازده تعدیل‌شده با اردربوک/صف (سقف مجاز نماد ≈ ±۵٪)
3. **شاخص اختیار** = بازده پایهٔ callهای پرمعامله با mid معتبر، وزن ارزش معامله، EMA

صفحهٔ اول: KPI + نمودار مشترک + breadth + کاوردکال سالانه با فیلتر ITM/DTE/ارزش.

زمان‌ها همیشه Asia/Tehran.

Config: `config/market_assumptions.json`
