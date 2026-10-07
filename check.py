"""
Графік відключень світла (Київ, ДТЕК/YASNO) -> сповіщення в Telegram.
Запускається GitHub Actions кожні ~10 хвилин. Лише стандартна бібліотека Python.

Секрети (Settings -> Secrets and variables -> Actions):
    BOT_TOKEN   токен бота від @BotFather
    CHAT_ID     ваш chat id (від @userinfobot); кілька — через кому: 111,222
    GROUP       ваша черга/група, наприклад 3.1
    WIFE_ID     (необов'язково) chat id дружини — отримує все те саме + компліменти зранку
    WIFE_NAME   (необов'язково) як звертатися зранку, за замовчуванням «кохана»

Підписка: будь-хто натискає /start у боті — і отримує сповіщення; /stop — відписатися;
/grafik — поточний графік. Відповідь приходить при наступному запуску (до ~10–20 хв).
Список підписників у state.json зашифровано ключем, похідним від BOT_TOKEN.
"""
import base64
import hashlib
import json
import os
import sys
import time as _time
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

API_URL = ("https://app.yasno.ua/api/blackout-service/public/shutdowns/"
           "regions/25/dsos/902/planned-outages")
TZ = ZoneInfo("Europe/Kyiv")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_IDS = [x.strip() for x in os.environ["CHAT_ID"].split(",") if x.strip()]
WIFE_ID = (os.environ.get("WIFE_ID") or "").strip()
WIFE_NAME = (os.environ.get("WIFE_NAME") or "").strip() or "кохана"
GROUP = (os.environ.get("GROUP") or "1.1").strip()
REMIND_MIN = int(os.environ.get("REMIND_MIN") or 45)    # вікно нагадування (із запасом на затримки GitHub)
MORNING_HOUR = int(os.environ.get("MORNING_HOUR") or 10)  # ранкове повідомлення щодня
SEND_NOW = os.environ.get("SEND_NOW") == "1"             # ручний запуск — надіслати графік одразу
OFF_TYPES = {"Definite"} | ({"Possible"} if os.environ.get("INCLUDE_POSSIBLE") == "1" else set())
POSSIBLE_TYPES = {"Possible"} - OFF_TYPES                 # показуються окремо, нагадувань не дають
STATE_FILE = Path("state.json")


WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
STATUS_TEXT = {
    "ScheduleApplies": "графік діє",
    "PlannedShutdowns": "планові відключення",
    "EmergencyShutdowns": "⚠️ екстрені відключення — графік може не діяти",
    "WaitingForSchedule": "очікується графік",
    "NoShutdowns": "відключень немає",
}
WD_SHORT = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "НД"]
SEP = "━━━━━━━━━━━━━━"

COMPLIMENTS = [
    "Твоя усмішка — найкраще джерело енергії в нашому домі.",
    "З тобою навіть найтемніший вечір стає затишним.",
    "Ти неймовірно красива — і зранку, і ввечері, і при свічках.",
    "Поруч із тобою все виходить легше.",
    "Ти — найкраща подія кожного мого дня.",
    "Дякую, що ти є. Без тебе все було б значно тьмяніше.",
    "Ти вмієш перетворити звичайний день на свято.",
    "Твоя доброта світить яскравіше за будь-яку лампу.",
    "Я пишаюся тобою більше, ніж ти думаєш.",
    "Ти найрозумніша і найчарівніша жінка, яку я знаю.",
    "У тебе найтепліші обійми у світі.",
    "Ти — мій дім, де б ми не були.",
    "Кожен день із тобою — подарунок.",
    "Твій сміх — моя улюблена музика.",
    "Ти сильніша, ніж сама собі уявляєш.",
    "Ти робиш мене кращим просто тим, що ти поруч.",
    "Твої очі світяться так, що жодні відключення не страшні.",
    "Ти прекрасна в усьому — навіть у тому, як п'єш ранкову каву.",
    "Мені неймовірно пощастило з тобою.",
    "Ти — найкраще рішення в моєму житті.",
    "Ти вмієш підтримати так, як ніхто інший.",
    "З тобою хочеться будувати плани й мріяти.",
    "Ти — моє натхнення і моя радість.",
    "Навіть у найсіріший день ти — мій сонячний промінчик.",
    "Ти дбаєш про всіх навколо — і я дуже це ціную.",
    "Ти з кожним днем стаєш ще прекраснішою.",
    "Твоя ніжність робить наш дім особливим.",
    "Я закоханий у тебе так само, як у перший день, — і навіть більше.",
    "Ти — моя найважливіша людина.",
    "Ти робиш цей світ теплішим навіть тоді, коли в ньому немає світла.",
]
MOTIVATION = [
    "Нехай сьогодні все складається легко, а справи робляться ніби самі собою.",
    "Маленькі кроки теж ведуть до великої мети — зроби сьогодні хоча б один.",
    "Ти впораєшся з усім, що принесе цей день.",
    "Сьогодні чудовий день, щоб зробити щось приємне для себе.",
    "Усміхнися — день уже став кращим.",
    "Не поспішай: усе важливе встигнеться.",
    "Хай цей день подарує тобі багато щасливих хвилин.",
    "Будь до себе такою ж доброю, як до інших.",
    "Кожен ранок — новий шанс почати з чистого аркуша.",
    "Енергія всередині тебе не залежить від жодного графіка.",
    "Зроби сьогодні те, за що завтра скажеш собі «дякую».",
    "Навіть найдовша темрява закінчується світлом.",
    "Знайди сьогодні привід для радості — він точно є.",
    "Ти вже багато чого досягла, і попереду ще більше.",
    "Хай сьогодні буде більше тиші, тепла і добрих новин.",
    "Відпочинок — теж частина успіху. Не забувай про нього.",
    "Твоя наполегливість обов'язково дасть результат.",
    "Хай усе, що ти задумала, сьогодні вдається.",
    "Дихай глибше, усміхайся частіше.",
    "Сьогоднішній день — твій. Проживи його із задоволенням.",
    "Добрі думки притягують добрі події.",
    "Не бійся просити про допомогу — я завжди поруч.",
    "Сьогодні вийде навіть краще, ніж ти очікуєш.",
    "Хай кава буде гарячою, а настрій — сонячним.",
    "Кожна дрібниця, зроблена з любов'ю, має значення.",
]


# ---------- мережа ----------
def http_json(url: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch() -> dict:
    for attempt in range(1, 4):
        try:
            return http_json(API_URL)
        except Exception as e:
            print(f"API спроба {attempt}: {e}")
            _time.sleep(5)
    sys.exit("API YASNO недоступний")


def send(chat_id: str, text: str) -> None:
    http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
              {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True})


def get_updates(offset: int | None) -> list[dict]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?timeout=0&allowed_updates=%5B%22message%22%5D"
    if offset:
        url += f"&offset={offset}"
    try:
        return http_json(url).get("result", [])
    except Exception as e:
        print(f"getUpdates: {e}")
        return []


# ---------- шифрування списку підписників (репозиторій публічний) ----------
def _keystream(n: int, nonce: bytes) -> bytes:
    key = hashlib.sha256(("svitlo-subs:" + BOT_TOKEN).encode()).digest()
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:n]


def enc(obj) -> str:
    raw = json.dumps(obj).encode()
    nonce = os.urandom(16)
    return base64.b64encode(nonce + bytes(a ^ b for a, b in zip(raw, _keystream(len(raw), nonce)))).decode()


def dec(s: str | None) -> list:
    if not s:
        return []
    try:
        b = base64.b64decode(s)
        nonce, c = b[:16], b[16:]
        return json.loads(bytes(a ^ b for a, b in zip(c, _keystream(len(c), nonce))))
    except Exception:
        return []


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


# ---------- обчислення ----------
def day_date(d: dict):
    return datetime.fromisoformat(d["date"]).date()


def intervals(d: dict | None, types: set) -> list[list[datetime]]:
    if not d or "date" not in d or not types:
        return []
    base = datetime.combine(day_date(d), time(0), TZ)
    return [[base + timedelta(minutes=s["start"]), base + timedelta(minutes=s["end"])]
            for s in d.get("slots", []) if s.get("type") in types]


def merge(ivs: list[list[datetime]]) -> list[list[datetime]]:
    res: list[list[datetime]] = []
    for a, b in sorted(ivs):
        if res and res[-1][1] >= a:
            res[-1][1] = max(res[-1][1], b)
        else:
            res.append([a, b])
    return res


def hm(dt: datetime, day) -> str:
    return "24:00" if dt.date() > day and dt.time() == time(0) else f"{dt:%H:%M}"


def dur(td: timedelta) -> str:
    h, m = divmod(round(td.total_seconds() / 60), 60)
    if h and m:
        return f"{h} год {m} хв"
    return f"{h} год" if h else f"{m} хв"


def when(dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    prefix = {0: "сьогодні", 1: "завтра"}.get(days, f"{dt:%d.%m}")
    return f"{prefix} о <b>{dt:%H:%M}</b>"


def ivs_text(ivs, day) -> str:
    return ", ".join(f"{a:%H:%M}–{hm(b, day)}" for a, b in ivs) or "без відключень"


# ---------- оформлення ----------
def fmt_day(d: dict | None, title: str, mark: set | None = None) -> str:
    """Компактний блок дня. mark — нові інтервали (start, end), позначаються 🆕."""
    if not d or "date" not in d:
        return f"<blockquote>📆 <b>{title.upper()}</b> — графіка ще немає</blockquote>"
    dd = day_date(d)
    status = d.get("status") or ""
    lines = [f"📆 <b>{title.upper()}, {WD_SHORT[dd.weekday()]} {dd:%d.%m}</b>"]
    if status == "EmergencyShutdowns":
        lines.append(f"<b>{STATUS_TEXT[status]}</b>")
    elif status and status != "ScheduleApplies":
        lines.append(f"<i>{STATUS_TEXT.get(status, status)}</i>")
    off = merge(intervals(d, OFF_TYPES))
    pos = merge(intervals(d, POSSIBLE_TYPES))
    if off:
        lines += [f"🔴 <b>{a:%H:%M}–{hm(b, dd)}</b> ({dur(b - a)})"
                  + (" 🆕" if mark and (a, b) in mark else "") for a, b in off]
        total_off = sum((b - a for a, b in off), timedelta())
        lines.append(f"💡 світло {dur(timedelta(days=1) - total_off)} · без світла {dur(total_off)}")
    else:
        lines.append("🟢 <b>Без відключень</b>")
    if pos:
        lines.append(f"🟡 можливі: {ivs_text(pos, dd)}")
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def now_line(off: list, now: datetime) -> str:
    for a, b in off:
        if a <= now < b:
            return f"⏳ Зараз без світла до <b>{hm(b, a.date())}</b> (ще {dur(b - now)})"
    nxt = next((a for a, _ in off if a > now), None)
    if nxt:
        day = "" if nxt.date() == now.date() else "завтра "
        return f"⏳ Наступне відключення {day}о <b>{nxt:%H:%M}</b> (через {dur(nxt - now)})"
    return "⏳ Відключень за графіком більше немає"


def summary(head: str, today_d, tomorrow_d, all_off, now) -> str:
    return (f"{head}\n{now_line(all_off, now)}\n"
            + fmt_day(today_d, "Сьогодні") + "\n" + fmt_day(tomorrow_d, "Завтра"))


# ---------- головна логіка ----------
def main() -> None:
    st = load_state()
    if st.get("group") != GROUP:              # змінили групу — графік заново, підписники лишаються
        st = {"group": GROUP, "fp": {}, "reminded": [],
              "offset": st.get("offset"), "subs": st.get("subs")}
    subs = [str(x) for x in dec(st.get("subs"))]
    subs_before = list(subs)

    data = fetch()
    g = data.get(GROUP)
    if g is None:
        keys = ", ".join(sorted(k for k, v in data.items() if isinstance(v, dict)))
        send(CHAT_IDS[0], f"⚠️ Групу <b>{GROUP}</b> не знайдено в графіку.\nДоступні: {keys}")
        sys.exit(1)

    now = datetime.now(TZ)
    today_key = now.date().isoformat()
    today_d, tomorrow_d = g.get("today"), g.get("tomorrow")
    all_off = merge(intervals(today_d, OFF_TYPES) + intervals(tomorrow_d, OFF_TYPES))
    fp = st["fp"]
    first_run = not fp
    msgs: list[tuple[str, str | None]] = []   # (текст для всіх, окремий текст для дружини або None)
    direct: list[tuple[str, str]] = []        # (chat_id, текст) — відповіді на команди

    # 0) команди: /start, /stop, /grafik
    updates = get_updates(st.get("offset"))
    for u in updates:
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        cid = str((msg.get("chat") or {}).get("id", ""))
        cmd = (msg.get("text") or "").strip().split("@")[0].split(" ")[0].lower()
        if not cid:
            continue
        if cmd == "/start":
            if cid not in subs and cid not in CHAT_IDS and cid != WIFE_ID:
                subs.append(cid)
            direct.append((cid, summary(f"✅ <b>Ви підписані на сповіщення</b> · група {GROUP}\n"
                                        f"<i>/grafik — графік, /stop — відписатися</i>",
                                        today_d, tomorrow_d, all_off, now)))
        elif cmd == "/stop":
            if cid in subs:
                subs.remove(cid)
            direct.append((cid, "👋 Ви відписалися від сповіщень. Повернутися — /start"))
        elif cmd in ("/grafik", "/schedule", "/графік"):
            direct.append((cid, summary(f"📋 <b>Поточний графік</b> · група {GROUP}",
                                        today_d, tomorrow_d, all_off, now)))
    recipients = list(dict.fromkeys(CHAT_IDS + ([WIFE_ID] if WIFE_ID else []) + subs))

    # 1) поточний графік: перший запуск або ручний запуск
    if SEND_NOW or first_run:
        head = "✅ <b>Бот працює</b>" if first_run else "📋 <b>Поточний графік</b>"
        msgs.append((summary(f"{head} · група {GROUP}", today_d, tomorrow_d, all_off, now), None))

    # 2) ранкове повідомлення щодня (незалежно від змін)
    if (not first_run and st.get("morning") != today_key
            and MORNING_HOUR <= now.hour < MORNING_HOUR + 4):
        n = now.date().toordinal()
        wife_head = (f"☀️ <b>Доброго ранку, {WIFE_NAME}!</b>\n"
                     f"💖 {COMPLIMENTS[n % len(COMPLIMENTS)]}\n"
                     f"✨ <i>{MOTIVATION[n % len(MOTIVATION)]}</i>\n{SEP}")
        common = summary(f"☀️ <b>Доброго ранку!</b> · група {GROUP}", today_d, tomorrow_d, all_off, now)
        msgs.append((common, summary(wife_head, today_d, tomorrow_d, all_off, now)))
    if first_run or st.get("morning") != today_key and now.hour >= MORNING_HOUR:
        st["morning"] = today_key

    # 3) новий / змінений графік
    for d, title in ((today_d, "Сьогодні"), (tomorrow_d, "Завтра")):
        if not d or "date" not in d:
            continue
        dk = day_date(d).isoformat()
        new = json.dumps({"s": d.get("status"), "slots": d.get("slots", [])}, sort_keys=True)
        old = fp.get(dk)
        fp[dk] = new
        if first_run or old == new:
            continue
        dd = day_date(d)
        emergency = d.get("status") == "EmergencyShutdowns"
        prev = json.loads(old) if old else {}
        was_emergency = prev.get("s") == "EmergencyShutdowns"
        banner = ""
        if emergency and not was_emergency:
            banner = ("🚨🚨🚨 <b>ЕКСТРЕНІ ВІДКЛЮЧЕННЯ!</b> 🚨🚨🚨\n"
                      "Світло можуть вимкнути поза графіком · 🔋 зарядіть пристрої\n\n")
        elif was_emergency and not emergency:
            banner = "✅✅ <b>ЕКСТРЕНІ ВІДКЛЮЧЕННЯ СКАСОВАНО</b> ✅✅\n\n"
        if old is None:
            msgs.append((f"{banner}📅 <b>НОВИЙ ГРАФІК</b> · група {GROUP}\n{fmt_day(d, title)}", None))
        else:
            prev_d = dict(d, slots=prev.get("slots", []), status=prev.get("s"))
            prev_off = merge(intervals(prev_d, OFF_TYPES))
            new_off = merge(intervals(d, OFF_TYPES))
            prev_set = {(a, b) for a, b in prev_off}
            fresh = {(a, b) for a, b in new_off if (a, b) not in prev_set}
            head = "" if banner and prev_off == new_off else (
                f"⚠️⚠️ <b>ГРАФІК ЗМІНЕНО!</b> ⚠️⚠️\nгрупа {GROUP}\n")
            was = ivs_text(prev_off, dd)
            tail = "" if prev_off == new_off else f"\n↩️ було: <s>{was}</s>"
            msgs.append((f"{banner}{head}{fmt_day(d, title, fresh)}{tail}", None))

    # 4) нагадування до відключення і до увімкнення
    rem = set(st["reminded"])
    for i, (a, b) in enumerate(all_off):
        nxt = all_off[i + 1][0] if i + 1 < len(all_off) else None
        for ev, t in (("off", a), ("on", b)):
            k = f"{ev}|{t.isoformat()}"
            left = (t - now).total_seconds() / 60
            if not (0 < left <= REMIND_MIN) or k in rem:
                continue
            m = round(left)
            if ev == "off":
                text = (f"⏰ <b>Через ~{m} хв відключення</b>\n"
                        f"<blockquote>🔴 <b>{a:%H:%M}–{hm(b, a.date())}</b> ({dur(b - a)})</blockquote>")
            else:
                tail = (f"🔴 далі: {when(nxt, now)}" if nxt else "🟢 далі відключень немає")
                text = f"💡 <b>Через ~{m} хв світло</b> (о <b>{b:%H:%M}</b>)\n{tail}"
            msgs.append((text, None))
            rem.add(k)

    # відправка кожному окремо: помилка в одного не зупиняє інших
    ok = failed = 0
    jobs = direct + [(cid, wife_text if (wife_text and cid == WIFE_ID) else text)
                     for text, wife_text in msgs for cid in recipients]
    removed: set[str] = set()
    for cid, text in jobs:
        if cid in removed:
            continue
        try:
            send(cid, text)
            ok += 1
        except urllib.error.HTTPError as e:
            if e.code in (400, 403) and cid in subs:     # підписник заблокував бота / чат зник
                subs.remove(cid)
                removed.add(cid)
                print(f"Підписника {cid} видалено ({e.code})")
            else:
                print(f"Не вдалося надіслати {cid}: {e}")
                failed += 1
        except Exception as e:
            print(f"Не вдалося надіслати {cid}: {e}")
            failed += 1
    if jobs and ok == 0 and failed:
        sys.exit("Telegram недоступний — повторимо наступного запуску")

    if subs != subs_before or "subs" not in st:    # перешифровуємо лише при зміні (менше комітів)
        st["subs"] = enc(subs)

    keep_from = (now.date() - timedelta(days=1)).isoformat()
    st["fp"] = {k: v for k, v in fp.items() if k >= keep_from}
    st["reminded"] = sorted(k for k in rem
                            if datetime.fromisoformat(k.split("|", 1)[1]) > now - timedelta(days=1))
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
    if failed:
        sys.exit(f"Не доставлено повідомлень: {failed} (чи натиснули Start у боті?)")


if __name__ == "__main__":
    main()
