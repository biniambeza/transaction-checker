"""Opens a bank receipt link in a hidden browser (like a phone would) and reads the receipt.

Needed for receipt pages that load their data with JavaScript, like CBE's new QR links
(https://mbreciept.cbe.com.et/v2-...). Uses Playwright + Chromium:
    pip install playwright
    python -m playwright install chromium
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import config

log = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

DEBUG_DIR = config.DATA_DIR / "debug"
LOADING_MARKERS = ("loading receipt", "loading...", "loading…")
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36")

_pw = None
_browser = None
_lock = asyncio.Lock()


@dataclass
class PageResult:
    url: str
    text: str = ""                                   # what is shown on the page
    json_payloads: list = field(default_factory=list)  # data the page downloaded
    pdfs: list = field(default_factory=list)           # PDF bytes the page downloaded

    def all_text(self) -> str:
        parts = [self.text]
        for p in self.json_payloads:
            parts.append(json.dumps(p.get("data"), ensure_ascii=False))
        return "\n".join(parts)


async def _get_browser():
    global _pw, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(headless=True, args=["--ignore-certificate-errors"])
    return _browser


async def open_page(url: str) -> PageResult:
    """Loads the page, waits for the receipt to appear, returns its text and any JSON/PDF it fetched."""
    if not AVAILABLE:
        raise RuntimeError("Playwright is not installed")
    timeout_ms = config.BANK_TIMEOUT * 1000
    browser = await _get_browser()
    context = await browser.new_context(ignore_https_errors=True, user_agent=MOBILE_UA,
                                        viewport={"width": 420, "height": 900})
    result = PageResult(url=url)
    pending: list[asyncio.Task] = []

    async def grab(response):
        ctype = (response.headers or {}).get("content-type", "").lower()
        try:
            if "json" in ctype:
                result.json_payloads.append({"url": response.url, "data": await response.json()})
            elif "pdf" in ctype:
                result.pdfs.append(await response.body())
        except Exception:  # noqa: BLE001 - body may be gone for redirects
            pass

    try:
        page = await context.new_page()
        page.on("response", lambda resp: pending.append(asyncio.ensure_future(grab(resp))))
        try:
            await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        except Exception as e:  # noqa: BLE001 - keep whatever loaded
            log.info("Receipt page did not settle: %s", e)
        try:
            await page.wait_for_function(
                "m => !m.some(x => document.body.innerText.toLowerCase().includes(x))",
                arg=list(LOADING_MARKERS), timeout=timeout_ms)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(500)
        result.text = await page.inner_text("body")
        for frame in page.frames[1:]:  # receipts are sometimes shown in an iframe
            try:
                result.text += "\n" + await frame.inner_text("body")
            except Exception:  # noqa: BLE001
                pass
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await context.close()
    _save_debug(result)
    return result


def _save_debug(result: PageResult) -> None:
    """Keeps a copy of what the bank page showed — useful for tuning the reader."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", result.url.rstrip("/").rsplit("/", 1)[-1])[:60] or "page"
        with open(DEBUG_DIR / f"{name}.txt", "w", encoding="utf-8") as f:
            f.write(f"URL: {result.url}\n\n=== PAGE TEXT ===\n{result.text}\n")
            for p in result.json_payloads:
                f.write(f"\n=== JSON from {p['url']} ===\n{json.dumps(p['data'], ensure_ascii=False, indent=2)[:20000]}\n")
            f.write(f"\n=== PDFs downloaded: {len(result.pdfs)} ===\n")
    except OSError as e:
        log.debug("Could not save debug page: %s", e)


async def close() -> None:
    global _pw, _browser
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()
    _pw = _browser = None
