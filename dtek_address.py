"""
Перевірка відключень за адресою на сайті ДТЕК Київські електромережі (як у формі «Відсутня електроенергія?»).
Сайт захищений від програм, тому сторінку відкриваємо справжнім браузером (Playwright + Chromium) і робимо
той самий запит getHomeNum, що й сайт. Запускається окремим workflow кожні ~10 хв.

Змінні середовища (секрети GitHub): BOT_TOKEN, CHAT_ID, WIFE_ID, SYNC_URL, SYNC_KEY,
ADDR_STREET (за замовчуванням «вул. Кульженків Сім'ї»), ADDR_HOUSE (за замовчуванням «35»).
Стан — у файлі addr_state.json (його читає й основний бот).
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import check  # спільні функції основного бота (надсилання, підписники тощо)
from check import TZ, CHAT_IDS, WIFE_ID, esc, send, err_text

STREET = (os.environ.get("ADDR_STREET") or "вул. Кульженків Сім'ї").strip()
HOUSE = (os.environ.get("ADDR_HOUSE") or "35").strip()
STATE = Path("addr_state.json")
URL = "https://www.dtek-kem.com.ua/ua/shutdowns"
GETHOME_JS = """async (street) => {
                const token = document.querySelector('meta[name="csrf-token"]').content;
                const ajax = (document.querySelector('meta[name="ajaxUrl"]') || {}).content || '/ua/ajax';
                const fact = (window.DisconSchedule && window.DisconSchedule.fact && window.DisconSchedule.fact.update) || '';
                const body = new URLSearchParams();
                body.append('method', 'getHomeNum');
                body.append('data[0][name]', 'street');
                body.append('data[0][value]', street);
                body.append('data[1][name]', 'updateFact');
                body.append('data[1][value]', fact);
                const r = await fetch(ajax, { method: 'POST', body,
                    headers: { 'X-CSRF-Token': token, 'X-Requested-With': 'XMLHttpRequest',
                               'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' } });
                return { status: r.status, text: await r.text() };
            }"""


def street_variants(s: str) -> list[str]:
    try:                                             # спершу — варіант, який спрацював минулого разу
        good = json.loads(STATE.read_text("utf-8")).get("street_ok")
    except Exception:
        good = None
    v = ([good] if good else []) + [s, s.replace("'", "’"), s.replace("'", "ʼ"), s.replace("’", "'"), s.replace("ʼ", "'")]
    return list(dict.fromkeys(v))


def fetch_address() -> dict | None:
    """Відкриває сторінку ДТЕК і повертає відповідь getHomeNum для нашої вулиці."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="uk-UA", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"))
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_function("() => !!document.querySelector('meta[name=\"csrf-token\"]')", timeout=30000)
        except Exception:
            print("ДТЕК: сторінка не відкрилась повністю (можливо, захист від ботів)")
            print("Заголовок сторінки:", page.title()[:100])
            browser.close()
            return None
        city = page.evaluate("""() => {
            const t = (document.body && document.body.innerText) || '';
            const out = [];
            for (const line of t.split('\\n')) {
                const l = line.trim();
                if (l && /екстрен|аварійн|не діють|не застосов/i.test(l) && l.length < 400
                    && !/якщо|імовірно|просимо перевірити|оформіть заявку/i.test(l)) out.push(l);
            }
            return out.slice(0, 8);
        }""")
        print("ДТЕК, текст сторінки про екстрені:", city if city else "немає")
        res = None
        for st in street_variants(STREET):
            res = page.evaluate(GETHOME_JS, st)
            print(f"ДТЕК getHomeNum «{st}»: HTTP {res.get('status')}, {len(res.get('text') or '')} символів")
            try:
                data = json.loads(res.get("text") or "")
            except Exception:
                data = None
            if isinstance(data, dict) and (data.get("data") or {}):
                try:
                    lookup_requests(page)              # черга за адресою для підписників
                except Exception as e:
                    print(f"Адреси підписників: {err_text(e)}")
                browser.close()
                data["_street"] = st
                data["_city"] = city
                extra = {k: v for k, v in data.items() if k not in ("data", "_street", "_city")}
                print("ДТЕК, загальні поля відповіді:", json.dumps(extra, ensure_ascii=False)[:600])
                return data
        if res:
            print("ДТЕК: відповідь без даних:", (res.get("text") or "")[:500])
        browser.close()
        return None


def house_info(data: dict) -> dict | None:
    """Дані саме нашого будинку з відповіді getHomeNum; None — якщо будинку у відповіді немає."""
    houses = data.get("data") or {}
    h = houses.get(HOUSE)
    if h is None:                                    # іноді ключі з літерами/дробами — шукаємо збіг
        h = next((v for k, v in houses.items() if re.sub(r"\s", "", str(k)).lower() == HOUSE.lower()), None)
    if h is None:
        print(f"ДТЕК: будинку «{HOUSE}» у відповіді немає. Є: {', '.join(list(houses)[:40])}")
        return None
    print(f"ДТЕК: будинок {HOUSE} знайдено: {json.dumps(h, ensure_ascii=False)[:300]}")
    h = h or {}
    return {"reason": (h.get("sub_type") or "").strip(), "start": (h.get("start_date") or "").strip(),
            "end": (h.get("end_date") or "").strip(), "type": str(h.get("type") or ""),
            "groups": h.get("sub_type_reason") or [], "updated": str(data.get("updateTimestamp") or "")}


def main() -> None:
    now = datetime.now(TZ)
    try:
        prev = json.loads(STATE.read_text("utf-8"))
    except Exception:
        prev = {}
    try:
        data = fetch_address()
    except Exception as e:
        print(f"ДТЕК: помилка браузера: {err_text(e)}")
        data = None
    if not data:
        sys.exit(0)                                  # нічого не змінюємо — спробуємо наступного разу
    info = house_info(data)
    if info is None:
        sys.exit(0)                                  # не знайшли будинок — стан не змінюємо
    active = bool(info["reason"] or info["start"])
    print(f"Адреса {STREET}, {HOUSE}: {'НЕМАЄ світла' if active else 'ДТЕК не показує відключення'} · {info}")

    banner = " ".join(data.get("_city") or [])
    extra_txt = json.dumps({k: v for k, v in data.items() if k not in ("data", "_street", "_city")}, ensure_ascii=False)
    explicit = re.compile(r"(застосову\w*|діють|запроваджен\w*|оголошен\w*)\s+(\w+\s+){0,2}екстрен|"
                          r"екстрен\w*\s+відключення\s+(застосову|діють|запроваджен)|"
                          r"графік\w*\s+(стабілізаційн\w*\s+)?(погодинних\s+)?відключень\s+не\s+діють", re.I)
    city_em = bool(explicit.search(banner) or explicit.search(extra_txt))   # лише явні оголошення, не загальні підказки
    print(f"Екстрені по місту (сторінка ДТЕК): {'ТАК' if city_em else 'ні'}")
    cur = {"active": active, **info, "emergency": bool(re.search(r"екстрен", info["reason"], re.I)),   # аварія на лінії ≠ екстрені
           "city_emergency": city_em, "city_text": banner[:300],
           "checked": now.isoformat(), "address": f"{STREET}, {HOUSE}", "street_ok": data.get("_street")}
    msgs = []
    addr = f"🏠 {esc(STREET)}, {esc(HOUSE)}"
    planned = bool(re.search(r"стабілізаційн|планов", info["reason"], re.I))  # за графіком — про це вже є нагадування
    if active and (not prev.get("active") or prev.get("start") != info["start"]) and not planned:
        head = "🚨 <b>ЗА АДРЕСОЮ НЕМАЄ СВІТЛА</b> 🚨" if cur["emergency"] else "⚡ <b>ЗА АДРЕСОЮ НЕМАЄ СВІТЛА</b>"
        msgs.append(f"{head}\n{addr}\n<blockquote>Причина: <b>{esc(info['reason'])}</b>\n"
                    f"🕐 Початок: {esc(info['start'])}\n💡 Орієнтовне відновлення: <b>{esc(info['end'] or 'невідомо')}</b>"
                    f"</blockquote>\n" + ("⚠️ Графіки стабілізаційних відключень зараз не діють\n" if cur["emergency"] else "")
                    + f"<i>Дані ДТЕК, оновлено {esc(info['updated'])}</i>")
    elif active and prev.get("active") and prev.get("end") != info["end"] and info["end"] and not planned:
        msgs.append(f"🕐 <b>ОНОВЛЕНО ЧАС ВІДНОВЛЕННЯ</b>\n{addr}\n💡 Тепер орієнтовно: <b>{esc(info['end'])}</b>"
                    f" (було {esc(prev.get('end') or '—')})\n<i>Дані ДТЕК, оновлено {esc(info['updated'])}</i>")
    elif not active and prev.get("active") and not prev.get("planned"):
        # ДТЕК прибрав запис. Це не гарантує, що світло вже є: часто запис зникає, коли минув орієнтовний час
        msgs.append(f"ℹ️ <b>ДТЕК БІЛЬШЕ НЕ ПОКАЗУЄ ВІДКЛЮЧЕННЯ ЗА АДРЕСОЮ</b>\n{addr}\n"
                    f"Відключення було з {esc(prev.get('start') or '—')}, орієнтовно до {esc(prev.get('end') or '—')}.\n"
                    "Якщо світла досі немає — можлива нова аварія: ДТЕК радить перевірити ще раз через 15 хв "
                    "або оформити заявку на сайті.\n<i>Дані ДТЕК, оновлено " + esc(info["updated"]) + "</i>")
    cur["planned"] = planned
    STATE.write_text(json.dumps(cur, ensure_ascii=False, indent=1), "utf-8")

    if not msgs:
        return
    # кому: вам, дружині та підписникам із темою «Світло» (список — з Cloudflare)
    to = list(dict.fromkeys(CHAT_IDS + ([WIFE_ID] if WIFE_ID else [])))
    if check.WORKER_MODE:
        try:
            ex = check.http_json(f"{check.SYNC_URL}/export?key={check.urllib.parse.quote(check.SYNC_KEY)}")
            to += [c for c, on in (ex.get("subs") or {}).items() if "svitlo" in on and c not in to]
        except Exception as e:
            print(f"Підписники: {err_text(e)}")
    for m in msgs:
        for cid in to:
            try:
                send(cid, m)
            except Exception as e:
                print(f"Не вдалося надіслати {cid}: {err_text(e)}")


TYPES = [("проспект", "просп."), ("просп", "просп."), ("вулиця", "вул."), ("вул", "вул."), ("бульвар", "бульв."), ("бульв", "бульв."),
         ("провулок", "пров."), ("пров", "пров."), ("площа", "пл."), ("пл", "пл."), ("шосе", "шосе"), ("набережна", "наб."), ("узвіз", "узвіз")]


def addr_variants(street: str) -> list[str]:
    """«Оболонський проспект» / «просп Оболонський» / «Оболонський» → варіанти назви, як на сайті ДТЕК."""
    words = [w for w in re.split(r"[\s,]+", street.strip()) if w]
    kind, rest = None, []
    for w in words:
        k = next((full for t, full in TYPES if w.lower().rstrip(".") == t), None)
        if k and not kind:
            kind = k
        else:
            rest.append(w[:1].upper() + w[1:])
    name = " ".join(rest)
    kinds = [kind] if kind else ["вул.", "просп.", "бульв.", "пров.", "пл.", "шосе"]
    out = [f"{k} {name}" for k in kinds]
    return list(dict.fromkeys(v for x in out for v in (x, x.replace("'", "’"), x.replace("’", "'"))))


def lookup_requests(page) -> None:
    """Підписники ввели адресу — шукаємо їхню чергу на сайті ДТЕК і повідомляємо Worker."""
    if not check.WORKER_MODE:
        return
    try:
        reqs = (check.http_json(f"{check.SYNC_URL}/export?key={check.urllib.parse.quote(check.SYNC_KEY)}") or {}).get("addr_req") or {}
    except Exception as e:
        print(f"Адреси підписників: {err_text(e)}")
        return
    for cid, r in list(reqs.items())[:5]:
        street, house = str(r.get("street") or ""), str(r.get("house") or "").lower().replace(" ", "")
        found = None
        for st in addr_variants(street):
            res = page.evaluate(GETHOME_JS, st)
            try:
                data = json.loads(res.get("text") or "")
            except Exception:
                continue
            houses = data.get("data") or {}
            h = next((v for k, v in houses.items() if str(k).lower().replace(" ", "") == house), None)
            if h is not None:
                grp = next((g[3:] for g in (h.get("sub_type_reason") or []) if str(g).startswith("GPV")), None)
                found = {"street": st, "house": house, "group": grp}
                break
        print(f"Адреса підписника: {street}, {house} → {found or 'не знайдено'}")
        try:
            check.http_json(f"{check.SYNC_URL}/setgroup?key={check.urllib.parse.quote(check.SYNC_KEY)}",
                            {"cid": cid, **(found or {"error": "not_found"})})
        except Exception as e:
            print(f"setgroup: {err_text(e)}")


def voe_probe() -> None:
    """Раз на день: проба сайту «Вінницяобленерго» — які форми й запити є на сторінці адресного графіка (лише в лог)."""
    p = Path("voe_probe.json")
    today = datetime.now(TZ).date().isoformat()
    try:
        if json.loads(p.read_text("utf-8")).get("day") == today:
            return
    except Exception:
        pass
    from playwright.sync_api import sync_playwright
    found = {"day": today, "pages": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="uk-UA")
        xhr = []
        page.on("request", lambda r: xhr.append(f"{r.method} {r.url}") if r.resource_type in ("xhr", "fetch") else None)
        for url in ("https://www.voe.com.ua/disconnection/detailed", "https://www.voe.com.ua/hrafik-pohodynnykh-vidklyuchen"):
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
                info = page.evaluate("""() => ({ title: document.title,
                    forms: [...document.forms].map(f => ({ action: f.action, method: f.method,
                        fields: [...f.elements].map(e => e.name || e.id).filter(Boolean).slice(0, 30) })),
                    text: (document.body.innerText || '').slice(0, 600) })""")
                found["pages"].append({"url": url, **info, "xhr": xhr[-20:]})
                print(f"ВОЕ проба {url}: «{info['title'][:80]}» · форм {len(info['forms'])} · XHR {len(xhr)}")
                for f in info["forms"][:3]:
                    print(f"   форма {f['method']} {f['action'][:100]} · поля: {', '.join(f['fields'][:15])}")
                for x in xhr[-10:]:
                    print(f"   запит: {x[:140]}")
            except Exception as e:
                print(f"ВОЕ проба {url}: {err_text(e)[:120]}")
        browser.close()
    p.write_text(json.dumps(found, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()                                            # проба «Вінницяобленерго» вимкнена: сайт закритий захистом Cloudflare
