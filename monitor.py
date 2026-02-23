"""
monitor.py — Competitor menu monitoring orchestrator.

Usage:
    python3 monitor.py [--restaurant SLUG] [--dry-run]

Flow:
    1. Read restaurants.json → list of restaurants + their menus
    2. For each restaurant/menu: scrape current state
    3. Compare with previous snapshot (checksum fast-path)
    4. Save snapshots (rotate: current → previous)
    5. Print JSON diff to stdout (for the cron agent to format & send)
       OR if no changes: send "🟢 Без изменений" directly and exit silently.

Exit codes:
    0 — success (with or without changes)
    1 — fatal error (could not read config, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ─────────────────────────────────────────────────────────────────────

WORKSPACE = Path(os.path.expanduser("~/.openclaw/workspace"))
COMPETITORS_FILE = WORKSPACE / "data" / "competitors" / "restaurants.json"
SNAPSHOTS_DIR = WORKSPACE / "data" / "competitors" / "snapshots"
SKILL_DIR = Path(__file__).parent

# ── Imports from sibling modules ─────────────────────────────────────────────

sys.path.insert(0, str(SKILL_DIR))
from scraper import scrape_menu, discover_menu_links, _fetch_static, slugify
from differ import diff_restaurant


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def snapshot_path(restaurant_slug: str, menu_id: str, which: str) -> Path:
    """which: 'current' or 'previous'"""
    return SNAPSHOTS_DIR / restaurant_slug / menu_id / f"{which}.json"


def load_snapshot(restaurant_slug: str, menu_id: str, which: str) -> dict | None:
    p = snapshot_path(restaurant_slug, menu_id, which)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def save_snapshot(restaurant_slug: str, menu_id: str, which: str, data: dict) -> None:
    p = snapshot_path(restaurant_slug, menu_id, which)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def rotate_snapshots(restaurant_slug: str, menu_id: str, new_current: dict) -> None:
    """Move current → previous, save new_current as current."""
    old_current = load_snapshot(restaurant_slug, menu_id, "current")
    if old_current:
        save_snapshot(restaurant_slug, menu_id, "previous", old_current)
    save_snapshot(restaurant_slug, menu_id, "current", new_current)


# ── Auto-discovery: add missing menus to restaurant entry ─────────────────────

def auto_discover_menus(restaurant: dict) -> list[dict]:
    """
    If restaurant has no 'menus' list or only a placeholder,
    try to discover sub-menus from the website homepage.
    Returns the (possibly enriched) menus list.
    """
    menus = restaurant.get("menus") or []

    # If we already have real menu URLs, return as-is
    if any(m.get("url") for m in menus):
        return menus

    # Try to find menu links from website
    website = restaurant.get("website") or restaurant.get("menu_url")
    if not website:
        return menus

    html = _fetch_static(website)
    if not html:
        return menus

    discovered = discover_menu_links(html, website)

    if discovered:
        print(f"  [discover] Found {len(discovered)} sub-menus for {restaurant['name']}", file=sys.stderr)
        return discovered
    else:
        # Single menu — use website/menu_url directly
        url = restaurant.get("menu_url") or restaurant.get("website")
        return [{"id": "main", "label": "Menu", "url": url}]


# ── Send Telegram message directly (no LLM) ──────────────────────────────────

def send_telegram(message: str, chat_id: str = "-5089763501") -> bool:
    """Send message via openclaw CLI — used for 'no changes' notification."""
    try:
        result = subprocess.run(
            ["openclaw", "message", "send", "-c", "telegram", "-t", chat_id, message],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"[monitor] Telegram send failed: {e}", file=sys.stderr)
        return False


# ── Core monitoring logic ─────────────────────────────────────────────────────

def monitor_restaurant(restaurant: dict, dry_run: bool = False) -> dict[str, Any]:
    """
    Scrape all menus for one restaurant, diff against previous snapshots.
    Returns:
        {
            "name": str,
            "slug": str,
            "menus_checked": int,
            "changes": {menu_id: {section: {...}}},  # empty if no changes
            "failed_menus": [menu_id],
            "first_run_menus": [menu_id],
        }
    """
    slug = restaurant.get("slug") or slugify(restaurant["name"])
    result: dict[str, Any] = {
        "name": restaurant["name"],
        "slug": slug,
        "menus_checked": 0,
        "changes": {},
        "failed_menus": [],
        "first_run_menus": [],
    }

    menus = auto_discover_menus(restaurant)
    if not menus:
        print(f"  [skip] {restaurant['name']}: no menu URLs", file=sys.stderr)
        return result

    for menu in menus:
        menu_id = menu.get("id", "main")
        url = menu.get("url")
        if not url:
            continue

        print(f"  [scrape] {restaurant['name']} / {menu_id}: {url}", file=sys.stderr)

        try:
            current = scrape_menu(
                url=url,
                restaurant_name=restaurant["name"],
                slug=slug,
                menu_id=menu_id,
            )
        except Exception as e:
            print(f"  [error] {restaurant['name']} / {menu_id}: {e}", file=sys.stderr)
            result["failed_menus"].append(menu_id)
            continue

        result["menus_checked"] += 1

        previous = load_snapshot(slug, menu_id, "current")  # current becomes "previous" reference
        diff = diff_restaurant(current, previous)

        if not dry_run:
            rotate_snapshots(slug, menu_id, current)

        if diff["is_first_run"]:
            result["first_run_menus"].append(menu_id)
            item_count = sum(len(v) for v in current["sections"].values())
            print(f"  [first-run] {menu_id}: saved {item_count} items", file=sys.stderr)
        elif diff["has_changes"]:
            result["changes"][menu_id] = diff["sections"]
            print(f"  [changed] {menu_id}: {list(diff['sections'].keys())}", file=sys.stderr)
        else:
            print(f"  [no-change] {menu_id}", file=sys.stderr)

    return result


def run_monitor(only_slug: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """
    Run full monitoring cycle.
    Returns summary dict printed to stdout for the cron agent.
    """
    if not COMPETITORS_FILE.exists():
        print(f"[monitor] ERROR: {COMPETITORS_FILE} not found", file=sys.stderr)
        sys.exit(1)

    config = json.loads(COMPETITORS_FILE.read_text())
    restaurants = config.get("restaurants", [])

    if not restaurants:
        return {"error": "restaurants list is empty", "restaurants_checked": 0, "has_changes": False}

    summary: dict[str, Any] = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "restaurants_checked": 0,
        "restaurants_with_changes": 0,
        "first_run": [],
        "no_changes": [],
        "with_changes": [],
        "failed": [],
        "changes": [],  # detailed list
    }

    for restaurant in restaurants:
        slug = restaurant.get("slug") or slugify(restaurant["name"])
        if only_slug and slug != only_slug:
            continue

        print(f"\n[monitor] Processing: {restaurant['name']}", file=sys.stderr)
        res = monitor_restaurant(restaurant, dry_run=dry_run)

        if res["menus_checked"] == 0 and res["failed_menus"]:
            summary["failed"].append(restaurant["name"])
            continue

        summary["restaurants_checked"] += 1

        if res["first_run_menus"]:
            summary["first_run"].append(restaurant["name"])

        if res["changes"]:
            summary["restaurants_with_changes"] += 1
            summary["with_changes"].append(restaurant["name"])

            # Flatten changes for report
            for menu_id, sections in res["changes"].items():
                total_added = sum(len(s.get("added", [])) for s in sections.values())
                total_removed = sum(len(s.get("removed", [])) for s in sections.values())
                total_price = sum(len(s.get("price_changes", [])) for s in sections.values())
                summary["changes"].append({
                    "restaurant": restaurant["name"],
                    "menu": menu_id,
                    "sections": sections,
                    "totals": {
                        "added": total_added,
                        "removed": total_removed,
                        "price_changes": total_price,
                    },
                })
        else:
            if not res["first_run_menus"]:
                summary["no_changes"].append(restaurant["name"])

    summary["has_changes"] = bool(summary["changes"])
    return summary


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Competitor menu monitor")
    parser.add_argument("--restaurant", metavar="SLUG", help="Monitor only this restaurant slug")
    parser.add_argument("--dry-run", action="store_true", help="Do not save snapshots")
    parser.add_argument("--json", action="store_true", default=True, help="Output JSON (default)")
    args = parser.parse_args()

    summary = run_monitor(only_slug=args.restaurant, dry_run=args.dry_run)

    if not summary.get("has_changes") and not summary.get("first_run"):
        # No changes — notify Telegram directly and stay silent (0 LLM tokens)
        msg = f"🟢 Без изменений у конкурентов ({summary['date']}). Следующая проверка завтра."
        if not args.dry_run:
            send_telegram(msg)
        # Print nothing → cron agent sees empty output and does nothing
        print("", end="")
        return

    # Changes found — print JSON for the cron agent to format into Russian report
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
