"""
Графік відключень світла (Київ, ДТЕК/YASNO) -> сповіщення в Telegram.
Запускається GitHub Actions кожні ~10 хвилин. Лише стандартна бібліотека Python.

Секрети (Settings -> Secrets and variables -> Actions):
    BOT_TOKEN   токен бота від @BotFather
    CHAT_ID     ваш chat id (від @userinfobot)
    GROUP       ваша черга/група, наприклад 3.1
"""
import json
import os
import sys
import time as _time
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

API_URL = ("https://app.yasno.ua/api/blackout-service/public/shutdowns/"
           "regions/25/dsos/902/planned-outages")
TZ = ZoneInfo("Europe/Kyiv")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
GROUP = (os.environ.get("GROUP") or "1.1").strip()
REMIND_MIN = int(os.environ.get("REMIND_MIN") or 45)    # вікно нагадування (із запасом на затримки GitHub)
SEND_NOW = os.environ.get("SEND_NOW") == "1"             # ручний запуск — надіслати графік одразу
OFF_TYPES = {"Definite"} | ({"Possible"} if os.environ.get("INCLUDE_POSSIBLE") == "1" else set())
POSSIBLE_TYPES = {"Possible"} - OFF_TYPES                 # показуються окремо, нагадувань не дають
STATE_FILE = Path("state.json")

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
STATUS_TEXT = {
    "ScheduleApplies": "графік діє",
    "PlannedShutdowns": "планові відключення",
    "EmergencyShutdowns": "⚠️ екстрені відключення — графік може не діяти",
    "WaitingForSchedule": "очікується графік",
    "NoShutdowns": "відключень немає",
}
SEP = "──────────────"


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


def send(text: str) -> None:
    http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
              {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True})


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


def overlap_min(ivs, s: datetime, e: datetime) -> float:
    return sum(max(0.0, (min(b, e) - max(a, s)).total_seconds() / 60) for a, b in ivs)


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
    return f"{prefix} о {dt:%H:%M}"


def ivs_text(ivs, day) -> str:
    return ", ".join(f"{a:%H:%M}–{hm(b, day)}" for a, b in ivs) or "без відключень"


# ---------- оформлення ----------
def timeline(d: dict) -> str:
    """Шкала по годинах: 🟩 світло є, 🟥 немає, 🟧 частково, 🟨 можливо."""
    base = datetime.combine(day_date(d), time(0), TZ)
    off = merge(intervals(d, OFF_TYPES))
    pos = merge(intervals(d, POSSIBLE_TYPES))
    cells = []
    for h in range(24):
        s = base + timedelta(hours=h)
        e = s + timedelta(hours=1)
        ov = overlap_min(off, s, e)
        cells.append("🟥" if ov >= 60 else "🟧" if ov > 0 else "🟨" if overlap_min(pos, s, e) > 0 else "🟩")
    return f"<code>00</code> {''.join(cells[:12])}\n<code>12</code> {''.join(cells[12:])}"


def fmt_day(d: dict | None, title: str) -> str:
    if not d or "date" not in d:
        return f"📆 <b>{title}</b>\nГрафіка ще немає"
    dd = day_date(d)
    status = d.get("status") or ""
    off = merge(intervals(d, OFF_TYPES))
    pos = merge(intervals(d, POSSIBLE_TYPES))
    lines = [f"📆 <b>{title}, {WEEKDAYS[dd.weekday()]} {dd:%d.%m}</b>",
             f"<i>{STATUS_TEXT.get(status, status)}</i>", "", timeline(d), ""]
    if off:
        lines.append("🔴 <b>Без світла:</b>")
        lines += [f"     {a:%H:%M}–{hm(b, dd)}  ·  {dur(b - a)}" for a, b in off]
        total_off = sum((b - a for a, b in off), timedelta())
        lines.append(f"💡 Зі світлом <b>{dur(timedelta(days=1) - total_off)}</b>"
                     f" · без світла <b>{dur(total_off)}</b>")
    else:
        lines.append("🟢 Відключень не заплановано")
    if pos:
        lines.append(f"🟡 <b>Можливі:</b> {ivs_text(pos, dd)}")
    return "\n".join(lines)


def now_line(off: list, now: datetime) -> str:
    for a, b in off:
        if a <= now < b:
            return f"⚡ <b>Зараз:</b> світла немає · увімкнуть {when(b, now)} (через {dur(b - now)})"
    nxt = next((a for a, _ in off if a > now), None)
    if nxt:
        return f"⚡ <b>Зараз:</b> світло є · відключення {when(nxt, now)} (через {dur(nxt - now)})"
    return "⚡ <b>Зараз:</b> світло є · відключень за графіком не заплановано"


LEGEND = "<i>🟩 є · 🟥 немає · 🟧 частково · 🟨 можливо</i>"


# ---------- головна логіка (умови сповіщень без змін) ----------
def main() -> None:
    st = load_state()
    if st.get("group") != GROUP:              # змінили групу — починаємо заново
        st = {"group": GROUP, "fp": {}, "reminded": []}

    data = fetch()
    g = data.get(GROUP)
    if g is None:
        keys = ", ".join(sorted(k for k, v in data.items() if isinstance(v, dict)))
        send(f"⚠️ Групу <b>{GROUP}</b> не знайдено в графіку.\nДоступні: {keys}")
        sys.exit(1)

    now = datetime.now(TZ)
    today_d, tomorrow_d = g.get("today"), g.get("tomorrow")
    all_off = merge(intervals(today_d, OFF_TYPES) + intervals(tomorrow_d, OFF_TYPES))
    fp = st["fp"]
    first_run = not fp
    msgs: list[str] = []

    # 1) поточний графік: перший запуск або ручний запуск
    if SEND_NOW or first_run:
        head = "✅ <b>Бот працює</b>" if first_run else "📋 <b>Поточний графік</b>"
        msgs.append(f"{head} · група {GROUP}\n{now_line(all_off, now)}\n{SEP}\n"
                    + fmt_day(today_d, "Сьогодні") + f"\n{SEP}\n"
                    + fmt_day(tomorrow_d, "Завтра") + f"\n\n{LEGEND}")

    # 2) новий / змінений графік
    for d, title in ((today_d, "Сьогодні"), (tomorrow_d, "Завтра")):
        if not d or "date" not in d:
            continue
        dk = day_date(d).isoformat()
        new = json.dumps({"s": d.get("status"), "slots": d.get("slots", [])}, sort_keys=True)
        old = fp.get(dk)
        fp[dk] = new
        if first_run or old == new:
            continue
        if old is None:
            msgs.append(f"📅 <b>Опубліковано графік</b> · група {GROUP}\n\n{fmt_day(d, title)}")
        else:
            prev = json.loads(old)
            prev_d = dict(d, slots=prev.get("slots", []), status=prev.get("s"))
            was = ivs_text(merge(intervals(prev_d, OFF_TYPES)), day_date(d))
            msgs.append(f"🔄 <b>Графік змінено</b> · група {GROUP}\n\n{fmt_day(d, title)}"
                        f"\n\n↩️ <i>Було: {was}</i>")

    # 3) нагадування до відключення і до увімкнення
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
                        f"🔴 {a:%H:%M}–{hm(b, a.date())}  ·  {dur(b - a)}\n"
                        f"💡 Світло має з'явитися {when(b, now)}")
            else:
                tail = (f"🔴 Наступне відключення {when(nxt, now)}" if nxt
                        else "🟢 Далі відключень за графіком немає")
                text = f"💡 <b>Через ~{m} хв має з'явитися світло</b> (о {b:%H:%M})\n{tail}"
            msgs.append(text)
            rem.add(k)

    for msg in msgs:          # спершу відправка, потім збереження: при помилці повторимо наступного запуску
        send(msg)

    keep_from = (now.date() - timedelta(days=1)).isoformat()
    st["fp"] = {k: v for k, v in fp.items() if k >= keep_from}
    st["reminded"] = sorted(k for k in rem
                            if datetime.fromisoformat(k.split("|", 1)[1]) > now - timedelta(days=1))
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
