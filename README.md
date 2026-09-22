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

```bash
git clone https://github.com/Kamyarb/NovaDashboard.git
cd NovaDashboard
```

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

```bash
streamlit run app.py
```

Streamlit will display the local address in the terminal. Open that address in your browser.

## Run the data collector

The collector can be started with:

```bash
python data_collector.py
```

The application stores collected market snapshots in:

```text
data/market.sqlite
```

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

```bash
git check-ignore -v data/market.sqlite
```

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

[LinkedIn](https://www.linkedin.com/in/kamyarbagha/)

---

NovaDashboard is an independent open-source project.
