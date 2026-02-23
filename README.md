# Restaurant Menu Analysis

Automated competitor menu monitoring system for restaurants. Tracks menu changes (added/removed dishes, price changes) across multiple competitor restaurants and delivers daily reports via Telegram.

## Overview

Built to help **Sole Mio Kitchen** (Boynton Beach, FL) track Italian restaurant competitors in the area. Runs daily via [OpenClaw](https://openclaw.ai) cron scheduler.

### Key Features

- **Multi-menu support** — each restaurant can have separate lunch, dinner, happy hour, specials menus
- **Auto-discovery** — automatically finds sub-menu links on restaurant websites
- **Cascading scraper** — tries static HTML → browser service → Playwright Stealth → PDF → Image (OCR)
- **Deterministic diff** — pure Python comparison by section/item/price, no LLM for comparison
- **Token-efficient** — LLM only formats the final report; if no changes, sends notification directly (0 LLM tokens)
- **Structured snapshots** — normalised JSON stored per restaurant/menu with rotation (current ↔ previous)

---

## Architecture

```
Cron (8:30 AM ET, daily)
  └─► Agent (OpenClaw): runs monitor.py
        ├─► scraper.py  — fetch each menu URL (HTML/PDF/Image)
        ├─► differ.py   — compare current vs previous snapshot
        ├─► snapshots/  — rotate: current → previous → save new current
        └─► Output:
              • JSON diff → Agent formats Russian Telegram report
              • Empty     → script sends "🟢 No changes" directly (0 LLM tokens)
```

### Token Savings

| Scenario | Old System | New System |
|---|---|---|
| No changes | ~20k tokens | **0 tokens** (direct send) |
| Changes found | ~100k tokens | **~1–2k tokens** (diff JSON only) |
| First run | ~100k tokens | **~5k tokens** |

---

## File Structure

```
restaurant-menu-analysis/
├── monitor.py          # Orchestrator: reads config, runs scrape+diff, outputs result
├── scraper.py          # Scraping pipeline: HTML / PDF / Image
├── differ.py           # Pure Python diff engine (no LLM)
├── restaurants.json    # Competitor catalog (template)
├── requirements.txt    # Python dependencies
└── README.md

Data (runtime, not in repo):
~/.openclaw/workspace/data/competitors/
├── restaurants.json          # Active config
└── snapshots/
    ├── le-sorelle/
    │   ├── dinner/
    │   │   ├── current.json  # Latest snapshot
    │   │   └── previous.json # Previous snapshot
    │   └── lunch/
    │       ├── current.json
    │       └── previous.json
    └── olive-garden/
        └── main/
            ├── current.json
            └── previous.json
```

---

## Snapshot Format

Each snapshot (`current.json` / `previous.json`) contains:

```json
{
  "restaurant": "Le Sorelle Restaurant",
  "slug": "le-sorelle",
  "menu_id": "dinner",
  "url": "https://lesorellerestaurant.com/dinner-menu",
  "scraped_at": "2026-02-23T08:30:00Z",
  "scrape_method": "requests",
  "checksum": "sha256-of-sections-json",
  "sections": {
    "appetizers": [
      {
        "name": "Bruschetta",
        "price": 12.0,
        "description": "Toasted bread with tomatoes and basil",
        "modifiers": ["add prosciutto +$4"]
      }
    ],
    "pasta": [...],
    "entrees": [...],
    "desserts": [...]
  }
}
```

### Standard Section Keys

| Key | Italian equivalents | English equivalents |
|---|---|---|
| `appetizers` | antipasti, antipasto | starters, small plates, shareables |
| `salads` | insalate | salads |
| `soups` | zuppe | soups |
| `pasta` | pasta, paste | pasta |
| `entrees` | secondi | mains, main courses, entrées |
| `seafood` | pesce | seafood, fish |
| `meat` | carne | meat, poultry |
| `pizza` | pizze | pizza |
| `sides` | contorni | sides, side dishes |
| `desserts` | dolci | desserts, sweets |
| `drinks` | bevande | drinks, beverages, cocktails |
| `wine` | vini | wine, wines |
| `specials` | — | daily specials, chef's specials |
| `happy_hour` | — | happy hour |
| `brunch` | — | brunch |
| `kids` | — | kids, children's menu |

---

## Scraping Pipeline

```
URL → detect Content-Type
  ├── text/html ──► 1. requests (static)
  │                  2. Browser service @ 127.0.0.1:17000 (JS sites)
  │                  3. Playwright Stealth (Cloudflare-protected)
  │                  └── BeautifulSoup → section/item extraction
  ├── application/pdf ──► pdfplumber → text extraction
  │                        └── if scanned: render page → Image pipeline
  └── image/* ──► phash check (fast: did image change at all?)
                  └── if changed: vision LLM (Claude/GPT-4o-mini) → JSON
```

**Anti-bot measures (Playwright Stealth):**
- Hides `navigator.webdriver`
- Realistic iPhone User-Agent
- Random delays 5–20 seconds
- Cloudflare challenge detection + wait

---

## Diff Engine

Comparison is done **section by section, item by item**:

1. **Fast path**: compare `checksum` (SHA-256 of sections JSON). If equal → skip, 0 diff work.
2. **Full diff**: for each section, index items by normalised name (lowercase, stripped)
   - `added`: items in current but not in previous
   - `removed`: items in previous but not in current
   - `price_changes`: same name, price differs by > $0.01
   - `desc_changes`: same name, description changed

---

## Diff Output (for LLM formatter)

```json
{
  "date": "2026-02-23",
  "restaurants_checked": 8,
  "restaurants_with_changes": 2,
  "has_changes": true,
  "first_run": [],
  "no_changes": ["olive-garden", "porto-bella"],
  "with_changes": ["le-sorelle"],
  "failed": [],
  "changes": [
    {
      "restaurant": "Le Sorelle Restaurant",
      "menu": "dinner",
      "totals": {"added": 3, "removed": 1, "price_changes": 2},
      "sections": {
        "entrees": {
          "added": [{"name": "Branzino al Forno", "price": 34.0, "description": "..."}],
          "removed": [{"name": "Winter Special", "price": 28.0, "description": null}],
          "price_changes": [{"name": "Pasta Primavera", "old_price": 18.0, "new_price": 21.0}],
          "desc_changes": []
        }
      }
    }
  ]
}
```

---

## restaurants.json Config

```json
{
  "updated": "2026-02-23",
  "restaurants": [
    {
      "name": "Le Sorelle Restaurant",
      "slug": "le-sorelle",
      "address": "Delray Beach, FL",
      "cuisine": ["italian"],
      "website": "https://lesorellerestaurant.com/",
      "menus": [
        {"id": "dinner",     "label": "Dinner Menu", "url": "https://lesorellerestaurant.com/dinner-menu"},
        {"id": "lunch",      "label": "Lunch Menu",  "url": "https://lesorellerestaurant.com/lunch-menu"},
        {"id": "happy-hour", "label": "Happy Hour",  "url": "https://lesorellerestaurant.com/happy-hour"}
      ],
      "yelp": "https://yelp.com/biz/le-sorelle-restaurant-delray-beach",
      "notes": "Main competitor — active seasonal menu + events"
    }
  ]
}
```

If a restaurant has only one URL and you don't know sub-menu URLs, set `"menus": []` and the script will auto-discover links from the website homepage.

---

## Installation

```bash
git clone https://github.com/maksimemm/restaurant-menu-analysis.git
cd restaurant-menu-analysis
pip install -r requirements.txt
```

### Dependencies

```
requests
beautifulsoup4
pdfplumber
imagehash
Pillow
```

Optional (for vision LLM on image menus):
```
anthropic   # or
openai
```

Optional (for Playwright Stealth on protected sites):
```bash
npm install playwright
npx playwright install chromium
```

---

## Usage

```bash
# Monitor all restaurants
python3 monitor.py

# Dry run (no snapshot writes)
python3 monitor.py --dry-run

# Monitor one restaurant only
python3 monitor.py --restaurant le-sorelle

# Scrape a single URL and print snapshot
python3 scraper.py https://restaurant.com/menu "Restaurant Name" dinner

# Diff two snapshots
python3 differ.py snapshots/le-sorelle/dinner/current.json \
                  snapshots/le-sorelle/dinner/previous.json
```

---

## Integration with OpenClaw Cron

The system is designed to run as an [OpenClaw](https://openclaw.ai) cron job:

```json
{
  "name": "Competitors: Daily Monitor",
  "schedule": {"kind": "cron", "expr": "30 8 * * *", "tz": "America/New_York"},
  "payload": {
    "kind": "agentTurn",
    "message": "Run: python3 ~/.openclaw/workspace/skills/competitor-monitor/monitor.py\nIf JSON output with has_changes:true — format Russian report and send to Telegram.\nIf empty output — do nothing (script already sent notification).",
    "timeoutSeconds": 480
  }
}
```

---

## License

MIT
