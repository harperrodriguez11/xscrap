#!/usr/bin/env python3
"""
X.com video/image URL scraper using Playwright.

Auth is done via session cookies (auth_token, ct0, twid) passed through
environment variables that map to GitHub Actions encrypted secrets.
NEVER commit cookies to the repo or hardcode them in this file.

Instead of a CSV artifact, results are written straight into the same
Google Sheet the download workflow reads from:
    - all video tweet URLs -> "Videos" tab
    - all image tweet URLs -> "Images" tab
Each run REPLACES the previous list in those tabs with the freshly
scraped one (per your request — not appended).

Supports scraping several usernames in one run, concurrently (separate
browser contexts sharing one browser instance, same login cookies), with
results deduplicated and merged across all of them before writing.
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

TARGET_URL   = os.environ.get("X_TARGET_URL", "https://x.com/home")
USERNAMES    = os.environ.get("X_USERNAMES", "").strip()
CONCURRENCY  = int(os.environ.get("X_CONCURRENCY", "3"))
MAX_URLS     = int(os.environ.get("X_MAX_URLS", "50"))          # per target
MAX_STUCK    = int(os.environ.get("X_MAX_STUCK_TICKS", "8"))
HEADLESS     = os.environ.get("X_HEADLESS", "true").lower() != "false"

AUTH_TOKEN = os.environ.get("X_AUTH_TOKEN")
CT0        = os.environ.get("X_CT0")
TWID       = os.environ.get("X_TWID")

# Same sheet the download workflow reads from — "Videos" / "Images" tabs.
# https://docs.google.com/spreadsheets/d/17PZy32Hmr504A7gVkGoH0OMJSQPd1oZVtaVp6Cyu74Y/edit
URLS_SHEET_ID = os.environ.get("X_URLS_SHEET_ID", "17PZy32Hmr504A7gVkGoH0OMJSQPd1oZVtaVp6Cyu74Y")
URLS_SHEET_TABS = {"video": "Videos", "image": "Images"}
URL_HEADER = ["Tweet URL"]

if not AUTH_TOKEN or not CT0:
    print("ERROR: X_AUTH_TOKEN and X_CT0 must be set as secrets/env vars.", file=sys.stderr)
    sys.exit(1)

STATUS_RE = re.compile(r"(https://x\.com/[^/?#]+/status/\d+)")


def clean_url(href):
    if not href:
        return None
    if href.startswith("/"):
        href = "https://x.com" + href
    m = STATUS_RE.match(href) or STATUS_RE.search(href)
    return m.group(1) if m else None


def build_targets():
    """Returns the list of page URLs to scrape. If usernames were given,
    each becomes its own profile-page target; otherwise falls back to the
    single TARGET_URL (home timeline, a search, etc.)."""
    if USERNAMES:
        raw = re.split(r'[,\n\s]+', USERNAMES)
        names = [n.strip().lstrip('@') for n in raw if n.strip()]
        seen, targets = set(), []
        for n in names:
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            targets.append((n, f"https://x.com/{n}"))
        return targets
    return [(None, TARGET_URL)]


def human_delay() -> float:
    r = random.random()
    if r < 0.08:
        return random.uniform(2.4, 4.0)
    if r < 0.20:
        return random.uniform(0.9, 1.5)
    if r < 0.55:
        return random.uniform(0.42, 0.70)
    if r < 0.82:
        return random.uniform(0.30, 0.50)
    return random.uniform(0.30, 0.45)


def human_step(viewport_height: int) -> int:
    r = random.random()
    if r < 0.12:
        return int(viewport_height * random.uniform(0.15, 0.35))
    if r < 0.50:
        return int(viewport_height * random.uniform(0.40, 0.70))
    if r < 0.82:
        return int(viewport_height * random.uniform(0.65, 0.95))
    return int(viewport_height * random.uniform(1.0, 1.4))


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


async def scrape_one_target(browser, label, target_url, seen_videos, seen_images, merge_lock):
    """Scrapes a single page (profile/home/search) in its own context, then
    merges any URLs it found into the shared, deduped result sets."""
    local_videos, local_images = set(), set()

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

    tag = f"[{label or target_url}]"
    try:
        page = await context.new_page()
        await page.goto(target_url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2.0, 3.5))

        if "login" in page.url or "flow/login" in page.url:
            print(f"{tag} ERROR: not authenticated — cookies rejected or expired.", file=sys.stderr)
            return

        stuck_ticks = 0
        total_found = 0
        tick = 0
        start_time = time.time()

        while total_found < MAX_URLS:
            tick += 1
            nv, ni = await scan_page(page, local_videos, local_images)
            total_found = len(local_videos) + len(local_images)
            elapsed = time.time() - start_time

            if nv or ni:
                stuck_ticks = 0
            else:
                stuck_ticks += 1

            print(
                f"{tag} [tick {tick:>3}] +{len(nv)}v +{len(ni)}i this scroll  "
                f"| total {total_found}/{MAX_URLS}  "
                f"| idle {stuck_ticks}/{MAX_STUCK}  "
                f"| {elapsed:0.0f}s elapsed",
                flush=True,
            )

            if total_found >= MAX_URLS:
                print(f"{tag} [✓] Target of {MAX_URLS} reached.")
                break

            if stuck_ticks >= MAX_STUCK:
                print(f"{tag} [!] {MAX_STUCK} scrolls with no new URLs — feed exhausted, moving on.")
                break

            viewport = page.viewport_size or {"height": 900}
            step = human_step(viewport["height"])
            await page.mouse.wheel(0, step)
            await asyncio.sleep(human_delay())

    finally:
        await context.close()

    async with merge_lock:
        new_v = local_videos - seen_videos
        new_i = (local_images - local_videos) - seen_images
        seen_videos.update(new_v)
        seen_images.update(new_i)
    print(f"{tag} done — {len(local_videos)} video(s), {len(local_images)} image(s) found.")


async def scrape_all(targets):
    seen_videos: set = set()
    seen_images: set = set()
    merge_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, CONCURRENCY))

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )

        async def bound(label, url):
            async with sem:
                await scrape_one_target(browser, label, url, seen_videos, seen_images, merge_lock)

        await asyncio.gather(*[bound(label, url) for label, url in targets])
        await browser.close()

    return seen_videos, seen_images


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

def replace_url_list(sheets_service, spreadsheet_id, tab_name, urls):
    """Wipes everything below the header row, then writes the fresh list —
    this run's results fully replace whatever was there before."""
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!A2:A"
    ).execute()
    if not urls:
        return
    rows = [[u] for u in urls]
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!A2",
        valueInputOption="RAW", body={"values": rows}
    ).execute()


def main():
    targets = build_targets()
    label_desc = ", ".join(l or u for l, u in targets)
    print(f"🚀 Scraping {len(targets)} target(s) with concurrency={CONCURRENCY}: {label_desc}\n")

    seen_videos, seen_images = asyncio.run(scrape_all(targets))

    print(f"\nDone scraping. {len(seen_videos)} video URL(s), {len(seen_images)} image URL(s) total (deduped across all targets).")

    sheets_service = get_sheets_service()
    ensure_tab_exists(sheets_service, URLS_SHEET_ID, URLS_SHEET_TABS["video"], URL_HEADER)
    ensure_tab_exists(sheets_service, URLS_SHEET_ID, URLS_SHEET_TABS["image"], URL_HEADER)

    replace_url_list(sheets_service, URLS_SHEET_ID, URLS_SHEET_TABS["video"], sorted(seen_videos))
    replace_url_list(sheets_service, URLS_SHEET_ID, URLS_SHEET_TABS["image"], sorted(seen_images))

    print(f"📝 Replaced 'Videos' tab with {len(seen_videos)} URL(s).")
    print(f"📝 Replaced 'Images' tab with {len(seen_images)} URL(s).")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(f"### Scrape complete\n\n"
                    f"- Targets: {label_desc}\n"
                    f"- Videos found: {len(seen_videos)}\n"
                    f"- Images found: {len(seen_images)}\n"
                    f"- Written to sheet: {URLS_SHEET_ID}\n")


if __name__ == "__main__":
    main()
