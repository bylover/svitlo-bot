"""
Проверка графика отключений (Киев, ДТЕК/YASNO) и уведомления в Telegram.
Запускается GitHub Actions каждые ~10 минут. Только стандартная библиотека Python.

Секреты (Settings -> Secrets and variables -> Actions):
    BOT_TOKEN   токен бота от @BotFather
    CHAT_ID     ваш chat id (от @userinfobot)
    GROUP       ваша группа, например 3.1
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
REMIND_MIN = int(os.environ.get("REMIND_MIN") or 45)   # окно напоминания (с запасом на задержки GitHub)
SEND_NOW = os.environ.get("SEND_NOW") == "1"            # ручной запуск — прислать график сразу
OFF_TYPES = {"Definite"} | ({"Possible"} if os.environ.get("INCLUDE_POSSIBLE") == "1" else set())
STATE_FILE = Path("state.json")

STATUS_TEXT = {
    "ScheduleApplies": "график действует",
    "PlannedShutdowns": "плановые отключения",
    "EmergencyShutdowns": "⚠️ экстренные отключения — график может не соблюдаться",
    "WaitingForSchedule": "ожидается график",
    "NoShutdowns": "отключений нет",
}


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
            print(f"API попытка {attempt}: {e}")
            _time.sleep(5)
    sys.exit("API YASNO недоступен")


def send(text: str) -> None:
    http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
              {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def day_date(d: dict):
    return datetime.fromisoformat(d["date"]).date()


def intervals(d: dict | None) -> list[list[datetime]]:
    if not d or "date" not in d:
        return []
    base = datetime.combine(day_date(d), time(0), TZ)
    return [[base + timedelta(minutes=s["start"]), base + timedelta(minutes=s["end"])]
            for s in d.get("slots", []) if s.get("type") in OFF_TYPES]


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


def fmt_day(d: dict | None, title: str) -> str:
    if not d or "date" not in d:
        return f"<b>{title}</b>: графика пока нет"
    dd = day_date(d)
    status = d.get("status") or ""
    lines = [f"<b>{title}, {dd:%d.%m}</b> — {STATUS_TEXT.get(status, status)}"]
    ivs = merge(intervals(d))
    if ivs:
        lines += [f"🔴 {a:%H:%M}–{hm(b, dd)}" for a, b in ivs]
        mins = int(sum((b - a for a, b in ivs), timedelta()).total_seconds()) // 60
        lines.append(f"Без света: {mins // 60} ч {mins % 60:02d} мин")
    else:
        lines.append("🟢 Отключений не запланировано")
    return "\n".join(lines)


def main() -> None:
    st = load_state()
    if st.get("group") != GROUP:              # сменили группу — начинаем заново
        st = {"group": GROUP, "fp": {}, "reminded": []}

    data = fetch()
    g = data.get(GROUP)
    if g is None:
        keys = ", ".join(sorted(k for k, v in data.items() if isinstance(v, dict)))
        send(f"⚠️ Группа {GROUP} не найдена в графике. Доступны: {keys}")
        sys.exit(1)

    now = datetime.now(TZ)
    fp = st["fp"]
    first_run = not fp
    msgs: list[str] = []

    if SEND_NOW or first_run:
        head = "✅ Бот работает" if first_run else "📋 Текущий график"
        msgs.append(f"{head}, группа {GROUP}\n\n" + fmt_day(g.get("today"), "Сегодня")
                    + "\n\n" + fmt_day(g.get("tomorrow"), "Завтра"))

    # новый / изменённый график
    for key, title in (("today", "Сегодня"), ("tomorrow", "Завтра")):
        d = g.get(key)
        if not d or "date" not in d:
            continue
        dk = day_date(d).isoformat()
        new = json.dumps({"s": d.get("status"), "slots": d.get("slots", [])}, sort_keys=True)
        old = fp.get(dk)
        fp[dk] = new
        if first_run or old == new:
            continue
        head = "📅 Опубликован график" if old is None else "🔄 График изменён"
        msgs.append(f"{head}\n{fmt_day(d, title)}")

    # напоминания до отключения и до включения
    rem = set(st["reminded"])
    for a, b in merge(intervals(g.get("today")) + intervals(g.get("tomorrow"))):
        events = (
            ("off", a, f"⏰ Скоро отключение: {a:%H:%M}–{hm(b, a.date())} (через ~{{m}} мин)"),
            ("on", b, f"💡 Свет должны вернуть в {hm(b, a.date())} (через ~{{m}} мин)"),
        )
        for ev, t, text in events:
            k = f"{ev}|{t.isoformat()}"
            left = (t - now).total_seconds() / 60
            if 0 < left <= REMIND_MIN and k not in rem:
                msgs.append(text.format(m=round(left)))
                rem.add(k)

    for m in msgs:          # сначала отправка, потом сохранение: при ошибке повторим в следующий запуск
        send(m)

    keep_from = (now.date() - timedelta(days=1)).isoformat()
    st["fp"] = {k: v for k, v in fp.items() if k >= keep_from}
    st["reminded"] = sorted(k for k in rem
                            if datetime.fromisoformat(k.split("|", 1)[1]) > now - timedelta(days=1))
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
