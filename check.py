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
Меню команд вимкнено; /start і /stop працюють для підписки (відповідь до ~10–20 хв).
Список підписників у state.json зашифровано ключем, похідним від BOT_TOKEN.
"""
import base64
import hashlib
import html
import json
import os
import random
import re
import sys
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Джерело графіка: "dtek" — дані сайту ДТЕК Київські електромережі (dtek-kem.com.ua) через
# публічне JSON-дзеркало, що оновлюється кожні ~5 хв; "yasno" — API YASNO (старий варіант).
SOURCE = (os.environ.get("SOURCE") or "dtek").strip().lower()
# Cloudflare Worker: меню/кнопки відповідають миттєво, зміни графіка шле щохвилини він.
# Якщо задані SYNC_URL і SYNC_KEY — GitHub бере підписників з Worker і не обробляє команди сам.
SYNC_URL = (os.environ.get("SYNC_URL") or "").strip().rstrip("/")
SYNC_KEY = (os.environ.get("SYNC_KEY") or "").strip()
WORKER_MODE = bool(SYNC_URL and SYNC_KEY)
REQ_KINO = (os.environ.get("REQ_KINO") or "").strip()     # Worker просить зібрати кіно для цього чату
REQ_YT = (os.environ.get("REQ_YT") or "").strip()
# Екстрені відключення: на сайті ДТЕК їх немає в доступних даних, тому стежимо за публічним каналом
# будинку (пости зі словом «екстрен…»). Порожнє значення CHANNEL вимикає.
CHANNEL = (os.environ.get("CHANNEL") if os.environ.get("CHANNEL") is not None else "yaskravyi_power").strip().lstrip("@")
EMERG_ACTIVE = False
WEEK: dict | None = None                  # тижневий прогноз ДТЕК для групи: {1..7: [[хв_від, хв_до], ...]}
DTEK_URL = "https://raw.githubusercontent.com/mrkaktuz/outages-data/data/dtek-kem.json"
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
WEATHER_HOUR = int(os.environ.get("WEATHER_HOUR") or 20)  # прогноз погоди на завтра щодня
LAT = float(os.environ.get("LAT") or 50.512)               # Мінський масив, Київ
LON = float(os.environ.get("LON") or 30.494)
PLACE = os.environ.get("PLACE") or "Мінський масив"
TMDB_KEY = (os.environ.get("TMDB_KEY") or "").strip()
OMDB_KEY = (os.environ.get("OMDB_KEY") or "").strip()
YT_KEY = (os.environ.get("YT_KEY") or "").strip()
KINO_WEEKDAY, YT_WEEKDAY, DIGEST_HOUR = 4, 0, 11          # пт і пн о 11:00
GEMINI_KEY = (os.environ.get("GEMINI_KEY") or "").strip()  # безкоштовний ключ aistudio.google.com
GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]
GIPHY_KEY = (os.environ.get("GIPHY_KEY") or "").strip()    # безкоштовний ключ developers.giphy.com
PARA_DAYS, PARA_HOUR = (2, 4), 18                          # «Для пари»: ср і пт о 18:00
COMMANDS = [("grafik", "💡 Графік світла"), ("pogoda", "🌤 Погода"),
            ("kino", "🍿 Кіно на вихідні"), ("youtube", "▶️ Топ YouTube"),
            ("para", "💞 Для пари 18+"),
            ("settings", "⚙️ Налаштування сповіщень"), ("help", "ℹ️ Інструкція"),
            ("stop", "🔕 Відписатися")]
# теми сповіщень, які підписник може вмикати/вимикати
TOPICS = [("svitlo", f"💡 Світло · група {GROUP}"), ("pogoda", "🌤 Погода"),
          ("kino", "🍿 Кіно"), ("yt", "▶️ YouTube"), ("para", "💞 Для пари 18+")]
ALL_TOPICS = [t for t, _ in TOPICS]
DEFAULT_TOPICS = [t for t in ALL_TOPICS if t != "para"]     # «Для пари» підписник вмикає сам
SETTINGS_TEXT = ("⚙️ <b>Налаштування сповіщень</b>\n"
                 "Натисніть, щоб увімкнути ✅ або вимкнути ⬜.\n"
                 f"💡 Світло — графік, зміни, нагадування, ранкове зведення (група {GROUP}, Мінський масив)\n"
                 "🌤 Погода — щодня о 20:00 · 🍿 Кіно — пт 11:00 · ▶️ YouTube — пн 11:00\n"
                 "💞 Для пари 18+ — поради для подружжя, ср і пт о 18:00 (за замовчуванням вимкнено)\n"
                 "<i>Бот відповідає із затримкою до 5–20 хв.</i>")
HELP_TEXT = ("ℹ️ <b>ЯК КОРИСТУВАТИСЯ БОТОМ</b>\n\n"
             "<b>Що вміє бот</b>\n<blockquote>"
             f"💡 <b>Світло</b> (група {GROUP}, Мінський масив) — графік ДТЕК на сьогодні й завтра; сповіщення про "
             "новий, змінений чи відкликаний графік, прогноз на тиждень, екстрені відключення; нагадування за "
             "30–45 хв; ранкове зведення о 10:00\n"
             "🌤 <b>Погода</b> — прогноз на завтра щодня о 20:00\n"
             "🍿 <b>Кіно</b> — 7 фільмів і 3 серіали на вихідні, щоп'ятниці об 11:00\n"
             "▶️ <b>YouTube</b> — топ-10 українського YouTube за тиждень, щопонеділка об 11:00\n"
             "💞 <b>Для пари 18+</b> — делікатні поради для подружжя і цікаві статті, ср і пт о 18:00 "
             "(вмикається в /settings)</blockquote>\n"
             "<b>Меню</b> (кнопка зліва від поля вводу)\n<blockquote>"
             "/grafik — графік світла зараз\n/pogoda — прогноз погоди\n/kino — нова підбірка кіно\n"
             "/youtube — топ YouTube цього тижня\n/para — нова порада для пари\n/settings — увімкнути або вимкнути сповіщення\n"
             "/help — ця інструкція\n/stop — відписатися від усього</blockquote>\n"
             "<b>Важливо</b>\n<blockquote>"
             "• Графік — за даними сайту ДТЕК. «За графіком» не означає, що світло фактично є чи немає\n"
             f"• Сповіщення про світло — лише для групи {GROUP}. Якщо у вас інша група, вимкніть «💡 Світло» в /settings\n"
             "• Свою групу можна перевірити на dtek-kem.com.ua → «Відсутня електроенергія?»\n"
             "• Бот відповідає на команди із затримкою до 5–20 хв</blockquote>")
KINO_HINT = "<i>Натисніть 👀 з номером, якщо вже бачили, — більше не запропоную</i>\n"
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
def http_json(url: str, payload: dict | None = None, headers: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "Mozilla/5.0", "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_dtek() -> dict:
    for attempt in range(1, 4):
        try:
            return http_json(DTEK_URL)
        except Exception as e:
            print(f"ДТЕК спроба {attempt}: {e}")
            _time.sleep(5)
    sys.exit("❌ Дані ДТЕК недоступні — повторимо наступного запуску")


def dtek_to_days(doc: dict, group: str, now: datetime) -> dict | None:
    """Перетворює дані ДТЕК у формат {today, tomorrow} як у YASNO (хвилини від початку доби)."""
    sch = (doc.get("schedules") or {}).get(group)
    if sch is None:
        return None
    fact_days = {datetime.fromtimestamp(int(k), TZ).date()
                 for k in ((doc.get("raw") or {}).get("fact") or {}).get("data", {}) if str(k).isdigit()}
    out = {}
    for key, day in (("today", now.date()), ("tomorrow", now.date() + timedelta(days=1))):
        base = datetime.combine(day, time(0), TZ)
        end_day = base + timedelta(days=1)
        slots = []
        for iv in sch.get("intervals", []):
            if iv.get("origin") != "fact":            # лише графік на добу, не тижневий прогноз
                continue
            a = max(datetime.fromisoformat(iv["start"]).astimezone(TZ), base)
            b = min(datetime.fromisoformat(iv["end"]).astimezone(TZ), end_day)
            if a < b:
                slots.append({"start": round((a - base).total_seconds() / 60),
                              "end": round((b - base).total_seconds() / 60),
                              "type": "Definite" if iv.get("kind") == "off" else "Possible"})
        ready = day in fact_days or bool(slots)
        out[key] = {"date": base.isoformat(), "slots": sorted(slots, key=lambda x: x["start"]),
                    "status": "ScheduleApplies" if ready else "WaitingForSchedule"}
    return out


def dtek_week(doc: dict, group: str) -> dict | None:
    """Тижневий прогноз ДТЕК (шаблон по днях тижня): можливі відключення у хвилинах доби."""
    dat = (((doc.get("raw") or {}).get("preset") or {}).get("data") or {}).get(f"GPV{group}")
    if not dat:
        return None
    out = {}
    for wd_s, hrs in dat.items():
        ivs = []
        for h_s, v in hrs.items():
            a = (int(h_s) - 1) * 60
            if v in ("no", "maybe"):
                ivs.append([a, a + 60])
            elif v in ("first", "mfirst"):
                ivs.append([a, a + 30])
            elif v in ("second", "msecond"):
                ivs.append([a + 30, a + 60])
        merged: list[list[int]] = []
        for a, b in sorted(ivs):
            if merged and merged[-1][1] >= a:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out[int(wd_s)] = merged
    return out


def min_text(ivs: list) -> str:
    f = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    return ", ".join(f"{f(a)}–{f(b)}" for a, b in ivs) or "без відключень"


def get_group(now: datetime) -> tuple[dict | None, str]:
    """Повертає (графік групи {today, tomorrow}, список доступних груп)."""
    if SOURCE == "yasno":
        data = fetch()
        return data.get(GROUP), ", ".join(sorted(k for k, v in data.items() if isinstance(v, dict)))
    doc = fetch_dtek()
    if not (doc.get("status") or {}).get("ok", True):
        print(f"ДТЕК статус: {doc.get('status')}")
    print(f"ДТЕК оновлено: {(doc.get('status') or {}).get('sourceUpdatedAt')} · дзеркало: {doc.get('updatedAt')}")
    groups = doc.get("groups") or sorted((doc.get("schedules") or {}).keys())
    global WEEK
    WEEK = dtek_week(doc, GROUP)
    return dtek_to_days(doc, GROUP, now), ", ".join(groups)


def fetch() -> dict:
    for attempt in range(1, 4):
        try:
            return http_json(API_URL)
        except Exception as e:
            print(f"API спроба {attempt}: {e}")
            _time.sleep(5)
    sys.exit("API YASNO недоступний")


class Rich(str):
    """Текст повідомлення з медіа: {"type": "preview"|"photo"|"animation", ...}."""
    media: dict | None = None


def rich(text: str, media: dict | None) -> str:
    if not media:
        return text
    r = Rich(text)
    r.media = media
    return r


def plain_len(t: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", t)))


def tg_multipart(method: str, fields: dict, file_field: str, filename: str, data: bytes, ctype: str) -> dict:
    boundary = "----svitlo" + hashlib.md5(os.urandom(8)).hexdigest()
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def send_media(chat_id: str, text: str, media: dict, markup: dict | None) -> None:
    if media["type"] == "preview":               # прев'ю статті з її обкладинкою над текстом
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "link_preview_options": {"url": media["url"], "prefer_large_media": True, "show_above_text": True}}
        if markup:
            payload["reply_markup"] = markup
        http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", payload)
        return
    photo = media["type"] == "photo"
    field, method = ("photo", "sendPhoto") if photo else ("animation", "sendAnimation")
    fits = plain_len(text) <= 1000                # підпис до фото — до 1024 символів
    caption = text if fits else text.split("\n", 1)[0]
    fields = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if media.get("file_id"):                     # уже завантажене — повторно не вантажимо
        r = http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", {**fields, field: media["file_id"]})
    elif photo:
        r = tg_multipart(method, fields, field, "para.jpg", media["bytes"], "image/jpeg")
    else:
        r = http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", {**fields, field: media["url"]})
    res = r.get("result") or {}
    fid = ((res.get("photo") or [{}])[-1].get("file_id") if photo
           else (res.get("animation") or res.get("document") or {}).get("file_id"))
    if fid:
        media["file_id"] = fid
    if not fits:
        send(chat_id, text.split("\n", 1)[1] if "\n" in text else text, markup)


def send(chat_id: str, text: str, markup: dict | None = None) -> None:
    media = getattr(text, "media", None)
    if media:
        try:
            send_media(chat_id, str(text), media, markup)
            return
        except Exception as e:                    # медіа не вдалося — надсилаємо просто текст
            print(f"Медіа ({media.get('type')}): {err_text(e)}")
            text = str(text)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", payload)


def tg(method: str, payload: dict) -> None:
    try:
        http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", payload)
    except Exception as e:
        print(f"{method}: {e}")


def esc(x) -> str:
    return html.escape(str(x or ""), quote=False)


def num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + " млн"
    if n >= 10_000:
        return f"{round(n / 1000)} тис"
    return f"{n:,}".replace(",", " ")


# ---------- кіно (TMDB + OMDb) ----------
MOVIE_N, TV_N = 7, 3


def tmdb(path: str, **params) -> dict:
    headers = None
    if len(TMDB_KEY) > 60:                      # токен читання (v4) замість ключа v3
        headers = {"Authorization": f"Bearer {TMDB_KEY}"}
    else:
        params["api_key"] = TMDB_KEY
    return http_json(f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}", headers=headers)


def omdb(imdb_id: str | None) -> tuple[float | None, int | None]:
    if not (OMDB_KEY and imdb_id):
        return None, None
    try:
        r = http_json(f"https://www.omdbapi.com/?i={imdb_id}&apikey={OMDB_KEY}")
    except Exception as e:
        print(f"OMDb: {e}")
        return None, None
    imdb = float(r["imdbRating"]) if re.fullmatch(r"\d+(\.\d+)?", r.get("imdbRating") or "") else None
    rt = next((int(x["Value"].rstrip("%")) for x in r.get("Ratings", [])
               if x.get("Source") == "Rotten Tomatoes" and x.get("Value", "").rstrip("%").isdigit()), None)
    return imdb, rt


def trailer(videos: dict) -> str | None:
    res = [v for v in (videos or {}).get("results", []) if v.get("site") == "YouTube"]
    res.sort(key=lambda v: (v.get("type") != "Trailer", v.get("iso_639_1") != "uk"))
    return f"https://youtu.be/{res[0]['key']}" if res else None


def pick_titles(kind: str, need: int, seen: set) -> list[dict]:
    """kind: movie | tv. Високий рейтинг, але не надто «заїжджені» (обмеження кількості голосів)."""
    if kind == "movie":
        base = {"vote_average.gte": 7.2, "vote_count.gte": 300, "vote_count.lte": 6000,
                "with_runtime.gte": 80, "without_genres": "16,10751,99",
                "primary_release_date.gte": "2005-01-01"}
        min_imdb, min_rt = 7.0, 75
    else:
        base = {"vote_average.gte": 7.5, "vote_count.gte": 150, "vote_count.lte": 4000,
                "without_genres": "16,10762,10763,10764,10767,99", "first_air_date.gte": "2012-01-01"}
        min_imdb, min_rt = 7.5, 80
    cands = []
    for page in random.sample(range(1, 9), 3):
        try:
            r = tmdb(f"/discover/{kind}", language="uk-UA", sort_by="popularity.desc",
                     include_adult="false", page=page, **base)
            cands += r.get("results", [])
        except Exception as e:
            print(f"TMDB discover: {e}")
    random.shuffle(cands)
    out, used = [], set()
    for c in cands:
        key = f"{kind[0]}{c['id']}"
        if key in seen or key in used:
            continue
        used.add(key)
        try:
            d = tmdb(f"/{kind}/{c['id']}", language="uk-UA",
                     append_to_response="external_ids,videos", include_video_language="uk,en")
            if not d.get("overview"):
                d["overview"] = tmdb(f"/{kind}/{c['id']}", language="en-US").get("overview", "")
        except Exception as e:
            print(f"TMDB details: {e}")
            continue
        imdb, rt = omdb((d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id"))
        if (imdb is not None and imdb < min_imdb) or (rt is not None and rt < min_rt):
            continue
        out.append({"key": key, "kind": kind, "d": d, "imdb": imdb, "rt": rt})
        if len(out) >= need:
            break
    return out


def kino_item(i: int, it: dict, ov_len: int = 160) -> str:
    d = it["d"]
    is_tv = it["kind"] == "tv"
    title = d.get("name") if is_tv else d.get("title")
    year = (d.get("first_air_date") if is_tv else d.get("release_date") or "")[:4]
    genres = ", ".join(g["name"].lower() for g in d.get("genres", [])[:3])
    if is_tv:
        ns = d.get("number_of_seasons") or 0
        length = f"📺 серіал · {ns} сез."
    else:
        rt_min = d.get("runtime") or 0
        length = f"⏱ {rt_min // 60} год {rt_min % 60:02d} хв" if rt_min else ""
    rating = [f"⭐ IMDb <b>{it['imdb']}</b>" if it["imdb"] else f"⭐ TMDB <b>{d.get('vote_average', 0):.1f}</b>"]
    if it["rt"] is not None:
        rating.append(f"🍅 <b>{it['rt']}%</b>")
    ov = (d.get("overview") or "").strip() if ov_len else ""
    if len(ov) > ov_len:
        ov = ov[:ov_len - 3].rsplit(" ", 1)[0] + "…"
    lines = [f"{'📺' if is_tv else '🎬'} <b>{i}. {esc(title)}</b> ({year})",
             " · ".join(x for x in (esc(genres), length) if x),
             " · ".join(rating)]
    if ov:
        lines.append(f"📝 {esc(ov)}")
    tr = trailer(d.get("videos"))
    if tr:
        lines.append(f'▶️ <a href="{tr}">Трейлер</a>')
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def build_kino(st: dict) -> tuple[str, dict] | None:
    if not TMDB_KEY:
        print("TMDB_KEY не задано — кіно пропущено")
        return None
    seen = set(st.get("kino_seen", []))
    items = pick_titles("movie", MOVIE_N, seen) + pick_titles("tv", TV_N, seen)
    if len(items) < 3:
        return None
    st["kino_seen"] = (st.get("kino_seen", []) + [x["key"] for x in items])[-600:]
    head = "🍿 <b>КІНО НА ВИХІДНІ</b>\n" + KINO_HINT
    for ov_len in (160, 110, 70, 0):            # ліміт Telegram — 4096 символів
        text = head + "\n".join(kino_item(i, it, ov_len) for i, it in enumerate(items, 1))
        if len(text) <= 4000:
            break
    btns = [{"text": f"👀 {i}", "callback_data": f"seen:{it['key']}"} for i, it in enumerate(items, 1)]
    markup = {"inline_keyboard": [btns[k:k + 5] for k in range(0, len(btns), 5)]}
    return text, markup


# ---------- «Для пари» (Gemini + Google News RSS) ----------
PARA_THEMES = [
    ("нова поза", "одна нова позиція для подружжя: як називається, чим цікава, що врахувати для комфорту обох"),
    ("прелюдія", "ідея для довшої й ніжнішої прелюдії"),
    ("масаж", "розслаблювальний масаж для партнера: техніка та атмосфера"),
    ("побачення вдома", "ідея романтичного вечора вдома для двох"),
    ("розмова", "як делікатно поговорити про бажання і фантазії"),
    ("нова поза", "ще одна нова позиція: варіант для неспішного вечора"),
    ("гра", "легка рольова або тактильна гра для пари"),
    ("атмосфера", "як створити атмосферу: світло, музика, аромати, дотики"),
    ("після", "як продовжити близькість після: обійми, турбота, розмова"),
    ("сюрприз", "невеликий романтичний сюрприз для партнера чи партнерки"),
]
PARA_FALLBACK = [
    ("Повільний вечір", "• Домовтеся, що сьогодні нікуди не поспішаєте\n• Почніть з 10 хвилин масажу плечей і спини\n"
     "• Говоріть одне одному, що подобається — це заводить більше, ніж здається"),
    ("Побачення без телефонів", "• Вимкніть сповіщення на весь вечір\n• Приготуйте разом щось просте й смачне\n"
     "• Завершіть вечір ванною чи душем удвох"),
    ("Гра «три бажання»", "• Кожен по черзі називає одне маленьке бажання на вечір\n• Без оцінок і без тиску — "
     "лише те, що комфортно обом\n• Наступного разу міняйтеся ролями"),
    ("Масаж зі свічками", "• Приглушене світло, тепла олія, спокійна музика\n• Повільні рухи від плечей до стоп\n"
     "• Питайте, де приємніше, — і слухайте відповідь"),
    ("Відверта розмова", "• Оберіть спокійний момент, не перед сном після важкого дня\n• Почніть з того, що вам "
     "найбільше подобається у вашій близькості\n• Запитайте, що партнер хотів би спробувати"),
    ("Нове місце", "• Змініть звичну кімнату чи обстановку — навіть перестановка додає новизни\n"
     "• Подбайте про затишок: плед, подушки, приглушене світло"),
    ("Ранкова ніжність", "• Прокиньтеся на 20 хвилин раніше\n• Без поспіху: обійми, поцілунки, кава в ліжко\n"
     "• Чудовий спосіб почати вихідний день"),
    ("Зав'язані очі", "• Легка пов'язка посилює інші відчуття\n• Чергуйте ніжні дотики, тепло дихання, шовк\n"
     "• Домовтеся про стоп-слово і вчасно знімайте пов'язку"),
]


def gemini(prompt: str) -> str | None:
    if not GEMINI_KEY:
        return None
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 1.0, "maxOutputTokens": 800}}
    for model in GEMINI_MODELS:
        try:
            r = http_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                          f"?key={urllib.parse.quote(GEMINI_KEY)}", body)
            parts = ((r.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            text = "".join(p_.get("text", "") for p_ in parts).strip()
            if text:
                return text
            print(f"Gemini {model}: порожня відповідь ({(r.get('candidates') or [{}])[0].get('finishReason')})")
        except Exception as e:
            print(f"Gemini {model}: {err_text(e)}")
    return None


def para_news(st: dict, now: datetime, limit: int = 3) -> list[tuple[str, str]]:
    """Свіжі статті за тиждень з Google News (українські сайти)."""
    import xml.etree.ElementTree as ET
    seen = set(st.get("para_links", []))
    items = []
    for q in ("поради для пари стосунки", "секс поради пара", "інтимні стосунки подружжя"):
        url = ("https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": f"{q} when:7d", "hl": "uk", "gl": "UA", "ceid": "UA:uk"}))
        try:
            root = ET.fromstring(http_text(url))
        except Exception as e:
            print(f"Google News: {e}")
            continue
        for it in root.iter("item"):
            link, title = it.findtext("link") or "", (it.findtext("title") or "").strip()
            if link and title and link not in seen:
                items.append((title, link))
                seen.add(link)
    random.shuffle(items)
    out = items[:limit]
    st["para_links"] = (st.get("para_links", []) + [lk for _, lk in out])[-300:]
    return out


PARA_IMG = {
    "нова поза": "a loving couple in a tender embrace, silhouettes against warm window light",
    "прелюдія": "a couple slowly dancing close together in a dim cozy room",
    "масаж": "a woman giving her partner a relaxing shoulder massage, candles, towels, spa mood",
    "побачення вдома": "a romantic dinner for two at home, candles, wine glasses, fairy lights",
    "розмова": "a couple talking softly face to face on a sofa under a blanket, holding hands",
    "гра": "a playful couple laughing and hugging on a bed with pillows, cozy evening",
    "атмосфера": "a cozy bedroom with candles, rose petals and soft warm light",
    "після": "a couple cuddling under a blanket, peaceful, morning light",
    "сюрприз": "a man giving a woman a small gift box with flowers, both smiling",
}
GIF_Q = {"нова поза": "romantic couple", "прелюдія": "couple dancing", "масаж": "couple massage",
         "побачення вдома": "date night", "розмова": "couple love talk", "гра": "couple flirting",
         "атмосфера": "romantic candles", "після": "cuddle", "сюрприз": "romantic surprise"}


def resolve_url(url: str) -> str | None:
    """Справжня адреса статті (Google News перенаправляє); None — якщо не вдалося."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            final = r.geturl()
        return None if "news.google." in final else final
    except Exception:
        return None


def para_visual(st: dict, label: str, news: list) -> dict | None:
    """Картинка до поради: по черзі кожен варіант, у випадковому порядку."""
    avail = (["preview"] if news else []) + (["ai"] if WORKER_MODE else []) + (["gif"] if GIPHY_KEY else [])
    bag = [x for x in st.get("para_bag", []) if x in avail] or random.sample(avail, len(avail))
    while bag:
        kind = bag.pop(0)
        try:
            if kind == "preview":
                for _, link in news:
                    real = resolve_url(link)
                    if real:
                        st["para_bag"] = bag
                        return {"type": "preview", "url": real}
            elif kind == "ai":
                scene = PARA_IMG.get(label, PARA_IMG["атмосфера"])
                r = http_json(f"{SYNC_URL}/img?key={urllib.parse.quote(SYNC_KEY)}", {"prompt":
                    f"{scene}, romantic tasteful artistic illustration, soft warm colors, gentle light, "
                    "fully clothed, no nudity, elegant, cinematic"})
                if r.get("image"):
                    st["para_bag"] = bag
                    return {"type": "photo", "bytes": base64.b64decode(r["image"])}
                print(f"ІІ-картинка: {r.get('error')}")
            elif kind == "gif":
                r = http_json("https://api.giphy.com/v1/gifs/search?" + urllib.parse.urlencode(
                    {"api_key": GIPHY_KEY, "q": GIF_Q.get(label, "romantic couple"), "limit": 25, "rating": "pg"}))
                data = [g for g in r.get("data", []) if (g.get("images") or {}).get("original")]
                if data:
                    o = random.choice(data)["images"]["original"]
                    st["para_bag"] = bag
                    return {"type": "animation", "url": o.get("mp4") or o.get("url")}
        except Exception as e:
            print(f"Картинка ({kind}): {err_text(e)}")
    st["para_bag"] = []
    return None


def build_para(st: dict, now: datetime, with_news: bool = True) -> str:
    i = st.get("para_i", 0)
    label, theme = PARA_THEMES[i % len(PARA_THEMES)]
    st["para_i"] = i + 1
    recent = st.get("para_titles", [])[-30:]
    prompt = (
        "Ти — тактовний консультант зі стосунків для дорослої подружньої пари (чоловік і дружина). "
        f"Напиши одну практичну пораду на тему: {theme}. "
        "Формат: перший рядок — короткий заголовок до 6 слів без лапок; далі 3–5 пунктів, кожен з нового "
        "рядка й починається з «• ». Тон теплий, легкий, трохи з гумором, романтичний; без вульгарності "
        "й анатомічних подробиць; наголос на згоді, комфорті та довірі обох. Мова — українська. "
        "Без Markdown, без зірочок і решіток. "
        + (f"Не повторюй ці теми: {'; '.join(recent)}." if recent else ""))
    text = gemini(prompt)
    if text:
        text = re.sub(r"[*#_`]+", "", text).strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        title, body = lines[0][:80], "\n".join(lines[1:])
    else:                                          # без ключа чи при відмові моделі — вбудована база
        j = st.get("para_fb", 0)
        title, body = PARA_FALLBACK[j % len(PARA_FALLBACK)]
        st["para_fb"] = j + 1
    st["para_titles"] = (recent + [title])[-30:]
    msg = (f"💞 <b>ДЛЯ ВАС ДВОХ</b> · {label}\n"
           f"<blockquote><b>{esc(title)}</b>\n{esc(body)}</blockquote>")
    news = para_news(st, now) if with_news else []
    if news:
        msg += "\n📰 <b>Цікаве за тиждень</b>\n" + "\n".join(
            f'• <a href="{html.escape(lk)}">{esc(t)}</a>' for t, lk in news)
    return rich(msg, para_visual(st, label, news))


# ---------- YouTube (Data API v3) ----------
UA_LETTERS = re.compile("[іїєґІЇЄҐ]")


def iso_dur(s: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def dur_text(sec: int) -> str:
    h, m = divmod(sec // 60, 60)
    return f"{h} год {m:02d} хв" if h else f"{m} хв"


def build_youtube(st: dict, now: datetime) -> str | None:
    if not YT_KEY:
        print("YT_KEY не задано — YouTube пропущено")
        return None
    monday = datetime.combine(now.date() - timedelta(days=now.weekday()), time(0), TZ)
    start, end = monday - timedelta(days=7), monday
    ids: list[str] = []
    for vd in ("medium", "long"):
        try:
            r = http_json("https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode({
                "part": "id", "type": "video", "order": "viewCount", "regionCode": "UA",
                "relevanceLanguage": "uk", "videoDuration": vd, "maxResults": 50, "key": YT_KEY,
                "publishedAfter": start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "publishedBefore": end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")}))
            ids += [x["id"]["videoId"] for x in r.get("items", []) if x.get("id", {}).get("videoId")]
        except Exception as e:
            print(f"YouTube search: {e}")
    seen = set(st.get("yt_seen", []))
    ids = [i for i in dict.fromkeys(ids) if i not in seen]
    vids = []
    for k in range(0, len(ids), 50):
        try:
            r = http_json("https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode({
                "part": "snippet,statistics,contentDetails,status", "id": ",".join(ids[k:k + 50]),
                "key": YT_KEY}))
            vids += r.get("items", [])
        except Exception as e:
            print(f"YouTube videos: {e}")
    good = []
    for v in vids:
        sn, stt = v.get("snippet", {}), v.get("statistics", {})
        lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower()
        title = sn.get("title", "")
        if sn.get("categoryId") == "25":                       # новини та політика
            continue
        if (v.get("status") or {}).get("madeForKids"):         # дитячий контент
            continue
        if lang.startswith("ru"):
            continue
        if not (lang.startswith("uk") or (not lang and UA_LETTERS.search(title))):
            continue
        if iso_dur((v.get("contentDetails") or {}).get("duration")) <= 60:  # Shorts
            continue
        good.append(v)
    # популярність: перегляди + коментарі (1 коментар ≈ 100 переглядів — він показує залученість глядачів)
    good.sort(key=lambda v: int(v["statistics"].get("viewCount", 0))
              + 100 * int(v["statistics"].get("commentCount", 0) or 0), reverse=True)
    top, per_channel = [], {}
    for v in good:
        ch = v["snippet"].get("channelId")
        if per_channel.get(ch, 0) >= 2:                        # не більше 2 відео з каналу
            continue
        per_channel[ch] = per_channel.get(ch, 0) + 1
        top.append(v)
        if len(top) == 10:
            break
    if not top:
        return None
    st["yt_seen"] = (st.get("yt_seen", []) + [v["id"] for v in top])[-800:]
    items = []
    for i, v in enumerate(top, 1):
        sn, stt = v["snippet"], v["statistics"]
        views, comments = int(stt.get("viewCount", 0)), int(stt.get("commentCount", 0) or 0)
        items.append("<blockquote>"
                     f"▶️ <b>{i}. {esc(sn['title'])}</b>\n"
                     f"📺 {esc(sn.get('channelTitle'))} · ⏱ {dur_text(iso_dur(v['contentDetails']['duration']))}\n"
                     f"👁 <b>{num(views)}</b> переглядів · 💬 <b>{num(comments)}</b> коментарів\n"
                     f'🔗 <a href="https://youtu.be/{v["id"]}">Дивитися</a></blockquote>')
    label = f"{start:%d.%m}–{(end - timedelta(days=1)):%d.%m}"
    return f"▶️ <b>ТОП-10 УКРАЇНСЬКОГО YOUTUBE · {label}</b>\n" + "\n".join(items)


# ---------- погода (Open-Meteo, без ключа) ----------
WMO = {0: ("☀️", "ясно"), 1: ("🌤", "переважно ясно"), 2: ("⛅", "мінлива хмарність"),
       3: ("☁️", "хмарно"), 45: ("🌫", "туман"), 48: ("🌫", "туман"),
       51: ("🌦", "мряка"), 53: ("🌦", "мряка"), 55: ("🌧", "сильна мряка"),
       56: ("🌧", "крижана мряка"), 57: ("🌧", "крижана мряка"),
       61: ("🌦", "невеликий дощ"), 63: ("🌧", "дощ"), 65: ("🌧", "сильний дощ"),
       66: ("🌧", "крижаний дощ"), 67: ("🌧", "крижаний дощ"),
       71: ("🌨", "невеликий сніг"), 73: ("🌨", "сніг"), 75: ("❄️", "сильний сніг"),
       77: ("🌨", "снігова крупа"), 80: ("🌦", "короткочасний дощ"), 81: ("🌧", "зливи"),
       82: ("⛈", "сильні зливи"), 85: ("🌨", "снігопад"), 86: ("❄️", "сильний снігопад"),
       95: ("⛈", "гроза"), 96: ("⛈", "гроза з градом"), 99: ("⛈", "гроза з градом")}
PERIODS = [("🌙", "ніч", 0, 6), ("🌅", "ранок", 6, 12), ("☀️", "день", 12, 18), ("🌆", "вечір", 18, 24)]


def fetch_weather() -> dict:
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={LAT}&longitude={LON}&timezone=Europe%2FKyiv&wind_speed_unit=ms&forecast_days=3"
           "&hourly=temperature_2m,precipitation_probability,weather_code,wind_speed_10m,wind_gusts_10m"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
           "sunrise,sunset,uv_index_max")
    return http_json(url)


def t_fmt(v: float) -> str:
    v = round(v)
    return f"+{v}°" if v > 0 else f"{v}°"


def weather_text(w: dict, day, title: str) -> str:
    h, dl = w["hourly"], w["daily"]
    key = day.isoformat()
    rows, rainy, gust_max, snow = [], [], 0.0, False
    for icon, name, h1, h2 in PERIODS:
        idx = [i for i, t in enumerate(h["time"])
               if t.startswith(key) and h1 <= int(t[11:13]) < h2]
        if not idx:
            continue
        temps = [h["temperature_2m"][i] for i in idx]
        code = max(h["weather_code"][i] or 0 for i in idx)
        prob = max(h["precipitation_probability"][i] or 0 for i in idx)
        wind = max(h["wind_speed_10m"][i] or 0 for i in idx)
        gust_max = max(gust_max, max(h["wind_gusts_10m"][i] or 0 for i in idx))
        snow = snow or code in (71, 73, 75, 77, 85, 86)
        lo, hi = t_fmt(min(temps)), t_fmt(max(temps))
        temp = lo if lo == hi else f"{lo}…{hi}"
        w_icon, w_txt = WMO.get(code, ("🌡", ""))
        extra = (f" · 💧{prob}%" if prob >= 20 else "") + (f" · 💨{round(wind)} м/с" if wind >= 7 else "")
        rows.append(f"{icon} {name}: <b>{temp}</b> {w_icon} {w_txt}{extra}")
        if prob >= 50:
            rainy.append(name)
    if key not in dl["time"]:
        return f"🌤 <b>ПОГОДА · {title.upper()}</b>\nПрогнозу ще немає"
    d = dl["time"].index(key)
    tmin, tmax = dl["temperature_2m_min"][d], dl["temperature_2m_max"][d]
    lines = [f"🌤 <b>ПОГОДА {title.upper()} · {WD_SHORT[day.weekday()]} {day:%d.%m}</b>",
             f"📍 {PLACE} · <b>{t_fmt(tmin)}…{t_fmt(tmax)}</b>",
             "<blockquote>" + "\n".join(rows) + "</blockquote>"]
    tips = []
    if rainy:
        tips.append(f"☂️ <b>Візьміть парасольку</b> ({', '.join(rainy)})")
    if snow:
        tips.append("❄️ <b>Можливий сніг</b> — обережно на дорогах")
    if tmin <= 0:
        tips.append(f"🧤 <b>Мороз до {t_fmt(tmin)}</b>")
    if gust_max >= 15:
        tips.append(f"💨 <b>Пориви вітру до {round(gust_max)} м/с</b>")
    if d > 0:
        diff = round(tmax - dl["temperature_2m_max"][d - 1])
        if diff <= -4:
            tips.append(f"🧥 <b>Холодніше, ніж напередодні, на {abs(diff)}°</b>")
        elif diff >= 4:
            tips.append(f"😎 Тепліше, ніж напередодні, на {diff}°")
    if (dl.get("uv_index_max") or [0] * 3)[d] and dl["uv_index_max"][d] >= 6:
        tips.append("🧴 Високий УФ-індекс")
    lines += tips or ["👌 Без погодних сюрпризів"]
    sr, ss = dl["sunrise"][d][11:16], dl["sunset"][d][11:16]
    lines.append(f"<i>🌅 схід {sr} · 🌇 захід {ss}</i>")
    return "\n".join(lines)


def weather_for(now: datetime) -> str | None:
    """До 18:00 — на сьогодні, після — на завтра."""
    try:
        w = fetch_weather()
    except Exception as e:
        print(f"Погода: {e}")
        return None
    if now.hour < 18:
        return weather_text(w, now.date(), "сьогодні")
    return weather_text(w, now.date() + timedelta(days=1), "на завтра")


def set_commands() -> None:
    http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands",
              {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})


# ---------- екстрені відключення (публічний Telegram-канал) ----------
def http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def channel_posts() -> list[tuple[int, datetime, str]]:
    page = http_text(f"https://t.me/s/{CHANNEL}")
    posts = []
    for chunk in page.split('data-post="')[1:]:
        m_id = re.match(r'[^/"]+/(\d+)"', chunk)
        m_tx = re.search(r'class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', chunk, re.S)
        m_dt = re.search(r'<time datetime="([^"]+)"', chunk)
        if not (m_id and m_tx and m_dt):
            continue
        text = re.sub(r"<br\s*/?>", "\n", m_tx.group(1))
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        posts.append((int(m_id.group(1)), datetime.fromisoformat(m_dt.group(1)).astimezone(TZ), text))
    return sorted(posts)


CANCEL_WORDS = ("скасов", "відмін", "припин", "завершен", "не застосов", "більше не",
                "повертаємось до граф", "повертаємося до граф")


def emerg_kind(text: str) -> str | None:
    t = text.lower()
    if "екстрен" not in t:
        return None
    return "off" if any(w in t for w in CANCEL_WORDS) else "on"


def emergency_check(st: dict, now: datetime) -> list[str]:
    """Нові пости про екстрені відключення -> повідомлення. Помилка не ламає бота."""
    if not CHANNEL:
        return []
    ch = st.get("emerg") or {}
    if ch.get("on") and ch.get("since") and now - datetime.fromisoformat(ch["since"]) > timedelta(hours=24):
        ch["on"] = False                                   # страховка, якщо не було поста про скасування
    try:
        posts = channel_posts()
    except Exception as e:
        print(f"Канал {CHANNEL}: {e}")
        st["emerg"] = ch
        return []
    if not posts:
        st["emerg"] = ch
        return []
    if "last" not in ch:                                   # перший запуск — лише запам'ятати стан
        ch["last"] = posts[-1][0]
        for pid, dt, text in reversed(posts):
            k = emerg_kind(text)
            if k and now - dt < timedelta(hours=24):
                ch["on"], ch["since"] = k == "on", dt.isoformat()
                break
        st["emerg"] = ch
        return []
    out = []
    for pid, dt, text in posts:
        if pid <= ch["last"]:
            continue
        k = emerg_kind(text)
        if not k:
            continue
        excerpt = text if len(text) <= 350 else text[:347].rsplit(" ", 1)[0] + "…"
        src = f'<a href="https://t.me/{CHANNEL}/{pid}">джерело</a> · {dt:%H:%M}'
        if k == "on":
            ch["on"], ch["since"] = True, dt.isoformat()
            out.append("🚨🚨🚨 <b>ЕКСТРЕНІ ВІДКЛЮЧЕННЯ!</b> 🚨🚨🚨\n"
                       "Графік може не діяти · 🔋 зарядіть пристрої\n"
                       f"<blockquote>{esc(excerpt)}</blockquote>\n{src}")
        else:
            ch["on"], ch["since"] = False, dt.isoformat()
            out.append("✅✅ <b>ЕКСТРЕНІ ВІДКЛЮЧЕННЯ СКАСОВАНО</b> ✅✅\n"
                       "Знову діє графік ДТЕК\n"
                       f"<blockquote>{esc(excerpt)}</blockquote>\n{src}")
    ch["last"] = max(ch["last"], posts[-1][0])
    st["emerg"] = ch
    return out


def get_updates(offset: int | None) -> list[dict]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?timeout=0&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D"
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
        return f"<blockquote>📆 <b>{title.upper()}</b>\n⏳ <i>графік ще не сформовано</i></blockquote>"
    dd = day_date(d)
    status = d.get("status") or ""
    lines = [f"📆 <b>{title.upper()}, {WD_SHORT[dd.weekday()]} {dd:%d.%m}</b>"]
    if status == "WaitingForSchedule" and not intervals(d, OFF_TYPES | POSSIBLE_TYPES):
        return "<blockquote>" + lines[0] + "\n⏳ <i>графік ще не сформовано</i></blockquote>"
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
            return f"⏳ За графіком зараз немає світла до <b>{hm(b, a.date())}</b> (ще {dur(b - now)})"
    nxt = next((a for a, _ in off if a > now), None)
    if nxt:
        day = "" if nxt.date() == now.date() else "завтра "
        return f"⏳ За графіком наступне відключення {day}о <b>{nxt:%H:%M}</b> (через {dur(nxt - now)})"
    return "⏳ За графіком відключень більше не буде"


def summary(head: str, today_d, tomorrow_d, all_off, now) -> str:
    emerg = "🚨 <b>Діють екстрені відключення</b> — графік може не діяти\n" if EMERG_ACTIVE else ""
    return (f"{head}\n{emerg}{now_line(all_off, now)}\n"
            + fmt_day(today_d, "Сьогодні") + "\n" + fmt_day(tomorrow_d, "Завтра"))


def settings_markup(on: list[str]) -> dict:
    b = [{"text": f"{'✅' if t in on else '⬜'} {label}", "callback_data": f"t:{t}"} for t, label in TOPICS]
    return {"inline_keyboard": [b[0:1], b[1:2], b[2:4], b[4:5]]}


# ---------- головна логіка ----------
def err_text(e: Exception) -> str:
    """Текст помилки разом із поясненням від Telegram/API (якщо є)."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            return f"{e.code} {e.read().decode('utf-8', 'replace')[:300]}"
        except Exception:
            return str(e)
    return str(e)


def check_config() -> None:
    """Зрозумілі повідомлення в лозі, якщо секрет заданий неправильно."""
    problems = []
    if not re.fullmatch(r"\d+:[\w-]{30,}", BOT_TOKEN):
        problems.append("BOT_TOKEN порожній або неправильний (має бути вигляду 123456789:AA...)")
    if not CHAT_IDS or not all(re.fullmatch(r"-?\d+", c) for c in CHAT_IDS):
        problems.append("CHAT_ID порожній або неправильний (лише цифри)")
    if WIFE_ID and not re.fullmatch(r"-?\d+", WIFE_ID):
        problems.append("WIFE_ID неправильний (лише цифри)")
    if not re.fullmatch(r"\d+\.\d+", GROUP):
        problems.append("GROUP неправильна (вигляду 4.1)")
    print(f"Налаштування: CHAT_ID={','.join(c[:4] + '…' for c in CHAT_IDS)} · GROUP={GROUP} · "
          f"дружина={'так' if WIFE_ID else 'ні'} · Worker={'так' if WORKER_MODE else 'ні'} · "
          f"кіно={'так' if TMDB_KEY else 'ні'} · YouTube={'так' if YT_KEY else 'ні'}")
    for p_ in problems:
        print("❌ " + p_)
    if problems:
        sys.exit("❌ Виправте секрети в Settings → Secrets and variables → Actions")


def main() -> None:
    check_config()
    st = load_state()
    if st.get("group") != GROUP or st.get("source", "yasno") != SOURCE:
        # змінили групу або джерело — графік заново (без хибного «змінено»), підписники лишаються
        st.update(group=GROUP, fp={}, reminded=[])
    raw_subs = dec(st.get("subs"))               # {chat_id: [теми]}; старий формат — список id
    subs: dict[str, list[str]] = ({str(x): list(DEFAULT_TOPICS) for x in raw_subs} if isinstance(raw_subs, list)
                                  else {str(k): list(v) for k, v in raw_subs.items()})
    worker_alive = False
    worker_req: dict = {}
    if WORKER_MODE:                              # підписники, «бачили» і «пульс» — з Cloudflare Worker
        try:
            ex = http_json(f"{SYNC_URL}/export?key={urllib.parse.quote(SYNC_KEY)}&take=1")
            worker_req = ex.get("req") or {}
            subs = {str(k): list(v) for k, v in (ex.get("subs") or {}).items()}
            if ex.get("seen"):
                st["kino_seen"] = list(dict.fromkeys(st.get("kino_seen", []) + list(ex["seen"])))[-600:]
            worker_alive = _time.time() * 1000 - float(ex.get("beat") or 0) < 25 * 60 * 1000
        except Exception as e:
            print(f"Worker недоступний: {e} — зміни графіка надішле GitHub")
        print(f"Worker: {'працює' if worker_alive else 'НЕ відповідає'} · підписників: {len(subs)}")
    subs_before = json.dumps(subs, sort_keys=True)

    st["source"] = SOURCE
    g, keys = get_group(datetime.now(TZ))
    if g is None:
        print(f"❌ Групу {GROUP} не знайдено. Доступні: {keys}")
        try:
            send(CHAT_IDS[0], f"⚠️ Групу <b>{GROUP}</b> не знайдено в графіку.\nДоступні: {keys}")
        except Exception as e:
            print(f"Telegram: {err_text(e)}")
        sys.exit(1)

    now = datetime.now(TZ)
    today_key = now.date().isoformat()
    today_d, tomorrow_d = g.get("today"), g.get("tomorrow")
    all_off = merge(intervals(today_d, OFF_TYPES) + intervals(tomorrow_d, OFF_TYPES))
    fp = st["fp"]
    first_run = not fp
    global EMERG_ACTIVE
    emerg_msgs = emergency_check(st, now)
    EMERG_ACTIVE = bool((st.get("emerg") or {}).get("on"))
    msgs: list[tuple[str, str | None]] = [(m, None) for m in emerg_msgs]
    # (текст для всіх, окремий текст для дружини або None)
    direct: list[tuple[str, str, bool, dict | None]] = []  # (chat_id, текст, це зведення?, кнопки)
    fam_msgs: list[str] = []                  # лише вам і дружині (запуск/ручна перевірка)

    # 0) меню команд і кнопки (для всіх; відповідь при наступному запуску)
    family = list(dict.fromkeys(CHAT_IDS + ([WIFE_ID] if WIFE_ID else [])))
    kino_req: list[str] = []
    yt_req: list[str] = []
    para_req: list[str] = []
    updates = [] if WORKER_MODE else get_updates(st.get("offset"))   # з Worker команди обробляє він
    for u in updates:
        st["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if cq:
            data = str(cq.get("data", ""))
            m = cq.get("message") or {}
            ccid = str((m.get("chat") or {}).get("id", ""))
            if data.startswith("seen:"):
                if ccid in family:                       # «бачили» враховуємо лише від вас і дружини
                    key = data[5:]
                    st["kino_seen"] = list(dict.fromkeys(st.get("kino_seen", []) + [key]))[-600:]
                    kb = (m.get("reply_markup") or {}).get("inline_keyboard")
                    if kb:
                        for row in kb:
                            for b in row:
                                if b.get("callback_data") == data:
                                    b["text"] = b["text"].replace("👀", "✅")
                        tg("editMessageReplyMarkup", {"chat_id": m["chat"]["id"], "message_id": m["message_id"],
                                                      "reply_markup": {"inline_keyboard": kb}})
                tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Запам'ятав 👍"})
            elif data.startswith("t:") and ccid:
                t = data[2:]
                if ccid in family:
                    tg("answerCallbackQuery", {"callback_query_id": cq["id"],
                                               "text": "Ви основний отримувач — отримуєте все"})
                elif t in ALL_TOPICS:
                    on = subs.setdefault(ccid, list(DEFAULT_TOPICS))
                    if t in on:
                        on.remove(t)
                    else:
                        on.append(t)
                    tg("editMessageReplyMarkup", {"chat_id": m["chat"]["id"], "message_id": m["message_id"],
                                                  "reply_markup": settings_markup(on)})
                    tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Збережено ✅"})
            continue
        msg = u.get("message") or {}
        cid = str((msg.get("chat") or {}).get("id", ""))
        cmd = (msg.get("text") or "").strip().split("@")[0].split(" ")[0].lower()
        if not cid:
            continue
        primary = cid in family
        if cmd == "/start" or (cmd == "/settings" and not primary):
            if primary:
                direct.append((cid, summary("✅ <b>Ви основний отримувач</b> — отримуєте все",
                                            today_d, tomorrow_d, all_off, now), True, None))
            else:
                new_sub = cid not in subs
                on = subs.setdefault(cid, list(DEFAULT_TOPICS))
                if cmd == "/start":                      # одразу показуємо чинний графік
                    direct.append((cid, summary(f"📋 <b>Чинний графік</b> · група {GROUP}",
                                                today_d, tomorrow_d, all_off, now), True, None))
                head = ("✅ <b>Ви підписані!</b> Меню — кнопка зліва від поля вводу, інструкція — /help\n\n"
                        if new_sub else "")
                direct.append((cid, head + SETTINGS_TEXT, False, settings_markup(on)))
        elif cmd == "/settings":
            direct.append((cid, "⚙️ Ви основний отримувач — отримуєте всі сповіщення.", False, None))
        elif cmd == "/help":
            direct.append((cid, HELP_TEXT, False, None))
        elif cmd == "/stop":
            subs.pop(cid, None)
            direct.append((cid, "ℹ️ Ви основний отримувач — відписатися через бота не можна." if primary
                           else "👋 Ви відписалися від усіх сповіщень. Повернутися — /start", False, None))
        elif cmd in ("/grafik", "/графік"):
            direct.append((cid, summary(f"📋 <b>Поточний графік</b> · група {GROUP}",
                                        today_d, tomorrow_d, all_off, now), True, None))
        elif cmd in ("/pogoda", "/погода"):
            direct.append((cid, weather_for(now) or "Не вдалося отримати прогноз, спробуйте пізніше.",
                           False, None))
        elif cmd in ("/kino", "/кіно"):
            kino_req.append(cid)
        elif cmd in ("/youtube", "/ютуб"):
            yt_req.append(cid)
        elif cmd in ("/para", "/пара"):
            para_req.append(cid)

    if REQ_KINO and re.fullmatch(r"-?\d+", REQ_KINO):
        kino_req.append(REQ_KINO)
    if REQ_YT and re.fullmatch(r"-?\d+", REQ_YT):
        yt_req.append(REQ_YT)
    kino_req += [str(c) for c in worker_req.get("kino", [])]   # запити з меню, які прийняв Worker
    yt_req += [str(c) for c in worker_req.get("yt", [])]
    para_req += [str(c) for c in worker_req.get("para", [])]
    kino_req, yt_req = list(dict.fromkeys(kino_req)), list(dict.fromkeys(yt_req))
    para_req = list(dict.fromkeys(para_req))

    def audience(topic: str) -> list[str]:
        return family + [c for c, on in subs.items() if topic in on and c not in family]

    svitlo_to = audience("svitlo")

    # 1) поточний графік: перший запуск або ручний запуск
    if SEND_NOW or first_run:
        head = "✅ <b>Бот працює</b>" if first_run else "📋 <b>Поточний графік</b>"
        fam_msgs.append(summary(f"{head} · група {GROUP}", today_d, tomorrow_d, all_off, now))

    # 2) ранкове повідомлення щодня (незалежно від змін)
    if (not first_run and st.get("morning") != today_key
            and MORNING_HOUR <= now.hour < MORNING_HOUR + 4):
        n = now.date().toordinal()
        wife_head = (f"☀️ <b>Доброго ранку, {WIFE_NAME}!</b>\n"
                     f"💖 {COMPLIMENTS[n % len(COMPLIMENTS)]}\n"
                     f"✨ <i>{MOTIVATION[n % len(MOTIVATION)]}</i>\n{SEP}")
        common = summary(f"☀️ <b>Доброго ранку!</b> · група {GROUP}", today_d, tomorrow_d, all_off, now)
        msgs.append((common, summary(wife_head, today_d, tomorrow_d, all_off, now)))
        st["morning_sent"] = now.isoformat()
    if first_run or st.get("morning") != today_key and now.hour >= MORNING_HOUR:
        st["morning"] = today_key

    # 3) новий / змінений графік (якщо працює Cloudflare Worker — вам і дружині надсилає він)
    change_msgs: list[str] = []
    for d, title in ((today_d, "Сьогодні"), (tomorrow_d, "Завтра")):
        if not d or "date" not in d:
            continue
        dk = day_date(d).isoformat()
        new = json.dumps({"s": d.get("status"), "slots": d.get("slots", [])}, sort_keys=True)
        old = fp.get(dk)
        fp[dk] = new
        print(f"[{now:%d.%m %H:%M}] {title} {dk}: {d.get('status')} | "
              f"{ivs_text(merge(intervals(d, OFF_TYPES)), day_date(d))} | "
              f"{'перший запуск' if first_run else 'без змін' if old == new else 'ЗМІНА'}")
        if old != new:
            st.setdefault("log", []).append(
                f"{now:%d.%m %H:%M} {title} {dk} {d.get('status')}: "
                f"{ivs_text(merge(intervals(d, OFF_TYPES)), day_date(d))}"
                + (" (перший запуск — без повідомлення)" if first_run else ""))
            st["log"] = st["log"][-40:]
        if first_run or old == new:
            continue
        dd = day_date(d)
        prev = json.loads(old) if old else {}
        prev_waiting = prev.get("s") == "WaitingForSchedule" and not any(
            x.get("type") in OFF_TYPES for x in prev.get("slots", []))
        if d.get("status") == "WaitingForSchedule" and not intervals(d, OFF_TYPES):
            if old and not prev_waiting:           # графік був, а тепер його прибрали
                change_msgs.append(f"↩️ <b>ГРАФІК НА {title.upper()} ВІДКЛИКАНО</b> · група {GROUP}\n"
                                   f"ДТЕК прибрав графік на {dd:%d.%m} — чекаємо на новий")
            continue                               # графік ще не сформовано
        emergency = d.get("status") == "EmergencyShutdowns"
        was_emergency = prev.get("s") == "EmergencyShutdowns"
        banner = ""
        if emergency and not was_emergency:
            banner = ("🚨🚨🚨 <b>ЕКСТРЕНІ ВІДКЛЮЧЕННЯ!</b> 🚨🚨🚨\n"
                      "Світло можуть вимкнути поза графіком · 🔋 зарядіть пристрої\n\n")
        elif was_emergency and not emergency:
            banner = "✅✅ <b>ЕКСТРЕНІ ВІДКЛЮЧЕННЯ СКАСОВАНО</b> ✅✅\n\n"
        if old is None or prev_waiting:
            change_msgs.append(f"{banner}📅 <b>НОВИЙ ГРАФІК</b> · група {GROUP}\n{fmt_day(d, title)}")
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
            prev_pos = merge(intervals(prev_d, POSSIBLE_TYPES))
            new_pos = merge(intervals(d, POSSIBLE_TYPES))
            if prev_pos != new_pos:
                tail += f"\n↩️ можливі було: <s>{ivs_text(prev_pos, dd)}</s>"
            change_msgs.append(f"{banner}{head}{fmt_day(d, title, fresh)}{tail}")

    # 3б) тижневий прогноз ДТЕК (орієнтовний) — повідомляємо, якщо змінився
    if WEEK is not None:
        new_w = json.dumps(WEEK, sort_keys=True)
        old_w = st.get("week_fp")
        st["week_fp"] = new_w
        if old_w and old_w != new_w and not first_run:
            prev_w = {int(k): v for k, v in json.loads(old_w).items()}
            lines = []
            for i in range(7):
                day = now.date() + timedelta(days=i)
                cur, was = WEEK.get(day.isoweekday(), []), prev_w.get(day.isoweekday(), [])
                lines.append(f"{WD_SHORT[day.weekday()]} {day:%d.%m}: <b>{min_text(cur)}</b>"
                             + (f" 🆕\n      було: <s>{min_text(was)}</s>" if cur != was else ""))
            change_msgs.append(f"📊 <b>ЗМІНЕНО ПРОГНОЗ НА ТИЖДЕНЬ</b> · група {GROUP}\n"
                               f"<i>можливі відключення, орієнтовно</i>\n"
                               f"<blockquote>" + "\n".join(lines) + "</blockquote>")
            st.setdefault("log", []).append(f"{now:%d.%m %H:%M} прогноз на тиждень змінено")

    # 4) нагадування до відключення і до увімкнення
    rem = set(st["reminded"])
    for i, (a, b) in enumerate(all_off):
        nxt = all_off[i + 1][0] if i + 1 < len(all_off) else None
        for ev, t in (("off", a), ("on", b)):
            k = f"{ev}|{t.isoformat()}"
            left = (t - now).total_seconds() / 60
            if EMERG_ACTIVE or not (0 < left <= REMIND_MIN) or k in rem:
                continue
            m = round(left)
            if ev == "off":
                text = (f"⏰ <b>За графіком через ~{m} хв відключення</b>\n"
                        f"<blockquote>🔴 <b>{a:%H:%M}–{hm(b, a.date())}</b> ({dur(b - a)})</blockquote>")
            else:
                tail = (f"🔴 далі за графіком: {when(nxt, now)}" if nxt
                        else "🟢 далі за графіком відключень не буде")
                text = f"💡 <b>За графіком через ~{m} хв має бути світло</b> (о <b>{b:%H:%M}</b>)\n{tail}"
            msgs.append((text, None))
            rem.add(k)

    # 5) погода на завтра щодня о WEATHER_HOUR — лише вам і дружині
    topic_msgs: list[tuple[str, str, dict | None, str]] = []   # (тема, текст, кнопки, текст без кнопок)
    if st.get("weather") != today_key and now.hour >= WEATHER_HOUR:
        wt = weather_for(now)
        if wt:
            topic_msgs.append(("pogoda", wt, None, wt))
            st["weather"] = today_key

    # 6) кіно — п'ятниця 10:00; 7) YouTube — понеділок 10:00 (лише вам і дружині)
    week_key = "%d-%02d" % now.isocalendar()[:2]
    in_window = DIGEST_HOUR <= now.hour < DIGEST_HOUR + 4
    direct_m: list[tuple[str, str, dict | None]] = []
    kino_due = now.weekday() == KINO_WEEKDAY and in_window and st.get("kino_week") != week_key
    if kino_due:                                   # щотижнева підбірка — одна для всіх
        k = build_kino(st)
        if k:
            st["kino_last"] = {"text": k[0], "markup": k[1]}
            st["kino_week"] = week_key
            topic_msgs.append(("kino", k[0], k[1], k[0].replace(KINO_HINT, "")))
    if kino_req:                                   # з меню — щоразу НОВА підбірка (без повторів)
        k_new = build_kino(st) if TMDB_KEY else None
        for c in kino_req:
            if not k_new:
                direct_m.append((c, "🍿 Кіно ще не налаштовано (немає ключа TMDB)." if not TMDB_KEY
                                 else "🍿 Не вдалося зібрати підбірку, спробуйте пізніше.", None))
                continue
            text = k_new[0].replace("КІНО НА ВИХІДНІ", "НОВА ПІДБІРКА КІНО", 1)
            direct_m.append((c, text, k_new[1]) if c in family else (c, text.replace(KINO_HINT, ""), None))
    if (now.weekday() == YT_WEEKDAY and in_window and st.get("yt_week") != week_key) or yt_req:
        y = None
        if now.weekday() == YT_WEEKDAY and in_window and st.get("yt_week") != week_key:
            y = build_youtube(st, now)
            if y:
                st["yt_week"], st["yt_last"] = week_key, y
                topic_msgs.append(("yt", y, None, y))
                yt_req = [c for c in yt_req if c not in audience("yt")]
        elif yt_req and not st.get("yt_last"):
            y = build_youtube(st, now)
            if y:
                st["yt_last"] = y
        for c in yt_req:
            direct_m.append((c, st.get("yt_last") or ("▶️ YouTube ще не налаштовано (немає ключа)." if not YT_KEY
                                                      else "▶️ Не вдалося зібрати підбірку, спробуйте пізніше."), None))

    # 8) «Для пари» — ср і пт о 18:00 (вам, дружині та підписникам, які це ввімкнули); з меню — нова порада
    para_due = (now.weekday() in PARA_DAYS and PARA_HOUR <= now.hour < PARA_HOUR + 4
                and st.get("para_sent") != today_key)
    if para_due:
        pm = build_para(st, now)
        topic_msgs.append(("para", pm, None, pm))
        st["para_sent"] = today_key
    if para_req:
        pm = build_para(st, now, with_news=False)
        direct_m += [(c, pm, None) for c in para_req]

    # меню команд у Telegram (оновлюється автоматично при зміні списку)
    cmd_ver = ",".join(c for c, _ in COMMANDS)
    if not WORKER_MODE and st.get("cmds") != cmd_ver:
        try:
            set_commands()
            st["cmds"] = cmd_ver
        except Exception as e:
            print(f"setMyCommands: {e}")

    # відправка кожному окремо: помилка в одного не зупиняє інших
    ok = failed = 0
    failed_ids: set[str] = set()
    # хто вже отримує зведення в цьому запуску — тому не дублюємо відповідь на /grafik чи /start
    got_summary = set(family) if (SEND_NOW or first_run) else set()
    if st.get("morning_sent") == now.isoformat():
        got_summary |= set(svitlo_to)
    jobs: list[tuple[str, str, dict | None]] = [
        (c, t, mk) for c, t, is_sum, mk in direct if not (is_sum and c in got_summary)]
    jobs += [(cid, t, None) for t in fam_msgs for cid in family]
    jobs += [(cid, wife_text if (wife_text and cid == WIFE_ID) else text, None)
             for text, wife_text in msgs for cid in svitlo_to]
    jobs += direct_m
    change_to = [] if worker_alive else svitlo_to      # працює Worker — зміни вже надіслав він
    jobs += [(cid, text, None) for text in change_msgs for cid in change_to]
    for topic, text, mk, plain in topic_msgs:
        jobs += [(cid, text, mk) if cid in family else (cid, plain, None) for cid in audience(topic)]
    removed: set[str] = set()
    for cid, text, mk in jobs:
        if cid in removed:
            continue
        try:
            send(cid, text, mk)
            ok += 1
        except urllib.error.HTTPError as e:
            if e.code in (400, 403) and cid in subs and WORKER_MODE:
                print(f"Підписник {cid} недоступний ({e.code})")
                removed.add(cid)
            elif e.code in (400, 403) and cid in subs:     # підписник заблокував бота / чат зник
                subs.pop(cid, None)
                removed.add(cid)
                print(f"Підписника {cid} видалено ({e.code})")
            else:
                print(f"Не вдалося надіслати {cid}: {err_text(e)}")
                failed += 1
                failed_ids.add(cid)
        except Exception as e:
            print(f"Не вдалося надіслати {cid}: {err_text(e)}")
            failed += 1
            failed_ids.add(cid)
    if jobs and ok == 0 and failed:
        sys.exit("❌ Жодне повідомлення не надіслано (див. рядки «Не вдалося надіслати» вище). "
                 "Найчастіше: неправильний BOT_TOKEN або ви не натиснули Start у боті")

    if json.dumps(subs, sort_keys=True) != subs_before or "subs" not in st:   # лише при зміні
        st["subs"] = enc(subs)

    keep_from = (now.date() - timedelta(days=1)).isoformat()
    st["fp"] = {k: v for k, v in fp.items() if k >= keep_from}
    st["reminded"] = sorted(k for k in rem
                            if datetime.fromisoformat(k.split("|", 1)[1]) > now - timedelta(days=1))
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
    # недоставлені повідомлення: раз на день попередження вам у Telegram (без червоного запуску)
    if failed_ids and st.get("warned") != today_key:
        who = ", ".join("дружині" if c == WIFE_ID else c for c in sorted(failed_ids))
        try:
            send(CHAT_IDS[0], f"⚠️ Не вдалося надіслати: {who}.\nНехай відкриє бота і натисне <b>Start</b>.")
            st["warned"] = today_key
            STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
        except Exception as e:
            print(f"Попередження: {e}")


if __name__ == "__main__":
    main()
