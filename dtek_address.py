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


def street_variants(s: str) -> list[str]:
    v = [s, s.replace("'", "’"), s.replace("'", "ʼ"), s.replace("’", "'"), s.replace("ʼ", "'")]
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
        res = None
        for st in street_variants(STREET):
            res = page.evaluate("""async (street) => {
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
            }""", st)
            print(f"ДТЕК getHomeNum «{st}»: HTTP {res.get('status')}, {len(res.get('text') or '')} символів")
            try:
                data = json.loads(res.get("text") or "")
            except Exception:
                data = None
            if isinstance(data, dict) and (data.get("data") or {}):
                browser.close()
                return data
        if res:
            print("ДТЕК: відповідь без даних:", (res.get("text") or "")[:500])
        browser.close()
        return None


def house_info(data: dict) -> dict:
    """Дані саме нашого будинку з відповіді getHomeNum."""
    houses = data.get("data") or {}
    h = houses.get(HOUSE)
    if h is None:                                    # іноді ключі з літерами/дробами — шукаємо збіг
        h = next((v for k, v in houses.items() if re.sub(r"\s", "", str(k)).lower() == HOUSE.lower()), {})
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
    active = bool(info["reason"] or info["start"])
    print(f"Адреса {STREET}, {HOUSE}: {'НЕМАЄ світла' if active else 'відключень немає'} · {info}")

    cur = {"active": active, **info, "emergency": bool(re.search(r"екстрен|аварійн", info["reason"], re.I)),
           "checked": now.isoformat(), "address": f"{STREET}, {HOUSE}"}
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
        msgs.append(f"💡 <b>ДТЕК ЗНЯВ ВІДКЛЮЧЕННЯ ЗА АДРЕСОЮ</b>\n{addr}\n"
                    f"Світло має бути (відключення було з {esc(prev.get('start') or '—')})")
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


if __name__ == "__main__":
    main()
