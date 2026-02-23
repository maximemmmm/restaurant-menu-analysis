"""
differ.py — Pure Python diff between two normalized menu snapshots.

Compares current vs previous snapshot JSON by section → item name → price/description.
No LLM required. Returns structured dict; empty dict = no changes.
"""

from __future__ import annotations
import hashlib
import json
from typing import Any


def snapshot_checksum(sections: dict) -> str:
    """SHA-256 of canonical JSON of sections — fast equality check."""
    canonical = json.dumps(sections, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _items_by_name(items: list[dict]) -> dict[str, dict]:
    """Index items by normalised name (lowercase, stripped)."""
    result: dict[str, dict] = {}
    for item in items:
        key = item.get("name", "").strip().lower()
        if key:
            result[key] = item
    return result


def diff_sections(current: dict, previous: dict) -> dict[str, Any]:
    """
    Compare two snapshot dicts (keyed by section name).

    Returns:
        {
            "appetizers": {
                "added":         [{"name": ..., "price": ..., "description": ...}],
                "removed":       [{"name": ..., "price": ..., "description": ...}],
                "price_changes": [{"name": ..., "old_price": ..., "new_price": ...}],
                "desc_changes":  [{"name": ..., "old_desc": ..., "new_desc": ...}],
            },
            ...
        }
    Empty dict → no changes at all.
    """
    curr_sections: dict = current.get("sections", {})
    prev_sections: dict = previous.get("sections", {})
    all_sections = set(curr_sections) | set(prev_sections)

    result: dict[str, Any] = {}

    for section in sorted(all_sections):
        curr_items = _items_by_name(curr_sections.get(section, []))
        prev_items = _items_by_name(prev_sections.get(section, []))

        added = [
            curr_items[n]
            for n in curr_items
            if n not in prev_items
        ]
        removed = [
            prev_items[n]
            for n in prev_items
            if n not in curr_items
        ]
        price_changes = []
        desc_changes = []

        for name in curr_items:
            if name not in prev_items:
                continue
            curr_item = curr_items[name]
            prev_item = prev_items[name]

            curr_price = curr_item.get("price")
            prev_price = prev_item.get("price")
            if curr_price is not None and prev_price is not None:
                # Float comparison with tolerance to avoid floating-point noise
                if abs(float(curr_price) - float(prev_price)) > 0.005:
                    price_changes.append({
                        "name": curr_item["name"],
                        "old_price": prev_price,
                        "new_price": curr_price,
                    })

            curr_desc = (curr_item.get("description") or "").strip().lower()
            prev_desc = (prev_item.get("description") or "").strip().lower()
            if curr_desc != prev_desc and (curr_desc or prev_desc):
                desc_changes.append({
                    "name": curr_item["name"],
                    "old_desc": prev_item.get("description"),
                    "new_desc": curr_item.get("description"),
                })

        if added or removed or price_changes or desc_changes:
            result[section] = {
                "added": added,
                "removed": removed,
                "price_changes": price_changes,
                "desc_changes": desc_changes,
            }

    return result


def diff_restaurant(current_snap: dict, previous_snap: dict | None) -> dict[str, Any]:
    """
    High-level diff for a single restaurant (one menu).

    Returns:
        {
            "has_changes": bool,
            "is_first_run": bool,   # previous_snap was None
            "sections": { ... }     # output of diff_sections; empty if no changes
        }
    """
    if previous_snap is None:
        return {
            "has_changes": False,
            "is_first_run": True,
            "sections": {},
        }

    # Fast path: checksum equality
    curr_checksum = current_snap.get("checksum") or snapshot_checksum(current_snap.get("sections", {}))
    prev_checksum = previous_snap.get("checksum") or snapshot_checksum(previous_snap.get("sections", {}))
    if curr_checksum == prev_checksum:
        return {
            "has_changes": False,
            "is_first_run": False,
            "sections": {},
        }

    section_diff = diff_sections(current_snap, previous_snap)
    return {
        "has_changes": bool(section_diff),
        "is_first_run": False,
        "sections": section_diff,
    }


# ── CLI helper for quick testing ────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python differ.py current.json previous.json", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        curr = json.load(f)
    with open(sys.argv[2]) as f:
        prev = json.load(f)

    result = diff_restaurant(curr, prev)
    print(json.dumps(result, ensure_ascii=False, indent=2))
