"""
Daily Market Desk v2
Fact-first Indian market newspaper engine.

Design principles:
1. No headline sentiment scoring.
2. Never convert a positive/negative word into a market call.
3. Separate FACTS from MARKET INTERPRETATION.
4. Explain the transmission mechanism in simple language.
5. Show direction only when a reasonable mechanism exists; otherwise say "Unclear".
6. Show time horizon and confidence.
7. Never invent stock-specific effects, causal claims, or "buy/sell" advice.
"""

import feedparser
import json
import re
import hashlib
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

STOCK_FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Moneycontrol Markets": "https://www.moneycontrol.com/rss/marketreports.xml",
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

SECTORS = {
    "Banks & Financials": ["bank", "nbfc", "rbi", "hdfc", "icici", "sbi", "axis bank",
                           "kotak", "credit", "loan", "npa", "repo rate", "bond yield"],
    "IT Services": ["tcs", "infosys", "wipro", "hcltech", "tech mahindra", "it services",
                    "software services", "technology spending"],
    "Auto": ["maruti", "tata motors", "mahindra", "bajaj auto", "hero motocorp",
             "auto sector", "car sales", "two-wheeler", "ev sales"],
    "Pharma & Healthcare": ["pharma", "drug", "usfda", "sun pharma", "cipla", "dr reddy",
                            "hospital", "healthcare"],
    "FMCG & Consumption": ["fmcg", "hindustan unilever", "nestle india", "britannia",
                            "dabur", "consumer", "consumption", "rural demand"],
    "Energy": ["oil", "gas", "reliance industries", "ongc", "crude", "opec", "energy",
               "power", "renewable"],
    "Metals & Mining": ["steel", "tata steel", "jsw steel", "hindalco", "coal india",
                        "mining", "aluminium", "metal prices"],
    "Real Estate & Cement": ["realty", "real estate", "infrastructure", "construction",
                             "cement", "dlf", "housing"],
    "Telecom": ["telecom", "airtel", "jio", "vodafone idea", "5g"],
    "PSU & Government": ["psu", "disinvestment", "budget", "government stake", "psu bank"],
    "Global Macro": ["fed", "federal reserve", "nasdaq", "dow jones", "dollar index",
                     "inflation", "gdp", "fii", "dii", "nifty", "sensex", "rupee",
                     "foreign investors", "tariff", "trade war", "geopolitical"],
}

SECTOR_MECHANISM = {
    "Banks & Financials": {
        "watch": ["Bank Nifty", "HDFC Bank", "ICICI Bank", "SBI"],
        "why": "Bank earnings are sensitive to loan growth, funding costs, asset quality and RBI policy."
    },
    "IT Services": {
        "watch": ["Nifty IT", "TCS", "Infosys", "Wipro", "HCLTech"],
        "why": "Indian IT companies depend heavily on overseas technology spending and the rupee-dollar rate."
    },
    "Auto": {
        "watch": ["Nifty Auto", "Maruti Suzuki", "Tata Motors", "M&M", "Bajaj Auto"],
        "why": "Vehicle sales reflect consumer demand, financing costs and input costs."
    },
    "Pharma & Healthcare": {
        "watch": ["Nifty Pharma", "Sun Pharma", "Cipla", "Dr Reddy's"],
        "why": "Pharma moves are often company-specific and can be driven by USFDA actions, approvals and US pricing."
    },
    "FMCG & Consumption": {
        "watch": ["Nifty FMCG", "HUL", "ITC", "Nestle India"],
        "why": "Consumption companies respond to household demand, rural demand and input costs."
    },
    "Energy": {
        "watch": ["Nifty Energy", "Reliance Industries", "ONGC", "oil marketing companies"],
        "why": "Crude prices affect India's import bill, inflation, the rupee and margins across energy-sensitive businesses."
    },
    "Metals & Mining": {
        "watch": ["Nifty Metal", "Tata Steel", "JSW Steel", "Hindalco"],
        "why": "Metal prices are driven strongly by global demand, China and commodity cycles."
    },
    "Real Estate & Cement": {
        "watch": ["Nifty Realty", "DLF", "cement stocks"],
        "why": "Real estate is sensitive to financing costs, housing demand and construction activity."
    },
    "Telecom": {
        "watch": ["Bharti Airtel", "Vodafone Idea", "Reliance Industries"],
        "why": "Tariffs, subscriber growth and network investment directly affect telecom economics."
    },
    "PSU & Government": {
        "watch": ["relevant PSU index", "policy-linked stocks"],
        "why": "Government policy, regulation and disinvestment can materially affect PSU valuations."
    },
    "Global Macro": {
        "watch": ["Nifty 50", "Sensex", "USD/INR", "India VIX"],
        "why": "Macro events can affect liquidity, currencies, bond yields and risk appetite across the market."
    },
    "General Markets": {
        "watch": ["Nifty 50", "Sensex"],
        "why": "The story does not have enough evidence for a specific sector-level conclusion."
    },
}

INDEX_SYMBOLS = {
    "NIFTY 50": "%5ENSEI",
    "SENSEX": "%5EBSESN",
    "BANK NIFTY": "%5ENSEBANK",
}

SECTOR_INDEX_SYMBOLS = {
    "Banks & Financials": "%5ENSEBANK",
    "IT Services": "%5ECNXIT",
    "Auto": "%5ECNXAUTO",
    "Pharma & Healthcare": "%5ECNXPHARMA",
    "Energy": "%5ECNXENERGY",
    "Real Estate & Cement": "%5ECNXREALTY",
}

SOURCE_PRIORITY = {
    "Reserve Bank of India": 5,
    "SEBI": 5,
    "NSE": 5,
    "BSE": 5,
    "Economic Times Markets": 4,
    "Business Standard Markets": 4,
    "Business Standard Companies": 4,
    "Financial Express Markets": 4,
    "LiveMint Markets": 4,
    "Moneycontrol Markets": 3,
    "Moneycontrol Business": 3,
    "VCCircle": 3,
    "Inc42": 2,
    "YourStory": 2,
    "Entrackr": 2,
}

POSITIVE_EVENT_RULES = [
    (r"rate cut|cuts.*rate|reduces.*rate|lower.*repo", "Lower policy rates can reduce financing costs."),
    (r"rate hike|hikes.*rate|raises.*rate|higher.*repo", "Higher policy rates can increase financing costs."),
    (r"crude.*fall|oil.*fall|oil.*drop|crude.*drop", "Lower crude can reduce India's import-cost and inflation pressure."),
    (r"crude.*rise|oil.*rise|oil.*surge|crude.*surge", "Higher crude can increase India's import-cost and inflation pressure."),
    (r"client.*cut.*technology|tech.*spending.*cut|technology budget.*cut", "Lower overseas technology spending can pressure Indian IT revenue growth."),
    (r"client.*increase.*technology|tech.*spending.*increase", "Higher overseas technology spending can support Indian IT demand."),
    (r"fii.*buy|foreign investors.*buy|foreign investors.*bought", "Foreign buying can add demand to Indian equities."),
    (r"fii.*sell|foreign investors.*sell|foreign investors.*sold", "Foreign selling can add supply to Indian equities."),
]

def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()

def parse_published(entry):
    raw = entry.get("published") or entry.get("updated") or ""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return raw

def classify_sector(text):
    t = text.lower()
    scores = {}
    for sector, words in SECTORS.items():
        scores[sector] = sum(1 for w in words if w in t)
    best = max(scores, key=scores.get)
    return best if scores[best] else "General Markets"

def classify_page(text, source):
    t = text.lower()
    if source in STARTUP_FEEDS or re.search(r"startup|series [abc]|seed round|unicorn|venture capital|raises \$|raises ₹", t):
        return "startups"
    if source in PE_IB_FEEDS or re.search(r"private equity|investment bank|m&a|acquisition|merger|ipo|stake sale|buyout|block deal", t):
        return "pe_ib"
    return "stock_market"

def article_id(link):
    return hashlib.sha256(link.encode("utf-8")).hexdigest()[:12]

def market_impact(text, sector):
    t = text.lower()
    hits = [desc for pattern, desc in POSITIVE_EVENT_RULES if re.search(pattern, t)]
    direction = "Unclear"
    mechanism = SECTOR_MECHANISM.get(sector, SECTOR_MECHANISM["General Markets"])["why"]

    if any("Lower crude" in h for h in hits):
        direction = "Potentially Positive for India"
        mechanism = "Lower crude prices reduce the cost of India's energy imports and can reduce inflation pressure."
    elif any("Higher crude" in h for h in hits):
        direction = "Potentially Negative for India"
        mechanism = "Higher crude prices increase the import bill and can raise inflation pressure."
    elif any("Lower policy" in h for h in hits):
        direction = "Potentially Positive for rate-sensitive sectors"
        mechanism = "Lower rates can reduce borrowing costs and support credit, housing and rate-sensitive demand."
    elif any("Higher policy" in h for h in hits):
        direction = "Potentially Negative for rate-sensitive sectors"
        mechanism = "Higher rates can increase borrowing costs and reduce demand for rate-sensitive assets."
    elif any("Lower overseas" in h for h in hits):
        direction = "Potentially Negative for IT"
        mechanism = "Indian IT companies depend on overseas technology spending; lower client budgets can reduce new project demand."
    elif any("Higher overseas" in h for h in hits):
        direction = "Potentially Positive for IT"
        mechanism = "Higher client technology budgets can support project demand and revenue growth."
    elif any("Foreign buying" in h for h in hits):
        direction = "Potentially Positive for equities"
        mechanism = "Foreign buying adds demand to the market, although it does not by itself prove a lasting trend."
    elif any("Foreign selling" in h for h in hits):
        direction = "Potentially Negative for equities"
        mechanism = "Foreign selling adds supply to the market, although it does not by itself prove a lasting downtrend."

    confidence = "Low"
    if len(hits) >= 2:
        confidence = "Medium"
    if re.search(r"\bRBI\b|Reserve Bank|SEBI|NSE|BSE|company statement|exchange filing", text, re.I):
        confidence = "Medium" if confidence == "Low" else "High"

    horizon = "Near term"
    if re.search(r"earnings|profit|revenue|guidance|order book|capex|acquisition|merger", t):
        horizon = "1–4 quarters"
    elif re.search(r"rate|inflation|crude|fed|tariff|rupee|fii|dii", t):
        horizon = "Days to weeks"

    return {
        "direction": direction,
        "mechanism": mechanism,
        "confidence": confidence,
        "horizon": horizon,
        "evidence": hits[:2],
    }

def simple_news(title, summary):
    # Keep this deliberately factual; do not prepend emotional labels.
    if summary:
        return summary[:420]
    return title

def _yahoo_quote(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode())
    meta = payload["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    if price is None or not prev:
        return None
    change = price - prev
    return {"price": round(price, 2), "change": round(change, 2), "pct": round(change / prev * 100, 2)}

def get_quotes(symbols):
    out = {}
    for name, symbol in symbols.items():
        try:
            q = _yahoo_quote(symbol)
            if q:
                out[name] = q
        except Exception as e:
            print(f"[warn] {name}: {e}")
    return out

def status(pct):
    if pct > 1: return "Strong"
    if pct > 0.2: return "Up"
    if pct >= -0.2: return "Flat"
    if pct >= -1: return "Down"
    return "Weak"

def main():
    now = datetime.now(timezone.utc)
    all_articles = []

    for source, url in ALL_FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[warn] {source}: {e}")
            continue

        for entry in feed.entries[:20]:
            title = clean_text(entry.get("title", ""))
            summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
            link = entry.get("link", "")
            if not title or not link:
                continue

            published = parse_published(entry)
            full_text = f"{title}. {summary}"
            sector = classify_sector(full_text)
            page = classify_page(full_text, source)
            impact = market_impact(full_text, sector)

            # Skip very old stories when a timestamp is available.
            stale = False
            if published:
                try:
                    dt = datetime.fromisoformat(published)
                    stale = now - dt > timedelta(days=3)
                except Exception:
                    pass
            if stale:
                continue

            all_articles.append({
                "id": article_id(link),
                "title": title,
                "summary": simple_news(title, summary),
                "link": link,
                "source": source,
                "published": published,
                "sector": sector,
                "page": page,
                "market_impact": impact,
                "watch": SECTOR_MECHANISM.get(sector, SECTOR_MECHANISM["General Markets"])["watch"],
            })

    # Deduplicate by normalized title, retaining the strongest source.
    dedup = {}
    for a in all_articles:
        key = re.sub(r"[^a-z0-9]+", " ", a["title"].lower()).strip()
        old = dedup.get(key)
        if not old or SOURCE_PRIORITY.get(a["source"], 1) > SOURCE_PRIORITY.get(old["source"], 1):
            dedup[key] = a
    unique = list(dedup.values())

    # Market-impact stories first; never rank by "sentiment".
    stock = [a for a in unique if a["page"] == "stock_market"]
    stock.sort(key=lambda a: (
        {"High": 3, "Medium": 2, "Low": 1}.get(a["market_impact"]["confidence"], 1),
        1 if a["market_impact"]["direction"] != "Unclear" else 0,
        SOURCE_PRIORITY.get(a["source"], 1)
    ), reverse=True)

    lead = stock[0] if stock else None
    secondary = stock[1:9]

    sectors = {}
    for a in stock:
        sectors.setdefault(a["sector"], []).append(a)

    data = {
        "generated_at_utc": now.isoformat(),
        "edition_date": now.astimezone().strftime("%A, %d %B %Y"),
        "methodology": {
            "principle": "Facts first. Market impact is an explanation, not sentiment.",
            "sentiment_removed": True,
            "direction_labels": ["Potentially Positive", "Potentially Negative", "Unclear"],
            "confidence": ["High", "Medium", "Low"],
        },
        "index_snapshot": get_quotes(INDEX_SYMBOLS),
        "sector_performance": get_quotes(SECTOR_INDEX_SYMBOLS),
        "lead_story": lead,
        "pages": {
            "stock_market": secondary,
            "startups": [a for a in unique if a["page"] == "startups"][:10],
            "pe_ib": [a for a in unique if a["page"] == "pe_ib"][:10],
        },
        "sectors": sectors,
        "article_count": len(unique),
        "disclaimer": "Market impact is an analytical interpretation of the reported facts. It is not a buy/sell recommendation. Verify important claims in the original source.",
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
