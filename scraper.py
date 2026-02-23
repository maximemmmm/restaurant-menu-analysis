"""
scraper.py — Menu scraper with HTML / PDF / Image pipeline.

Strategy (cascading, fastest first):
  1. requests (static HTML)
  2. Browser service at http://127.0.0.1:17000 (JS-heavy sites)
  3. Playwright Stealth (Cloudflare-protected)
  4. PDF via pdfplumber
  5. Image via vision LLM (claude or openai) — only when necessary

Output: normalized snapshot dict:
  {
    "restaurant": str,
    "slug": str,
    "menu_id": str,
    "url": str,
    "scraped_at": str (ISO),
    "scrape_method": str,
    "checksum": str,
    "sections": {
        "appetizers": [{"name": str, "price": float|None, "description": str|None, "modifiers": [str]}],
        ...
    }
  }
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

# ── Section name normalisation map ──────────────────────────────────────────

SECTION_MAP: dict[str, str] = {
    # Italian
    "antipasti": "appetizers", "antipasto": "appetizers",
    "insalate": "salads", "insalata": "salads",
    "zuppe": "soups", "zuppa": "soups",
    "pasta": "pasta", "paste": "pasta",
    "risotto": "risotto",
    "secondi": "entrees", "secondo": "entrees",
    "pesce": "seafood",
    "carne": "meat",
    "pizze": "pizza", "pizza": "pizza",
    "focacce": "flatbreads",
    "contorni": "sides", "contorno": "sides",
    "dolci": "desserts", "dolce": "desserts",
    "bevande": "drinks", "bevanda": "drinks",
    "vini": "wine", "vino": "wine",
    "birre": "beer",
    # English (common variants)
    "starters": "appetizers", "appetizers": "appetizers",
    "small plates": "appetizers", "shareables": "appetizers",
    "first courses": "appetizers",
    "salads": "salads",
    "soups": "soups", "soups & salads": "soups",
    "mains": "entrees", "main courses": "entrees", "entrees": "entrees",
    "entrées": "entrees", "main dishes": "entrees",
    "seafood": "seafood", "fish": "seafood",
    "meat": "meat", "poultry": "meat", "chicken": "meat",
    "sides": "sides", "side dishes": "sides", "sides & extras": "sides",
    "desserts": "desserts", "sweets": "desserts",
    "drinks": "drinks", "beverages": "drinks", "cocktails": "drinks",
    "wine": "wine", "wines": "wine", "wine list": "wine",
    "beer": "beer", "beers": "beer",
    "specials": "specials", "daily specials": "specials",
    "chef's specials": "specials", "chef specials": "specials",
    "lunch specials": "specials",
    "happy hour": "happy_hour",
    "kids": "kids", "children": "kids", "children's menu": "kids",
    "brunch": "brunch",
    "breakfast": "breakfast",
}

MENU_LINK_KEYWORDS = [
    "lunch", "dinner", "brunch", "breakfast",
    "happy hour", "happyhour", "happy-hour",
    "specials", "catering", "takeout", "to-go",
    "wine", "cocktail", "bar menu",
]

SKIP_TAGS = {"script", "style", "noscript", "head", "nav", "footer", "header", "aside"}
PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")
MODIFIER_RE = re.compile(r"(?:add|with|sub|substitute|\+\$)\s+.{3,50}", re.IGNORECASE)


# ── Utilities ────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")


def normalise_section(raw: str) -> str:
    key = raw.strip().lower()
    return SECTION_MAP.get(key, slugify(key) or "other")


def snapshot_checksum(sections: dict) -> str:
    canonical = json.dumps(sections, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── HTML parser ─────────────────────────────────────────────────────────────

def _parse_price(text: str) -> float | None:
    m = PRICE_RE.search(text)
    return float(m.group(1)) if m else None


def _extract_modifiers(text: str) -> list[str]:
    return [m.group(0).strip() for m in MODIFIER_RE.finditer(text)]


def parse_html_menu(html: str) -> dict[str, list[dict]]:
    """
    Extract menu sections from HTML.

    Strategy: look for section headings (h2/h3/h4 or elements with 'section'
    class/id) followed by lists/divs of items with prices.
    Falls back to plain text line-by-line parsing if structure is sparse.
    """
    try:
        from bs4 import BeautifulSoup, NavigableString
    except ImportError:
        return _parse_text_menu(_html_to_text(html))

    soup = BeautifulSoup(html, "html.parser")

    # Remove noise tags
    for tag in soup(SKIP_TAGS):
        tag.decompose()

    sections: dict[str, list[dict]] = {}
    current_section = "other"

    # Heading tags that typically mark menu sections
    heading_tags = {"h1", "h2", "h3", "h4", "h5"}

    # Walk all tags in document order
    for el in soup.find_all(True):
        tag_name = el.name.lower() if el.name else ""

        # ── Detect section heading ──
        if tag_name in heading_tags:
            text = el.get_text(" ", strip=True)
            if text and len(text) < 80:
                candidate = normalise_section(text)
                # Accept as section if it's a known key or short phrase
                if candidate in SECTION_MAP.values() or len(text.split()) <= 4:
                    current_section = candidate
                    if current_section not in sections:
                        sections[current_section] = []
                    continue

        # Check for div/section with class/id hinting at menu sections
        cls = " ".join(el.get("class", [])).lower()
        eid = (el.get("id") or "").lower()
        combined = f"{cls} {eid}"
        for kw, norm in SECTION_MAP.items():
            if kw in combined:
                current_section = norm
                if current_section not in sections:
                    sections[current_section] = []
                break

        # ── Detect ALL-CAPS section headings in <p>/<div>/<span> ──
        # Many restaurant sites use styled divs, not heading tags, for section names
        if tag_name in {"p", "div", "span", "li", "strong", "b"}:
            raw = el.get_text(" ", strip=True)
            # Must be short, no digits (not a price line), ALL CAPS or in SECTION_MAP
            if raw and len(raw) < 60 and not re.search(r"\d", raw):
                candidate = normalise_section(raw)
                if candidate in SECTION_MAP.values():
                    current_section = candidate
                    if current_section not in sections:
                        sections[current_section] = []
                    continue
                # Also catch ALL CAPS text that looks like a category (e.g. "APPETIZERS", "PASTA")
                if raw == raw.upper() and len(raw.split()) <= 4 and len(raw) > 3:
                    candidate = normalise_section(raw)
                    current_section = candidate
                    if current_section not in sections:
                        sections[current_section] = []
                    continue

        # ── Detect menu item ──
        # Item: element that contains a price
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 400:
            continue
        price = _parse_price(text)
        if price is None:
            continue

        # Try to split name from rest
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            continue
        name_line = lines[0]
        # Remove price from name
        name = PRICE_RE.sub("", name_line).strip().strip("-–").strip()
        if not name or len(name) > 120:
            continue

        # Description: remaining text minus price and name
        desc_lines = lines[1:] if len(lines) > 1 else []
        desc = " ".join(PRICE_RE.sub("", l) for l in desc_lines).strip() or None
        if desc and len(desc) > 300:
            desc = desc[:300]

        modifiers = _extract_modifiers(text)

        # Avoid duplicates within section
        existing = sections.setdefault(current_section, [])
        already = any(i["name"].lower() == name.lower() for i in existing)
        if not already:
            existing.append({
                "name": name,
                "price": price,
                "description": desc,
                "modifiers": modifiers,
            })

    # Remove empty sections and sections with suspiciously few items
    return {k: v for k, v in sections.items() if v}


def _html_to_text(html: str) -> str:
    """Strip HTML tags → plain text."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s{2,}", "\n", text)


def _parse_text_menu(text: str) -> dict[str, list[dict]]:
    """
    Fallback: parse menu from plain text line by line.
    Heuristic: lines with $price → menu items; lines without price but
    short enough → section headings.
    """
    sections: dict[str, list[dict]] = {}
    current_section = "other"
    pending_name: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        price = _parse_price(line)

        if price is not None:
            name_part = PRICE_RE.sub("", line).strip().strip("-–").strip()
            name = pending_name or name_part
            if name and len(name) < 120:
                sections.setdefault(current_section, []).append({
                    "name": name,
                    "price": price,
                    "description": None,
                    "modifiers": _extract_modifiers(line),
                })
            pending_name = None
        elif len(line) < 60 and not re.search(r"\d", line):
            # Could be a section heading or item name on its own line
            candidate = normalise_section(line)
            if candidate in SECTION_MAP.values():
                current_section = candidate
                pending_name = None
            else:
                pending_name = line  # might be item name on its own line
        else:
            pending_name = None

    return {k: v for k, v in sections.items() if v}


# ── PDF extraction ───────────────────────────────────────────────────────────

def extract_pdf_menu(pdf_path: str) -> dict[str, list[dict]]:
    """Extract text from PDF → parse as menu. Falls back to image if scanned."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber not installed; run: pip install pdfplumber")

    with pdfplumber.open(pdf_path) as pdf:
        pages_text = []
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                pages_text.append(t)

    full_text = "\n".join(pages_text).strip()
    if len(full_text) < 50:
        # Likely scanned PDF — render first page to image and use vision
        img_path = _render_pdf_page_to_image(pdf_path, page_num=0)
        if img_path:
            return extract_image_menu(img_path)
        return {}

    return _parse_text_menu(full_text)


def _render_pdf_page_to_image(pdf_path: str, page_num: int = 0) -> str | None:
    """Render a PDF page to PNG using pdftoppm (poppler)."""
    try:
        out_dir = tempfile.mkdtemp()
        out_prefix = os.path.join(out_dir, "page")
        subprocess.run(
            ["pdftoppm", "-r", "150", "-f", str(page_num + 1), "-l", str(page_num + 1),
             "-png", pdf_path, out_prefix],
            check=True, capture_output=True,
        )
        imgs = sorted(f for f in os.listdir(out_dir) if f.endswith(".png"))
        if imgs:
            return os.path.join(out_dir, imgs[0])
    except Exception:
        pass
    return None


# ── Image extraction (vision LLM) ────────────────────────────────────────────

def _image_phash(image_path: str) -> str | None:
    """Perceptual hash of image — cheap change detection."""
    try:
        import imagehash
        from PIL import Image
        img = Image.open(image_path)
        return str(imagehash.phash(img))
    except ImportError:
        # Fallback: SHA-256 of raw bytes
        with open(image_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def extract_image_menu(image_path: str) -> dict[str, list[dict]]:
    """
    Call a vision LLM to extract structured menu from image.
    Uses claude or openai depending on available env vars.
    Returns parsed sections dict.
    """
    import base64

    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    prompt = (
        "Extract all menu items from this restaurant menu image. "
        "Return ONLY valid JSON (no markdown), format:\n"
        '{"sections": {"appetizers": [{"name": "...", "price": 0.00, '
        '"description": "...", "modifiers": []}], '
        '"salads": [...], "entrees": [...], "pasta": [...], '
        '"pizza": [...], "desserts": [...], "drinks": [...]}}\n'
        "Use standard English section keys: appetizers, salads, soups, pasta, "
        "entrees, seafood, meat, pizza, sides, desserts, drinks, wine, beer, "
        "specials, happy_hour, brunch, kids. "
        "price as float (null if not shown). "
        "description as string (null if not shown). "
        "modifiers as list of strings (empty list if none)."
    )

    # Try Anthropic Claude first (preferred for vision)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    raw_json = None

    if anthropic_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64",
                                                      "media_type": "image/jpeg",
                                                      "data": image_b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            raw_json = resp.content[0].text.strip()
        except Exception as e:
            print(f"[scraper] Claude vision error: {e}")

    if raw_json is None and openai_key:
        try:
            import openai
            client = openai.OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            raw_json = resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"[scraper] OpenAI vision error: {e}")

    if raw_json:
        # Strip markdown fences if present
        raw_json = re.sub(r"^```(?:json)?\n?", "", raw_json)
        raw_json = re.sub(r"\n?```$", "", raw_json)
        try:
            data = json.loads(raw_json)
            return data.get("sections", {})
        except json.JSONDecodeError as e:
            print(f"[scraper] JSON parse error from vision: {e}")

    return {}


# ── HTTP fetching ─────────────────────────────────────────────────────────────

def _fetch_static(url: str, timeout: int = 15) -> str | None:
    """Simple requests fetch — works for static sites."""
    try:
        import requests
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception:
        return None


def _fetch_browser_service(url: str, timeout: int = 30) -> str | None:
    """Fetch via local browser service at http://127.0.0.1:17000."""
    try:
        import requests
        payload = {"token": "super-secret", "url": url, "action": "html"}
        r = requests.post("http://127.0.0.1:17000/task", json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        # Response may contain html directly or a file id
        return data.get("html") or data.get("content") or data.get("text")
    except Exception:
        return None


def _fetch_playwright(url: str, timeout_ms: int = 15000) -> str | None:
    """Fetch via Playwright Stealth script."""
    stealth_script = os.path.expanduser(
        "~/.openclaw/workspace/skills/playwright-scraper-skill/scripts/playwright-stealth.js"
    )
    if not os.path.exists(stealth_script):
        return None
    try:
        env = os.environ.copy()
        env["WAIT_TIME"] = str(timeout_ms)
        env["HEADLESS"] = "true"
        result = subprocess.run(
            ["node", stealth_script, url],
            capture_output=True, text=True, timeout=timeout_ms // 1000 + 30, env=env,
        )
        if result.returncode == 0 and result.stdout:
            # Output is JSON: {"title":..., "content":..., "htmlFile":...}
            data = json.loads(result.stdout)
            html_file = data.get("htmlFile")
            if html_file and os.path.exists(html_file):
                with open(html_file) as f:
                    return f.read()
            return data.get("content") or data.get("text") or ""
    except Exception as e:
        print(f"[scraper] Playwright error: {e}")
    return None


def _detect_content_type(url: str) -> str:
    """HEAD request to detect Content-Type."""
    try:
        import requests
        r = requests.head(url, timeout=10, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        if "pdf" in ct:
            return "pdf"
        if ct.startswith("image/"):
            return "image"
    except Exception:
        pass
    # Guess from URL
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if re.search(r"\.(png|jpe?g|webp|gif)$", path):
        return "image"
    return "html"


def _download_file(url: str, suffix: str) -> str | None:
    """Download a binary file to a temp path."""
    try:
        import requests
        r = requests.get(url, timeout=30, stream=True)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        for chunk in r.iter_content(chunk_size=8192):
            tmp.write(chunk)
        tmp.close()
        return tmp.name
    except Exception:
        return None


# ── Auto-discovery of multiple menus ─────────────────────────────────────────

def discover_menu_links(html: str, base_url: str) -> list[dict]:
    """
    Find links on the page that likely lead to separate menus
    (lunch, dinner, happy hour, specials, catering, brunch, etc.).
    Returns [{"id": slug, "label": text, "url": absolute_url}]
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    found: list[dict] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True).lower()
        abs_url = urljoin(base_url, href)

        if abs_url in seen_urls:
            continue
        # Must stay on same domain
        if urlparse(abs_url).netloc != urlparse(base_url).netloc:
            continue

        for kw in MENU_LINK_KEYWORDS:
            if kw in text or kw in href.lower():
                label = a.get_text(" ", strip=True)
                menu_id = slugify(label) or slugify(kw)
                found.append({"id": menu_id, "label": label, "url": abs_url})
                seen_urls.add(abs_url)
                break

    return found


# ── Main scrape entry point ───────────────────────────────────────────────────

def scrape_menu(
    url: str,
    restaurant_name: str = "",
    slug: str = "",
    menu_id: str = "main",
) -> dict[str, Any]:
    """
    Scrape a single menu URL and return a normalized snapshot dict.

    Tries: static → browser service → Playwright Stealth (HTML)
           pdfplumber (PDF), vision LLM (image).
    """
    scraped_at = datetime.now(timezone.utc).isoformat()
    content_type = _detect_content_type(url)
    sections: dict[str, list] = {}
    method = "unknown"

    if content_type == "pdf":
        tmp_pdf = _download_file(url, ".pdf")
        if tmp_pdf:
            try:
                sections = extract_pdf_menu(tmp_pdf)
                method = "pdfplumber"
            finally:
                try:
                    os.unlink(tmp_pdf)
                except OSError:
                    pass

    elif content_type == "image":
        tmp_img = _download_file(url, ".jpg")
        if tmp_img:
            try:
                sections = extract_image_menu(tmp_img)
                method = "vision_llm"
            finally:
                try:
                    os.unlink(tmp_img)
                except OSError:
                    pass

    else:
        # HTML pipeline — cascade through fetchers until we parse ≥1 menu item.
        # Escalation triggers on BOTH fetch failure (None) AND 0 items parsed
        # (covers JS-rendered sites that return skeleton HTML via requests).
        _FETCHERS = [
            ("requests", _fetch_static),
            ("browser_service", _fetch_browser_service),
            ("playwright_stealth", _fetch_playwright),
        ]
        for fetch_method, fetcher in _FETCHERS:
            html = fetcher(url)
            if not html:
                print(f"[scraper] {fetch_method}: fetch returned nothing for {url}")
                continue
            sections = parse_html_menu(html)
            if sections:
                method = fetch_method
                break
            print(f"[scraper] {fetch_method}: 0 items parsed at {url}, escalating…")

    checksum = snapshot_checksum(sections)

    return {
        "restaurant": restaurant_name,
        "slug": slug or slugify(restaurant_name),
        "menu_id": menu_id,
        "url": url,
        "scraped_at": scraped_at,
        "scrape_method": method,
        "checksum": checksum,
        "sections": sections,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python scraper.py <url> [restaurant_name] [menu_id]", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    mid = sys.argv[3] if len(sys.argv) > 3 else "main"
    result = scrape_menu(url, restaurant_name=name, menu_id=mid)
    print(json.dumps(result, ensure_ascii=False, indent=2))
