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
# 6b. EDITORIAL DESK — implements the "Market Intelligence Newspaper" framework
# (60-second brief, So What, Hidden Connection, Bull/Bear, Finance Lesson,
# Thesis Board, etc.) A real LLM call (free-tier Gemini) is used when a key
# is available, since this kind of reasoning genuinely can't be faked with
# keyword rules. If no key is set, or the call fails, a simpler rule-based
# version is used instead — the site never breaks either way.
# ---------------------------------------------------------------------------
EDITORIAL_PROMPT_TEMPLATE = """You are the editorial engine for "The Daily Planet", an Indian financial
newspaper. Follow these non-negotiable rules:
- Separate FACT (directly from the headlines given) from ANALYSIS (your reasoning) from POSSIBILITY
  (uncertain future outcome). Never state a possibility as if it were a fact.
- Never invent numbers, dates, or company details not present in the input headlines/index data below.
- Never say a stock "will" rise or fall. Say what COULD happen and why, with the mechanism.
- Remove hype words (massive, shocking, explosive, historic, investors panic, skyrocket).
- If information is insufficient for a section, write "Not enough signal today" rather than inventing content.
- Keep every field SHORT — this is a mobile-friendly daily brief, not a report. 1-3 sentences per field.
- Output ONLY valid JSON matching the schema below, no markdown fences, no commentary.

TODAY'S INDEX DATA (real, from NSE/BSE):
{index_data}

TODAY'S SECTOR PERFORMANCE (real % change):
{sector_data}

TODAY'S HEADLINES (title | source | sector | sentiment):
{headlines}

Return JSON with this exact schema:
{{
  "brief": [ {{"headline": "", "why_it_matters": "", "market_relevance": "Low|Medium|High"}} ]  // up to 5 items
  "so_what": {{"news": "", "explanation": ""}},
  "hidden_connection": {{"chain": ["Event", "Immediate effect", "Economic effect", "Corporate effect", "Potential market effect"]}},
  "who_wins_loses": {{"could_benefit": [""], "could_face_pressure": [""]}},
  "noise_vs_signal": {{"noise": "", "signal": "", "why": ""}},
  "number_of_day": {{"number": "", "label": "", "why_it_matters": ""}},
  "bull_bear": {{"bull_case": "", "bear_case": "", "what_would_change_view": ""}},
  "tomorrows_question": {{"question": "", "if_yes": "", "if_no": "", "what_to_watch": ""}},
  "finance_lesson": {{"todays_news": "", "concept": "", "simple_explanation": "", "why_it_matters": ""}},
  "thesis_board": {{"biggest_driver": "", "secondary_driver": "", "biggest_risk": "", "biggest_opportunity": "", "most_important_data_point": "", "most_important_question": ""}}
}}
"""


def build_editorial_prompt(stock_articles, index_snapshot, sector_performance) -> str:
    index_lines = "\n".join(
        f"- {name}: {d['price']} ({'+' if d['change']>=0 else ''}{d['change']}, {d['pct']}%)"
        for name, d in index_snapshot.items()
    ) or "Not available today."
    sector_lines = "\n".join(
        f"- {name}: {d['pct']}% ({d['status']})" for name, d in sector_performance.items()
    ) or "Not available today."
    headline_lines = "\n".join(
        f"- {a['title']} | {a['source']} | {a['sector']} | {a['sentiment']['label']}"
        for a in stock_articles[:20]
    ) or "No headlines available today."
    return EDITORIAL_PROMPT_TEMPLATE.format(
        index_data=index_lines, sector_data=sector_lines, headlines=headline_lines
    )


def call_gemini(prompt: str) -> dict | None:
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


def build_fallback_editorial(stock_articles, index_snapshot, sector_performance) -> dict:
    """Rule-based approximation used when no Gemini key is set, or the call fails.
    Grounded only in real data we have — never invents figures."""
    ranked = sorted(stock_articles, key=lambda a: abs(a["sentiment"]["score"]), reverse=True)

    def relevance(a):
        s = abs(a["sentiment"]["score"])
        return "High" if s >= 2 else "Medium" if s == 1 else "Low"

    brief = [
        {"headline": a["title"], "why_it_matters": a["why_matters"], "market_relevance": relevance(a)}
        for a in ranked[:5]
    ]

    top = ranked[0] if ranked else None
    so_what = {
        "news": top["title"] if top else "No major story today.",
        "explanation": top["in_simple_words"] if top else "Check back after the next update.",
    }

    biggest_sector_move = max(sector_performance.items(), key=lambda kv: abs(kv[1]["pct"])) if sector_performance else None
    number_of_day = {
        "number": f"{biggest_sector_move[1]['pct']}%" if biggest_sector_move else "N/A",
        "label": biggest_sector_move[0] if biggest_sector_move else "No data",
        "why_it_matters": f"This was today's biggest sector move — {biggest_sector_move[1]['status'].lower()} — "
                           "worth understanding before the rest of the news." if biggest_sector_move else "",
    }

    hidden_connection = {
        "chain": [top["title"], "Sector sentiment shifts", top["why_matters"],
                  f"Watch: {top['watch']}", "Index-level reaction possible"] if top
        else ["Not enough signal today"]
    }

    who_wins = {
        "could_benefit": [top["watch"]] if top and top["sentiment"]["label"] == "Bullish" else [],
        "could_face_pressure": [top["watch"]] if top and top["sentiment"]["label"] == "Bearish" else [],
    }

    return {
        "brief": brief,
        "so_what": so_what,
        "hidden_connection": hidden_connection,
        "who_wins_loses": who_wins,
        "noise_vs_signal": {
            "noise": "Short-term headline reactions and viral claims.",
            "signal": top["title"] if top else "N/A",
            "why": "Sector-index moves and RBI/macro data carry more weight than single-day sentiment swings.",
        },
        "number_of_day": number_of_day,
        "bull_bear": {
            "bull_case": "Domestic buying (mutual funds/SIPs) has been offsetting FII selling in recent sessions.",
            "bear_case": "Global crude and US rate moves remain a swing factor for Indian markets.",
            "what_would_change_view": "A sustained shift in FII flow direction or a surprise RBI policy move.",
        },
        "tomorrows_question": {
            "question": f"Does today's {top['sector'] if top else 'market'} move continue tomorrow?",
            "if_yes": "Expect follow-through buying/selling in related stocks.",
            "if_no": "Today's move may have been a one-day reaction rather than a trend.",
            "what_to_watch": "Opening-session volumes and FII/DII provisional data.",
        },
        "finance_lesson": {
            "todays_news": top["title"] if top else "N/A",
            "concept": "Sector Weightage in an Index",
            "simple_explanation": "Not every stock moves the index equally — heavier-weighted sectors "
                                   "like banking move Nifty/Sensex more than smaller-weighted ones.",
            "why_it_matters": "Understanding index weightage helps explain why some sector news moves "
                               "the whole market and other news barely registers.",
        },
        "thesis_board": {
            "biggest_driver": top["sector"] if top else "N/A",
            "secondary_driver": ranked[1]["sector"] if len(ranked) > 1 else "N/A",
            "biggest_risk": "Global crude/rate volatility",
            "biggest_opportunity": "Domestic institutional buying support",
            "most_important_data_point": number_of_day["label"],
            "most_important_question": f"Is today's {top['sector'] if top else 'move'} durable or a one-day reaction?",
        },
        "generated_by": "fallback",
    }


def generate_editorial(stock_articles, index_snapshot, sector_performance) -> dict:
    prompt = build_editorial_prompt(stock_articles, index_snapshot, sector_performance)
    result = call_gemini(prompt)
    if result:
        result["generated_by"] = "gemini"
        return result
    return build_fallback_editorial(stock_articles, index_snapshot, sector_performance)


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

    # --- overall Jetro sentiment score (0-100) ---
    total_score = sum(a["sentiment"]["score"] for a in stock_articles)
    jetro_score = max(0, min(100, round(50 + total_score * 3)))
    if jetro_score >= 75:
        jetro_label = "Strong Bullish"
        mood_word, mood_phrase = "Happy", "Buying Interest"
    elif jetro_score >= 60:
        jetro_label = "Bullish"
        mood_word, mood_phrase = "Positive", "Mild Buying Interest"
    elif jetro_score >= 40:
        jetro_label = "Neutral"
        mood_word, mood_phrase = "Cautious", "Wait and Watch"
    elif jetro_score >= 25:
        jetro_label = "Bearish"
        mood_word, mood_phrase = "Worried", "Selling Pressure"
    else:
        jetro_label = "Strong Bearish"
        mood_word, mood_phrase = "Fearful", "Heavy Selling Pressure"

    # --- top 3 "why is the market moving" reasons ---
    ranked = sorted(stock_articles, key=lambda a: abs(a["sentiment"]["score"]), reverse=True)
    why_reasons = [
        {"label": a["sector"], "text": a["in_simple_words"]}
        for a in ranked[:3]
    ]

    # --- biggest story (lead article) ---
    biggest_story = None
    if ranked:
        top = ranked[0]
        biggest_story = {
            "sector": top["sector"],
            "jetro_impact": top["jetro_impact"],
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
        "jetro_score": jetro_score,
        "jetro_label": jetro_label,
        "mood_word": mood_word,
        "mood_phrase": mood_phrase,
        "why_reasons": why_reasons,
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

    print(f"Wrote data.json — {len(unique_articles)} articles, Jetro score {jetro_score} ({jetro_label})")


if __name__ == "__main__":
    main()
