"""
Перевірка відключень за адресою на сайті ДТЕК Київські електромережі (як у формі «Відсутня електроенергія?»).
Сайт захищений від програм, тому сторінку відкриваємо справжнім браузером (Playwright + Chromium) і робимо
той самий запит getHomeNum, що й сайт. Запускається окремим workflow кожні ~10 хв.

Змінні середовища (секрети GitHub): BOT_TOKEN, CHAT_ID, WIFE_ID, SYNC_URL, SYNC_KEY,
ADDR_STREET і ADDR_HOUSE — лише із секретів GitHub (адреса не зберігається в коді).
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

STREET = (os.environ.get("ADDR_STREET") or "").strip()      # з секрету ADDR_STREET
HOUSE = (os.environ.get("ADDR_HOUSE") or "").strip()        # з секрету ADDR_HOUSE
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
        try:
            lookup_requests(page)                      # черга за адресою для підписників — завжди, незалежно від нашої адреси
        except Exception as e:
            print(f"Адреси підписників: {err_text(e)}")
        if not STREET or not HOUSE:
            print("ДТЕК: секрети ADDR_STREET / ADDR_HOUSE не задано — власну адресу не перевіряю")
            browser.close()
            return None
        res = None
        for st in street_variants(STREET):
            res = page.evaluate(GETHOME_JS, st)
            print(f"ДТЕК getHomeNum (власна адреса, варіант): HTTP {res.get('status')}, {len(res.get('text') or '')} символів")
            try:
                data = json.loads(res.get("text") or "")
            except Exception:
                data = None
            if isinstance(data, dict) and (data.get("data") or {}):
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
    if h is None:                                    # ключі з літерами/дробами: 42/Б = 42Б = 42 б
        h = next((v for k, v in houses.items() if hkey(k).replace("/", "") == hkey(HOUSE).replace("/", "")), None)
    if h is None:
        print(f"ДТЕК: власного будинку у відповіді немає (будинків на вулиці: {len(houses)})")
        return None
    print(f"ДТЕК: власний будинок знайдено: {json.dumps(h, ensure_ascii=False)[:300]}")
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
    print(f"Власна адреса: {'НЕМАЄ світла' if active else 'ДТЕК не показує відключення'} · {info}")

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


STREETS_JS = """() => {                                       // список вулиць зі сторінки ДТЕК (для автопідказки)
  const seen = new Set(), out = [];
  const scan = (o, depth) => { if (!o || depth > 3) return;
    for (const k of Object.keys(o)) { let v; try { v = o[k]; } catch (e) { continue; }
      if (Array.isArray(v) && v.length > 50 && typeof v[0] === 'string') { for (const s of v) if (!seen.has(s)) { seen.add(s); out.push(s); } }
      else if (v && typeof v === 'object' && !Array.isArray(v) && depth < 2) scan(v, depth + 1); } };
  scan(window.DisconSchedule || {}, 0);
  document.querySelectorAll('datalist option, #street option, [name=street] option').forEach(o => { const s = o.value || o.textContent; if (s && !seen.has(s)) { seen.add(s); out.push(s); } });
  return out;
}"""
TYPE_WORDS = (r"(?<![а-яіїєґa-z])(вулиця|вул|улица|ул|проспект|просп|пр-т|пр|бульвар|бульв|б-р|провулок|пров|переулок|пер|площа|площадь|пл|"
              r"шосе|шоссе|набережна|набережная|наб|узвіз|спуск|тупик|алея|аллея|майдан|станція|ст)(?![а-яіїєґa-z])\.?")
CITY_WORDS = re.compile(r"(?<![а-яіїєґa-z])(в|у|м\.?|г\.?)?\s*(києві|київ|киеве|киев|kyiv|kiev|вінниці|вінниця|виннице|винница)(?![а-яіїєґa-z])", re.I)


def skel(s: str) -> str:
    """Кістяк назви без голосних — однаковий для укр. і рос. написання («Героїв Дніпра» = «Героев Днепра» → «грв днпр»)."""
    s = norm_street(s).replace("ґ", "г").replace("щ", "шч").replace("'", "")
    return " ".join(re.sub(r"[аеєиіїйоуюяыэёьъ]", "", w) for w in s.split())


def hkey(h: str) -> str:
    """Номер будинку в одному форматі: 42б = 42 Б = 42-б = 42b; 3/1 = 3 / 1."""
    h = str(h).lower().replace(" ", "").replace("-", "")
    return h.translate(str.maketrans({"a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "k": "к", "c": "с", "o": "о"}))


def norm_street(s: str) -> str:
    s = CITY_WORDS.sub(" ", s.lower())
    s = s.replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("ё", "е")
    s = re.sub(TYPE_WORDS, " ", s)
    s = re.sub(r"[^a-zа-яіїєґ0-9' -]", " ", s)
    return " ".join(s.split())


def match_streets(user: str, streets: list[str]) -> list[str]:
    """Нечіткий пошук: частина назви, помилки, без «вул.», інший порядок слів."""
    import difflib
    q = norm_street(user)
    if not q:
        return []
    scored = []
    for st_ in streets:
        n = norm_street(st_)
        if not n:
            continue
        if q == n:
            score = 1.0
        elif skel(user) and skel(user) == skel(st_):
            score = 0.97                               # те саме, інше написання (рос./укр., помилка в голосних)
        elif q in n or all(w in n for w in q.split()):
            score = 0.9 - 0.001 * len(n)               # частина назви — коротші вище
        else:
            score = max(difflib.SequenceMatcher(None, q, n).ratio(),
                        max((difflib.SequenceMatcher(None, w, x).ratio() for w in q.split() for x in n.split()), default=0) - 0.05)
        if score >= 0.72:
            scored.append((score, st_))
    scored.sort(key=lambda x: -x[0])
    return [s_ for _, s_ in scored[:8]]


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
    full = {"вул.": "вулиця", "просп.": "проспект", "бульв.": "бульвар", "пров.": "провулок", "пл.": "площа"}
    out = [f"{k} {name}" for k in kinds] + [f"{full[k]} {name}" for k in kinds if k in full] + [name] + [f"{name} {k}" for k in kinds[:2]]
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
    try:
        streets = page.evaluate(STREETS_JS) or []
    except Exception:
        streets = []
    print(f"Адреси: список вулиць ДТЕК — {len(streets)}")
    try:                                              # список вулиць — у Cloudflare (вибір вулиці за першою літерою в боті)
        if streets and check.WORKER_MODE:
            check.http_json(f"{check.SYNC_URL}/filepush?key={check.urllib.parse.quote(check.SYNC_KEY)}", {"name": "streets_kyiv.json", "json": sorted(set(streets))})
    except Exception as e:
        print(f"Список вулиць → Cloudflare: {err_text(e)[:60]}")
    for cid, r in list(reqs.items())[:5]:
        street, house = str(r.get("street") or ""), str(r.get("house") or "").lower().replace(" ", "")
        found = None
        tried: list = []                                  # діагностика: які варіанти пробували й скільки будинків повернув ДТЕК
        exact = bool(r.get("exact"))                     # користувач уже обрав вулицю зі списку
        cands = [street] if exact else (match_streets(street, streets) if streets else [])
        if len(cands) > 1 and skel(cands[0]) == skel(street):
            cands = cands[:1]                         # точний збіг (інше написання) — не питаємо
        if not exact and len(cands) > 1 and norm_street(cands[0]) != norm_street(street):
            print(f"Адреса підписника: {street} → кілька варіантів {cands[:5]}")
            try:                                          # просимо уточнити вулицю кнопками
                check.http_json(f"{check.SYNC_URL}/setgroup?key={check.urllib.parse.quote(check.SYNC_KEY)}",
                                {"cid": cid, "choices": cands[:8], "house": house})
            except Exception as e:
                print(f"setgroup: {err_text(e)}")
            continue
        for st in (cands or addr_variants(street))[:14]:
            res = page.evaluate(GETHOME_JS, st)
            try:
                data = json.loads(res.get("text") or "")
            except Exception:
                continue
            houses = data.get("data") or {}
            tried.append(f"{st}→{len(houses)}")
            h = next((v for k, v in houses.items() if hkey(k) == hkey(house) or hkey(k).replace("/", "") == hkey(house).replace("/", "")), None)
            if h is None and houses:
                num = re.match(r"\d+", hkey(house))
                same = [k for k in houses if num and re.match(r"\d+", hkey(k)) and re.match(r"\d+", hkey(k)).group(0) == num.group(0)]
                if len(same) == 1:                     # «42б» ↔ «42 корп. Б» тощо — єдиний будинок з цим номером
                    h = houses[same[0]]
            if h is None and houses and len(cands) == 1:   # вулицю знайдено, а такого будинку немає — запропонуємо список
                found = {"street": st, "houses": sorted(houses, key=lambda k: (re.sub(r"\D.*", "", str(k)).zfill(5), str(k)))[:60], "house": house}
                break
            if h is not None:
                grp = next((g[3:] for g in (h.get("sub_type_reason") or []) if str(g).startswith("GPV")), None)
                found = {"street": st, "house": house, "group": grp}
                break
        print(f"Адреса підписника: {street}, {re.sub(r'\d', '•', house)} → {(found or {}).get('group') or ('список будинків' if (found or {}).get('houses') else 'не знайдено')}"
              f" · кандидати зі списку ДТЕК: {cands[:3]} · спроби: {tried[:8]}")
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
