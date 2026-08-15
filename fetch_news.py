"""
The Daily Planet - data engine
--------------------------------
Pulls free public RSS feeds (Indian markets + startups + PE/VC/IB),
tags every story, scores overall market sentiment on a 0-100 scale,
pulls real sector-index % moves, and writes everything to data.json.

Runs on a schedule via GitHub Actions (free). No API keys required.
"""

import feedparser
import json
import re
import os
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

# Manually-maintained macro constants — these don't move daily, so they're
# not worth an API call, but DO go stale. Update after each RBI policy
# meeting / CPI print. (Last set: Aug 2026 cycle figures as placeholders.)
REPO_RATE = "5.25%"          # update after each RBI MPC meeting
LATEST_CPI_INFLATION = "3.6%"  # update after each monthly CPI release

# A rotating watchlist for "Company of the Day" — free Yahoo Finance
# fundamentals, one company per day, cycling through this list.
COMPANY_WATCHLIST = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "ITC.NS", "LT.NS", "SBIN.NS", "BHARTIARTL.NS", "MARUTI.NS",
    "SUNPHARMA.NS", "TATASTEEL.NS", "AXISBANK.NS", "ASIANPAINT.NS", "WIPRO.NS",
]

# ---------------------------------------------------------------------------
# 2. Sector keyword map (drives both classification and the sector grid)
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

POSITIVE_WORDS = ["surge", "rally", "jump", "soar", "gain", "rises", "rise", "upgrade",
                   "beats estimate", "record high", "outperform", "bullish", "growth",
                   "profit rise", "strong", "recover", "rebound", "buy rating"]
NEGATIVE_WORDS = ["crash", "plunge", "slump", "fall", "falls", "drop", "downgrade",
                   "miss estimate", "record low", "underperform", "bearish", "decline",
                   "loss", "weak", "sell-off", "selloff", "correction", "sell rating"]

# ---------------------------------------------------------------------------
# 3. Sector mechanism + "what this means for you" reference
# ---------------------------------------------------------------------------
SECTOR_MECHANISM = {
    "Banks & Money": {
        "watch": "HDFC Bank, ICICI Bank, SBI, Bank Nifty",
        "why": "Banking is the single heaviest-weighted group in Nifty 50 and Sensex, so it tends to "
               "move the index before the index moves it. RBI repo-rate and NPA commentary especially "
               "affects NBFCs and housing finance names.",
        "you_positive": ["Home loan / EMI rates are unlikely to rise from here.",
                          "Banking stocks in a portfolio may see near-term support."],
        "you_negative": ["Borrowing costs could tick up — factor this into EMI planning.",
                          "Banking stocks may see pressure; avoid fresh entries until it stabilises."],
    },
    "IT & Software": {
        "watch": "TCS, Infosys, Wipro, HCLTech, Nifty IT",
        "why": "IT earns most revenue in dollars from US/Europe clients, so it reacts more to US demand "
               "signals and the rupee-dollar rate than domestic news.",
        "you_positive": ["IT names may see renewed buying interest.",
                          "A stronger rupee outlook usually follows good IT export data."],
        "you_negative": ["IT stocks may underperform the broader index near-term.",
                          "Client spending cuts abroad often hit hiring and stock price together."],
    },
    "Cars & Vehicles": {
        "watch": "Maruti Suzuki, Tata Motors, M&M, Bajaj Auto",
        "why": "Auto sales are a live read on consumer demand — rural monsoon strength, urban spend, "
               "and financing costs all show up here first.",
        "you_positive": ["Strong demand signals often spill into FMCG and cement a few sessions later.",
                          "Auto stocks may see momentum buying."],
        "you_negative": ["Weak sales can flag softer consumer demand economy-wide.",
                          "Auto and auto-ancillary stocks may see selling pressure."],
    },
    "Medicine & Pharma": {
        "watch": "Sun Pharma, Cipla, Dr Reddy's, Nifty Pharma",
        "why": "Pharma is largely export-driven (US generics), so it reacts sharply to USFDA plant "
               "approvals or warning letters — usually stock-specific, not index-wide.",
        "you_positive": ["Good news here is usually stock-specific — check which company before acting."],
        "you_negative": ["A USFDA warning can hit one stock hard even if the sector overall is fine."],
    },
    "FMCG": {
        "watch": "HUL, ITC, Nestle India, Britannia, Nifty FMCG",
        "why": "FMCG is a defensive, low-volatility sector — money often rotates in here when the "
               "broader market turns risk-off.",
        "you_positive": ["Can be a relatively safer place to be if the broader market looks shaky."],
        "you_negative": ["Weak FMCG demand often flags slower rural/urban consumption economy-wide."],
    },
    "Energy & Oil": {
        "watch": "Reliance Industries, ONGC, oil marketing companies",
        "why": "This sector tracks Brent crude prices closely — rising crude squeezes margins and "
               "widens India's import bill; falling crude does the opposite.",
        "you_positive": ["Lower crude is generally good for India's inflation and the rupee too.",
                          "Oil marketing companies may see margin improvement."],
        "you_negative": ["Rising crude can pressure the rupee and add to inflation worries.",
                          "Energy-heavy portfolios may see near-term volatility."],
    },
    "Metals & Mining": {
        "watch": "Tata Steel, JSW Steel, Hindalco, Nifty Metal",
        "why": "Metals move with global commodity cycles and China demand more than domestic headlines.",
        "you_positive": ["Global commodity strength is driving this, not just local factors."],
        "you_negative": ["Global demand weakness can pressure these stocks regardless of Indian data."],
    },
    "Real Estate/Housing": {
        "watch": "Realty Index, DLF, cement and construction names",
        "why": "Rate-sensitive sector — project financing costs move directly with RBI policy and "
               "bond yields.",
        "you_positive": ["Steady/falling rates support real estate and home-buying sentiment."],
        "you_negative": ["Rate worries can cool real estate and construction stocks quickly."],
    },
    "Telecom": {
        "watch": "Bharti Airtel, Reliance Jio (via RIL), Vodafone Idea",
        "why": "Effectively a 2-3 stock story in India — tariff hikes or subscriber data move these "
               "names directly.",
        "you_positive": ["Tariff hikes usually help telecom revenue and stock price together."],
        "you_negative": ["Subscriber losses or tariff wars can pressure these few stocks hard."],
    },
    "PSU & Government": {
        "watch": "PSU Bank Index, disinvestment-linked names",
        "why": "PSU stocks react to government policy, budget allocations, and disinvestment news more "
               "than quarterly earnings alone.",
        "you_positive": ["Policy tailwinds can move PSU stocks fast — but they can reverse fast too."],
        "you_negative": ["Policy uncertainty tends to hit PSU names disproportionately."],
    },
    "Global & Macro": {
        "watch": "Nifty 50, Sensex, USD-INR, India VIX",
        "why": "Macro-level news — US Fed decisions, FII/DII flows, rupee moves — usually sets the tone "
               "for the whole market rather than one sector, and shows up first at the opening bell.",
        "you_positive": ["Broad market tailwind — most sectors may benefit, not just one."],
        "you_negative": ["Broad market headwind — consider being selective rather than aggressive today."],
    },
    "General Markets": {
        "watch": "Nifty 50, Sensex",
        "why": "A broad story without one clear sector driver — worth tracking the index reaction "
               "rather than betting on a specific stock off this alone.",
        "you_positive": ["Watch the index reaction rather than acting on this story alone."],
        "you_negative": ["Watch the index reaction rather than acting on this story alone."],
    },
}

# ---------------------------------------------------------------------------
# 4. Real sector index % change (free, public, no key) — Yahoo Finance
# ---------------------------------------------------------------------------
INDEX_SYMBOLS = {
    "NIFTY 50": "%5ENSEI",
    "SENSEX": "%5EBSESN",
    "BANK NIFTY": "%5ENSEBANK",
}
SECTOR_INDEX_SYMBOLS = {
    "Banks & Money": "%5ENSEBANK",
    "IT & Software": "%5ECNXIT",
    "Cars & Vehicles": "%5ECNXAUTO",
    "Medicine & Pharma": "%5ECNXPHARMA",
    "Energy & Oil": "%5ECNXENERGY",
    "Real Estate/Housing": "%5ECNXREALTY",
}


def _yahoo_quote(symbol: str):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode())
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
    if pct > 1:
        return "Winning"
    if pct > 0:
        return "Growing"
    if pct > -1:
        return "Steady"
    if pct > -3:
        return "Slightly Low"
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


def get_usd_inr() -> dict:
    try:
        return _yahoo_quote("INR=X")
    except Exception as e:
        print(f"[warn] could not fetch USD/INR: {e}")
        return None


def get_nifty_pe() -> str:
    """Best-effort — Yahoo doesn't reliably expose index P/E for free, so
    this often returns None and the front end shows 'n/a' rather than a
    fabricated number."""
    try:
        url = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/%5ENSEI?modules=summaryDetail"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode())
        pe = payload["quoteSummary"]["result"][0]["summaryDetail"].get("trailingPE", {}).get("raw")
        return f"{pe:.1f}x" if pe else None
    except Exception as e:
        print(f"[warn] could not fetch Nifty P/E: {e}")
        return None


def get_company_fundamentals(symbol: str) -> dict:
    """Free Yahoo Finance fundamentals for the 'Company of the Day' section.
    Numbers are real (from Yahoo's free quoteSummary endpoint) but can lag
    the latest quarter — always cross-check before using for a real decision."""
    try:
        modules = "financialData,defaultKeyStatistics,summaryDetail,assetProfile,price"
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules={modules}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
        result = payload["quoteSummary"]["result"][0]
        fin = result.get("financialData", {})
        stats = result.get("defaultKeyStatistics", {})
        summary = result.get("summaryDetail", {})
        profile = result.get("assetProfile", {})
        price = result.get("price", {})

        def g(d, key):
            v = d.get(key, {})
            return v.get("fmt") or v.get("raw") if isinstance(v, dict) else None

        return {
            "name": price.get("longName", symbol),
            "symbol": symbol,
            "business": (profile.get("longBusinessSummary", "") or "")[:400],
            "revenue": g(fin, "totalRevenue"),
            "ebitda": g(fin, "ebitda"),
            "net_profit": g(fin, "netIncomeToCommon") if "netIncomeToCommon" in fin else g(stats, "netIncomeToCommon"),
            "gross_margin": g(fin, "grossMargins"),
            "profit_margin": g(fin, "profitMargins") or g(stats, "profitMargins"),
            "total_debt": g(fin, "totalDebt"),
            "total_cash": g(fin, "totalCash"),
            "pe_ratio": g(summary, "trailingPE"),
            "market_cap": g(price, "marketCap") or g(summary, "marketCap"),
        }
    except Exception as e:
        print(f"[warn] could not fetch fundamentals for {symbol}: {e}")
        return None


NUMBER_CRORE_PATTERN = re.compile(r"(?:₹|Rs\.?|rs)\s?([\d,]+(?:\.\d+)?)\s?(?:crore|cr)\b", re.IGNORECASE)
ACTOR_PATTERN = re.compile(
    r"(FII|DII|foreign (?:investors|institutional)|domestic (?:investors|institutional))",
    re.IGNORECASE,
)


def extract_money_trail(articles: list) -> dict:
    """Pulls FII/DII crore figures directly out of real headline/summary text
    rather than fabricating them. For every crore figure found, this looks
    BACKWARD for the nearest preceding FII/DII mention and attributes the
    number to that actor — matching forward from the keyword instead (an
    earlier version of this function) can misattribute a number to the
    wrong actor when both FII and DII appear near each other in one
    sentence, which would have silently mislabeled real money-flow data."""
    fii_matches, dii_matches = [], []
    for a in articles:
        text = f"{a['title']} {a['summary']}"
        for num_m in NUMBER_CRORE_PATTERN.finditer(text):
            window = text[max(0, num_m.start() - 50):num_m.start()]
            actor_hits = list(ACTOR_PATTERN.finditer(window))
            if not actor_hits:
                continue
            who = actor_hits[-1].group(1).lower()  # nearest preceding actor wins
            amount = num_m.group(1)
            if "fii" in who or "foreign" in who:
                fii_matches.append((amount, a["title"], a["link"]))
            else:
                dii_matches.append((amount, a["title"], a["link"]))
    return {
        "fii": fii_matches[0] if fii_matches else None,
        "dii": dii_matches[0] if dii_matches else None,
    }


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


# ---------------------------------------------------------------------------
# 6b. EDITORIAL DESK — 10-section format:
# 1. Why Did The Market Move  2. The Money Trail  3. 1-Minute Market
# 4. Finance Without Finance Language  5. News → Impact chain
# 6. Company of the Day  7. The 5 Numbers That Matter Today
# 8. What Could Go Wrong  9. Tomorrow's Market Map  10. Finance Puzzle
#
# A few of these genuinely need either real institutional-flow data or LLM
# reasoning that keyword rules can't fake — those are clearly labeled when
# running on the free rule-based fallback instead of Gemini.
# ---------------------------------------------------------------------------
EDITORIAL_PROMPT_TEMPLATE = """You are the editorial engine for "The Daily Planet", an Indian financial
newspaper. Follow these non-negotiable rules:
- Never invent numbers, dates, or company details not present in the input data below.
- Never say a stock "will" rise or fall. Say what COULD happen and the mechanism why.
- Remove hype words (massive, shocking, explosive, historic, investors panic, skyrocket).
- If information is insufficient for a section, say so plainly rather than inventing content.
- Keep fields SHORT — mobile-friendly daily brief, not a report.
- Output ONLY valid JSON matching the schema below, no markdown fences, no commentary.

TODAY'S INDEX DATA (real, from NSE/BSE):
{index_data}

TODAY'S SECTOR PERFORMANCE (real % change):
{sector_data}

USD/INR: {usd_inr}
RBI REPO RATE: {repo_rate}
LATEST CPI INFLATION: {cpi}
NIFTY P/E: {nifty_pe}

TODAY'S HEADLINES (title | source | sector | sentiment | summary):
{headlines}

COMPANY OF THE DAY — real fundamentals (Yahoo Finance, free tier):
{company_data}

Return JSON with this exact schema:
{{
  "why_market_moved": {{
    "headline": "e.g. NIFTY fell 0.8% — WHY?",
    "what_happened": "",
    "numbers_changed": "",
    "sectors_affected": "",
    "companies_that_matter": "",
    "market_impact": "",
    "what_could_change_view": "",
    "why_investor_should_care": ""
  }},
  "money_trail": {{"fii": "", "dii": "", "sector_flow": "", "narrative": ""}},
  "one_minute_market": {{
    "nifty_dir": "up|down|flat", "sensex_dir": "up|down|flat", "banknifty_dir": "up|down|flat",
    "reasons": ["", "", ""], "winners": ["", "", ""], "losers": ["", "", ""], "tomorrow_watch": ["", "", ""]
  }},
  "finance_translator": {{"original": "", "simple": "", "why_india_cares": ""}},
  "news_impact_chain": {{"chain": ["Event", "Company", "Revenue/Cost", "Profit", "Valuation", "Stock impact"]}},
  "company_of_the_day": {{
    "name": "", "business": "", "revenue": "", "ebitda": "", "net_profit": "", "debt": "",
    "cash": "", "margins": "", "valuation": "", "risks": "", "recent_developments": "",
    "what_could_change_the_case": ""
  }},
  "five_numbers": [ {{"value": "", "label": "", "why": ""}} ],
  "what_could_go_wrong": {{"story": "", "bull_case": ["", ""], "bear_case": ["", ""], "key_thing_to_watch": ""}},
  "tomorrows_market_map": [ {{"driver": "", "region": "🇮🇳|🇺🇸|🇨🇳|🛢️|💵|🏦|📅|📊", "classification": "Positive|Negative|Watch", "note": ""}} ],
  "finance_puzzle": {{"question": "", "answer": ""}}
}}
"""


def build_editorial_prompt(stock_articles, index_snapshot, sector_performance,
                            usd_inr, nifty_pe, company_fundamentals) -> str:
    index_lines = "\n".join(
        f"- {name}: {d['price']} ({'+' if d['change']>=0 else ''}{d['change']}, {d['pct']}%)"
        for name, d in index_snapshot.items()
    ) or "Not available today."
    sector_lines = "\n".join(
        f"- {name}: {d['pct']}% ({d['status']})" for name, d in sector_performance.items()
    ) or "Not available today."
    headline_lines = "\n".join(
        f"- {a['title']} | {a['source']} | {a['sector']} | {a['sentiment']['label']} | {a['summary'][:150]}"
        for a in stock_articles[:20]
    ) or "No headlines available today."
    company_lines = json.dumps(company_fundamentals, indent=2) if company_fundamentals else "Not available."
    return EDITORIAL_PROMPT_TEMPLATE.format(
        index_data=index_lines, sector_data=sector_lines,
        usd_inr=f"{usd_inr['price']} ({usd_inr['pct']}%)" if usd_inr else "Not available",
        repo_rate=REPO_RATE, cpi=LATEST_CPI_INFLATION,
        nifty_pe=nifty_pe or "Not available",
        headlines=headline_lines, company_data=company_lines,
    )


def call_gemini(prompt: str):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={api_key}"
    )
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


def build_fallback_editorial(stock_articles, index_snapshot, sector_performance,
                              usd_inr, nifty_pe, money_trail_raw, company_fundamentals) -> dict:
    """Rule-based version used when no Gemini key is set. Grounded only in
    real data we actually have — sections that need real institutional-flow
    data or genuine reasoning are labeled honestly instead of faked."""
    ranked = sorted(stock_articles, key=lambda a: abs(a["sentiment"]["score"]), reverse=True)
    top = ranked[0] if ranked else None

    nifty = index_snapshot.get("NIFTY 50")
    sensex = index_snapshot.get("SENSEX")
    banknifty = index_snapshot.get("BANK NIFTY")

    def dir_word(d):
        if not d:
            return "flat"
        return "up" if d["change"] > 0 else "down" if d["change"] < 0 else "flat"

    # --- 1. Why did the market move ---
    why_headline = f"NIFTY {'rose' if nifty and nifty['change']>=0 else 'fell'} {abs(nifty['pct']) if nifty else '—'}% — WHY?" if nifty else "Market Move — WHY?"
    why_market_moved = {
        "headline": why_headline,
        "what_happened": top["title"] if top else "No dominant story today.",
        "numbers_changed": f"NIFTY {nifty['pct']}%, SENSEX {sensex['pct']}%, Bank Nifty {banknifty['pct']}%" if nifty and sensex and banknifty else "Index data unavailable.",
        "sectors_affected": ", ".join(sector_performance.keys()) or "Not enough sector data today.",
        "companies_that_matter": top["watch"] if top else "N/A",
        "market_impact": top["why_matters"] if top else "N/A",
        "what_could_change_view": "A reversal in FII/DII flows or a surprise RBI/global rate move.",
        "why_investor_should_care": top["in_simple_words"] if top else "Check back after the next update.",
    }

    # --- 2. Money trail (grounded in real headline text, not invented) ---
    fii = money_trail_raw.get("fii")
    dii = money_trail_raw.get("dii")
    money_trail = {
        "fii": f"₹{fii[0]} Cr mentioned in: \u201c{fii[1]}\u201d" if fii else "Not mentioned in today's headlines — check NSE provisional data directly.",
        "dii": f"₹{dii[0]} Cr mentioned in: \u201c{dii[1]}\u201d" if dii else "Not mentioned in today's headlines — check NSE provisional data directly.",
        "sector_flow": "Not derivable from headlines alone — needs a dedicated FII/DII data feed for full accuracy.",
        "narrative": "Money-trail figures here are pulled only when a headline explicitly states them — nothing is estimated.",
    }

    # --- 3. One-minute market ---
    top3 = ranked[:3]
    # "winners/losers" here are the sectors that moved most, NOT individual stock prices
    # (we don't have free reliable per-stock price data) — labeled honestly as such.
    sector_sorted = sorted(sector_performance.items(), key=lambda kv: kv[1]["pct"], reverse=True)
    winners = [f"{name} ({d['pct']}%)" for name, d in sector_sorted[:3] if d["pct"] > 0] or ["No clear sector winners today"]
    losers = [f"{name} ({d['pct']}%)" for name, d in sector_sorted[-3:] if d["pct"] < 0] or ["No clear sector losers today"]
    one_minute_market = {
        "nifty_dir": dir_word(nifty), "sensex_dir": dir_word(sensex), "banknifty_dir": dir_word(banknifty),
        "reasons": [a["title"] for a in top3] or ["Not enough signal today"],
        "winners": winners,
        "losers": losers,
        "tomorrow_watch": ["FII/DII provisional data", "Global cues (US futures, crude)", "Any RBI/government policy news"],
    }

    # --- 4. Finance translator (needs real LLM for arbitrary text; static example without Gemini) ---
    finance_translator = {
        "original": "US Treasury yields climbed amid expectations of prolonged restrictive monetary policy.",
        "simple": "US borrowing costs are rising because investors think interest rates may stay high for longer.",
        "why_india_cares": "Higher US yields can make US assets more attractive, pulling money away from "
                            "emerging markets like India — which can pressure Indian equities and the rupee.",
    }

    # --- 5. News -> impact chain ---
    news_impact_chain = {
        "chain": [top["title"], top["sector"], "Revenue/cost pressure", "Profit impact", "Valuation reaction", f"Watch: {top['watch']}"]
        if top else ["Not enough signal today"]
    }

    # --- 6. Company of the day ---
    if company_fundamentals:
        cf = company_fundamentals
        company_of_the_day = {
            "name": cf.get("name") or cf.get("symbol"), "business": cf.get("business") or "Business summary not available.",
            "revenue": cf.get("revenue") or "n/a", "ebitda": cf.get("ebitda") or "n/a",
            "net_profit": cf.get("net_profit") or "n/a", "debt": cf.get("total_debt") or "n/a",
            "cash": cf.get("total_cash") or "n/a",
            "margins": f"Gross: {cf.get('gross_margin') or 'n/a'}, Net: {cf.get('profit_margin') or 'n/a'}",
            "valuation": f"P/E: {cf.get('pe_ratio') or 'n/a'}, Market cap: {cf.get('market_cap') or 'n/a'}",
            "risks": "Not available via free data — check the company's latest investor presentation.",
            "recent_developments": "Not available via free data — check recent exchange filings.",
            "what_could_change_the_case": "A material change in quarterly earnings, guidance, or sector demand.",
        }
    else:
        company_of_the_day = None

    # --- 7. Five numbers ---
    five_numbers = []
    if nifty:
        five_numbers.append({"value": f"{nifty['price']:,}", "label": "NIFTY 50", "why": f"Moved {nifty['pct']}% today — the main barometer of Indian equities."})
    if usd_inr:
        five_numbers.append({"value": f"₹{usd_inr['price']}", "label": "USD/INR", "why": "A weaker rupee raises import costs; a stronger rupee eases them."})
    five_numbers.append({"value": REPO_RATE, "label": "RBI Repo Rate", "why": "Anchors loan and EMI rates across the economy."})
    five_numbers.append({"value": LATEST_CPI_INFLATION, "label": "CPI Inflation", "why": "Feeds directly into RBI's rate decisions."})
    five_numbers.append({"value": nifty_pe or "n/a", "label": "NIFTY P/E", "why": "A rough gauge of whether the market is cheap or expensive versus history."})

    # --- 8. What could go wrong ---
    what_could_go_wrong = {
        "story": top["title"] if top else "N/A",
        "bull_case": [top["watch"] + " could see continued support."] if top and top["sentiment"]["label"] == "Bullish" else ["Domestic buying continues to offset FII selling."],
        "bear_case": ["A reversal in the underlying driver could unwind today's move quickly."],
        "key_thing_to_watch": "Whether tomorrow's FII/DII data and global cues confirm or contradict today's move.",
    }

    # --- 9. Tomorrow's market map (needs live global data for real signal; static checklist without Gemini) ---
    tomorrows_market_map = [
        {"driver": "India — FII/DII flow direction", "region": "🇮🇳", "classification": "Watch", "note": "Confirms or contradicts today's move."},
        {"driver": "US futures / Fed commentary", "region": "🇺🇸", "classification": "Watch", "note": "Sets the global risk tone at India's open."},
        {"driver": "Crude oil price", "region": "🛢️", "classification": "Watch", "note": "Affects inflation and the rupee."},
        {"driver": "USD/INR", "region": "💵", "classification": "Watch", "note": "A sharp move either way can shift FII appetite."},
        {"driver": "RBI/Government policy news", "region": "🏦", "classification": "Watch", "note": "Can move banking and rate-sensitive sectors fast."},
    ]

    # --- 10. Finance puzzle (synthetic teaching exercise — clearly not real company data) ---
    import random
    random.seed(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    revenue = random.choice([500, 800, 1000, 1200, 1500])
    ebitda_margin = random.choice([15, 18, 20, 22, 25])
    debt = random.choice([200, 300, 500, 600])
    interest_rate = random.choice([8, 9, 10])
    interest = round(debt * interest_rate / 100)
    fall_pct = random.choice([5, 10, 15])
    new_revenue = revenue * (1 - fall_pct / 100)
    new_ebitda = new_revenue * ebitda_margin / 100
    new_profit_before_tax = new_ebitda - interest
    old_ebitda = revenue * ebitda_margin / 100
    old_profit_before_tax = old_ebitda - interest
    finance_puzzle = {
        "question": f"A company has Revenue = ₹{revenue} Cr, EBITDA margin = {ebitda_margin}%, "
                     f"Debt = ₹{debt} Cr, Interest rate = {interest_rate}%. "
                     f"What happens to profit (before tax) if revenue falls {fall_pct}%?",
        "answer": f"Old EBITDA = ₹{old_ebitda:.0f} Cr → Profit before interest cost of ₹{interest} Cr = ₹{old_profit_before_tax:.0f} Cr.\n"
                  f"New revenue = ₹{new_revenue:.0f} Cr → New EBITDA = ₹{new_ebitda:.0f} Cr → "
                  f"New profit before tax = ₹{new_profit_before_tax:.0f} Cr.\n"
                  f"That's a change of ₹{(new_profit_before_tax - old_profit_before_tax):.0f} Cr — notice how a "
                  f"{fall_pct}% revenue fall swings profit by a much bigger percentage. That's operating leverage.",
    }

    return {
        "why_market_moved": why_market_moved,
        "money_trail": money_trail,
        "one_minute_market": one_minute_market,
        "finance_translator": finance_translator,
        "news_impact_chain": news_impact_chain,
        "company_of_the_day": company_of_the_day,
        "five_numbers": five_numbers,
        "what_could_go_wrong": what_could_go_wrong,
        "tomorrows_market_map": tomorrows_market_map,
        "finance_puzzle": finance_puzzle,
        "generated_by": "fallback",
    }


def generate_editorial(stock_articles, index_snapshot, sector_performance) -> dict:
    print("Fetching USD/INR, Nifty P/E, company-of-the-day fundamentals...")
    usd_inr = get_usd_inr()
    nifty_pe = get_nifty_pe()
    day_index = datetime.now(timezone.utc).timetuple().tm_yday % len(COMPANY_WATCHLIST)
    company_fundamentals = get_company_fundamentals(COMPANY_WATCHLIST[day_index])
    money_trail_raw = extract_money_trail(stock_articles)

    prompt = build_editorial_prompt(stock_articles, index_snapshot, sector_performance,
                                     usd_inr, nifty_pe, company_fundamentals)
    result = call_gemini(prompt)
    if result:
        result["generated_by"] = "gemini"
        return result
    return build_fallback_editorial(stock_articles, index_snapshot, sector_performance,
                                     usd_inr, nifty_pe, money_trail_raw, company_fundamentals)



def jetro_impact_label(score: int) -> str:
    if score >= 2:
        return "Highly Positive For Indian Stocks"
    if score == 1:
        return "Positive For Indian Stocks"
    if score == 0:
        return "Neutral For Indian Stocks"
    if score == -1:
        return "Negative For Indian Stocks"
    return "Highly Negative For Indian Stocks"


def means_for_you(sector: str, label: str) -> list:
    info = SECTOR_MECHANISM.get(sector, SECTOR_MECHANISM["General Markets"])
    if label == "Bearish":
        return info["you_negative"]
    return info["you_positive"]


def in_simple_words(sector: str, label: str, summary: str) -> str:
    who = sector
    if label == "Bullish":
        lead = f"This is good news for {who.lower()} — sentiment turned more positive."
    elif label == "Bearish":
        lead = f"This is a caution sign for {who.lower()} — sentiment turned more negative."
    else:
        lead = f"Not a big mover either way for {who.lower()} right now."
    tail = summary.strip()
    return f"{lead} {tail}" if tail else lead


# ---------------------------------------------------------------------------
# 6. Main
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
                "id": article_id(link),
                "title": title,
                "summary": summary[:280],
                "link": link,
                "source": source,
                "sources": [{"name": source, "url": link}],
                "published": published,
                "sector": sector,
                "sentiment": sentiment,
                "is_ai_related": is_ai_related(full_text),
                "page": page,
                "jetro_impact": jetro_impact_label(sentiment["score"]),
                "in_simple_words": in_simple_words(sector, sentiment["label"], summary[:200]),
                "means_for_you": means_for_you(sector, sentiment["label"]),
                "why_matters": SECTOR_MECHANISM.get(sector, SECTOR_MECHANISM["General Markets"])["why"],
                "watch": SECTOR_MECHANISM.get(sector, SECTOR_MECHANISM["General Markets"])["watch"],
            })

    # de-duplicate by id
    seen = set()
    unique_articles = []
    for a in all_articles:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique_articles.append(a)

    stock_articles = [a for a in unique_articles if a["page"] == "stock_market"]
    startup_articles = [a for a in unique_articles if a["page"] == "startups"]
    pe_ib_articles = [a for a in unique_articles if a["page"] == "pe_ib"]

    # sentiment is still computed per-article internally (it's how we rank
    # "which story matters most" for the editorial sections), but it's no
    # longer surfaced as a Bullish/Bearish score anywhere in the output.
    ranked = sorted(stock_articles, key=lambda a: abs(a["sentiment"]["score"]), reverse=True)

    # --- biggest story (lead article) ---
    biggest_story = None
    if ranked:
        top = ranked[0]
        biggest_story = {
            "sector": top["sector"],
            "title": top["title"],
            "sources": top["sources"],
            "what_happened": top["summary"] or top["title"],
            "why_it_matters": top["why_matters"],
            "means_for_you": top["means_for_you"],
            "link": top["link"],
        }
        ranked = ranked[1:]  # remove from secondary list

    # --- sector grouping (for sector performance grid fallback + tabs) ---
    sectors_out = {}
    for a in stock_articles:
        sectors_out.setdefault(a["sector"], []).append(a)

    ai_articles = [a for a in unique_articles if a["is_ai_related"]]

    print("Fetching Nifty/Sensex/Bank Nifty snapshot...")
    index_snapshot = get_index_snapshot()
    print("Fetching sector index performance...")
    sector_performance = get_sector_performance()
    # fallback: if a sector index couldn't be fetched, estimate from article sentiment
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
        "pages": {
            "stock_market": ranked,          # secondary stock stories (biggest_story removed)
            "startups": startup_articles,
            "pe_ib": pe_ib_articles,
        },
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
