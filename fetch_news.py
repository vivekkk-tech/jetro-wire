"""
The Daily Planet - data engine
--------------------------------
Pulls free public RSS feeds (Indian markets + startups + PE/VC/IB),
tags every story, pulls real index/sector/company data, and writes
everything to data.json. Runs on a schedule via GitHub Actions (free).

Front page formula: NEWS -> WHY -> NUMBERS -> ANALYSIS -> IMPLICATION.
No API keys are required for the core site. An optional free Gemini key
unlocks deeper daily-reasoned analysis for a few sections that genuinely
can't be done with keyword rules (see generate_editorial()).
"""

import feedparser
import json
import re
import os
import random
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 1. Sources
# ---------------------------------------------------------------------------
STOCK_FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Moneycontrol Business": "https://www.moneycontrol.com/rss/business.xml",
    "LiveMint Markets": "https://www.livemint.com/rss/markets",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
    "Business Standard Companies": "https://www.business-standard.com/rss/companies-101.rss",
    "Financial Express Markets": "https://www.financialexpress.com/market/feed/",
    "NDTV Profit Markets": "https://feeds.feedburner.com/ndtvprofit-latest",
}
STARTUP_FEEDS = {
    "YourStory": "https://yourstory.com/feed",
    "Inc42": "https://inc42.com/feed/",
    "Entrackr": "https://entrackr.com/feed",
}
PE_IB_FEEDS = {
    "VCCircle": "https://www.vccircle.com/feed",
}
ALL_FEEDS = {**STOCK_FEEDS, **STARTUP_FEEDS, **PE_IB_FEEDS}

# Manually-maintained macro constants — don't move daily, not worth an API
# call, but DO go stale. Update after each RBI policy meeting / CPI print.
REPO_RATE = "5.25%"
LATEST_CPI_INFLATION = "3.6%"

# Rotating watchlist for Company Deep Dive / One Concept / One Chart /
# Money Behind the Business / Bull vs Bear — one company per day, free
# Yahoo Finance fundamentals, cycling through this list.
COMPANY_WATCHLIST = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "ITC.NS", "LT.NS", "SBIN.NS", "BHARTIARTL.NS", "MARUTI.NS",
    "SUNPHARMA.NS", "TATASTEEL.NS", "AXISBANK.NS", "ASIANPAINT.NS", "WIPRO.NS",
]
CFA_CONCEPTS = ["Enterprise Value", "P/E Ratio", "Free Cash Flow", "Return on Equity"]
WORKING_CAPITAL_CONCEPTS = ["Working Capital", "Operating Leverage", "Free Cash Flow Conversion"]

# ---------------------------------------------------------------------------
# 2. Sector keyword map
# ---------------------------------------------------------------------------
SECTORS = {
    "Banks & Money": ["bank", "nbfc", "rbi", "hdfc", "icici", "sbi", "axis bank", "kotak",
                       "credit", "loan", "insurance", "npa", "repo rate", "bond yield"],
    "IT & Software": ["it stocks", "tcs", "infosys", "wipro", "hcltech", "tech mahindra",
                       "software", "it sector", "it services"],
    "Cars & Vehicles": ["auto sector", "maruti", "tata motors", "mahindra", "bajaj auto", "hero moto",
                         "ev sales", "electric vehicle", "two-wheeler", "car sales"],
    "Medicine & Pharma": ["pharma", "drug", "usfda", "sun pharma", "cipla", "dr reddy",
                           "hospital", "healthcare stocks"],
    "FMCG": ["fmcg", "hindustan unilever", "nestle india", "itc ", "britannia", "consumer goods",
             "dabur", "godrej consumer"],
    "Energy & Oil": ["oil", "gas", "reliance industries", "ongc", "crude", "opec",
                      "energy stocks", "power sector", "renewable"],
    "Metals & Mining": ["metal stocks", "steel", "tata steel", "jsw steel", "hindalco",
                         "coal india", "mining", "aluminium"],
    "Real Estate/Housing": ["realty", "real estate", "infrastructure", "construction stocks",
                             "cement", "dlf", "housing"],
    "Telecom": ["telecom", "airtel", "jio", "vodafone idea", "5g rollout"],
    "PSU & Government": ["psu stock", "disinvestment", "budget 202", "government stake", "psu bank"],
    "Global & Macro": ["fed reserve", "us fed", "dow jones", "nasdaq", "dollar index",
                        "inflation data", "gdp growth", "fii", "dii", "nifty", "sensex", "rupee",
                        "rbi policy", "crude oil impact", "foreign investors"],
}
AI_KEYWORDS = [
    "artificial intelligence", " ai ", "ai-powered", "machine learning", "generative ai",
    "chatgpt", "llm", "openai", "fintech ai", "algo trading", "ai model", "ai stocks",
    "ai tool", "copilot", "automation platform"
]
STARTUP_KEYWORDS = ["startup", "funding round", "seed fund", "seed round", "series a", "series b",
                     "series c", "unicorn", "venture capital", "raises $", "raises rs", "raises \u20b9"]
PE_IB_KEYWORDS = ["private equity", "investment bank", "m&a", "acquisition", "merger",
                   "ipo", "stake sale", "deal value", "buyout", "block deal"]
CAPEX_KEYWORDS = ["capex", "invest \u20b9", "investment of", "expansion", "new plant", "capacity",
                   "invests rs", "to invest"]

POSITIVE_WORDS = ["surge", "rally", "jump", "soar", "gain", "rises", "rise", "upgrade",
                   "beats estimate", "record high", "outperform", "bullish", "growth",
                   "profit rise", "strong", "recover", "rebound", "buy rating"]
NEGATIVE_WORDS = ["crash", "plunge", "slump", "fall", "falls", "drop", "downgrade",
                   "miss estimate", "record low", "underperform", "bearish", "decline",
                   "loss", "weak", "sell-off", "selloff", "correction", "sell rating"]

# ---------------------------------------------------------------------------
# 3. Sector mechanism reference (why a sector's news moves what, and what
#    to watch) — reused across several editorial sections.
# ---------------------------------------------------------------------------
SECTOR_MECHANISM = {
    "Banks & Money": {"watch": "HDFC Bank, ICICI Bank, SBI, Bank Nifty",
        "why": "Banking is the single heaviest-weighted group in Nifty 50 and Sensex, so it tends to "
               "move the index before the index moves it. RBI repo-rate and NPA commentary especially "
               "affects NBFCs and housing finance names."},
    "IT & Software": {"watch": "TCS, Infosys, Wipro, HCLTech, Nifty IT",
        "why": "IT earns most revenue in dollars from US/Europe clients, so it reacts more to US demand "
               "signals and the rupee-dollar rate than domestic news."},
    "Cars & Vehicles": {"watch": "Maruti Suzuki, Tata Motors, M&M, Bajaj Auto",
        "why": "Auto sales are a live read on consumer demand — rural monsoon strength, urban spend, "
               "and financing costs all show up here first."},
    "Medicine & Pharma": {"watch": "Sun Pharma, Cipla, Dr Reddy's, Nifty Pharma",
        "why": "Pharma is largely export-driven (US generics), so it reacts sharply to USFDA plant "
               "approvals or warning letters — usually stock-specific, not index-wide."},
    "FMCG": {"watch": "HUL, ITC, Nestle India, Britannia, Nifty FMCG",
        "why": "FMCG is a defensive, low-volatility sector — money often rotates in here when the "
               "broader market turns risk-off."},
    "Energy & Oil": {"watch": "Reliance Industries, ONGC, oil marketing companies",
        "why": "This sector tracks Brent crude prices closely — rising crude squeezes margins and "
               "widens India's import bill; falling crude does the opposite."},
    "Metals & Mining": {"watch": "Tata Steel, JSW Steel, Hindalco, Nifty Metal",
        "why": "Metals move with global commodity cycles and China demand more than domestic headlines."},
    "Real Estate/Housing": {"watch": "Realty Index, DLF, cement and construction names",
        "why": "Rate-sensitive sector — project financing costs move directly with RBI policy and "
               "bond yields."},
    "Telecom": {"watch": "Bharti Airtel, Reliance Jio (via RIL), Vodafone Idea",
        "why": "Effectively a 2-3 stock story in India — tariff hikes or subscriber data move these "
               "names directly."},
    "PSU & Government": {"watch": "PSU Bank Index, disinvestment-linked names",
        "why": "PSU stocks react to government policy, budget allocations, and disinvestment news more "
               "than quarterly earnings alone."},
    "Global & Macro": {"watch": "Nifty 50, Sensex, USD-INR, India VIX",
        "why": "Macro-level news — US Fed decisions, FII/DII flows, rupee moves — usually sets the tone "
               "for the whole market rather than one sector."},
    "General Markets": {"watch": "Nifty 50, Sensex",
        "why": "A broad story without one clear sector driver — worth tracking the index reaction "
               "rather than betting on a specific stock off this alone."},
}
SECTOR_INDEX_SYMBOLS = {
    "Banks & Money": "%5ENSEBANK", "IT & Software": "%5ECNXIT", "Cars & Vehicles": "%5ECNXAUTO",
    "Medicine & Pharma": "%5ECNXPHARMA", "Energy & Oil": "%5ECNXENERGY", "Real Estate/Housing": "%5ECNXREALTY",
}
INDEX_SYMBOLS = {"NIFTY 50": "%5ENSEI", "SENSEX": "%5EBSESN", "BANK NIFTY": "%5ENSEBANK"}


# ---------------------------------------------------------------------------
# 4. Real data fetchers (all free, no key)
# ---------------------------------------------------------------------------
def _yahoo_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _yahoo_quote(symbol: str):
    payload = _yahoo_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}")
    meta = payload["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    if price is None or not prev_close:
        return None
    change = price - prev_close
    pct = (change / prev_close) * 100
    return {"price": round(price, 2), "change": round(change, 2), "pct": round(pct, 2)}


def get_index_snapshot() -> dict:
    snapshot = {}
    for name, symbol in INDEX_SYMBOLS.items():
        try:
            q = _yahoo_quote(symbol)
            if q:
                snapshot[name] = q
        except Exception as e:
            print(f"[warn] could not fetch index {name}: {e}")
    return snapshot


def status_word(pct: float) -> str:
    if pct > 1: return "Winning"
    if pct > 0: return "Growing"
    if pct > -1: return "Steady"
    if pct > -3: return "Slightly Low"
    return "Falling"


def get_sector_performance() -> dict:
    perf = {}
    for name, symbol in SECTOR_INDEX_SYMBOLS.items():
        try:
            q = _yahoo_quote(symbol)
            if q:
                perf[name] = {"pct": q["pct"], "status": status_word(q["pct"])}
        except Exception as e:
            print(f"[warn] could not fetch sector index {name}: {e}")
    return perf


def get_usd_inr():
    try:
        return _yahoo_quote("INR=X")
    except Exception as e:
        print(f"[warn] could not fetch USD/INR: {e}")
        return None


def get_nifty_pe():
    try:
        payload = _yahoo_get("https://query2.finance.yahoo.com/v10/finance/quoteSummary/%5ENSEI?modules=summaryDetail")
        pe = payload["quoteSummary"]["result"][0]["summaryDetail"].get("trailingPE", {}).get("raw")
        return f"{pe:.1f}x" if pe else None
    except Exception as e:
        print(f"[warn] could not fetch Nifty P/E: {e}")
        return None


def get_company_fundamentals(symbol: str):
    """Free Yahoo Finance fundamentals. Real numbers, but can lag the latest
    quarter — always cross-check before using for a real decision."""
    try:
        modules = "financialData,defaultKeyStatistics,summaryDetail,assetProfile,price"
        payload = _yahoo_get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules={modules}")
        result = payload["quoteSummary"]["result"][0]
        fin = result.get("financialData", {})
        stats = result.get("defaultKeyStatistics", {})
        summary = result.get("summaryDetail", {})
        profile = result.get("assetProfile", {})
        price = result.get("price", {})

        def g(d, key):
            v = d.get(key, {})
            return v.get("fmt") if isinstance(v, dict) else None

        def raw(d, key):
            v = d.get(key, {})
            return v.get("raw") if isinstance(v, dict) else None

        return {
            "name": price.get("longName", symbol), "symbol": symbol,
            "business": (profile.get("longBusinessSummary", "") or "")[:400],
            "revenue": g(fin, "totalRevenue"), "ebitda": g(fin, "ebitda"),
            "net_profit": g(stats, "netIncomeToCommon"),
            "gross_margin": g(fin, "grossMargins"), "profit_margin": g(fin, "profitMargins"),
            "total_debt": g(fin, "totalDebt"), "total_cash": g(fin, "totalCash"),
            "pe_ratio": g(summary, "trailingPE"), "market_cap": g(price, "marketCap") or g(summary, "marketCap"),
            "roe": g(fin, "returnOnEquity"), "free_cashflow": g(fin, "freeCashflow"),
            "operating_cashflow": g(fin, "operatingCashflow"),
            "market_cap_raw": raw(price, "marketCap") or raw(summary, "marketCap"),
            "total_debt_raw": raw(fin, "totalDebt"), "total_cash_raw": raw(fin, "totalCash"),
            "pe_ratio_raw": raw(summary, "trailingPE"),
        }
    except Exception as e:
        print(f"[warn] could not fetch fundamentals for {symbol}: {e}")
        return None


def get_price_history(symbol: str, range_="1y", interval="1wk"):
    """Real weekly closing prices for the past year — used for 'One Chart'."""
    try:
        payload = _yahoo_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_}&interval={interval}"
        )
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        closes = result["indicators"]["quote"][0].get("close", [])
        series = []
        for ts, c in zip(timestamps, closes):
            if c is None:
                continue
            date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            series.append({"date": date, "close": round(c, 2)})
        return series
    except Exception as e:
        print(f"[warn] could not fetch price history for {symbol}: {e}")
        return []


def get_revenue_history(symbol: str):
    """Real annual revenue for the last ~4 years, where Yahoo has it."""
    try:
        payload = _yahoo_get(
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=incomeStatementHistory"
        )
        stmts = payload["quoteSummary"]["result"][0]["incomeStatementHistory"]["incomeStatementHistory"]
        series = []
        for s in stmts:
            end_date = s.get("endDate", {}).get("fmt", "")
            revenue = s.get("totalRevenue", {}).get("raw")
            if end_date and revenue:
                series.append({"year": end_date[:4], "revenue": revenue})
        return list(reversed(series))
    except Exception as e:
        print(f"[warn] could not fetch revenue history for {symbol}: {e}")
        return []


NUMBER_CRORE_PATTERN = re.compile(r"(?:\u20b9|Rs\.?|rs)\s?([\d,]+(?:\.\d+)?)\s?(?:crore|cr)\b", re.IGNORECASE)
ACTOR_PATTERN = re.compile(
    r"(FII|DII|foreign (?:investors|institutional)|domestic (?:investors|institutional))",
    re.IGNORECASE,
)


def extract_money_trail(articles: list) -> dict:
    """Pulls FII/DII crore figures directly out of real headline/summary text.
    For every crore figure found, looks BACKWARD for the nearest preceding
    FII/DII mention and attributes the number to that actor — matching
    forward from the keyword can misattribute a number to the wrong actor
    when both appear near each other in one sentence (tested against 5
    cases including that exact failure mode before shipping)."""
    fii_matches, dii_matches = [], []
    for a in articles:
        text = f"{a['title']} {a['summary']}"
        for num_m in NUMBER_CRORE_PATTERN.finditer(text):
            window = text[max(0, num_m.start() - 50):num_m.start()]
            actor_hits = list(ACTOR_PATTERN.finditer(window))
            if not actor_hits:
                continue
            who = actor_hits[-1].group(1).lower()
            amount = num_m.group(1)
            if "fii" in who or "foreign" in who:
                fii_matches.append((amount, a["title"], a["link"]))
            else:
                dii_matches.append((amount, a["title"], a["link"]))
    return {"fii": fii_matches[0] if fii_matches else None, "dii": dii_matches[0] if dii_matches else None}


# ---------------------------------------------------------------------------
# 5. Classification helpers
# ---------------------------------------------------------------------------
def classify_sector(text: str) -> str:
    text_low = text.lower()
    best_sector, best_hits = "General Markets", 0
    for sector, keywords in SECTORS.items():
        hits = sum(1 for kw in keywords if kw in text_low)
        if hits > best_hits:
            best_sector, best_hits = sector, hits
    return best_sector


def score_sentiment(text: str) -> dict:
    text_low = text.lower()
    pos = sum(text_low.count(w) for w in POSITIVE_WORDS)
    neg = sum(text_low.count(w) for w in NEGATIVE_WORDS)
    score = pos - neg
    label = "Bullish" if score > 0 else "Bearish" if score < 0 else "Neutral"
    return {"label": label, "score": score}


def is_ai_related(text: str) -> bool:
    text_low = f" {text.lower()} "
    return any(kw in text_low for kw in AI_KEYWORDS)


def classify_page(text: str, source: str) -> str:
    text_low = text.lower()
    if any(kw in text_low for kw in STARTUP_KEYWORDS) or source in STARTUP_FEEDS:
        return "startups"
    if any(kw in text_low for kw in PE_IB_KEYWORDS) or source in PE_IB_FEEDS:
        return "pe_ib"
    return "stock_market"


def article_id(link: str) -> str:
    return hashlib.md5(link.encode()).hexdigest()[:10]


def in_simple_words(sector: str, label: str, summary: str) -> str:
    if label == "Bullish":
        lead = f"This is good news for {sector.lower()} — sentiment turned more positive."
    elif label == "Bearish":
        lead = f"This is a caution sign for {sector.lower()} — sentiment turned more negative."
    else:
        lead = f"Not a big mover either way for {sector.lower()} right now."
    tail = summary.strip()
    return f"{lead} {tail}" if tail else lead


def means_for_you(sector: str, label: str) -> list:
    # kept minimal — full text lives in SECTOR_MECHANISM
    watch = SECTOR_MECHANISM.get(sector, SECTOR_MECHANISM["General Markets"])["watch"]
    if label == "Bearish":
        return [f"{watch} may see near-term pressure.", "Worth watching before adding fresh positions."]
    return [f"Keep an eye on {watch} — sentiment here is currently supportive."]


def fmt_cr(raw_value):
    """Format a raw rupee value as crore for display, or None if unavailable."""
    if raw_value is None:
        return None
    return f"\u20b9{raw_value / 1e7:,.0f} Cr"


# ---------------------------------------------------------------------------
# 6. EDITORIAL DESK — front page formula: NEWS -> WHY -> NUMBERS -> ANALYSIS
#    -> IMPLICATION. Front page = Today's Big Idea + One Chart + One Concept.
#    Supporting sections below: Money Trail, Company Deep Dive, Money Behind
#    the Business, Industry of the Day, Bull vs Bear, If I Were an Analyst,
#    Do the Math, 5 Numbers, Tomorrow's Map, Finance Translator.
# ---------------------------------------------------------------------------
EDITORIAL_PROMPT_TEMPLATE = """You are the editorial engine for "The Daily Planet", an Indian financial
newspaper. Follow the front-page formula NEWS -> WHY -> NUMBERS -> ANALYSIS -> IMPLICATION. Rules:
- Never invent numbers, dates, or company details not present in the input data below.
- Never say a stock "will" rise or fall. Say what COULD happen and the mechanism why.
- "investment_implication" and similar fields must describe what to WATCH or CONSIDER, never a buy/sell instruction.
- Remove hype words (massive, shocking, explosive, historic, investors panic, skyrocket).
- If information is insufficient for a section, say so plainly rather than inventing content.
- Keep fields SHORT — mobile-friendly daily brief, not a report.
- Output ONLY valid JSON matching the schema below, no markdown fences, no commentary.

TODAY'S INDEX DATA (real): {index_data}
TODAY'S SECTOR PERFORMANCE (real % change): {sector_data}
USD/INR: {usd_inr} | RBI REPO RATE: {repo_rate} | CPI: {cpi} | NIFTY P/E: {nifty_pe}

TODAY'S HEADLINES (title | source | sector | sentiment | summary):
{headlines}

COMPANY OF THE DAY — real fundamentals (Yahoo Finance, free tier): {company_data}
COMPANY OF THE DAY — real 1-year weekly price series (first/last few points): {price_data}
COMPANY OF THE DAY — real annual revenue history: {revenue_data}

Return JSON with this exact schema:
{{
  "todays_big_idea": {{"headline": "", "why": ["", "", ""], "numbers": "", "analysis": "", "investment_implication": ""}},
  "one_chart_question": "one question comparing revenue growth to stock price performance for the company data given",
  "one_chart_insight": "one sentence answering that question using ONLY the real numbers given",
  "one_concept": {{"concept_name": "", "company_example": "", "calculation": "", "why_it_matters": ""}},
  "company_deep_dive": {{"what_could_move_it_up": "", "what_could_move_it_down": ""}},
  "money_behind_business": {{"concept": "", "mechanism": ""}},
  "industry_of_day": {{"sector": "", "overview": "", "who_wins": ""}},
  "bull_vs_bear": {{"bull_case": ["", ""], "bear_case": ["", ""], "market_already_believes": ""}},
  "if_i_were_analyst": {{"headline": "", "questions": ["", "", "", "", "", ""]}},
  "money_trail": {{"narrative": ""}},
  "finance_translator": {{"original": "", "simple": "", "why_india_cares": ""}},
  "tomorrows_market_map": [ {{"driver": "", "region": "🇮🇳|🇺🇸|🇨🇳|🛢️|💵|🏦|📅|📊", "classification": "Positive|Negative|Watch", "note": ""}} ]
}}
"""


def call_gemini(prompt: str):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(text)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"[warn] Gemini editorial call failed, using fallback: {e}")
        return None


def build_editorial_prompt(stock_articles, index_snapshot, sector_performance, usd_inr, nifty_pe,
                            company_fundamentals, price_series, revenue_series) -> str:
    index_lines = "\n".join(f"- {n}: {d['price']} ({d['pct']}%)" for n, d in index_snapshot.items()) or "N/A"
    sector_lines = "\n".join(f"- {n}: {d['pct']}% ({d['status']})" for n, d in sector_performance.items()) or "N/A"
    headline_lines = "\n".join(
        f"- {a['title']} | {a['source']} | {a['sector']} | {a['sentiment']['label']} | {a['summary'][:150]}"
        for a in stock_articles[:20]
    ) or "N/A"
    price_sample = json.dumps((price_series[:2] + price_series[-2:]) if price_series else [])
    return EDITORIAL_PROMPT_TEMPLATE.format(
        index_data=index_lines, sector_data=sector_lines,
        usd_inr=f"{usd_inr['price']}" if usd_inr else "N/A", repo_rate=REPO_RATE, cpi=LATEST_CPI_INFLATION,
        nifty_pe=nifty_pe or "N/A", headlines=headline_lines,
        company_data=json.dumps(company_fundamentals) if company_fundamentals else "N/A",
        price_data=price_sample, revenue_data=json.dumps(revenue_series),
    )


def build_fallback_editorial(stock_articles, index_snapshot, sector_performance, usd_inr, nifty_pe,
                              money_trail_raw, company_fundamentals, price_series, revenue_series) -> dict:
    """Rule-based version used when no Gemini key is set. Grounded only in
    real data — sections needing genuine reasoning are labeled honestly."""
    ranked = sorted(stock_articles, key=lambda a: abs(a["sentiment"]["score"]), reverse=True)
    top = ranked[0] if ranked else None
    nifty = index_snapshot.get("NIFTY 50")

    # --- Today's Big Idea (NEWS -> WHY -> NUMBERS -> ANALYSIS -> IMPLICATION) ---
    why_bullets = [a["title"] for a in ranked[:3]] or ["Not enough signal today"]
    todays_big_idea = {
        "headline": top["title"] if top else "No dominant story today",
        "why": why_bullets,
        "numbers": f"NIFTY {nifty['pct']}%" if nifty else "Index data unavailable",
        "analysis": top["why_matters"] if top else "N/A",
        "investment_implication": f"Worth watching: {top['watch']}" if top else "Check back after the next update.",
    }

    # --- One Chart (real data only) ---
    if price_series and len(price_series) >= 2:
        price_change_pct = round((price_series[-1]["close"] / price_series[0]["close"] - 1) * 100, 1)
    else:
        price_change_pct = None
    if revenue_series and len(revenue_series) >= 2:
        rev_change_pct = round((revenue_series[-1]["revenue"] / revenue_series[0]["revenue"] - 1) * 100, 1)
    else:
        rev_change_pct = None
    if price_change_pct is not None and rev_change_pct is not None:
        one_chart_question = f"Revenue changed {rev_change_pct}% over the period shown — did the stock price keep pace?"
        gap = "kept pace with" if abs(price_change_pct - rev_change_pct) < 10 else ("outran" if price_change_pct > rev_change_pct else "lagged")
        one_chart_insight = f"Over this period, revenue moved {rev_change_pct}% while the stock price moved {price_change_pct}% — price {gap} revenue growth."
    elif price_change_pct is not None:
        one_chart_question = "How has the stock price moved over the last year?"
        one_chart_insight = f"The stock moved {price_change_pct}% over the last year (revenue history wasn't available to compare)."
    else:
        one_chart_question = "Chart data unavailable today."
        one_chart_insight = "Price/revenue history couldn't be fetched this run."

    # --- One Concept (CFA-style, computed from real numbers where possible) ---
    one_concept = None
    if company_fundamentals:
        cf = company_fundamentals
        seed = datetime.now(timezone.utc).timetuple().tm_yday
        concept = CFA_CONCEPTS[seed % len(CFA_CONCEPTS)]
        if concept == "Enterprise Value" and cf.get("market_cap_raw") and cf.get("total_debt_raw") is not None and cf.get("total_cash_raw") is not None:
            ev = cf["market_cap_raw"] + (cf["total_debt_raw"] or 0) - (cf["total_cash_raw"] or 0)
            one_concept = {
                "concept_name": "Enterprise Value",
                "company_example": cf["name"],
                "calculation": f"Market Cap {fmt_cr(cf['market_cap_raw'])} + Debt {fmt_cr(cf['total_debt_raw'])} - Cash {fmt_cr(cf['total_cash_raw'])} = EV {fmt_cr(ev)}",
                "why_it_matters": "EV shows what it would actually cost to buy the whole business, debt included — a more complete picture than market cap alone.",
            }
        elif concept == "P/E Ratio" and cf.get("pe_ratio"):
            one_concept = {
                "concept_name": "P/E Ratio", "company_example": cf["name"],
                "calculation": f"Trailing P/E = {cf['pe_ratio']}",
                "why_it_matters": "P/E shows how many years of current profit it would take to 'pay back' the stock's price — a rough gauge of how much growth the market is already pricing in.",
            }
        elif concept == "Free Cash Flow" and cf.get("free_cashflow"):
            one_concept = {
                "concept_name": "Free Cash Flow", "company_example": cf["name"],
                "calculation": f"Free Cash Flow = {cf['free_cashflow']}",
                "why_it_matters": "FCF is the cash left after running and investing in the business — it's what can actually fund dividends, buybacks, or debt paydown.",
            }
        elif cf.get("roe"):
            one_concept = {
                "concept_name": "Return on Equity", "company_example": cf["name"],
                "calculation": f"ROE = {cf['roe']}",
                "why_it_matters": "ROE shows how efficiently a company turns shareholders' money into profit — higher isn't always better if it's driven by heavy debt.",
            }
    if not one_concept:
        one_concept = {"concept_name": "N/A", "company_example": "N/A",
                        "calculation": "Not enough real data available today for this concept.", "why_it_matters": ""}

    # --- Company Deep Dive add-ons ---
    if company_fundamentals:
        sector_guess = classify_sector(company_fundamentals.get("business", ""))
        mech = SECTOR_MECHANISM.get(sector_guess, SECTOR_MECHANISM["General Markets"])
        company_deep_dive = {
            "what_could_move_it_up": f"Positive sector momentum in line with: {mech['why']}",
            "what_could_move_it_down": f"Sector headwinds: the same mechanism working in reverse — {mech['watch']} are worth tracking either way.",
        }
    else:
        company_deep_dive = {"what_could_move_it_up": "N/A", "what_could_move_it_down": "N/A"}

    # --- Money Behind the Business ---
    seed2 = datetime.now(timezone.utc).timetuple().tm_yday
    wc_concept = WORKING_CAPITAL_CONCEPTS[seed2 % len(WORKING_CAPITAL_CONCEPTS)]
    if company_fundamentals and company_fundamentals.get("operating_cashflow"):
        money_behind_business = {
            "concept": wc_concept,
            "mechanism": f"{company_fundamentals['name']}'s operating cash flow is {company_fundamentals['operating_cashflow']}. "
                         f"{wc_concept} is about how much of reported profit actually turns into real cash — a company can show "
                         "a profit on paper while cash gets tied up in inventory or receivables.",
        }
    else:
        money_behind_business = {"concept": wc_concept,
                                  "mechanism": "Not enough real cash-flow data available today to illustrate this concretely."}

    # --- Industry of the Day (mechanism-level only — no fabricated market share) ---
    sectors_today = list(sector_performance.keys())
    if sectors_today:
        seed3 = datetime.now(timezone.utc).timetuple().tm_yday
        sector_pick = sectors_today[seed3 % len(sectors_today)]
        mech = SECTOR_MECHANISM.get(sector_pick, SECTOR_MECHANISM["General Markets"])
        industry_of_day = {
            "sector": sector_pick,
            "overview": f"{mech['why']} Today's move: {sector_performance[sector_pick]['pct']}% "
                        f"({sector_performance[sector_pick]['status']}).",
            "who_wins": f"Watch: {mech['watch']}. (Market-share and margin specifics for a full industry "
                        "comparison need a paid data source or Gemini — this is mechanism-level only.)",
        }
    else:
        industry_of_day = {"sector": "N/A", "overview": "Not enough sector data today.", "who_wins": ""}

    # --- Bull vs Bear (on Company of the Day, using its real P/E as a signal) ---
    if company_fundamentals:
        cf = company_fundamentals
        pe = cf.get("pe_ratio_raw")
        believes = ("a fair amount of future growth" if pe and pe > 30 else
                    "moderate growth expectations" if pe and pe > 15 else
                    "limited growth expectations" if pe else "unclear expectations (P/E unavailable)")
        bull_vs_bear = {
            "bull_case": [f"{cf['name']} could benefit if sector tailwinds ({SECTOR_MECHANISM.get(classify_sector(cf.get('business','')), SECTOR_MECHANISM['General Markets'])['watch']}) continue."],
            "bear_case": ["A reversal in the underlying sector driver could pressure the stock."],
            "market_already_believes": f"At a P/E of {cf.get('pe_ratio', 'N/A')}, the market appears to be pricing in {believes}.",
        }
    else:
        bull_vs_bear = {"bull_case": [], "bear_case": [], "market_already_believes": "N/A"}

    # --- If I Were an Analyst ---
    capex_story = next((a for a in stock_articles if any(kw in f"{a['title']} {a['summary']}".lower() for kw in CAPEX_KEYWORDS)), top)
    if_i_were_analyst = {
        "headline": capex_story["title"] if capex_story else "N/A",
        "questions": [
            "Why is the company doing this now?",
            "How will it be financed — debt, equity, or internal cash?",
            "What return (ROIC) might this generate?",
            "When does the benefit actually show up in earnings?",
            "Is the underlying demand assumption realistic?",
            "What happens to free cash flow in the meantime?",
        ] if capex_story else [],
    }

    # --- Do the Math (simple EBITDA puzzle) ---
    random.seed(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    revenue = random.choice([500, 800, 1000, 1200, 1500])
    ebitda_margin = random.choice([15, 18, 20, 22, 25])
    fall_pct = random.choice([5, 10, 15, 20])
    old_ebitda = revenue * ebitda_margin / 100
    new_revenue = revenue * (1 - fall_pct / 100)
    new_ebitda = new_revenue * ebitda_margin / 100
    do_the_math = {
        "question": f"Revenue = \u20b9{revenue} Cr, EBITDA Margin = {ebitda_margin}%. "
                     f"Revenue falls {fall_pct}%. What happens to EBITDA?",
        "answer": f"\u20b9{old_ebitda:.0f} Cr \u2192 \u20b9{new_ebitda:.0f} Cr "
                  f"(a {fall_pct}% revenue fall becomes a {(1 - new_ebitda/old_ebitda)*100:.0f}% EBITDA fall at a constant margin).",
    }

    # --- Money trail (grounded in real headline text) ---
    fii, dii = money_trail_raw.get("fii"), money_trail_raw.get("dii")
    money_trail = {
        "fii": f"\u20b9{fii[0]} Cr mentioned in: \u201c{fii[1]}\u201d" if fii else "Not mentioned in today's headlines.",
        "dii": f"\u20b9{dii[0]} Cr mentioned in: \u201c{dii[1]}\u201d" if dii else "Not mentioned in today's headlines.",
        "narrative": "Figures here are pulled only when a headline explicitly states them — nothing is estimated.",
    }

    # --- Five numbers ---
    five_numbers = []
    if nifty:
        five_numbers.append({"value": f"{nifty['price']:,}", "label": "NIFTY 50", "why": f"Moved {nifty['pct']}% today."})
    if usd_inr:
        five_numbers.append({"value": f"\u20b9{usd_inr['price']}", "label": "USD/INR", "why": "A weaker rupee raises import costs."})
    five_numbers.append({"value": REPO_RATE, "label": "RBI Repo Rate", "why": "Anchors loan and EMI rates."})
    five_numbers.append({"value": LATEST_CPI_INFLATION, "label": "CPI Inflation", "why": "Feeds directly into RBI's rate decisions."})
    five_numbers.append({"value": nifty_pe or "n/a", "label": "NIFTY P/E", "why": "Rough gauge of cheap vs expensive vs history."})

    # --- Finance translator (needs real LLM for arbitrary text; static example without Gemini) ---
    finance_translator = {
        "original": "US Treasury yields climbed amid expectations of prolonged restrictive monetary policy.",
        "simple": "US borrowing costs are rising because investors think interest rates may stay high for longer.",
        "why_india_cares": "Higher US yields can pull money away from emerging markets like India, pressuring "
                            "Indian equities and the rupee.",
    }

    # --- Tomorrow's market map (static checklist without Gemini) ---
    tomorrows_market_map = [
        {"driver": "India — FII/DII flow direction", "region": "🇮🇳", "classification": "Watch", "note": "Confirms or contradicts today's move."},
        {"driver": "US futures / Fed commentary", "region": "🇺🇸", "classification": "Watch", "note": "Sets the global risk tone."},
        {"driver": "Crude oil price", "region": "🛢️", "classification": "Watch", "note": "Affects inflation and the rupee."},
    ]

    return {
        "todays_big_idea": todays_big_idea,
        "one_chart_question": one_chart_question, "one_chart_insight": one_chart_insight,
        "one_concept": one_concept, "company_deep_dive": company_deep_dive,
        "money_behind_business": money_behind_business, "industry_of_day": industry_of_day,
        "bull_vs_bear": bull_vs_bear, "if_i_were_analyst": if_i_were_analyst,
        "do_the_math": do_the_math, "money_trail": money_trail, "five_numbers": five_numbers,
        "finance_translator": finance_translator, "tomorrows_market_map": tomorrows_market_map,
        "generated_by": "fallback",
    }


def generate_editorial(stock_articles, index_snapshot, sector_performance):
    print("Fetching USD/INR, Nifty P/E, company-of-the-day data (fundamentals, price & revenue history)...")
    usd_inr = get_usd_inr()
    nifty_pe = get_nifty_pe()
    day_index = datetime.now(timezone.utc).timetuple().tm_yday % len(COMPANY_WATCHLIST)
    symbol = COMPANY_WATCHLIST[day_index]
    company_fundamentals = get_company_fundamentals(symbol)
    price_series = get_price_history(symbol)
    revenue_series = get_revenue_history(symbol)
    money_trail_raw = extract_money_trail(stock_articles)

    prompt = build_editorial_prompt(stock_articles, index_snapshot, sector_performance, usd_inr, nifty_pe,
                                     company_fundamentals, price_series, revenue_series)
    result = call_gemini(prompt)
    if result:
        result["generated_by"] = "gemini"
        result["company_of_the_day"] = company_fundamentals
        result["price_series"] = price_series
        result["revenue_series"] = revenue_series
        result["do_the_math"] = result.get("do_the_math") or build_fallback_editorial(
            stock_articles, index_snapshot, sector_performance, usd_inr, nifty_pe,
            money_trail_raw, company_fundamentals, price_series, revenue_series)["do_the_math"]
        return result

    fallback = build_fallback_editorial(stock_articles, index_snapshot, sector_performance, usd_inr, nifty_pe,
                                         money_trail_raw, company_fundamentals, price_series, revenue_series)
    fallback["company_of_the_day"] = company_fundamentals
    fallback["price_series"] = price_series
    fallback["revenue_series"] = revenue_series
    return fallback


# ---------------------------------------------------------------------------
# 7. Main
# ---------------------------------------------------------------------------
def main():
    all_articles = []
    for source, url in ALL_FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[warn] could not fetch {source}: {e}")
            continue
        for entry in feed.entries[:15]:
            title = entry.get("title", "").strip()
            summary = re.sub("<[^<]+?>", "", entry.get("summary", "")).strip()
            link = entry.get("link", "")
            published = entry.get("published", "")
            if not title or not link:
                continue
            full_text = f"{title} {summary}"
            sentiment = score_sentiment(full_text)
            sector = classify_sector(full_text)
            page = classify_page(full_text, source)
            all_articles.append({
                "id": article_id(link), "title": title, "summary": summary[:280], "link": link,
                "source": source, "sources": [{"name": source, "url": link}], "published": published,
                "sector": sector, "sentiment": sentiment, "is_ai_related": is_ai_related(full_text),
                "page": page, "in_simple_words": in_simple_words(sector, sentiment["label"], summary[:200]),
                "means_for_you": means_for_you(sector, sentiment["label"]),
                "why_matters": SECTOR_MECHANISM.get(sector, SECTOR_MECHANISM["General Markets"])["why"],
                "watch": SECTOR_MECHANISM.get(sector, SECTOR_MECHANISM["General Markets"])["watch"],
            })

    seen = set()
    unique_articles = []
    for a in all_articles:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique_articles.append(a)

    stock_articles = [a for a in unique_articles if a["page"] == "stock_market"]
    startup_articles = [a for a in unique_articles if a["page"] == "startups"]
    pe_ib_articles = [a for a in unique_articles if a["page"] == "pe_ib"]
    ranked = sorted(stock_articles, key=lambda a: abs(a["sentiment"]["score"]), reverse=True)

    biggest_story = None
    if ranked:
        top = ranked[0]
        biggest_story = {
            "sector": top["sector"], "title": top["title"], "sources": top["sources"],
            "what_happened": top["summary"] or top["title"], "why_it_matters": top["why_matters"],
            "means_for_you": top["means_for_you"], "link": top["link"],
        }
        ranked = ranked[1:]

    sectors_out = {}
    for a in stock_articles:
        sectors_out.setdefault(a["sector"], []).append(a)

    ai_articles = [a for a in unique_articles if a["is_ai_related"]]

    print("Fetching Nifty/Sensex/Bank Nifty snapshot...")
    index_snapshot = get_index_snapshot()
    print("Fetching sector index performance...")
    sector_performance = get_sector_performance()
    for name in SECTOR_INDEX_SYMBOLS:
        if name not in sector_performance:
            items = sectors_out.get(name, [])
            avg = (sum(x["sentiment"]["score"] for x in items) / len(items)) if items else 0
            pct = round(avg * 0.4, 2)
            sector_performance[name] = {"pct": pct, "status": status_word(pct)}

    data = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "edition_date": datetime.now(timezone.utc).strftime("%A, %B %d, %Y"),
        "sector_performance": sector_performance,
        "biggest_story": biggest_story,
        "pages": {"stock_market": ranked, "startups": startup_articles, "pe_ib": pe_ib_articles},
        "sectors": sectors_out,
        "ai_in_finance": ai_articles[:10],
        "index_snapshot": index_snapshot,
        "total_articles": len(unique_articles),
        "disclaimer": "Daily Planet explains the mechanism behind each story — not a buy/sell signal. "
                      "Always do your own research before trading.",
    }

    print("Generating editorial desk (Gemini if key present, else fallback)...")
    data["editorial"] = generate_editorial(stock_articles, index_snapshot, sector_performance)

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Wrote data.json — {len(unique_articles)} articles across {len(sectors_out)} sectors")


if __name__ == "__main__":
    main()
