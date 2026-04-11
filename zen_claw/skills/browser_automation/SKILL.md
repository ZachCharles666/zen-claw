---
name: browser_automation
description: Automate web browsers for scraping, form filling, and UI testing using Playwright or web_fetch.
metadata: {"zen-claw":{"emoji":"🌐","scopes":["exec","network"],"requires":{"bins_optional":["playwright","npx"]}}}
---

# Browser Automation Skill

Automate browser interactions for scraping, testing, and data extraction. Use Playwright when available; fall back to `web_fetch` for simple page retrieval.

## Strategy Selection

| Task | Tool |
|------|------|
| Simple page fetch / API call | `web_fetch` |
| JavaScript-rendered content | Playwright (`exec`) |
| Form filling / clicking | Playwright (`exec`) |
| Screenshot / PDF capture | Playwright (`exec`) |
| Login-protected pages | Playwright (`exec`) |

## Playwright — Python

```bash
# Install (if needed)
pip install playwright && playwright install chromium

# One-shot page text extraction
python - <<'EOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("https://example.com")
    print(page.content())
    browser.close()
EOF

# Screenshot
python - <<'EOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("https://example.com")
    page.screenshot(path="screenshot.png")
    browser.close()
EOF

# Fill a form and submit
python - <<'EOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://example.com/login")
    page.fill("#username", "myuser")
    page.fill("#password", "mypassword")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    print(page.url)
    browser.close()
EOF
```

## Playwright — Node.js (npx)

```bash
# One-shot with npx (no install required)
npx playwright@latest chromium https://example.com --screenshot=screenshot.png
```

## web_fetch Fallback

For static pages or APIs, use `web_fetch` directly — no browser overhead:

```
web_fetch: GET https://example.com
web_fetch: GET https://api.example.com/data.json
```

## Guidelines

- Default to `headless=True` for all Playwright runs.
- Do not store credentials in scripts; accept them as environment variables.
- For scraping, respect `robots.txt` and rate-limit requests.
- If Playwright is not installed, fall back to `web_fetch` and inform the user about the limitation.
- Use `wait_for_load_state("networkidle")` after navigation on JS-heavy pages.
