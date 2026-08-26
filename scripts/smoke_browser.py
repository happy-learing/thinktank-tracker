from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main(url: str, screenshot_path: str) -> None:
    target = Path(screenshot_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720}, locale="zh-CN")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        heading = page.locator("h1").first.inner_text()
        page.screenshot(path=target, full_page=True)
        browser.close()
    if "研究追踪工作台" not in heading:
        raise RuntimeError(f"Unexpected dashboard heading: {heading}")
    if target.stat().st_size < 5_000:
        raise RuntimeError("Browser screenshot is unexpectedly small or blank")
    print(f"Bundled Chromium passed: {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
