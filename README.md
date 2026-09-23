# NovaDashboard

<<<<<<< HEAD
**NovaDashboard** is an open-source market observatory for the Tehran Stock Exchange (TSE), built with Python and Streamlit.

It is designed to turn live market data, order-book information, derivatives activity, and market-pressure signals into a single interactive dashboard.

> **Status:** Active development  
> **Data source:** TSETMC / Tehran market data  
> **Interface:** Streamlit  
> **Storage:** SQLite

## What it does

NovaDashboard brings several market signals into one place:

- Market-wide snapshot and instrument universe
- Order-book and trading-pressure analysis
- Stock, fund, and combined market indices
- Options and derivatives monitoring
- Market breadth and liquidity signals
- Real-time / near-real-time TSE data collection
- Local SQLite storage for collected snapshots
- Interactive charts and tables through Streamlit

The project is intentionally designed as a **market observatory**, rather than a conventional price-only dashboard.

## Architecture

The application is centered around a small local data pipeline:

```text
TSETMC
   │
   ▼
Market Snapshot
   │
   ├── Market / Instrument Data
   ├── Order Book Data
   └── Derivatives Data
   │
   ▼
Pressure & Index Engine
   │
   ▼
SQLite
   │
   ▼
Streamlit Dashboard
```

### Main components

| File | Purpose |
|---|---|
| `app.py` | Main dashboard, market calculations, snapshot collection and application logic |
| `data_collector.py` | Collector entry point |
| `utils/tse_tools.py` | TSE/TSETMC data utilities |
| `data/` | Local market database and runtime data; intentionally excluded from Git |
| `.gitignore` | Keeps local market data and other generated files out of the repository |

## Installation with Miniconda

NovaDashboard is developed and tested with **Python 3.11** and can be run using **Miniconda**.

### 1. Install Miniconda

Download and install Miniconda for your operating system:

https://docs.anaconda.com/miniconda/

After installation, restart your terminal if necessary and verify Conda:

```bash
conda --version
```

### 2. Clone the repository
=======
### داشبورد هوشمند پایش و تحلیل بازار سرمایه ایران

NovaDashboard یک پروژه متن‌باز برای **دریافت، ذخیره‌سازی و تحلیل داده‌های بازار سرمایه ایران** است که با Python و Streamlit توسعه داده شده است.

هدف پروژه فقط نمایش قیمت‌ها نیست؛ NovaDashboard تلاش می‌کند تصویری یکپارچه از **وضعیت بازار، نقدشوندگی، دفتر سفارشات، فشار خرید و فروش، شاخص‌های ترکیبی و فعالیت ابزارهای مشتقه** ارائه کند.

> **وضعیت:** در حال توسعه  
> **منبع داده:** TSETMC  
> **رابط کاربری:** Streamlit  
> **ذخیره‌سازی:** SQLite  
> **زبان:** Python 3.11

---

## امکانات

| بخش | توضیح |
|---|---|
| نمای کلی بازار | مشاهده وضعیت کلی بازار و مجموعه نمادها |
| فشار بازار | تحلیل فشار خرید و فروش و عدم‌تعادل سفارشات |
| دفتر سفارشات | استفاده از اطلاعات خرید و فروش برای تحلیل نقدشوندگی و فشار بازار |
| شاخص‌های بازار | محاسبه شاخص‌های سهام، صندوق‌ها و شاخص ترکیبی |
| ابزارهای مشتقه | پایش اطلاعات مرتبط با اختیار معامله و مشتقات |
| جمع‌آوری داده | دریافت Snapshotهای بازار از TSETMC |
| ذخیره‌سازی | نگهداری داده‌های جمع‌آوری‌شده در SQLite |
| داشبورد | نمایش تعاملی نمودارها، جداول و شاخص‌ها |

---

## ایده اصلی پروژه

NovaDashboard به‌جای تمرکز صرف بر قیمت یک نماد، بازار را به‌عنوان یک سیستم بزرگ از **قیمت، حجم، نقدشوندگی، سفارشات و رفتار جمعی نمادها** بررسی می‌کند.

داده‌های خام بازار در چند مرحله پردازش می‌شوند و در نهایت شاخص‌هایی تولید می‌شوند که برای مشاهده وضعیت بازار در سطح کلان قابل استفاده هستند.

### لایه‌های سیستم

| لایه | مسئولیت |
|---|---|
| Market Data | دریافت اطلاعات بازار و نمادها |
| Processing | پاک‌سازی، اعتبارسنجی و آماده‌سازی داده |
| Pressure Engine | محاسبه فشار بازار و شاخص‌های مرتبط |
| Storage | ذخیره Snapshotها و مقادیر محاسبه‌شده |
| Dashboard | نمایش و تحلیل داده‌ها در محیط Streamlit |

---

## شاخص‌های بازار

NovaDashboard در حال حاضر شاخص‌های بازار را در چند گروه اصلی محاسبه می‌کند:

- **Stock** — وضعیت بخش سهام بازار
- **Fund** — وضعیت صندوق‌ها
- **Combined** — شاخص ترکیبی بازار

در محاسبات فشار بازار، اطلاعاتی مانند وضعیت دفتر سفارشات، عدم‌تعادل سفارشات، وزن نمادها و اعتبار داده‌ها مورد استفاده قرار می‌گیرند.

این ساختار امکان توسعه شاخص‌های جدید بدون تغییر اساسی در رابط کاربری را فراهم می‌کند.

---

## ساختار پروژه

```text
NovaDashboard/
├── app.py
├── data_collector.py
├── pressure_engine.py
├── market_data.py
├── storage.py
├── requirements.txt
├── utils/
│   └── tse_tools.py
├── data/
│   └── market.sqlite
└── README.md
```

### فایل‌های اصلی

| فایل | کاربرد |
|---|---|
| `app.py` | هسته داشبورد، منطق برنامه و پردازش Snapshot |
| `data_collector.py` | نقطه ورود برای اجرای Collector |
| `market_data.py` | دریافت و آماده‌سازی داده‌های بازار |
| `pressure_engine.py` | محاسبه فشار بازار و شاخص‌های تحلیلی |
| `storage.py` | مدیریت ارتباط با SQLite |
| `utils/tse_tools.py` | توابع مرتبط با TSETMC |
| `data/` | داده‌های محلی و دیتابیس Runtime |

---

# نصب

## پیش‌نیازها

برای اجرای پروژه به موارد زیر نیاز دارید:

- Python 3.10 یا بالاتر
- اتصال اینترنت برای دریافت داده‌های بازار
- مرورگر وب

نسخه پیشنهادی پروژه:

**Python 3.11**

بررسی نسخه Python:

```bash
python --version
```

---

## دریافت پروژه
>>>>>>> origin/main

```bash
git clone https://github.com/Kamyarb/NovaDashboard.git
cd NovaDashboard
```

<<<<<<< HEAD
### 3. Create the Conda environment

```bash
conda create -n novadashboard python=3.11 -y
```

Activate it:

```bash
conda activate novadashboard
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

Verify the Python environment:

```bash
python --version
```

You should see Python 3.11.x.

## Run the dashboard

With the `novadashboard` Conda environment activated:
=======
---

## نصب وابستگی‌ها

```bash
python -m pip install -r requirements.txt
```

در صورت نیاز:

```bash
python -m pip install --upgrade pip
```

---

# اجرای داشبورد

پس از نصب وابستگی‌ها:
>>>>>>> origin/main

```bash
streamlit run app.py
```

<<<<<<< HEAD
Streamlit will display the local address in the terminal. Open that address in your browser.

## Run the data collector

The collector can be started with:
=======
Streamlit آدرس محلی برنامه را در ترمینال نمایش می‌دهد. معمولاً برنامه از آدرس زیر در دسترس خواهد بود:

```text
http://localhost:8501
```

---

# جمع‌آوری داده

برای اجرای Collector:
>>>>>>> origin/main

```bash
python data_collector.py
```

<<<<<<< HEAD
The application stores collected market snapshots in:
=======
Collector اطلاعات بازار را دریافت کرده و Snapshotهای جمع‌آوری‌شده را در دیتابیس محلی ذخیره می‌کند.

دیتابیس پیش‌فرض:
>>>>>>> origin/main

```text
data/market.sqlite
```

<<<<<<< HEAD
The `data/` directory is intentionally excluded from Git. The local SQLite database can become large and is therefore not part of the GitHub repository.

## Development workflow

A typical local development workflow is:

```bash
conda activate novadashboard
cd NovaDashboard
streamlit run app.py
```

Before committing changes, check the repository:

```bash
git status
```

The local market database should remain ignored:
=======
---

# ذخیره‌سازی

NovaDashboard از SQLite به‌عنوان لایه ذخیره‌سازی محلی استفاده می‌کند.

ساختار دیتابیس شامل جداول اصلی برای نگهداری Snapshotهای بازار و مقادیر شاخص‌های محاسبه‌شده است.

### snapshots

اطلاعات Snapshotهای دریافتی از بازار را نگهداری می‌کند.

### index_values

مقادیر شاخص‌های محاسبه‌شده برای بخش‌های مختلف بازار را نگهداری می‌کند.

استفاده از SQLite باعث می‌شود پروژه بدون نیاز به راه‌اندازی سرویس دیتابیس جداگانه، به‌صورت محلی قابل اجرا باشد.

---

# داده‌های بازار

داده‌های بازار از سرویس‌های مرتبط با **TSETMC** دریافت می‌شوند.

به دلیل ماهیت سرویس‌های عمومی بازار، موارد زیر ممکن است در طول زمان تغییر کنند:

- ساختار پاسخ سرویس‌ها
- دسترسی به Endpointها
- محدودیت درخواست‌ها
- وضعیت سرویس در ساعات مختلف
- رفتار داده‌ها در زمان باز و بسته بودن بازار

به همین دلیل، لایه دریافت داده از بخش تحلیل جدا نگه داشته شده است.

---

# داده‌های محلی و Git

داده‌های Runtime پروژه در پوشه `data/` قرار می‌گیرند و در مخزن Git نگهداری نمی‌شوند.

این موضوع به‌خصوص برای فایل:

```text
data/market.sqlite
```

اهمیت دارد؛ زیرا دیتابیس محلی می‌تواند در طول زمان حجم قابل‌توجهی پیدا کند.

برای بررسی اینکه دیتابیس توسط Git نادیده گرفته می‌شود:
>>>>>>> origin/main

```bash
git check-ignore -v data/market.sqlite
```

<<<<<<< HEAD
## Important notes about data

NovaDashboard depends on market data obtained from TSETMC endpoints. Availability, response formats, rate limits, and market-session behavior can change independently of this project.

The dashboard is intended as an analytical and research tool. Market indicators and calculated signals are not investment advice.

## Project principles

NovaDashboard is built around a few simple ideas:

1. **Observe the whole market, not only individual prices.**
2. **Combine price, liquidity, order-book and derivatives information.**
3. **Keep the data pipeline transparent and locally inspectable.**
4. **Separate raw market data from derived analytical signals.**
5. **Keep large runtime datasets outside the source repository.**

## Development

Contributions, bug reports, and ideas are welcome.

For development, keep local runtime data under `data/` and avoid committing generated databases, caches, credentials, or environment-specific files.

## Author

**Kamyar Bagha**

AI / Data / Product
=======
نباید دیتابیس، فایل‌های Cache، Credentialها یا سایر داده‌های محیط محلی را به مخزن اضافه کرد.

---

# توسعه

برای شروع توسعه:

```bash
git clone https://github.com/Kamyarb/NovaDashboard.git
cd NovaDashboard
python -m pip install -r requirements.txt
streamlit run app.py
```

بررسی وضعیت Git:

```bash
git status
```

برای بررسی Syntax فایل‌های اصلی:

```bash
python -m py_compile app.py
python -m py_compile data_collector.py
python -m py_compile pressure_engine.py
```

---

# مسیر توسعه پروژه

NovaDashboard می‌تواند به‌عنوان یک زیرساخت برای توسعه ابزارهای پیشرفته‌تر تحلیل بازار مورد استفاده قرار گیرد.

برخی از مسیرهای توسعه:

- تاریخچه فشار بازار
- تحلیل فشار بازار در طول روز
- تشخیص تغییر رژیم بازار
- هشدار تغییرات غیرعادی
- تحلیل عمیق‌تر دفتر سفارشات
- مقایسه صنایع و گروه‌های مختلف
- تحلیل نقدشوندگی
- شاخص‌های جدید برای بازار مشتقه
- ذخیره‌سازی تاریخچه بلندمدت بازار
- مدل‌های آماری و یادگیری ماشین
- لایه هوشمند برای تحلیل خودکار وضعیت بازار

---

# اصول طراحی

NovaDashboard بر چند اصل ساده بنا شده است:

1. مشاهده بازار در سطح کلان، نه فقط قیمت یک نماد
2. ترکیب قیمت، حجم، نقدشوندگی و دفتر سفارشات
3. جدا نگه داشتن داده خام از شاخص‌های محاسبه‌شده
4. قابل مشاهده و قابل بررسی بودن فرآیند پردازش داده
5. خارج نگه داشتن داده‌های حجیم Runtime از مخزن کد
6. امکان توسعه مستقل بخش دریافت داده، تحلیل و رابط کاربری

---

# وضعیت پروژه

NovaDashboard در حال توسعه فعال است.

تمرکز فعلی پروژه بر سه حوزه اصلی قرار دارد:

**دریافت پایدار داده بازار**

**ذخیره‌سازی و مدیریت تاریخچه**

**تحلیل فشار و وضعیت بازار**

قابلیت‌های جدید به‌صورت تدریجی به پروژه اضافه خواهند شد.

---

# نویسنده

**Kamyar Bagha**

AI · Data · Product

حوزه‌های فعالیت:

- هوش مصنوعی
- تحلیل داده
- محصولات داده‌محور
- بازارهای مالی
- سیستم‌های تصمیم‌یار
- الگوریتم‌های معاملاتی
>>>>>>> origin/main

[LinkedIn](https://www.linkedin.com/in/kamyarbagha/)

---

<<<<<<< HEAD
NovaDashboard is an independent open-source project.
=======
## مجوز

NovaDashboard یک پروژه مستقل و متن‌باز است.

جزئیات مجوز استفاده از پروژه در نسخه‌های بعدی مشخص خواهد شد.

---

<p align="center">
  <strong>NovaDashboard</strong>
  <br>
  Market Intelligence for Iranian Capital Markets
</p>
>>>>>>> origin/main
