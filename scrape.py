#!/usr/bin/env python3
"""
X.com video/image URL scraper using Playwright.

Auth is done via session cookies (auth_token, ct0, twid) passed through
environment variables that map to GitHub Actions encrypted secrets.
NEVER commit cookies to the repo or hardcode them in this file.

Output: list.csv with columns [type, url]
"""

import asyncio
import csv
import os
import random
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

# ── Config (override via env vars / workflow inputs) ────────────────────────

TARGET_URL   = os.environ.get("X_TARGET_URL", "https://x.com/home")
MAX_URLS     = int(os.environ.get("X_MAX_URLS", "50"))
MAX_STUCK    = int(os.environ.get("X_MAX_STUCK_TICKS", "8"))
OUTPUT_FILE  = os.environ.get("X_OUTPUT_FILE", "list.csv")
HEADLESS     = os.environ.get("X_HEADLESS", "true").lower() != "false"

AUTH_TOKEN = os.environ.get("X_AUTH_TOKEN")
CT0        = os.environ.get("X_CT0")
TWID       = os.environ.get("X_TWID")

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


async def run():
    seen_videos: set[str] = set()
    seen_images: set[str] = set()

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
        await page.goto(TARGET_URL, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2.0, 3.5))

        # Verify we're actually logged in
        if "login" in page.url or "flow/login" in page.url:
            print("ERROR: not authenticated — cookies rejected or expired.", file=sys.stderr)
            await browser.close()
            sys.exit(2)

        last_height = 0
        stuck_ticks = 0
        total_found = 0

        while total_found < MAX_URLS:
            nv, ni = await scan_page(page, seen_videos, seen_images)
            total_found = len(seen_videos) + len(seen_images)
            if nv or ni:
                stuck_ticks = 0
                print(f"[+] +{len(nv)} video, +{len(ni)} image  (total {total_found}/{MAX_URLS})")

            if total_found >= MAX_URLS:
                break

            height = await page.evaluate("document.documentElement.scrollHeight")
            if height == last_height:
                stuck_ticks += 1
            else:
                stuck_ticks = 0
                last_height = height

            if stuck_ticks >= MAX_STUCK:
                print("[!] Page appears exhausted (no new content) — stopping.")
                break

            viewport = page.viewport_size or {"height": 900}
            step = human_step(viewport["height"])
            await page.mouse.wheel(0, step)
            await asyncio.sleep(human_delay())

        await browser.close()

    # ── Write CSV ─────────────────────────────────────────────────────────
    out_path = Path(OUTPUT_FILE)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["type", "url"])
        for u in seen_videos:
            writer.writerow(["video", u])
        for u in seen_images:
            writer.writerow(["image", u])

    print(f"\nDone. {len(seen_videos)} videos, {len(seen_images)} images -> {out_path}")


if __name__ == "__main__":
    asyncio.run(run())
