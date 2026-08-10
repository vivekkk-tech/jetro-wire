"""
Jetro AI - Newspaper data engine
----------------------------------
Pulls free, public RSS feeds from Indian financial news sources,
tags each story by sector + sentiment + "AI in finance" relevance,
and writes everything to data.json which the website reads.

Runs on a schedule via GitHub Actions (free) - see .github/workflows/update.yml
No API keys, no paid services, no login required for any of this.
"""

import feedparser
import json
import re
import hashlib
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 1. Sources (all free public RSS feeds, no key needed)
# ---------------------------------------------------------------------------
FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Moneycontrol Business": "https://www.moneycontrol.com/rss/business.xml",
    "LiveMint Markets": "https://www.livemint.com/rss/markets",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
    "Business Standard Companies": "https://www.business-standard.com/rss/companies-101.rss",
    "Financial Express Markets": "https://www.financialexpress.com/market/feed/",
    "NDTV Profit Markets": "https://feeds.feedburner.com/ndtvprofit-latest",
}

# ---------------------------------------------------------------------------
# 2. Sector keyword map (simple, transparent, easy for you to tune later)
# ---------------------------------------------------------------------------
SECTORS = {
    "Banking & Financials": ["bank", "nbfc", "rbi", "hdfc", "icici", "sbi", "axis bank", "kotak",
                              "credit", "loan", "insurance", "npa", "repo rate", "bond yield"],
    "IT & Technology": ["it stocks", "tcs", "infosys", "wipro", "hcltech", "tech mahindra",
                         "software", "it sector", "it services"],
    "Auto": ["auto sector", "maruti", "tata motors", "mahindra", "bajaj auto", "hero moto",
             "ev sales", "electric vehicle", "two-wheeler", "car sales"],
    "Pharma & Healthcare": ["pharma", "drug", "usfda", "sun pharma", "cipla", "dr reddy",
                             "hospital", "healthcare stocks"],
    "FMCG": ["fmcg", "hindustan unilever", "nestle india", "itc ", "britannia", "consumer goods",
             "dabur", "godrej consumer"],
    "Energy & Oil-Gas": ["oil", "gas", "reliance industries", "ongc", "crude", "opec",
                          "energy stocks", "power sector", "renewable"],
    "Metals & Mining": ["metal stocks", "steel", "tata steel", "jsw steel", "hindalco",
                         "coal india", "mining", "aluminium"],
    "Realty & Infra": ["realty", "real estate", "infrastructure", "construction stocks",
                        "cement", "dlf", "housing"],
    "Telecom": ["telecom", "airtel", "jio", "vodafone idea", "5g rollout"],
    "PSU & Government": ["psu stock", "disinvestment", "budget 202", "government stake",
                          "psu bank"],
    "Global & Macro": ["fed reserve", "us fed", "dow jones", "nasdaq", "dollar index",
                        "inflation data", "gdp growth", "fii", "dii", "nifty", "sensex", "rupee",
                        "rbi policy", "repo rate", "crude oil impact", "foreign investors"],
}

AI_KEYWORDS = [
    "artificial intelligence", " ai ", "ai-powered", "machine learning", "generative ai",
    "chatgpt", "llm", "openai", "fintech ai", "algo trading", "ai model", "ai stocks",
    "ai tool", "copilot", "automation platform"
]

POSITIVE_WORDS = ["surge", "rally", "jump", "soar", "gain", "rises", "rise", "upgrade",
                   "beats estimate", "record high", "outperform", "bullish", "growth",
                   "profit rise", "strong", "recover", "rebound", "buy rating"]
NEGATIVE_WORDS = ["crash", "plunge", "slump", "fall", "falls", "drop", "downgrade",
                   "miss estimate", "record low", "underperform", "bearish", "decline",
                   "loss", "weak", "sell-off", "selloff", "correction", "sell rating"]


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
    if score > 0:
        label = "Bullish"
    elif score < 0:
        label = "Bearish"
    else:
        label = "Neutral"
    return {"label": label, "score": score}


def is_ai_related(text: str) -> bool:
    text_low = f" {text.lower()} "
    return any(kw in text_low for kw in AI_KEYWORDS)


# ---------------------------------------------------------------------------
# Live Nifty 50 / Sensex snapshot — free public Yahoo Finance endpoint,
# no API key needed. This is what makes the dashboard genuinely India-first
# instead of just filtered news.
# ---------------------------------------------------------------------------
INDEX_SYMBOLS = {
    "NIFTY 50": "%5ENSEI",
    "SENSEX": "%5EBSESN",
    "BANK NIFTY": "%5ENSEBANK",
}


def get_index_snapshot() -> dict:
    snapshot = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    for name, symbol in INDEX_SYMBOLS.items():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode())
            meta = payload["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
            if price is not None and prev_close:
                change = price - prev_close
                pct = (change / prev_close) * 100
                snapshot[name] = {
                    "price": round(price, 2),
                    "change": round(change, 2),
                    "pct": round(pct, 2),
                }
        except Exception as e:
            print(f"[warn] could not fetch index {name}: {e}")
    return snapshot


SECTOR_PLAIN = {
    "Banking & Financials": "banks and lenders",
    "IT & Technology": "IT/software companies",
    "Auto": "car and bike makers",
    "Pharma & Healthcare": "drug and healthcare companies",
    "FMCG": "everyday consumer goods companies",
    "Energy & Oil-Gas": "energy and oil/gas companies",
    "Metals & Mining": "metal and mining companies",
    "Realty & Infra": "real estate and infrastructure firms",
    "Telecom": "telecom companies",
    "PSU & Government": "government-owned companies",
    "Global & Macro": "FIIs, the rupee, and the broader Indian market",
    "General Markets": "the overall market",
}


def plain_english(sector: str, sentiment_label: str) -> str:
    """A simple, jargon-free one-liner so you don't need a finance degree
    to know what a headline actually means for the market."""
    who = SECTOR_PLAIN.get(sector, "the market")
    if sentiment_label == "Bullish":
        return f"In simple terms: this is good news — {who} look stronger after this."
    if sentiment_label == "Bearish":
        return f"In simple terms: this is a warning sign — {who} may come under pressure."
    return f"In simple terms: not a big mover either way for {who} right now."


def article_id(link: str) -> str:
    return hashlib.md5(link.encode()).hexdigest()[:10]


def main():
    all_articles = []

    for source, url in FEEDS.items():
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

            all_articles.append({
                "id": article_id(link),
                "title": title,
                "summary": summary[:280],
                "link": link,
                "source": source,
                "published": published,
                "sector": sector,
                "sentiment": sentiment,
                "is_ai_related": is_ai_related(full_text),
                "plain_english": plain_english(sector, sentiment["label"]),
            })

    # de-duplicate by id (same story from multiple feeds)
    seen = set()
    unique_articles = []
    for a in all_articles:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique_articles.append(a)

    # overall market sentiment = net of all scored articles
    total_score = sum(a["sentiment"]["score"] for a in unique_articles)
    if total_score > 2:
        overall_label = "Bullish"
    elif total_score < -2:
        overall_label = "Bearish"
    else:
        overall_label = "Neutral / Mixed"

    # group by sector
    sectors_out = {}
    for a in unique_articles:
        sectors_out.setdefault(a["sector"], []).append(a)

    ai_articles = [a for a in unique_articles if a["is_ai_related"]]

    # top stories = first 8, most recent feeds first (feeds are already newest-first)
    top_stories = unique_articles[:8]

    # chart data: counts for pie/bar charts on the front end
    sentiment_counts = {"Bullish": 0, "Bearish": 0, "Neutral": 0}
    for a in unique_articles:
        sentiment_counts[a["sentiment"]["label"]] += 1

    sector_counts = {sector: len(items) for sector, items in sectors_out.items()}

    print("Fetching Nifty/Sensex/Bank Nifty snapshot...")
    index_snapshot = get_index_snapshot()

    data = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_sentiment": {"label": overall_label, "score": total_score},
        "total_articles": len(unique_articles),
        "top_stories": top_stories,
        "sectors": sectors_out,
        "ai_in_finance": ai_articles[:10],
        "sentiment_counts": sentiment_counts,
        "sector_counts": sector_counts,
        "index_snapshot": index_snapshot,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Wrote data.json with {len(unique_articles)} articles "
          f"({len(ai_articles)} AI-related). Overall sentiment: {overall_label}")


if __name__ == "__main__":
    main()
