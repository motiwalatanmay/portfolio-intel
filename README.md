# portfolio-intel

Deterministic daily fetch of price, Tier-B news, and primary BSE/NSE filings for 30 portfolio tickers.

## Output

`portfolio_daily.json` — regenerated every 07:00 IST by GitHub Actions.

Schema per ticker:
- `price`: close, day_change_pct, week_change_pct, volume, as_of (via yfinance)
- `news`: Tier-B publishers only (ET, Mint, BS, Moneycontrol, Reuters, Bloomberg, etc.) from Google News RSS, 7-day window
- `bse_filings`: BSE corporate announcements, 7-day window, with PDF attachment URLs
- `nse_filings`: NSE corporate announcements, 7-day window, with PDF attachment URLs

## Purpose

Grounds downstream Claude Routines so they synthesize from real, cited data instead of hallucinating prices or inventing news.

## Run locally

```
pip install -r requirements.txt
python fetch_portfolio_intel.py
```

## Editing tickers

Edit `tickers.json` (requires both NSE symbol and BSE scrip code per ticker).
