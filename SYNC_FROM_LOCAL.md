# همگام‌سازی از session توسعه

فایل‌های زیر را از پوشهٔ محلی پروژه کنار `app.py` بگذار (نسخهٔ کامل session):

- `app.py` — صفحهٔ اول pulse، options scope، options_ticks، فیلتر کاوردکال سالانه
- `nova_first_page.py` — شاخص اختیار، کاوردکال، میانگین زمانی
- `market_metrics.py` — breadth/صف پیش‌محاسبه

```bat
git add app.py nova_first_page.py market_metrics.py
git commit -m "feat: complete pulse UI and options pipeline"
git push
```

`nova_pulse.py`، `run_nova.bat`، `tools/`، `config/` و README از قبل روی main هستند.
