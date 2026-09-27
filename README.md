# NovaDashboard

### رصدخانه هوشمند بازار سرمایه ایران

داشبورد متن‌باز برای **دریافت، ذخیره و تحلیل لحظه‌ای** داده‌های بورس تهران — با تمرکز روی فشار سفارش‌ها، شاخص‌های ترکیبی حول مبنای ۱۰۰، اختیار معامله و فرصت‌های کاوردکال.

| | |
|---|---|
| **وضعیت** | در حال توسعه فعال |
| **منبع داده** | TSETMC |
| **UI** | Streamlit (تم تیره، RTL، فونت وزیر) |
| **ذخیره** | SQLite (`data/market.sqlite`) |
| **زبان** | Python 3.10+ |

---

## چه چیزی این نسخه را متمایز می‌کند؟

### صفحهٔ اول = نبض بازار (سریع و خلوت)

- **ساعت تهران + تاریخ شمسی** بالای صفحه
- **سه (تا چهار) شاخص حول ۱۰۰** روی یک نمودار:
  - شاخص **معامله‌شده** (بازده last وزنی)
  - شاخص **فشار دفتر/صف** (تعدیل با اردربوک؛ صف می‌تواند تا حدود ±۵٪ از دیروز برود)
  - شاخص **اختیار** (پایهٔ callهای پرمعامله، وزن ارزش معامله، هموار با EMA)
  - شاخص **صندوق** (نقطه‌چین)
- نمودار **تعداد نمادهای مثبت / منفی** در طول روز
- **کاوردکال**: فیلتر ITM، DTE، ارزش معامله (میلیارد تومان)، نماد — بازده **سالانه**
- نمودار **میانگین بازده سالانه کاوردکال در طول روز** (با همان فیلترها) تا ببینی فرصت ساعت ۹:۳۰ فقط لحظه‌ای بوده یا ماندگار

### قانون روز معاملاتی (تهران)

| بازه | رفتار |
|------|--------|
| **[08:45 , 12:30)** | Collector ذخیره می‌کند؛ صفحهٔ اول دادهٔ همان جلسه |
| بعد از 08:45 و هنوز بدون اسنپ‌شات | «در انتظار داده» |
| بعد از بسته شدن تا 08:45 فردا | آخرین جلسه با برچسب **بازار بسته** |

همهٔ محورهای زمان در UI به **Asia/Tehran** تبدیل می‌شوند (دیگر ۵:۳۰ صبح به‌جای ۹ دیده نمی‌شود).

### معماری داده

```
TSETMC → collector (هر ~۶۰ثانیه در جلسه)
       → build_indices + breadth + options ticks
       → SQLite
UI     → فقط SELECT از خلاصه‌های ازپیش‌محاسبه (بدون parse دوبارهٔ raw_json در مسیر داغ)
```

| جدول | نقش |
|------|-----|
| `snapshots` / `index_values` | اسنپ‌شات و شاخص‌ها |
| `breadth_values` | مثبت/منفی/صف |
| `options_ticks` | تیک اختیار + بازده کاوردکال سالانه در طول جلسه |
| `options_research_eod` | آرشیو پایان‌روز: ۲۰ اختیار پرمعامله |
| `index_series_archive` | سری سبک شاخص‌ها برای تحقیق |

پایان روز (حجم کنترل‌شده):

```bat
python tools\\eod_archive.py --db data\\market.sqlite --day YYYY-MM-DD
python tools\\eod_archive.py --db data\\market.sqlite --day YYYY-MM-DD --apply
```

---

## اجرا با یک فایل

```bat
run_nova.bat
```

یا دستی:

```bat
python data_collector.py --interval 60 --start 08:45 --end 12:30 --weekdays-only
streamlit run app.py
```

هر دو را از **همان پوشهٔ ریشه** اجرا کنید تا به `data\\market.sqlite` برسند.

### نصب

```bash
git clone https://github.com/Kamyarb/NovaDashboard.git
cd NovaDashboard
python -m pip install -r requirements.txt
```

پیشنهاد: Python 3.11

---

## ساختار پروژه

```text
NovaDashboard/
├── app.py                 # هسته UI + موتور شاخص + collector CLI
├── data_collector.py      # نقطه ورود collector (--collect)
├── nova_pulse.py          # روز نمایش، زمان تهران، نبض شاخص
├── nova_first_page.py     # شاخص اختیار، کاوردکال، فیلتر و میانگین زمانی
├── market_metrics.py      # breadth / صف پیش‌محاسبه
├── run_nova.bat           # اجرای یک‌کلیکی
├── config/
│   └── market_assumptions.json
├── docs/
│   └── ARCHITECTURE.md
├── tools/
│   ├── cleanup_offhours.py   # پاک‌سازی اسنپ‌شات خارج از [08:45,12:30)
│   └── eod_archive.py        # آرشیو پایان‌روز اختیار + سبک‌سازی
├── utils/
│   └── tse_tools.py
├── assets/                # فونت وزیر
└── data/                  # market.sqlite (در git نیست)
```

---

## اصول

1. **صحت داده** قبل از زیبایی UI  
2. Collector نویسنده است؛ UI خواننده  
3. عدد شاخص رسمی بازار را جعل نمی‌کنیم — مبنا ۱۰۰ خودمان  
4. اثر صف دوبار شمرده نمی‌شود  
5. بدون مظنهٔ معتبر، IV/Greeks اختراع نمی‌شود  
6. `data/market.sqlite` و لاگ‌ها در مخزن نیستند  

جزئیات فرمول‌ها: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

---

## نویسنده

**Kamyar Bagha** — AI · Data · Product  

[LinkedIn](https://www.linkedin.com/in/kamyarbagha/)

---

<p align="center">
  <strong>NovaDashboard</strong><br/>
  Market intelligence for Iranian capital markets
</p>
