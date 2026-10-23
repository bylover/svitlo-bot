"""🔎 Hotline через браузер (Chrome на GitHub): пошук → картки моделей (мін. ціна, к-сть магазинів, оцінка) → топ у категоріях + проба сторінки пропозицій."""
import json
import re
import time
import urllib.parse
from datetime import datetime

import check
from check import TZ, err_text, http_json, _num

from playwright.sync_api import sync_playwright

# категорія: (назва, запит, дозволені сегменти URL категорії Hotline — щоб «вібратор» не став будівельним)
HL_CATS = [("🔞 Вібратори", "вібратор для жінок", r"intim|seks|sex|erot|zdorov"), ("🔞 Вакуумні стимулятори", "вакуумний стимулятор", r"intim|seks|sex|erot|zdorov"),
           ("🔞 Лубриканти", "лубрикант", r"intim|seks|sex|erot|zdorov|kosmet"), ("🔞 Презервативи", "презервативи", r"intim|seks|sex|erot|zdorov|kontrac"),
           ("🎧 Навушники", "бездротові навушники", r"naushn|headph|audio|av-"), ("🔋 Павербанки", "павербанк", r"power|zaryad|akkum|batar"),
           ("⚡ Зарядні станції", "зарядна станція", r"stanc|power|zaryad|generat|elektro"), ("⌚ Смарт-годинники", "смарт годинник", r"smart|chasy|godyn|watch"),
           ("🤖 Роботи-пилососи", "робот пилосос", r"pylesos|robot|vacuum"), ("☕ Кавоварки", "кавоварка", r"kof|kav|coffee"),
           ("🏋️ Гантелі", "гантелі розбірні", r"gant|sport|fitnes"), ("👟 Кросівки", "кросівки чоловічі", r"kross|obuv|vzutt|sneak")]
DBG = {"tries": []}
CARDS_JS = """() => [...document.querySelectorAll('.list-item')].map((el) => {
  const a = el.querySelector('a.list-item__title, a.list-item__img, a[href^="/ua/"]');
  const t = el.innerText || '';
  return { name: (el.querySelector('.list-item__title') || {}).innerText || (el.querySelector('img') || {}).alt || t.split('\\n')[0],
           href: a ? a.getAttribute('href') : '', text: t.slice(0, 400) };
})"""


def launch(pw):
    try:
        return pw.chromium.launch(headless=True, channel="chrome", args=["--disable-blink-features=AutomationControlled"])
    except Exception:
        return pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])


def open_page(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    for _ in range(8):
        page.wait_for_timeout(2000)
        if not re.search(r"зачекайте|just a moment|подождите|attention required", page.title() or "", re.I):
            break


def hl_search(page, q: str, cat_rx: str) -> list:
    rec = {"q": q}
    try:
        open_page(page, "https://hotline.ua/ua/sr/?q=" + urllib.parse.quote(q))
        cards = page.evaluate(CARDS_JS) or []
    except Exception as e:
        rec["err"] = err_text(e)[:100]; cards = []
    out = []
    for c in cards:
        href, text = c.get("href") or "", c.get("text") or ""
        seg = href.strip("/").split("/")[1] if href.count("/") >= 3 else ""
        if cat_rx and seg and not re.search(cat_rx, seg, re.I):
            continue                                       # не та категорія (напр. будівельні «вібратори»)
        pline = next((ln for ln in text.split("\n") if "₴" in ln or "грн" in ln), "")   # рядок із ціною: «1 990 – 4 750 ₴»
        prices = [_num(x) for x in re.findall(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})*", pline)]
        prices = [p for p in prices if p >= 10]
        if not prices or re.search(r"немає в наявності|очікується", text, re.I):
            continue
        offers = int((re.search(r"пропозиці\w*\s*\((\d+)\)", text) or [None, 1])[1])
        rv = _num((re.search(r"(\d+)\s*відгук", text) or [None, 0])[1])
        st = _num((re.search(r"(\d[.,]\d)\s*(?:з\s*5|★)", text) or [None, 0])[1])
        out.append({"n": str(c.get("name") or "").strip()[:140], "p": min(prices), "pmin": min(prices), "pmax": max(prices), "n_off": offers,
                    "u": "https://hotline.ua" + href if href.startswith("/") else href, "s": f"{offers} магазин(ів)", "st": st, "rv": rv, "grp": "all", "seg": seg})
    rec.update({"cards": len(cards), "kept": len(out), "segs": sorted({o["seg"] for o in out})[:6]})
    if len(DBG["tries"]) < 6:
        DBG["tries"].append(rec)
    return out


def main() -> None:
    now = datetime.now(TZ)
    res = {}
    with sync_playwright() as pw:
        br = launch(pw)
        page = br.new_context(locale="uk-UA", viewport={"width": 1366, "height": 900},
                              user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36").new_page()
        t0 = time.time()
        for title, q, rx in HL_CATS:
            if time.time() - t0 > 480:
                break
            offs = hl_search(page, q, rx)
            for o in offs:                                 # популярність: магазини, відгуки, оцінка
                o["pop"] = o["n_off"] * 3 + o["rv"] * 5 + o["st"] * 20
            offs.sort(key=lambda o: (-o["pop"], o["p"]))
            res[title] = {"all": offs[:15]}
        if any(v["all"] for v in res.values()):           # 🔬 проба сторінки пропозицій (для рейтингу магазинів і «усе в одного продавця»)
            first = next(v["all"][0] for v in res.values() if v["all"])
            try:
                open_page(page, first["u"].rstrip("/") + "/?tab=offers")
                DBG["offer"] = page.evaluate("""() => { const el = document.querySelector('.list__item, [class*="offer"], .shops-list__item, .price-list__item');
                  return el ? { cls: el.className, text: el.innerText.slice(0, 500), html: el.outerHTML.slice(0, 1800) } : { title: document.title, len: document.body.innerHTML.length }; }""")
            except Exception as e:
                DBG["offer"] = {"err": err_text(e)[:100]}
        br.close()
    n = sum(len(v["all"]) for v in res.values())
    print(f"Hotline: категорій {len(res)}, позицій {n} · {json.dumps(DBG['tries'][:3], ensure_ascii=False)[:500]}")
    key = urllib.parse.quote(check.SYNC_KEY)
    try:
        if n:
            http_json(f"{check.SYNC_URL}/filepush?key={key}", {"name": "hotline.json", "json": {"at": now.strftime("%d.%m %H:%M"), "cats": res}})
        http_json(f"{check.SYNC_URL}/filepush?key={key}", {"name": "rz_dbg.json", "json": {"at": now.strftime("%d.%m %H:%M"), "q": "Hotline",
                  "log": [json.dumps(t, ensure_ascii=False)[:300] for t in DBG["tries"]], "card": json.dumps(DBG.get("offer"), ensure_ascii=False)[:2500]}})
    except Exception as e:
        print(f"Hotline → Cloudflare: {err_text(e)[:60]}")


if __name__ == "__main__":
    main()
