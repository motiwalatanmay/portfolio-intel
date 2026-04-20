#!/usr/bin/env python3
"""
Fetch deterministic portfolio intel: prices + news + BSE filings for each ticker.
Writes portfolio_daily.json. No LLM involvement — all data from primary/public sources.
"""

import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
import yfinance as yf

ROOT = Path(__file__).parent
TICKERS_FILE = ROOT / "tickers.json"
OUT_FILE = ROOT / "portfolio_daily.json"

IST = timezone(timedelta(hours=5, minutes=30))
NOW_IST = datetime.now(IST)
WINDOW_DAYS = 7
WINDOW_START = NOW_IST - timedelta(days=WINDOW_DAYS)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

MAX_HEADLINES_PER_TICKER = 25  # pre-filter; Tier A/B only are kept

# Tier B — reputed business media we trust as secondary source
TIER_B_DOMAINS = {
    "economictimes.indiatimes.com", "economictimes.com",
    "business-standard.com", "bsmedia.business-standard.com",
    "livemint.com", "mint.livemint.com",
    "bloomberg.com", "bloombergquint.com", "bqprime.com",
    "reuters.com",
    "moneycontrol.com",
    "cnbctv18.com",
    "financialexpress.com",
    "thehindubusinessline.com", "thehindu.com",
    "ndtvprofit.com",
    "forbesindia.com",
    "morningstar.in",
    "outlookbusiness.com",
    "fortuneindia.com",
    "smartkarma.com",
    "equitymaster.com",
}

# Tier C — explicit low-quality aggregators (hard-reject)
TIER_C_DOMAINS = {
    "scanx.trade", "tipranks.com", "multibagg.ai", "equitybulls.com",
    "businessupturn.com", "indiaipo.in", "whalesbook.com", "storyboard18.com",
    "theglobeandmail.com", "univest.in", "equitypandit.com", "marketsmojo.com",
    "msn.com", "in.investing.com", "investing.com", "stockedge.com",
    "tradingview.com", "groww.in", "5paisa.com", "icicidirect.com",
    "goodreturns.in", "nseguide.com", "walletinvestor.com",
}


def classify_source(url: str, source_name: str = "") -> str:
    """Return 'A' (primary), 'B' (reputed), or 'C' (reject)."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower().lstrip("www.")
    # Walk parent domains
    parts = host.split(".")
    candidates = {host} | {".".join(parts[i:]) for i in range(len(parts) - 1)}
    if any(d in TIER_C_DOMAINS for d in candidates):
        return "C"
    if any(d in TIER_B_DOMAINS for d in candidates):
        return "B"
    # Unknown domain → Tier C (conservative)
    return "C"


def fetch_price(nse_symbol: str) -> dict:
    """Prices via yfinance. NSE symbols need .NS suffix."""
    try:
        tk = yf.Ticker(f"{nse_symbol}.NS")
        hist = tk.history(period="7d", auto_adjust=False)
        if hist.empty:
            return {"status": "no_data"}
        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) >= 2 else last
        week_ago = hist.iloc[0]
        close = float(last["Close"])
        prev_close = float(prev["Close"])
        week_open = float(week_ago["Open"])
        return {
            "status": "ok",
            "close": round(close, 2),
            "day_change_pct": round((close - prev_close) / prev_close * 100, 2),
            "week_change_pct": round((close - week_open) / week_open * 100, 2),
            "volume": int(last["Volume"]),
            "as_of": str(hist.index[-1].date()),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


def fetch_news(company_name: str, nse_symbol: str) -> list:
    """Google News RSS per company, 7d window. Keeps only Tier B publishers."""
    query = f'"{company_name}" OR "{nse_symbol}" when:7d'
    url = (
        "https://news.google.com/rss/search?"
        + urllib.parse.urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        items = []
        for entry in feed.entries[:MAX_HEADLINES_PER_TICKER]:
            pub = entry.get("published_parsed")
            if pub:
                dt = datetime(*pub[:6], tzinfo=timezone.utc).astimezone(IST)
                if dt < WINDOW_START:
                    continue
                published = dt.strftime("%Y-%m-%d %H:%M IST")
            else:
                published = ""
            src = entry.get("source", {}) if "source" in entry else {}
            source_url = src.get("href", "") if isinstance(src, dict) else ""
            source_name = src.get("title", "") if isinstance(src, dict) else ""
            tier = classify_source(source_url, source_name)
            if tier != "B":
                continue  # Tier A covered by BSE/NSE filings; Tier C rejected
            items.append({
                "title": entry.get("title", "").strip(),
                "source": source_name,
                "source_url": source_url,
                "tier": tier,
                "published": published,
                "url": entry.get("link", ""),
            })
        return items
    except Exception as e:
        return [{"error": str(e)[:200]}]


_NSE_SESSION = None

def nse_session():
    global _NSE_SESSION
    if _NSE_SESSION is None:
        s = requests.Session()
        s.headers.update({
            **HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        })
        try:
            s.get("https://www.nseindia.com/", timeout=10)
            s.get("https://www.nseindia.com/companies-listing/corporate-filings-announcements", timeout=10)
        except Exception:
            pass
        _NSE_SESSION = s
    return _NSE_SESSION


def fetch_nse_filings(nse_symbol: str) -> list:
    """NSE corporate announcements for last 7 days."""
    try:
        s = nse_session()
        params = {
            "index": "equities",
            "from_date": WINDOW_START.strftime("%d-%m-%Y"),
            "to_date": NOW_IST.strftime("%d-%m-%Y"),
            "symbol": nse_symbol,
        }
        r = s.get("https://www.nseindia.com/api/corporate-announcements",
                  params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else data.get("data", [])
        out = []
        for row in rows[:20]:
            out.append({
                "headline": (row.get("desc") or "").strip(),
                "detail": (row.get("attchmntText") or "").strip(),
                "date": (row.get("sort_date") or "")[:10],
                "attachment": row.get("attchmntFile", "") or "",
            })
        return out
    except Exception as e:
        return [{"error": str(e)[:200]}]


_BSE_SESSION = None

def bse_session():
    global _BSE_SESSION
    if _BSE_SESSION is None:
        s = requests.Session()
        s.headers.update({
            **HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com",
        })
        try:
            s.get("https://www.bseindia.com/", timeout=10)
        except Exception:
            pass
        _BSE_SESSION = s
    return _BSE_SESSION


def fetch_bse_filings(scrip_code: str) -> list:
    """BSE corporate announcements via public JSON endpoint. Needs warmed session."""
    if not scrip_code:
        return [{"status": "no_scrip_code"}]
    try:
        s = bse_session()
        params = {
            "strCat": "-1",
            "strPrevDate": WINDOW_START.strftime("%Y%m%d"),
            "strScrip": str(scrip_code),
            "strSearch": "P",
            "strToDate": NOW_IST.strftime("%Y%m%d"),
            "strType": "C",
        }
        r = s.get("https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w",
                  params=params, timeout=15)
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("Table", []) if isinstance(payload, dict) else []
        out = []
        for row in rows[:20]:
            out.append({
                "headline": (row.get("HEADLINE") or row.get("NEWSSUB") or "").strip(),
                "subject": (row.get("NEWSSUB") or "").strip(),
                "category": row.get("CATEGORYNAME", ""),
                "sub_category": row.get("SUBCATNAME", ""),
                "date": (row.get("NEWS_DT") or "")[:10],
                "attachment": (
                    f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{row.get('ATTACHMENTNAME')}"
                    if row.get("ATTACHMENTNAME") else ""
                ),
            })
        return out
    except Exception as e:
        return [{"error": str(e)[:200]}]


def fetch_one(ticker: dict) -> dict:
    nse = ticker["nse"]
    name = ticker["name"]
    bse = ticker.get("bse", "")
    print(f"  fetching {nse}...", flush=True)
    return {
        "nse": nse,
        "bse": bse,
        "name": name,
        "price": fetch_price(nse),
        "news": fetch_news(name, nse),
        "bse_filings": fetch_bse_filings(bse),
        "nse_filings": fetch_nse_filings(nse),
    }


def main():
    tickers_data = json.loads(TICKERS_FILE.read_text())
    all_tickers = tickers_data["part1"] + tickers_data["part2"]
    print(f"Fetching intel for {len(all_tickers)} tickers...", flush=True)

    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch_one, t): t for t in all_tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                results[t["nse"]] = fut.result()
            except Exception as e:
                results[t["nse"]] = {"nse": t["nse"], "name": t["name"], "error": str(e)[:200]}
            time.sleep(0.1)

    ordered = {t["nse"]: results[t["nse"]] for t in all_tickers if t["nse"] in results}

    out = {
        "generated_at_ist": NOW_IST.strftime("%Y-%m-%d %H:%M:%S IST"),
        "window_start_ist": WINDOW_START.strftime("%Y-%m-%d %H:%M:%S IST"),
        "window_days": WINDOW_DAYS,
        "ticker_count": len(ordered),
        "tickers": ordered,
    }

    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nWrote {OUT_FILE} ({OUT_FILE.stat().st_size // 1024} KB)", flush=True)

    ok_prices = sum(1 for v in ordered.values() if v.get("price", {}).get("status") == "ok")
    news_totals = sum(len(v.get("news") or []) for v in ordered.values())
    bse_totals = sum(
        len([x for x in (v.get("bse_filings") or []) if "headline" in x])
        for v in ordered.values()
    )
    nse_totals = sum(
        len([x for x in (v.get("nse_filings") or []) if "headline" in x])
        for v in ordered.values()
    )
    print(f"Summary: {ok_prices}/{len(ordered)} prices OK, "
          f"{news_totals} Tier-B news, "
          f"{bse_totals} BSE filings, {nse_totals} NSE filings.", flush=True)


if __name__ == "__main__":
    main()
