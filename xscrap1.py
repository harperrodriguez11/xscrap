#!/usr/bin/env python3
"""
X.com video/image URL scraper using Playwright — multi-username version.

Auth is done via session cookies (auth_token, ct0, twid) passed through
environment variables that map to GitHub Actions encrypted secrets.
NEVER commit cookies to the repo or hardcode them in this file.

You can pass either:
  - X_TARGET_USERNAMES: a comma- and/or newline-separated list of usernames
    (with or without "@") or full URLs (profile, search, home, etc). Targets
    are scraped ONE AT A TIME, in the order given — open target 1, scroll
    and collect until done/stuck, move to target 2, etc.
  - X_TARGET_URL: single-target fallback (old behavior), used only if
    X_TARGET_USERNAMES is not set.

Output: results are written straight into a Google Sheet —
    - all video tweet URLs -> "Videos" tab
    - all image tweet URLs -> "Images" tab
Each tab now has two columns: Tweet URL, Source (the username/URL it came
from). Each run REPLACES the previous contents of those tabs.
"""

import asyncio
import json
import os
import random
import re
import sys
import time

from playwright.async_api import async_playwright

# ── Config (override via env vars / workflow inputs) ────────────────────────

TARGET_USERNAMES_RAW = os.environ.get("X_TARGET_USERNAMES", "").strip()
TARGET_URL_FALLBACK  = os.environ.get("X_TARGET_URL", "https://x.com/home")

MAX_URLS_PER_TARGET = int(os.environ.get("X_MAX_URLS", "50"))
MAX_STUCK           = int(os.environ.get("X_MAX_STUCK_TICKS", "8"))
HEADLESS            = os.environ.get("X_HEADLESS", "true").lower() != "false"

BETWEEN_TARGET_DELAY = (1.5, 3.0)  # seconds, human-ish pause when switching accounts

AUTH_TOKEN = os.environ.get("X_AUTH_TOKEN")
CT0        = os.environ.get("X_CT0")
TWID       = os.environ.get("X_TWID")

# Sheet to write results into — "Videos" / "Images" tabs.
URLS_SHEET_ID = os.environ.get("X_URLS_SHEET_ID", "17PZy32Hmr504A7gVkGoH0OMJSQPd1oZVtaVp6Cyu74Y")
URLS_SHEET_TABS = {"video": "Videos", "image": "Images"}
URL_HEADER = ["Tweet URL", "Source"]

if not AUTH_TOKEN or not CT0:
    print("ERROR: X_AUTH_TOKEN and X_CT0 must be set as secrets/env vars.", file=sys.stderr)
    sys.exit(1)

STATUS_RE = re.compile(r"(https://x\.com/[^/?#]+/status/\d+)")


def clean_url(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("/"):
        href = "https://x.com" + href
    m = STATUS_RE.match(href) or STATUS_RE.search(href)
    return m.group(1) if m else None


def resolve_targets() -> list[tuple[str, str]]:
    """Return list of (label, url) pairs to scrape, in order."""
    if not TARGET_USERNAMES_RAW:
        return [(TARGET_URL_FALLBACK, TARGET_URL_FALLBACK)]

    # split on commas and/or newlines
    raw_items = re.split(r"[,\n]+", TARGET_USERNAMES_RAW)
    targets = []
    for item in raw_items:
        item = item.strip()
        if not item:
            continue
        if item.startswith("http://") or item.startswith("https://"):
            url = item
            label = item
        else:
            username = item.lstrip("@")
            url = f"https://x.com/{username}"
            label = username
        targets.append((label, url))
    return targets


def human_delay() -> float:
    r = random.random()
    if r < 0.05:
        return random.uniform(1.0, 1.8)   # occasional longer pause (was 2.4-4.0)
    if r < 0.15:
        return random.uniform(0.4, 0.7)   # was 0.9-1.5
    if r < 0.55:
        return random.uniform(0.15, 0.28) # was 0.42-0.70
    if r < 0.82:
        return random.uniform(0.10, 0.20) # was 0.30-0.50
    return random.uniform(0.08, 0.15)     # was 0.30-0.45

def human_step(viewport_height: int) -> int:
    r = random.random()
    if r < 0.10:
        return int(viewport_height * random.uniform(0.5, 0.8))   # was 0.15-0.35
    if r < 0.45:
        return int(viewport_height * random.uniform(0.9, 1.3))   # was 0.40-0.70
    if r < 0.80:
        return int(viewport_height * random.uniform(1.3, 1.8))   # was 0.65-0.95
    return int(viewport_height * random.uniform(1.8, 2.4))       # was 1.0-1.4

async def classify_article(article):
    """Return dict(url, is_video, is_image) or None."""
    href = None
    time_link = await article.query_selector('a[href*="/status/"] time')
    if time_link:
        anchor = await time_link.evaluate_handle("el => el.closest('a')")
        href = await anchor.get_property("href")
        href = await href.json_value() if href else None
    if not href:
        anchor = await article.query_selector('a[href*="/status/"]')
        if anchor:
            href = await anchor.get_attribute("href")

    url = clean_url(href)
    if not url:
        return None

    is_video = any([
        await article.query_selector("video"),
        await article.query_selector('[data-testid="videoPlayer"]'),
        await article.query_selector('[data-testid="videoComponent"]'),
        await article.query_selector('[data-testid="videoPreview"]'),
        await article.query_selector('[data-testid="previewInterstitial"]'),
        await article.query_selector('[data-testid="card.layoutLarge.media"]'),
        await article.query_selector('[data-testid="card.layoutSmall.media"]'),
        await article.query_selector('[aria-label*="Embedded video"]'),
        await article.query_selector('[data-testid="playButton"]'),
        await article.query_selector('[data-testid="gif"]'),
    ])

    is_image = any([
        await article.query_selector('[data-testid="tweetPhoto"]'),
        await article.query_selector('img[src*="pbs.twimg.com/media"]'),
    ])

    return {"url": url, "is_video": is_video, "is_image": is_image}


async def scan_page(page, seen_videos: set, seen_images: set):
    new_videos, new_images = [], []
    articles = await page.query_selector_all('article[data-testid="tweet"]')
    for article in articles:
        try:
            r = await classify_article(article)
        except Exception:
            continue
        if not r:
            continue
        if r["is_video"] and r["url"] not in seen_videos:
            seen_videos.add(r["url"])
            new_videos.append(r["url"])
        if r["is_image"] and not r["is_video"] and r["url"] not in seen_images and r["url"] not in seen_videos:
            seen_images.add(r["url"])
            new_images.append(r["url"])
    return new_videos, new_images


async def scrape_one_target(page, label, url, seen_videos, seen_images, video_source, image_source):
    """Open a single target, scroll until MAX_URLS_PER_TARGET or stuck, then return."""
    print(f"\n=== Target: {label} ({url}) ===", flush=True)
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(1.0, 1.8))

    if "login" in page.url or "flow/login" in page.url:
        print(f"  [!] Not authenticated when loading {label} — cookies rejected or expired. "
              f"Skipping this target.", file=sys.stderr)
        return 0, 0

    found_this_target = 0
    stuck_ticks = 0
    tick = 0
    start_time = time.time()

    while found_this_target < MAX_URLS_PER_TARGET:
        tick += 1
        before_v, before_i = len(seen_videos), len(seen_images)
        nv, ni = await scan_page(page, seen_videos, seen_images)

        # tag newly-discovered URLs with which target they came from
        for u in nv:
            video_source.setdefault(u, label)
        for u in ni:
            image_source.setdefault(u, label)

        found_this_target += (len(seen_videos) - before_v) + (len(seen_images) - before_i)
        elapsed = time.time() - start_time

        if nv or ni:
            stuck_ticks = 0
        else:
            stuck_ticks += 1

        print(
            f"  [{label} | tick {tick:>3}] +{len(nv)}v +{len(ni)}i this scroll  "
            f"| target total {found_this_target}/{MAX_URLS_PER_TARGET}  "
            f"| idle {stuck_ticks}/{MAX_STUCK}  "
            f"| {elapsed:0.0f}s elapsed",
            flush=True,
        )

        if found_this_target >= MAX_URLS_PER_TARGET:
            print(f"  [✓] {label}: target of {MAX_URLS_PER_TARGET} reached.")
            break

        if stuck_ticks >= MAX_STUCK:
            print(f"  [!] {label}: {MAX_STUCK} scrolls with no new URLs — feed exhausted, moving on.")
            break

        viewport = page.viewport_size or {"height": 900}
        step = human_step(viewport["height"])
        await page.mouse.wheel(0, step)
        await asyncio.sleep(human_delay())

    return found_this_target, tick


async def run():
    targets = resolve_targets()
    print(f"Loaded {len(targets)} target(s): {[t[0] for t in targets]}", flush=True)

    seen_videos: set[str] = set()
    seen_images: set[str] = set()
    video_source: dict[str, str] = {}
    image_source: dict[str, str] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )

        cookies = [
            {"name": "auth_token", "value": AUTH_TOKEN, "domain": ".x.com", "path": "/", "httpOnly": True, "secure": True},
            {"name": "ct0", "value": CT0, "domain": ".x.com", "path": "/", "httpOnly": False, "secure": True},
        ]
        if TWID:
            cookies.append({"name": "twid", "value": TWID, "domain": ".x.com", "path": "/", "secure": True})
        await context.add_cookies(cookies)

        page = await context.new_page()

        for i, (label, url) in enumerate(targets):
            await scrape_one_target(page, label, url, seen_videos, seen_images, video_source, image_source)
            if i < len(targets) - 1:
                pause = random.uniform(*BETWEEN_TARGET_DELAY)
                print(f"  ...pausing {pause:0.1f}s before next target...", flush=True)
                await asyncio.sleep(pause)

        await browser.close()

    print(f"\nDone scraping all targets. {len(seen_videos)} video URL(s), "
          f"{len(seen_images)} image URL(s) total.")

    # ── Write to Google Sheets ───────────────────────────────────────────
    sheets_service = get_sheets_service()
    ensure_tab_exists(sheets_service, URLS_SHEET_ID, URLS_SHEET_TABS["video"], URL_HEADER)
    ensure_tab_exists(sheets_service, URLS_SHEET_ID, URLS_SHEET_TABS["image"], URL_HEADER)

    video_rows = [[u, video_source.get(u, "")] for u in sorted(seen_videos)]
    image_rows = [[u, image_source.get(u, "")] for u in sorted(seen_images)]

    replace_url_list(sheets_service, URLS_SHEET_ID, URLS_SHEET_TABS["video"], video_rows)
    replace_url_list(sheets_service, URLS_SHEET_ID, URLS_SHEET_TABS["image"], image_rows)

    print(f"📝 Replaced 'Videos' tab with {len(video_rows)} row(s).")
    print(f"📝 Replaced 'Images' tab with {len(image_rows)} row(s).")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(f"### Scrape complete\n\n"
                    f"- Targets: {', '.join(t[0] for t in targets)}\n"
                    f"- Videos found: {len(seen_videos)}\n"
                    f"- Images found: {len(seen_images)}\n"
                    f"- Written to sheet: {URLS_SHEET_ID}\n")


# ─────────────────── Google Sheets — write results ───────────────────────────

def _load_google_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    raw = os.environ.get("GDRIVE_TOKEN_JSON")
    if not raw:
        raise RuntimeError("Missing GDRIVE_TOKEN_JSON env var.")
    info = json.loads(raw)
    creds = Credentials(
        token=info.get("token"), refresh_token=info["refresh_token"],
        client_id=info["client_id"], client_secret=info["client_secret"],
        token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
        scopes=info.get("scopes", ["https://www.googleapis.com/auth/drive"]),
    )
    creds.refresh(Request())
    return creds


def get_sheets_service():
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=_load_google_creds(), cache_discovery=False)


def ensure_tab_exists(sheets_service, spreadsheet_id, tab_name, header):
    meta = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets(properties(title))"
    ).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab_name not in titles:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
        ).execute()
        print(f"➕ Created missing tab '{tab_name}'.")
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!A1",
        valueInputOption="RAW", body={"values": [header]}
    ).execute()


def replace_url_list(sheets_service, spreadsheet_id, tab_name, rows):
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!A2:B"
    ).execute()
    if not rows:
        return
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!A2",
        valueInputOption="RAW", body={"values": rows}
    ).execute()


if __name__ == "__main__":
    asyncio.run(run())
