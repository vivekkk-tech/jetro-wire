# The Jetro Wire — your self-updating AI finance newspaper

Everything here is already built and free. You just need to do the one-time
setup (5-10 minutes), because I can't create accounts or click things on
your screen for you. After that, it runs itself forever — no cost, no
maintenance.

## What it does
- Pulls headlines every 2 hours from Economic Times, Moneycontrol, LiveMint,
  Business Standard, Financial Express (all free public RSS, no key needed)
- Tags each story: sector (Banking, IT, Auto, Pharma, FMCG, Energy, Metals,
  Realty, Telecom, PSU, Global/Macro) + sentiment (Bullish/Bearish/Neutral)
- Pulls out anything AI-in-finance related into its own section
- Shows an overall "market mood" gauge
- Rebuilds `data.json` and the site updates itself — you never touch it again

## The stack (why it's ₹0 forever)
| Piece | Tool | Cost |
|---|---|---|
| Hosting | GitHub Pages | Free |
| Scheduler | GitHub Actions | Free (2,000 min/month, this uses ~5 min/day) |
| Data source | Public RSS feeds | Free |
| Domain | yourname.github.io/jetro-wire | Free |

## Setup — do this once

1. **Create a free GitHub account** at github.com (skip if you have one).
2. **Create a new repository** — click "New", name it `jetro-wire`, set it
   to **Public** (required for free Actions minutes), don't add a README.
3. **Upload these 4 files/folders** keeping the exact structure:
   - `index.html`
   - `fetch_news.py`
   - `requirements.txt`
   - `data.json`
   - `.github/workflows/update.yml`
   Easiest way: on the repo page, click "Add file → Upload files", drag
   everything in (GitHub preserves the `.github/workflows/` folder path).
4. **Turn on Pages**: Settings → Pages → under "Build and deployment",
   Source = "Deploy from a branch", Branch = `main`, folder = `/ (root)` →
   Save. Your site goes live at `https://<your-username>.github.io/jetro-wire/`
5. **Run the update once manually**: go to the "Actions" tab → click
   "Update Jetro AI Newspaper Data" → "Run workflow" → Run. Wait ~30
   seconds. This replaces the demo placeholder stories with real live ones.
6. Done. It will now auto-run every 2 hours by itself, forever, for free.

## Making it feel more "yours"
- Change the masthead name/tagline at the top of `index.html`
- Add/edit sector keywords in `SECTORS` inside `fetch_news.py` if a story
  gets misclassified — it's plain keyword matching, easy to tune
- Want it to update faster? Change `cron: "0 */2 * * *"` in
  `update.yml` — e.g. `"0 * * * *"` for hourly (still free, well under limits)

## What I couldn't do for you
I don't have access to your GitHub account, so I can't click "create repo"
or "enable Pages" myself — those need your login. Everything else (the
scraping logic, sentiment scoring, sector tagging, design, automation) is
done. Ping me anytime you want a new section (options-flow data, IPO
tracker, your own DCF picks pinned to the front page, etc.) and I'll build
it into this same repo.
