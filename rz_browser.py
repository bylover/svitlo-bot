"""🟢 Rozetka через браузер (Chrome на GitHub): пошук → плитки товарів і стан сторінки → топ у категоріях + діагностика для адмінки."""
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime

import check
from check import PROM_CATS, TZ, err_text, http_json, _tok, _num

from playwright.sync_api import sync_playwright

DBG = {"tries": []}
TILE_JS = """() => {
  const out = [];
  document.querySelectorAll('rz-catalog-tile, .goods-tile, [data-testid="catalog-tile"], li.catalog-grid__cell').forEach((t) => {
    const a = t.querySelector('a.goods-tile__heading, a.tile-title, a[href*="/p"]');
    const title = (t.querySelector('.goods-tile__title, .tile-title, [data-testid="title"]') || a || {}).textContent || '';
    const price = (t.querySelector('.goods-tile__price-value, .price, [data-testid="price"]') || {}).textContent || '';
    const avail = (t.querySelector('.goods-tile__availability, [data-testid="availability"]') || {}).textContent || '';
    const rev = (t.querySelector('.goods-tile__reviews-link, .review-count, [data-testid="reviews"]') || {}).textContent || '';
    const stars = t.querySelector('.goods-tile__stars svg, .stars__rating, [data-testid="stars"]');
    const st = stars ? (stars.getAttribute('aria-label') || stars.getAttribute('style') || '') : '';
    const seller = (t.querySelector('.goods-tile__seller, [data-testid="seller"]') || {}).textContent || '';
    out.push({ title: title.trim(), price: price.trim(), avail: avail.trim(), rev: rev.trim(), st, seller: seller.trim(), href: a ? a.href : '' });
  });
  return out;
}"""


def state_goods(state) -> list:
    """Товари з даних сторінки Rozetka (rz-client-state): обʼєкти з назвою й ціною."""
    out, seen = [], set()
    def walk(d, depth=0):
        if depth > 14:
            return
        if isinstance(d, dict):
            if d.get("title") and (d.get("price") or d.get("price_pcs")) and (d.get("href") or d.get("id")) and str(d.get("id")) not in seen:
                seen.add(str(d.get("id"))); out.append(d)
            for v in d.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
                elif isinstance(v, str) and len(v) > 300 and v.lstrip()[:1] in "{[":
                    try:
                        walk(json.loads(v), depth + 1)
                    except Exception:
                        pass
        elif isinstance(d, list):
            for v in d:
                walk(v, depth + 1)
    walk(state)
    return out


def launch(pw):
    try:
        return pw.chromium.launch(headless=True, channel="chrome", args=["--disable-blink-features=AutomationControlled"])
    except Exception:
        return pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])


CAUGHT: list = []                                       # JSON-відповіді, які отримав сам браузер (пошук і картки товарів)


def on_response(resp):
    try:
        u = resp.url
        if resp.request.resource_type in ("xhr", "fetch") and re.search(r"search\.rozetka|xl-catalog-api|goods/get|catalog-api|search/api", u):
            CAUGHT.append({"u": u[:160], "j": resp.json()})
    except Exception:
        pass


def search(page, q: str) -> list:
    url = "https://rozetka.com.ua/ua/search/?" + urllib.parse.urlencode({"text": q})
    rec = {"q": q}
    CAUGHT.clear()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        for _ in range(12):                                # «Трохи зачекайте…» — перевірка захисту, чекаємо до ~30 с
            page.wait_for_timeout(2500)
            if not re.search(r"зачекайте|just a moment|подождите", page.title() or "", re.I):
                break
        try:
            page.wait_for_selector("rz-catalog-tile, .goods-tile, [data-testid='catalog-tile']", timeout=12000)
        except Exception:
            pass
        html = page.content()
        rec.update({"title": page.title()[:80], "len": len(html), "challenge": bool(re.search(r"Just a moment|cf-challenge|Attention Required|captcha", html, re.I))})
        tiles = page.evaluate(TILE_JS) or []
        rec["tiles"] = len(tiles)
        if tiles:
            rec["tile0"] = tiles[0]
        st = re.search(r'<script[^>]+id="rz-client-state"[^>]*>(.*?)</script>', html, re.S)
        rec["state"] = len(st.group(1)) if st else 0
        page.wait_for_timeout(3000)
        rec["xhr"] = [c["u"] for c in CAUGHT][:4]
        if not tiles:                                      # товари з відповідей, які браузер отримав сам
            goods = []
            for c in CAUGHT:
                goods += state_goods(c["j"])
            rec["xhr_goods"] = len(goods)
            if goods and "good0" not in rec:
                rec["good0"] = {k: goods[0].get(k) for k in list(goods[0])[:25]}
            for g in goods:
                sel = g.get("seller") or {}
                tiles.append({"title": str(g.get("title") or ""), "price": str(g.get("price") or g.get("price_pcs") or ""),
                              "avail": "" if str(g.get("sell_status") or "available") in ("available", "limited") else "немає",
                              "rev": str(g.get("comments_amount") or ""), "st": str(g.get("comments_mark") or ""),
                              "seller": str((sel.get("title") if isinstance(sel, dict) else "") or ("Rozetka" if str(g.get("seller_id")) == "5" else "")),
                              "href": g.get("href") or f"https://rozetka.com.ua/ua/{g.get('id')}/p{g.get('id')}/"})
        if st and not tiles:                               # товари з даних сторінки
            try:
                raw = st.group(1).replace("&q;", '"').replace("&a;", "&").replace("&s;", "'").replace("&l;", "<").replace("&g;", ">")
                goods = state_goods(json.loads(raw))
            except Exception as e:
                goods = []; rec["state_err"] = err_text(e)[:80]
            rec["state_goods"] = len(goods)
            if goods:
                rec["good0"] = {k: goods[0].get(k) for k in list(goods[0])[:25]}
            for g in goods:
                sel = g.get("seller") or {}
                tiles.append({"title": str(g.get("title") or ""), "price": str(g.get("price") or g.get("price_pcs") or ""),
                              "avail": "" if str(g.get("sell_status") or "available") in ("available", "limited") else "немає",
                              "rev": str(g.get("comments_amount") or ""), "st": str(g.get("comments_mark") or g.get("rating") or ""),
                              "seller": str((sel.get("title") if isinstance(sel, dict) else "") or g.get("seller_title") or ("Rozetka" if str(g.get("seller_id")) == "5" else "")),
                              "href": g.get("href") or f"https://rozetka.com.ua/ua/{g.get('id')}/p{g.get('id')}/"})
    except Exception as e:
        rec["err"] = err_text(e)[:120]; tiles = []
    if len(DBG["tries"]) < 6:
        DBG["tries"].append(rec)
    out = []
    for t in tiles:
        price = _num(re.sub(r"[^\d]", "", t.get("price") or ""))
        if not t.get("title") or not price or re.search(r"немає|закінч|очікується|під замовл", t.get("avail") or "", re.I):
            continue                                      # лише в наявності
        sel = t.get("seller") or ""
        grp = "eu" if re.search(r"\beu\b|європ", sel, re.I) else "other" if sel and not re.search(r"rozetka", sel, re.I) else "rz"
        out.append({"n": t["title"][:140], "p": price, "u": t.get("href") or "", "s": sel or "Rozetka", "sid": sel or "Rozetka", "grp": grp, "r": 100,
                    "rv": _num(re.sub(r"[^\d]", "", t.get("rev") or "")), "st": _num((re.search(r"([\d.,]+)", t.get("st") or "") or [None, 0])[1]), "sold": 0})
    return out


def main() -> None:
    now = datetime.now(TZ)
    res = {}
    with sync_playwright() as pw:
        br = launch(pw)
        ctx = br.new_context(locale="uk-UA", viewport={"width": 1366, "height": 900},
                             user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36")
        page = ctx.new_page()
        page.on("response", on_response)
        try:                                               # спершу головна — пройти перевірку «Трохи зачекайте…» й отримати cookies
            page.goto("https://rozetka.com.ua/ua/", wait_until="domcontentloaded", timeout=45000)
            for _ in range(15):
                page.wait_for_timeout(2500)
                if not re.search(r"зачекайте|just a moment|подождите", page.title() or "", re.I):
                    break
            DBG["home"] = page.title()[:60]
        except Exception as e:
            DBG["home"] = "помилка: " + err_text(e)[:60]
        t0 = time.time()
        for title, q in PROM_CATS:
            if time.time() - t0 > 420:
                break
            offers = search(page, q)
            cl_ = []
            for o in offers:
                tk = _tok(o["n"])
                c = next((c for c in cl_ if len(tk & c["tk"]) >= 0.6 * max(1, min(len(tk), len(c["tk"])))), None)
                (c["o"].append(o) if c else cl_.append({"tk": tk, "o": [o]}))
            tops = {"all": [], "rz": [], "eu": [], "other": []}
            for c in cl_:
                meta = {"pop": sum(o["rv"] for o in c["o"]) * 5 + 20 * max(o["st"] for o in c["o"]), "n_off": len(c["o"]),
                        "pmin": min(o["p"] for o in c["o"]), "pmax": max(o["p"] for o in c["o"])}
                tops["all"].append({**min(c["o"], key=lambda o: o["p"]), **meta})
                for g in ("rz", "eu", "other"):
                    gg = [o for o in c["o"] if o["grp"] == g]
                    if gg:
                        tops[g].append({**min(gg, key=lambda o: o["p"]), **meta})
            for g in tops:
                tops[g].sort(key=lambda x: (-x["pop"], x["p"])); tops[g] = tops[g][:15]
            res[title] = tops
            if not offers and len(DBG["tries"]) >= 2 and all(not t.get("tiles") for t in DBG["tries"]):
                break                                         # сторінки не віддають товари — не марнуємо час
        br.close()
    n = sum(len(v["all"]) for v in res.values())
    print(f"Rozetka (браузер): категорій {len(res)}, позицій {n} · {json.dumps(DBG['tries'][:2], ensure_ascii=False)[:600]}")
    try:
        key = urllib.parse.quote(check.SYNC_KEY)
        if n:
            http_json(f"{check.SYNC_URL}/filepush?key={key}", {"name": "rozetka.json", "json": {"at": now.strftime("%d.%m %H:%M"), "cats": res}})
        http_json(f"{check.SYNC_URL}/filepush?key={key}", {"name": "rz_dbg.json", "json": {"at": now.strftime("%d.%m %H:%M"), "q": "браузер", "mode": "browser",
                  "log": [f"головна: {DBG.get('home')}"] + [json.dumps(t, ensure_ascii=False)[:400] for t in DBG["tries"]], "card": json.dumps((DBG["tries"] or [{}])[0].get("tile0"), ensure_ascii=False)}})
    except Exception as e:
        print(f"Rozetka → Cloudflare: {err_text(e)[:60]}")


if __name__ == "__main__":
    main()
