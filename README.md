# LTR Investment Analyzer

Automatically screens for-sale properties for long-term rental cash flow.  
Replicates the logic in `LTR COC calculator.xlsx` at scale.

---

## What it does

1. Pulls active for-sale listings from [Rentcast](https://rentcast.io) for any city or ZIP code
2. Fetches an AVM rent estimate for each property
3. Calculates **Cash-on-Cash return** using your investment assumptions (down payment, rate, expenses)
4. Filters and ranks only the properties that meet your CoC threshold

Formula (matches your Excel):
```
Initial Investment = Down Payment + Closing Costs (% of price) + Rehab
Monthly Expenses   = Mortgage P&I + Vacancy/Maintenance + Property Tax + Insurance + Mgmt + HOA
Monthly Cash Flow  = Gross Rent − Monthly Expenses
CoC Return %       = (Annual Cash Flow / Initial Investment) × 100
```

---

## Setup (one-time)

### 1. Get a free Rentcast API key
Go to [https://app.rentcast.io/app/api-keys](https://app.rentcast.io/app/api-keys) and create a free account.  
Free tier: **50 API calls/month**. Results are cached locally in `.cache/` to minimize usage.

### 2. Create your virtual environment

```bash
cd "LTR_Analyzer"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **macOS with Homebrew Python:** Use `/opt/homebrew/bin/python3 -m venv .venv`

### 3. Add your API key

```bash
cp .env.example .env
# Edit .env and paste your Rentcast API key
```

Or enter it directly in the Streamlit sidebar when running the web app.

---

## Running the apps

### Web App (Streamlit)

```bash
source .venv/bin/activate
streamlit run app.py
```

Opens at `http://localhost:8501`.

### CLI (Terminal)

```bash
source .venv/bin/activate

# Basic search — Raleigh NC, default settings (5% CoC threshold)
python cli.py --city Raleigh --state NC

# Custom filters
python cli.py --city Raleigh --state NC --max-price 350000 --beds 3 --coc 8

# With custom investment assumptions
python cli.py --city Raleigh --state NC --down 25 --rate 7.0 --closing 3.0 --vacancy 15 --tax 1.12

# Search by ZIP code
python cli.py --zip 27601 --coc 5

# Save results to CSV
python cli.py --city Raleigh --state NC --coc 5 --save

# Print full expense detail for the top property
python cli.py --city Raleigh --state NC --detail 1

# Skip API rent (use 0.8% rule — good if you're out of free-tier calls)
python cli.py --city Raleigh --state NC --no-api-rent

# All options
python cli.py --help
```

---

## Investment assumption defaults

| Setting | Default | Notes |
|---------|---------|-------|
| Down payment | 20% | |
| Closing costs | 2.5% of price | Dynamic per property |
| Interest rate | 6.5% | 30-year fixed |
| Vacancy + Maintenance | 15% of rent | Matches your spreadsheet |
| Property tax rate | 1.0% annual | Wake County (Raleigh) ≈ 1.12% |
| Insurance | 0.5% annual | Estimated; provide actual if known |
| Property management | 0% | Raise to 8–10% if using a manager |
| Rehab budget | $0 | Add per deal |

All defaults can be overridden in the Streamlit sidebar or via CLI flags.

---

## Data source: Why Rentcast?

- **Single API** for both for-sale listings AND rent estimates
- Purpose-built for real estate investors
- Covers all US markets including Raleigh NC
- Free tier covers light use; results cached to disk to minimize calls
- Alternative: [RapidAPI Zillow](https://rapidapi.com/apimaker/api/zillow-com1/) for listings + Rentcast for rent AVM

---

## Rate limit tips

- Free tier = 50 calls/month. One search (listings = 1 call) + one rent estimate per property.
- Listings are cached for **12 hours**. Rent estimates cached for **7 days**.
- Use `--no-api-rent` to skip rent AVM and use the 0.8% rule fallback (no API calls for rent).
- Upgrade to Rentcast Starter ($29/mo) for 1,000 calls/month.

---

## Files

```
LTR_Analyzer/
├── app.py            — Streamlit web app
├── cli.py            — Terminal / CLI interface  
├── calculator.py     — Pure LTR CoC math (no dependencies)
├── fetcher.py        — Rentcast API client + local cache
├── requirements.txt
├── .env.example      — Copy to .env and add your API key
├── .env              — Your API key (git-ignored, never commit)
└── .cache/           — Auto-created, stores API responses
```
