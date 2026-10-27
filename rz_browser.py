"""🔎 Hotline через браузер (Chrome на GitHub): топ у всіх категоріях + «усе в одного магазину» (режим за запитом HL_Q)."""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

import check
from check import PROM_CATS, TZ, err_text, http_json, _num, _tok, q_variants, mk_compact

from playwright.sync_api import sync_playwright

ADULT_RX = r"adult|intim|seks|sex|erot|zdorov"           # для 🔞 — лише розділи для дорослих (щоб «вібратор» не став будівельним)
DBG = {"tries": []}
HL_ALT = {"вібратор": ["вібратор для жінок", "вибратор", "вібратор кролик"], "павербанк": ["power bank", "повербанк", "зовнішній акумулятор"],
          "бездротова мишка": ["миша бездротова", "миша комп'ютерна", "мишка"], "шуруповерт акумуляторний": ["шуруповерт", "дриль-шуруповерт"],
          "вакуумний стимулятор клітора": ["вакуумний стимулятор", "satisfyer"], "мастурбатор": ["мастурбатор чоловічий"], "анальна пробка": ["анальна пробка силікон"],
          "ерекційне кільце": ["ерекційне кільце вібро"], "бдсм набір": ["бдсм"], "еротична білизна": ["еротичний комплект"]}
CARDS_JS = """() => [...document.querySelectorAll('.list-item, .catalog-item, [class*="product-item"]')].map((el) => {
  const a = el.querySelector('a.list-item__title, a.list-item__img, a[href^="/ua/"]');
  const t = el.innerText || '';
  return { name: (el.querySelector('.list-item__title') || {}).innerText || (el.querySelector('img') || {}).alt || t.split('\\n')[0], href: a ? a.getAttribute('href') : '', text: t.slice(0, 400) };
})"""
OFFERS_JS = """() => {                                       // пропозиції магазинів: посилання /go/price/…, назва магазину, ціна в рядку
  const seen = new Set(), out = [];
  document.querySelectorAll('a[href^="/go/price/"]').forEach((a) => {
    const href = a.getAttribute('href'); if (seen.has(href)) return;
    let row = a; for (let i = 0; i < 8 && row.parentElement; i++) { row = row.parentElement; const ids = new Set([...row.querySelectorAll('a[href^="/go/price/"]')].map((x) => x.getAttribute('href'))); if (ids.size > 1) { row = null; break; } if (/₴/.test(row.innerText || '')) break; }
    if (!row) return;
    const shop = [...row.querySelectorAll('a[href^="/go/price/"]')].map((x) => (x.innerText || '').trim()).find((t) => t && !/₴|купити|магазин/i.test(t)) || (row.querySelector('img[alt]') || {}).alt || '';
    const m = (row.innerText || '').match(/(\\d{1,3}(?:[ \\u00a0\\u202f]\\d{3})*)\\s*₴/);
    if (!m || !shop) return; seen.add(href);
    out.push({ shop: shop.replace(/^Логотип інтернет-магазина\\s*/i, '').trim(), price: m[1], href: 'https://hotline.ua' + href, text: (row.innerText || '').slice(0, 200) });
  });
  return out;
}"""


def launch(pw):
    try:
        return pw.chromium.launch(headless=True, channel="chrome", args=["--disable-blink-features=AutomationControlled"])
    except Exception:
        return pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])


def open_page(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    for _ in range(8):
        page.wait_for_timeout(1800)
        if not re.search(r"зачекайте|just a moment|подождите|attention required", page.title() or "", re.I):
            break


def hl_search(page, q: str, cat_rx: str = "", pages: int = 2) -> list:
    rec = {"q": q}
    cards = []
    for qv in q_variants(q) + HL_ALT.get(q, []):          # варіанти запиту + запасні формулювання (якщо Hotline відкриває сторінку категорії)
        for pg in range(1, pages + 1):
            try:
                open_page(page, "https://hotline.ua/ua/sr/?q=" + urllib.parse.quote(qv) + (f"&p={pg - 1}" if pg > 1 else ""))
                got = page.evaluate(CARDS_JS) or []
            except Exception as e:
                rec["err"] = err_text(e)[:100]; got = []
            if not got:
                break
            cards += got
        if cards and (not cat_rx or any(re.search(cat_rx, (c.get("href") or ""), re.I) for c in cards)):
            rec["qv"] = qv; break
        rec.setdefault("urls", []).append((page.url or "")[:90])
        cards = []
    out, seen_h = [], set()
    for c in cards:
        href, text = c.get("href") or "", c.get("text") or ""
        if href in seen_h:
            continue                                       # без дублів (сторінки пошуку можуть повторювати моделі)
        seen_h.add(href)
        seg = href.strip("/").split("/")[1] if href.count("/") >= 3 else ""
        if cat_rx and seg and not re.search(cat_rx, seg, re.I):
            continue
        pline = next((ln for ln in text.split("\n") if "₴" in ln or "грн" in ln), "")
        prices = [p for p in (_num(x) for x in re.findall(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})*", pline)) if p >= 10]
        if not prices or re.search(r"немає в наявності|очікується", text, re.I):
            continue
        offers = int((re.search(r"пропозиці\w*\s*\((\d+)\)", text) or [None, 1])[1])
        out.append({"n": str(c.get("name") or "").strip()[:140], "p": min(prices), "pmin": min(prices), "pmax": max(prices), "n_off": offers,
                    "u": "https://hotline.ua" + href if href.startswith("/") else href, "st": _num((re.search(r"(\d[.,]\d)\s*(?:з\s*5|★)", text) or [None, 0])[1]),
                    "rv": _num((re.search(r"(\d+)\s*відгук", text) or [None, 0])[1]), "grp": "all", "seg": seg})
    rec.update({"cards": len(cards), "kept": len(out)})
    if len(DBG["tries"]) < 40:
        DBG["tries"].append(rec)
    return out


def tg_send(chat: str, text: str) -> None:
    data = json.dumps({"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/sendMessage", data=data, headers={"content-type": "application/json"}), timeout=30)


def cart(page, text: str) -> str:
    """🧺 Усе в одного магазину: для кожної позиції — найрелевантніша модель на Hotline → пропозиції магазинів → магазини, в яких є все, з найменшою сумою."""
    items = [x.strip() for x in re.split(r"[,;\n]", text) if x.strip()][:6]
    by_shop, cheapest, found = {}, {}, {}
    for it in items:
        tk = _tok(it)
        models = [m for m in hl_search(page, it, pages=1) if _tok(m["n"]) & tk]
        models.sort(key=lambda m: (-len(_tok(m["n"]) & tk), -m["n_off"]))
        best_ov = len(_tok(models[0]["n"]) & tk) if models else 0
        models = [m for m in models if len(_tok(m["n"]) & tk) >= max(1, best_ov - 1)][:2]   # до 2 підхожих моделей (інші версії, кольори) — швидше
        if not models:
            continue
        found[it] = models[0]["n"]
        for mdl in models:
            try:
                open_page(page, mdl["u"].rstrip("/") + "/?tab=offers")
                offs = page.evaluate(OFFERS_JS) or []
            except Exception:
                offs = []
            for o in offs:
                p = _num(re.sub(r"[^\d]", "", o["price"]))
                if not p:
                    continue
                cur = by_shop.setdefault(o["shop"], {})
                if it not in cur or p < cur[it]["p"]:
                    cur[it] = {"p": p, "u": o["href"], "n": mdl["n"]}
                if it not in cheapest or p < cheapest[it]:
                    cheapest[it] = p
    full = sorted([(s, v) for s, v in by_shop.items() if len(v) == len(items)], key=lambda sv: sum(x["p"] for x in sv[1].values()))
    sep = sum(cheapest.values())
    if not full:
        miss = [it for it in items if it not in found]
        return ("🧺 <b>Hotline: магазину, в якого є все, не знайшов.</b>" + (f"\nНе знайшов на Hotline: {', '.join(miss)}" if miss else "") +
                (f"\nНайдешевше окремо: {sep:.0f} ₴" if cheapest else "") + "\n<i>Спробуйте точніші назви (бренд + модель).</i>")
    out = [f"🧺 <b>Hotline · усе в одного магазину</b> · {len(items)} позицій\n"]
    for i, (s, v) in enumerate(full[:5], 1):
        out.append(f"{i}. <b>{s}</b> · <b>{sum(x['p'] for x in v.values()):.0f} ₴</b>\n" + "\n".join(f"   • <a href=\"{x['u']}\">{x['n'][:60]}</a> — {x['p']:.0f} ₴" for x in v.values()))
    out.append(f"\n<i>Найдешевше окремо в різних магазинах — {sep:.0f} ₴.</i>")
    return "\n".join(out)


def main() -> None:
    now = datetime.now(TZ)
    q_, chat_ = os.environ.get("HL_Q", "").strip(), os.environ.get("HL_CHAT", "").strip()
    with sync_playwright() as pw:
        br = launch(pw)
        page = br.new_context(locale="uk-UA", viewport={"width": 1366, "height": 900},
                              user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36").new_page()
        if q_ and chat_:                                   # 🧺 режим «усе в одного магазину» — за запитом із бота
            try:
                tg_send(chat_, cart(page, q_))
            except Exception as e:
                tg_send(chat_, f"🧺 Hotline: не вдалося перевірити ({err_text(e)[:60]}). Спробуйте пізніше.")
            br.close(); return
        res, t0 = {}, time.time()
        for title, q in PROM_CATS:                         # 🏆 топ у всіх категоріях (спільний список із Prom)
            if time.time() - t0 > 960:
                break
            offs = hl_search(page, q, ADULT_RX if title.startswith("🔞") else "", pages=1)
            for o in offs:
                o["pop"] = o["n_off"] * 3 + o["rv"] * 5 + o["st"] * 20
                o["q"] = bool(o["st"] >= 4.5 and o["rv"] >= 20)   # ✅ перевірені покупцями
            offs.sort(key=lambda o: (not o["q"], -o["pop"], o["p"]))
            res[title] = {"all": offs[:40]}
        br.close()
    n = sum(len(v["all"]) for v in res.values())
    print(f"Hotline: категорій {len(res)}, з товарами {sum(1 for v in res.values() if v['all'])}, позицій {n}")
    key = urllib.parse.quote(check.SYNC_KEY)
    try:
        if n:
            payload = {"at": now.strftime("%d.%m %H:%M"), "cats": {k: {"all": mk_compact(v["all"])} for k, v in res.items()}}
            http_json(f"{check.SYNC_URL}/filepush?key={key}&name=hotline.json", payload)   # як є, без розбору на боці бота
            print(f"Hotline → бот: {len(json.dumps(payload, ensure_ascii=False).encode()) // 1024} КБ")
        http_json(f"{check.SYNC_URL}/filepush?key={key}", {"name": "rz_dbg.json", "json": {"at": now.strftime("%d.%m %H:%M"), "q": "Hotline",
                  "log": [f"категорій з товарами: {sum(1 for v in res.values() if v['all'])} з {len(res)}"] + [json.dumps(t, ensure_ascii=False)[:160] for t in DBG["tries"] if not t.get("kept")][:12]}})
    except Exception as e:
        print(f"Hotline → Cloudflare: {err_text(e)[:60]}")


if __name__ == "__main__":
    main()
