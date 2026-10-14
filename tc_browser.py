"""🛍 Збір акцій ТЦ, брендів і секс-шопів справжнім браузером (Chromium): бачить акції, які сайти підвантажують скриптами,
і проходить більшість захистів. Запускається двічі на день (tc.yml) або командою адміна «🛍 Зібрати акції ТЦ зараз»."""
import json
import os
from datetime import datetime
from pathlib import Path

import check
from playwright.sync_api import sync_playwright

STATE = Path("tc_state.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def main() -> None:
    try:
        st = json.loads(STATE.read_text("utf-8"))
    except Exception:
        st = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="uk-UA", user_agent=UA, ignore_https_errors=True, viewport={"width": 1366, "height": 900})
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                            "window.chrome = {runtime: {}}; Object.defineProperty(navigator, 'languages', {get: () => ['uk-UA', 'uk', 'en']});")
        page = ctx.new_page()

        cache: dict = {}

        def get(url: str) -> str:
            if url in cache:                          # кожна сторінка — один раз за збір
                if isinstance(cache[url], Exception):
                    raise cache[url]
                return cache[url]
            try:
                r = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                try:                                  # гортаємо вниз — підвантажуються «ліниві» блоки акцій
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                if r is not None and r.status >= 400:
                    raise RuntimeError(f"HTTP {r.status}")
                cache[url] = page.content()
            except Exception as e:
                cache[url] = e
                raise
            return cache[url]

        check.tc_get = get
        report = check.tc_refresh(st, datetime.now(check.TZ), force=True)
        browser.close()
    STATE.write_text(json.dumps({k: st[k] for k in ("tc_first", "tc_cache", "tc_slot") if k in st}, ensure_ascii=False), "utf-8")
    chat = (os.environ.get("REQ_CHAT") or "").strip()
    if report and chat:
        check.send(chat, report)


if __name__ == "__main__":
    main()
