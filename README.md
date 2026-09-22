# NovaDashboard

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

## Installation

Clone the repository:

```bash
git clone https://github.com/Kamyarb/NovaDashboard.git
cd NovaDashboard
```

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

If you are developing locally and `requirements.txt` is not yet available, install the packages imported by the application according to your Python environment.

## Run the dashboard

Start Streamlit with:

```bash
streamlit run app.py
```

Then open the local Streamlit URL shown in the terminal.

## Data collection

NovaDashboard can run its collector entry point with:

```bash
python data_collector.py
```

The application stores collected snapshots in a local SQLite database under:

```text
data/market.sqlite
```

The database is **not committed to GitHub**. It is generated locally and can become large over time.

## Important notes about data

NovaDashboard depends on market data obtained from TSETMC endpoints. Availability, response formats, rate limits, and market-session behavior can change independently of this project.

The dashboard should therefore be treated as an analytical and research tool. Market indicators and calculated signals are not investment advice.

## Project principles

NovaDashboard is built around a few simple ideas:

1. **Observe the whole market, not only individual prices.**
2. **Combine price, liquidity, order-book and derivatives information.**
3. **Keep the data pipeline transparent and locally inspectable.**
4. **Separate raw market data from derived analytical signals.**
5. **Keep large runtime datasets outside the source repository.**

## Development

Contributions, bug reports, and ideas are welcome.

For development, it is recommended to keep local runtime data under `data/` and avoid committing generated databases, caches, credentials, or environment-specific files.

## Author

**Kamyar Bagha**

AI / Data / Product

[LinkedIn](https://www.linkedin.com/in/kamyarbagha/)

---

NovaDashboard is an independent open-source project.
