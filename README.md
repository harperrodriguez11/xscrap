# X Video/Image URL Scraper (Playwright + GitHub Actions)

## Setup

1. **Rotate your X cookies first.** If you've ever pasted your `auth_token`/`ct0`
   anywhere outside your own machine, treat that session as burned: log out of
   all sessions in X Settings → Security → Sessions, then log back in to get
   fresh values.

2. Get three cookie values from your browser's dev tools (Application →
   Cookies → x.com) while logged in:
   - `auth_token`
   - `ct0`
   - `twid` (optional but recommended)

3. In your GitHub repo: **Settings → Secrets and variables → Actions → New
   repository secret**, add:
   - `X_AUTH_TOKEN`
   - `X_CT0`
   - `X_TWID`

   Do this even for a private repo — never put these in the workflow file or
   commit them anywhere.

4. Push this repo, then run it manually from the **Actions** tab
   (`X URL Scraper` → `Run workflow`), optionally overriding the target URL
   and max URL count.

5. Download `list.csv` from the workflow run's **Artifacts** section.

## Local run (no GitHub Actions)

```bash
pip install -r requirements.txt
playwright install chromium

export X_AUTH_TOKEN="..."
export X_CT0="..."
export X_TWID="..."
export X_HEADLESS=false   # watch it work

python scrape.py
```

## Known limitations / honesty check

- **This runs against X's Terms of Service**, which prohibit automated
  scraping. Using it is a judgment call on your part, not something I can
  vouch for as risk-free.
- GitHub-hosted runners use datacenter IPs. X's anti-automation systems
  flag datacenter-IP + cookie-replay sessions more readily than a real
  browser on a residential IP. Expect this to be less reliable on CI than
  running the equivalent Chrome extension locally, and possibly to trigger
  rate limits, checkpoint prompts, or session invalidation on the account.
- No method is "100% working" against a platform that actively updates its
  bot detection — that's true of every scraper for every major platform,
  not a limitation specific to this script.
- If you want something durable, running this from a self-hosted runner
  (your own machine/VPS, real residential IP) is meaningfully more reliable
  than `ubuntu-latest`.
