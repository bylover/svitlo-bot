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
CHANNEL = (os.environ.get("CHANNEL") or "").strip().lstrip("@")   # канал ЖК для екстрених вимкнено (ненадійний)
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
GEMINI_MODELS = [m.strip() for m in (os.environ.get("GEMINI_MODEL") or "").split(",") if m.strip()] or \
    ["gemini-3.8-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]
GIPHY_KEY = (os.environ.get("GIPHY_KEY") or "").strip()    # безкоштовний ключ developers.giphy.com
SMM_WEEKDAY, SMM_HOUR = 0, 13                               # дайджест SMM і таргету: пн о 13:00
SMM_SUBS = ["FacebookAds", "PPC", "socialmedia", "TikTokAds", "InstagramMarketing"]   # «Фішки від практиків»
SILPO_WEEKDAY, SILPO_HOUR = 3, 13                           # акції Сільпо: чт о 13:00
SILPO_SNAP_WEEKDAY, SILPO_SNAP_HOUR = 2, 20                 # знімок цін усіх товарів: ср після 20:00
SILPO_LAT, SILPO_LON = 50.5135, 30.4935                     # вул. Калнишевського, 2 (Мінський масив)
SILPO_STORE = "Калнишевськ"                                 # яку адресу шукати серед магазинів
SILPO_BRANCH = (os.environ.get("SILPO_BRANCH") or "").strip()   # ручний вибір магазину (id), якщо знайдемо
SILPO_TOP = 5
SPLIT = "\n§§\n"                                            # роздільник повідомлень у довгих підбірках
REDDIT_ID = (os.environ.get("REDDIT_ID") or "").strip()     # Reddit script app (для «Обговорення тижня 18+»)
REDDIT_SECRET = (os.environ.get("REDDIT_SECRET") or "").strip()
REDDIT_USER = (os.environ.get("REDDIT_USER") or "").strip()
REDDIT_PASS = (os.environ.get("REDDIT_PASS") or "").strip()
REDDIT_SUBS = [x.strip() for x in (os.environ.get("REDDIT_SUBS") or "sex,sexover30").split(",") if x.strip()]
PARA_DAYS, PARA_HOUR = (2, 4), 18                          # «Для пари»: ср і пт о 18:00
COMMANDS = [("grafik", "💡 Графік світла"), ("pogoda", "🌤 Погода"), ("porady", "💞 Поради 18+"),
            ("kino", "🍿 Кіно: підбірка"), ("youtube", "▶️ YouTube: топ за тиждень"),
            ("smm", "📈 SMM і таргет за тиждень"), ("silpo", "🛒 Акції Сільпо"),
            ("settings", "⚙️ Налаштування сповіщень"), ("help", "ℹ️ Інструкція"),
            ("stop", "🔕 Відписатися")]
# теми сповіщень, які підписник може вмикати/вимикати
TOPICS = [("svitlo", f"💡 Світло · група {GROUP}"), ("pogoda", "🌤 Погода"),
          ("kino", "🍿 Кіно"), ("yt", "▶️ YouTube"), ("smm", "📈 SMM і таргет"), ("para", "💞 Поради 18+"),
          ("silpo", "🛒 Акції Сільпо")]
ALL_TOPICS = [t for t, _ in TOPICS]
DEFAULT_TOPICS = [t for t in ALL_TOPICS if t not in ("para", "smm", "silpo")]   # ці теми підписник вмикає сам
SETTINGS_TEXT = ("⚙️ <b>Налаштування сповіщень</b>\n"
                 "Натисніть, щоб увімкнути ✅ або вимкнути ⬜.\n"
                 f"💡 Світло — графік, зміни, нагадування, ранкове зведення (група {GROUP})\n"
                 "🌤 Погода — щодня о 20:00 · 🍿 Кіно — пт 11:00 · ▶️ YouTube — пн 11:00\n"
                 "📈 SMM і таргет — дайджест тижня, пн о 13:00 · 💞 Поради 18+ — ср і пт о 18:00\n"
                 "🛒 Акції Сільпо — топ тижня, чт о 13:00\n"
                 "<i>(📈, 💞 і 🛒 за замовчуванням вимкнено)</i>\n"
                 "<i>Бот відповідає із затримкою до 5–20 хв.</i>")
HELP_TEXT = ("ℹ️ <b>ЯК КОРИСТУВАТИСЯ БОТОМ</b>\n\n"
             "<b>Що вміє бот</b>\n<blockquote>"
             f"💡 <b>Світло</b> (група {GROUP}) — графік ДТЕК на сьогодні й завтра; сповіщення про "
             "новий, змінений чи відкликаний графік, прогноз на тиждень, екстрені відключення; нагадування за "
             "30–45 хв; стан за вашою адресою (причина й орієнтовний час відновлення); ранкове зведення о 10:00\n"
             "🌤 <b>Погода</b> — прогноз на завтра щодня о 20:00\n"
             "🍿 <b>Кіно</b> — 7 фільмів і 3 серіали на вихідні, щоп'ятниці об 11:00\n"
             "▶️ <b>YouTube</b> — топ-10 українського YouTube за тиждень, щопонеділка об 11:00\n"
             "📈 <b>SMM і таргет</b> — дайджест новинок і фішок за тиждень, щопонеділка о 13:00 (вмикається в /settings)\n"
             "🛒 <b>Акції Сільпо</b> — топ-5 у кожній категорії з реальною вигодою, щочетверга о 13:00 (вмикається в /settings)\n"
             "💞 <b>Поради 18+</b> — по одному: порада, стаття, техніка, огляд іграшок або досвід; ср і пт о 18:00 і за запитом з меню "
             "(вмикається в /settings)</blockquote>\n"
             "<b>Меню</b> (кнопка зліва від поля вводу)\n<blockquote>"
             "/grafik — графік світла зараз\n/pogoda — прогноз погоди\n/porady — нова порада, стаття, огляд або досвід 18+\n/kino — підбірка кіно\n"
             "/youtube — топ YouTube за тиждень\n/smm — SMM і таргет за тиждень\n/silpo — акції Сільпо\n/settings — увімкнути або вимкнути сповіщення\n"
             "/help — ця інструкція і звідки дані\n/stop — відписатися від усього</blockquote>\n"
             "<b>Важливо</b>\n<blockquote>"
             "• Графік — за даними сайту ДТЕК. «За графіком» не означає, що світло фактично є чи немає\n"
             f"• Сповіщення про світло — лише для групи {GROUP}. Якщо у вас інша група, вимкніть «💡 Світло» в /settings\n"
             "• Свою групу можна перевірити на dtek-kem.com.ua → «Відсутня електроенергія?»\n"
             "• Бот відповідає на команди із затримкою до 5–20 хв</blockquote>")
SOURCES_TEXT = (
    "ℹ️ <b>ЗВІДКИ ДАНІ І ЯК ФОРМУЄТЬСЯ</b>\n"
    "\n"
    "💡 <b>Світло</b>\n"
    "<blockquote>• Графік — із сайту ДТЕК (dtek-kem.com.ua) через відкрите дзеркало даних, яке оновлюється кожні ~5 хв; бот перевіряє зміни щохвилини\n"
    "• Прогноз на тиждень — з того ж сайту ДТЕК (орієнтовний)\n"
    "• Ваша адреса — перевірка на сайті ДТЕК, як у формі «Відсутня електроенергія?», кожні ~10 хв: чи є зараз відключення, причина, початок і орієнтовне відновлення\n"
    "• Екстрені відключення — за оголошенням на сторінці ДТЕК, перевіркою адреси, статусом YASNO та Telegram-каналами (канал будинку, Укренерго, ДТЕК); враховуються тільки явні оголошення й скасування, оголошення з каналу діє до 12 год\n"
    "• Фактично є/немає світла (для основних отримувачів) — СвітлоБот будинку: повідомлення й стрічка за добу; якщо світла немає, а жодне джерело не повідомляє ні про екстрені, ні про планові відключення — бот так і пише\n"
    "• «За графіком» — це план ДТЕК, а не факт наявності світла</blockquote>\n"
    "🌤 <b>Погода</b>\n"
    "<blockquote>• Open-Meteo — поєднує кілька метеомоделей; прогноз для координат вашого району\n"
    "• Ніч / ранок / день / вечір + поради: парасолька, мороз, вітер, різка зміна температури</blockquote>\n"
    "§§\n"
    "🍿 <b>Кіно</b>\n"
    "<blockquote>• Каталог і описи українською — TMDB; рейтинги — IMDb і Rotten Tomatoes (через OMDb)\n"
    "• Лише високий рейтинг, але не надто «заїжджені» фільми; без повторів, з урахуванням 👀 «бачили»\n"
    "• 80% підбірки — фільми й серіали за останні 10 років; натисніть на назву — відкриється вікно з постером, кадрами, акторами й описом\n"
    "• 🔞 відвертість: помірна — є оголення або постільні сцени; 🔞🔞 висока — відверті сексуальні сцени, еротика або рейтинг NC-17 (орієнтовно за тегами TMDB і віковим рейтингом США); у вікні — посилання на детальний опис з таймінгом (IMDb Parents Guide)\n"
    "• У вікні фільму імена акторів — посилання на їхню фільмографію</blockquote>\n"
    "▶️ <b>YouTube</b>\n"
    "<blockquote>• «В тренді» YouTube в Україні та пошук українською за останні 7 днів; лише україномовні відео (без російських та іноземних), без новин, дитячого контенту й Shorts\n"
    "• Порядок — перегляди + коментарі (1 коментар ≈ 100 переглядів), не більше 2 відео з каналу</blockquote>\n"
    "📈 <b>SMM і таргет</b>\n"
    "<blockquote>• Новини — Meta Newsroom, The Keyword (блог Google), Social Media Today, Marketing Dive і Google News — лише за останні 7 днів\n"
    "• Перегляди статей ці сайти не публікують, тож важливість визначаємо так: тема, про яку пишуть кілька джерел, — головна новина тижня; далі офіційні анонси Meta і Google та свіжість\n"
    "• Фішки практиків — блоги Jon Loomer, Social Media Examiner, AdEspresso, Hootsuite, Buffer, Later і Medium (теги про рекламу)\n"
    "• Відбір 8–10 найважливіших, переклад і переказ — ШІ Gemini; у кожному пункті — посилання на джерело</blockquote>\n"
    "💞 <b>Поради 18+</b>\n"
    "<blockquote>• Порада — пише ШІ Gemini (запасний — модель Cloudflare) за темами, що чергуються: пози, техніка, прелюдія, масаж, фантазії, ігри; без повторів. Картинка — ілюстрація ШІ або гіфка GIPHY\n"
    "• Статті й техніки — журнали Cosmopolitan, Men's Health, Women's Health, Glamour, Self, Refinery29 і блоги Gottman Institute, Sex With Emily, Kinkly, Autostraddle\n"
    "• Огляди іграшок і відгуки — Lovehoney, LELO, Good Vibrations, WhatToy, Dildo or Dildon't, Hey Epiphora, Bedbible, Mashable, Medium (sex-toys)\n"
    "• Досвід — особисті історії з Medium і блогу Girl on the Net і теми подкастів (зокрема Sex Unwrapped)\n"
    "• Співвідношення: приблизно 1 порада ШІ на 3 матеріали з джерел; найчастіше — техніка, пози, іграшки й девайси, рідше — проблеми й стосунки\n"
    "• Щоразу — один матеріал, обраний випадково серед свіжих (журнали — до 2 тижнів, огляди — до 6–8 тижнів): частіше потрапляють новіші й ті, де більше коментарів; тип і джерело чергуються; без повторів і дублікатів (одна стаття на кількох сайтах — один раз)\n"
    "• ШІ перекладає й переказує українською і сам визначає тип — у заголовку буде «Порада», «Техніка», «Стаття», «Огляд», «Досвід» чи «Подкаст»; зверху обкладинка, внизу посилання на оригінал</blockquote>\n"
    "🛒 <b>Акції Сільпо</b>\n"
    "<blockquote>• Дані — із сайту Сільпо для вашого магазину; у кожній категорії кнопка «➕ ще 5»\n"
    "• Беремо всі загальні акції з прямим зниженням ціни, зокрема «Ціну тижня» 🔥; без «2+1», «другий за…» і персональних\n"
    "• Щосереди ввечері бот запам'ятовує ціни всіх товарів магазину. <b>Заявлена знижка</b> — від «старої» ціни на ціннику; <b>реальна вигода</b> — від найнижчої ціни товару за останні 1–3 тижні\n"
    "• Реальна вигода менше 3% — у топ не потрапляє. Місце в топі: реальна вигода % + бонус за рейтинг ⭐ (+5 за кожен бал понад 4) + до +5 за популярність\n"
    "• Топ-5 у кожній категорії, новий список щочетверга о 13:00</blockquote>\n"
    "<i>Переклад і переказ від ШІ можуть бути неточними — завжди є посилання на першоджерело.</i>"
)
WELCOME = "👋 <b>Hey there, night owl!</b>\nI'm your blackout buddy — I know when the lights go out before your toe meets the furniture. 💡\n\n🌤 I read the sky, 🍿 pick movies for the couch, ▶️ dig up the best of YouTube, 📈 spy on marketing trends and 🛒 hunt Silpo deals.\n💞 And when it gets dark… I whisper 18+ tips that make blackouts way less boring. 😏\n\n<i>Tap the menu ☰ and let's turn the lights on — one way or another.</i>"
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
    payload = {"chat_id": chat_id, "text": str(text), "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    r = http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", payload)
    return (r.get("result") or {}).get("message_id") if isinstance(r, dict) else None


class Exp(str):
    """Повідомлення, яке бот сам видалить після expire (нагадування)."""
    expire: str = ""


def expiring(text: str, when_dt: datetime) -> str:
    e = Exp(text)
    e.expire = when_dt.isoformat()
    return e


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


ADULT_TAGS = {"nudity": "оголення", "female nudity": "оголення", "male nudity": "оголення",
              "full frontal nudity": "оголення", "sex scene": "сексуальні сцени", "explicit sex": "відверті сексуальні сцени",
              "erotic movie": "еротика", "erotica": "еротика", "erotic thriller": "еротичний трилер",
              "softcore": "еротика", "sexuality": "сексуальність", "erotic drama": "еротична драма"}


HIGH_TAGS = {"explicit sex", "erotic movie", "erotica", "softcore", "erotic thriller", "erotic drama", "full frontal nudity"}


def adult_tags(d: dict) -> list[str]:
    kw = (d.get("keywords") or {})
    names = [k.get("name", "").lower() for k in (kw.get("keywords") or kw.get("results") or [])]
    return list(dict.fromkeys(ADULT_TAGS[n] for n in names if n in ADULT_TAGS))


def us_cert(d: dict) -> str:
    """Віковий рейтинг США: R / NC-17 (фільми) або TV-MA (серіали)."""
    for r in (d.get("release_dates") or {}).get("results", []):
        if r.get("iso_3166_1") == "US":
            c = [x.get("certification") for x in r.get("release_dates", []) if x.get("certification")]
            if c:
                return c[0]
    for r in (d.get("content_ratings") or {}).get("results", []):
        if r.get("iso_3166_1") == "US" and r.get("rating"):
            return r["rating"]
    return ""


def adult_level(d: dict) -> tuple[int, list[str], str]:
    """0 — немає; 1 — помірна (оголення, постільні сцени); 2 — висока (відверті сцени, еротика, NC-17)."""
    kw = (d.get("keywords") or {})
    names = {k.get("name", "").lower() for k in (kw.get("keywords") or kw.get("results") or [])}
    tags, cert = adult_tags(d), us_cert(d)
    if not tags:
        return 0, [], cert
    return (2 if names & HIGH_TAGS or cert in ("NC-17", "X") else 1), tags, cert


LEVEL_TEXT = {1: "🔞 відвертість: помірна", 2: "🔞🔞 відвертість: висока"}


def pick_titles(kind: str, need: int, seen: set, since: str | None = None, until: str | None = None,
                pages: int = 2) -> list[dict]:
    """kind: movie | tv. Високий рейтинг, але не надто «заїжджені»; since/until — роки виходу."""
    dk = "primary_release_date" if kind == "movie" else "first_air_date"
    if kind == "movie":
        base = {"vote_average.gte": 7.2, "vote_count.gte": 300, "vote_count.lte": 6000,
                "with_runtime.gte": 80, "without_genres": "16,10751,99"}
        min_imdb, min_rt = 7.0, 75
    else:
        base = {"vote_average.gte": 7.5, "vote_count.gte": 150, "vote_count.lte": 4000,
                "without_genres": "16,10762,10763,10764,10767,99"}
        min_imdb, min_rt = 7.5, 80
    base[f"{dk}.gte"] = since or "1990-01-01"
    if until:
        base[f"{dk}.lte"] = until
    cands = []
    for page in random.sample(range(1, 7), pages):
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
            d = tmdb(f"/{kind}/{c['id']}", language="uk-UA", include_video_language="uk,en",
                     include_image_language="uk,en,null",
                     append_to_response="external_ids,videos,credits,images,keywords,"
                     + ("release_dates" if kind == "movie" else "content_ratings"))
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


def telegraph_page(st: dict, title: str, nodes: list) -> str | None:
    """Сторінка Telegraph (відкривається в Telegram як Instant View)."""
    try:
        if not st.get("tgph"):
            st["tgph"] = http_json("https://api.telegra.ph/createAccount",
                                   {"short_name": "svitlobot", "author_name": "Kino"})["result"]["access_token"]
        r = http_json("https://api.telegra.ph/createPage", {"access_token": st["tgph"], "title": title[:250],
                                                             "content": nodes, "return_content": False})
        return (r.get("result") or {}).get("url")
    except Exception as e:
        print(f"Telegraph: {err_text(e)[:120]}")
        return None


def kino_page(st: dict, it: dict) -> str | None:
    d, tv = it["d"], it["kind"] == "tv"
    title = d.get("name") if tv else d.get("title")
    year = ((d.get("first_air_date") if tv else d.get("release_date")) or "")[:4]
    img = lambda p, w="w780": f"https://image.tmdb.org/t/p/{w}{p}"
    cast = [(c.get("name"), c.get("id")) for c in (d.get("credits") or {}).get("cast", [])[:6] if c.get("name")]
    crew = (d.get("credits") or {}).get("crew", [])
    director = next((c.get("name") for c in crew if c.get("job") == "Director"), None) or \
        ", ".join(c.get("name") for c in (d.get("created_by") or [])[:2])
    genres = ", ".join(g["name"].lower() for g in d.get("genres", [])[:4])
    rating = (f"IMDb {it['imdb']}" if it["imdb"] else f"TMDB {d.get('vote_average', 0):.1f}") + \
        (f" · Rotten Tomatoes {it['rt']}%" if it["rt"] is not None else "")
    nodes = []
    if d.get("poster_path"):
        nodes.append({"tag": "img", "attrs": {"src": img(d["poster_path"])}})
    nodes.append({"tag": "p", "children": [{"tag": "b", "children": [f"{title} ({year})"]}]})
    nodes.append({"tag": "p", "children": [" · ".join(x for x in (genres, rating) if x)]})
    if cast:                                          # імена — посилання на фільмографію актора (TMDB)
        ch = [{"tag": "b", "children": ["У ролях: "]}]
        for k_, (nm, pid) in enumerate(cast):
            ch.append({"tag": "a", "attrs": {"href": f"https://www.themoviedb.org/person/{pid}?language=uk"}, "children": [nm]}
                      if pid else nm)
            if k_ < len(cast) - 1:
                ch.append(", ")
        nodes.append({"tag": "p", "children": ch})
        nodes.append({"tag": "p", "children": [{"tag": "i", "children": ["Натисніть на ім'я — інші фільми з цим актором"]}]})
    if director:
        nodes.append({"tag": "p", "children": [{"tag": "b", "children": ["Режисер: " if not tv else "Автори: "]}, director]})
    if d.get("overview"):
        nodes.append({"tag": "p", "children": [d["overview"]]})
    level, tags, cert = adult_level(d)
    if level:
        imdb_id = (d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id")
        nodes.append({"tag": "h4", "children": [LEVEL_TEXT[level].capitalize()]})
        nodes.append({"tag": "p", "children": [
            ("Помірна — є оголення або постільні сцени. " if level == 1 else
             "Висока — відверті сексуальні сцени, еротика або рейтинг NC-17. ")
            + f"Орієнтовно за тегами TMDB: {', '.join(tags)}" + (f"; віковий рейтинг США: {cert}" if cert else "") + "."]})
        if imdb_id:
            nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": f"https://www.imdb.com/title/{imdb_id}/parentalguide"},
                                                    "children": ["Повний опис сцен з таймінгом — IMDb Parents Guide"]}]})
    stills = [b.get("file_path") for b in (d.get("images") or {}).get("backdrops", [])[:4] if b.get("file_path")]
    if stills:
        nodes.append({"tag": "h4", "children": ["Кадри"]})
        nodes += [{"tag": "img", "attrs": {"src": img(p_)}} for p_ in stills]
    tr = trailer(d.get("videos"))
    if tr:
        nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": tr}, "children": ["▶️ Дивитися трейлер"]}]})
    return telegraph_page(st, f"{title} ({year})", nodes)


def kino_item(i: int, it: dict, ov_len: int = 160) -> str:
    d = it["d"]
    is_tv = it["kind"] == "tv"
    title = d.get("name") if is_tv else d.get("title")
    year = ((d.get("first_air_date") if is_tv else d.get("release_date")) or "")[:4]
    genres = ", ".join(g["name"].lower() for g in d.get("genres", [])[:3])
    if is_tv:
        length = f"📺 серіал · {d.get('number_of_seasons') or 0} сез."
    else:
        rt_min = d.get("runtime") or 0
        length = f"⏱ {rt_min // 60} год {rt_min % 60:02d} хв" if rt_min else ""
    lvl = adult_level(d)[0]
    adult = f" · {LEVEL_TEXT[lvl]}" if lvl else ""
    rating = [f"⭐ IMDb <b>{it['imdb']}</b>" if it["imdb"] else f"⭐ TMDB <b>{d.get('vote_average', 0):.1f}</b>"]
    if it["rt"] is not None:
        rating.append(f"🍅 <b>{it['rt']}%</b>")
    cast = ", ".join(c.get("name") for c in (d.get("credits") or {}).get("cast", [])[:3] if c.get("name"))
    ov = (d.get("overview") or "").strip() if ov_len else ""
    if len(ov) > ov_len:
        ov = ov[:ov_len - 3].rsplit(" ", 1)[0] + "…"
    name = f'<a href="{html.escape(it["page"])}">{esc(title)}</a>' if it.get("page") else esc(title)
    lines = [f"{'📺' if is_tv else '🎬'} <b>{i}. {name}</b> ({year})",
             " · ".join(x for x in (esc(genres), length) if x) + adult,
             " · ".join(rating)]
    if cast:
        lines.append(f"<i>🎭 {esc(cast)}</i>")
    if ov:
        lines.append(f"📝 {esc(ov)}")
    tr = trailer(d.get("videos"))
    if tr:
        lines.append(f'▶️ <a href="{tr}">Трейлер</a>')
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def build_kino(st: dict, title: str = "🍿 <b>ПІДБІРКА КІНО</b>") -> tuple[str, dict] | None:
    """10 позицій: ~80% — за останні 10 років (6 фільмів + 2 серіали), решта — старіші хіти."""
    if not TMDB_KEY:
        print("TMDB_KEY не задано — кіно пропущено")
        return None
    seen = set(st.get("kino_seen", []))
    y = datetime.now(TZ).year
    recent, old_until = f"{y - 10}-01-01", f"{y - 11}-12-31"
    movies = pick_titles("movie", 6, seen, since=recent) + pick_titles("movie", 1, seen, until=old_until, pages=1)
    tvs = pick_titles("tv", 2, seen, since=recent, pages=1) + pick_titles("tv", 1, seen, until=old_until, pages=1)
    items = movies + tvs
    if len(items) < 3:
        return None
    for it in items:                                  # окреме вікно з великим постером і кадрами
        it["page"] = kino_page(st, it)
    st["kino_seen"] = (st.get("kino_seen", []) + [x["key"] for x in items])[-600:]
    head = f"{title}\n" + KINO_HINT + "<i>Натисніть на назву — відкриється постер, кадри й опис</i>\n"
    for ov_len in (160, 110, 70, 0):            # ліміт Telegram — 4096 символів
        text = head + "\n".join(kino_item(i, it, ov_len) for i, it in enumerate(items, 1))
        if len(text) <= 4000:
            break
    btns = [{"text": f"👀 {i}", "callback_data": f"seen:{it['key']}"} for i, it in enumerate(items, 1)]
    markup = {"inline_keyboard": [btns[k:k + 5] for k in range(0, len(btns), 5)]}
    return text, markup


# ---------- «Для пари» (Gemini + Google News RSS) ----------
# (ключ, тема для Gemini, сцена для ІІ-картинки, запит для GIPHY)
PARA_THEMES = [
    ("поза", "одна нова поза: назва, як у неї увійти, чим вона приємна обом і як зробити її комфортною",
     "a loving couple in a tender embrace, silhouettes against warm window light", "romantic couple"),
    ("іграшки", "одна інтимна іграшка чи девайс: що це, як використовувати вдвох, на що звернути увагу при виборі",
     "a playful couple laughing on a bed with pillows, cozy evening", "couple laughing"),
    ("техніка", "техніка пестощів руками: ритм, тиск, темп — як довести партнера до задоволення",
     "two hands intertwined on silk sheets, soft warm light", "love"),
    ("поза", "ще одна нова поза для глибшої близькості: як увійти в неї і що врахувати",
     "a couple cuddling closely under a blanket, cozy evening, silhouettes", "cuddle"),
    ("прелюдія", "як зробити прелюдію довшою і яскравішою: конкретні прийоми",
     "a couple slowly dancing close together in a dim cozy room", "couple dancing"),
    ("техніка", "техніка поцілунків і пестощів губами, які заводять",
     "a couple about to kiss, close-up silhouettes, warm golden light", "romantic kiss"),
    ("іграшки", "вібратор чи інший девайс для пари: як урізноманітнити ним секс",
     "a silk blindfold and a single red rose on white sheets, artistic still life", "flirting"),
    ("масаж", "чуттєвий масаж, що переходить у пестощі: техніка, олія, послідовність",
     "a relaxing couples massage with candles and towels, spa mood", "couple massage"),
    ("поза", "поза для неспішного ранкового сексу: як зробити її найприємнішою",
     "a couple cuddling under a blanket, peaceful morning light", "cuddle"),
    ("гра", "легка рольова або тактильна гра з пов'язкою, кубиками чи льодом",
     "a silk blindfold and a single red rose on white sheets, artistic still life", "flirting"),
    ("техніка", "темп і ритм: як продовжити задоволення і досягти яскравішого оргазму",
     "two hands intertwined on silk sheets, soft warm light", "love"),
    ("атмосфера", "як створити атмосферу: світло, музика, білизна, мастило",
     "a cozy bedroom with candles and soft warm light, romantic mood", "romantic candles"),
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


GEMINI_BLOCK: dict = {}                               # {модель: до якого часу не використовувати}


def gemini(prompt: str, json_mode: bool = False, relaxed: bool = False, max_tokens: int = 800) -> str | None:
    if not GEMINI_KEY:
        return None
    cfg = {"temperature": 0.7 if json_mode else 1.0, "maxOutputTokens": max(max_tokens, 1500) + 2500,
           "thinkingConfig": {"thinkingLevel": "low"}}      # «думати» коротко, щоб текст не обрізався
    if json_mode:
        cfg["responseMimeType"] = "application/json"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}
    if relaxed:                                  # відверті, але не порнографічні обговорення не блокувати
        body["safetySettings"] = [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in (
            "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT")]
    blocked = GEMINI_BLOCK
    for model in GEMINI_MODELS:
        if blocked.get(model, "") > datetime.now(TZ).isoformat():
            continue                                 # денний ліміт моделі вичерпано
        for attempt in range(3):                 # перевантаження (503/429) або модель без «думання» — ще спроба
            try:
                r = http_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                              f"?key={urllib.parse.quote(GEMINI_KEY)}", body)
                cand = (r.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                text = "".join(p_.get("text", "") for p_ in parts if not p_.get("thought")).strip()
                if text and cand.get("finishReason") == "MAX_TOKENS" and not json_mode:
                    lines = text.splitlines()        # обрізано — до останнього завершеного рядка
                    if len(lines) > 2 and not re.search(r"[.!?…»)]$", lines[-1].strip()):
                        text = "\n".join(lines[:-1])
                if text:
                    return text
                print(f"Gemini {model}: порожня відповідь ({(r.get('candidates') or [{}])[0].get('finishReason')})")
                break
            except urllib.error.HTTPError as e:
                msg = err_text(e)
                print(f"Gemini {model}: {msg[:200]}")
                if e.code == 400 and "think" in msg.lower() and "thinkingConfig" in cfg:
                    cfg.pop("thinkingConfig")         # модель без «думання» — повторюємо без параметра
                    continue
                if e.code == 429 and "quota" in msg.lower():  # ліміт на добу — не чіпаємо до ~10:00 (Київ)
                    nxt = datetime.now(TZ).replace(hour=10, minute=5, second=0, microsecond=0)
                    if nxt <= datetime.now(TZ):
                        nxt += timedelta(days=1)
                    blocked[model] = nxt.isoformat()
                    break
                if e.code in (429, 500, 503) and attempt == 0:
                    _time.sleep(3)
                    continue
                break
            except Exception as e:
                print(f"Gemini {model}: {err_text(e)[:200]}")
                break
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


def resolve_url(url: str) -> str | None:
    """Справжня адреса статті (Google News перенаправляє); None — якщо не вдалося."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            final = r.geturl()
        return None if "news.google." in final else final
    except Exception:
        return None


def para_visual(st: dict, theme: tuple, news: list) -> dict | None:
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
                scene = theme[2]
                r = http_json(f"{SYNC_URL}/img?key={urllib.parse.quote(SYNC_KEY)}", {"prompt":
                    f"{scene}, romantic tasteful artistic illustration, soft warm colors, gentle light, "
                    "fully clothed, no nudity, elegant, cinematic"})
                if r.get("image"):
                    st["para_bag"] = bag
                    return {"type": "photo", "bytes": base64.b64decode(r["image"])}
                print(f"ІІ-картинка: {r.get('error')}")
            elif kind == "gif":
                r = http_json("https://api.giphy.com/v1/gifs/search?" + urllib.parse.urlencode(
                    {"api_key": GIPHY_KEY, "q": theme[3], "limit": 25, "rating": "pg-13"}))
                data = [g for g in r.get("data", []) if (g.get("images") or {}).get("original")]
                if data:
                    o = random.choice(data)["images"]["original"]
                    st["para_bag"] = bag
                    return {"type": "animation", "url": o.get("mp4") or o.get("url")}
        except Exception as e:
            print(f"Картинка ({kind}): {err_text(e)}")
    st["para_bag"] = []
    return None


def build_para(st: dict, now: datetime, with_news: bool = True, header: str = "💞 <b>ПОРАДА 18+</b>") -> str:
    i = st.get("para_i", 0)
    th = PARA_THEMES[i % len(PARA_THEMES)]
    theme = th[1]
    st["para_i"] = i + 1
    recent = st.get("para_titles", [])[-30:]
    prompt = (
        "Ти — сексолог і консультант зі стосунків для дорослої подружньої пари (чоловік і дружина). "
        f"Напиши одну практичну пораду на тему: {theme}. "
        "Формат: перший рядок — короткий заголовок до 6 слів без лапок; далі 3–5 пунктів, кожен з нового "
        "рядка й починається з «• ». Тон теплий, відвертий, але без вульгарності; можна про техніку, проте "
        "без анатомічних подробиць; наголос на згоді, комфорті та довірі обох. Мова — українська. "
        "Без Markdown, без зірочок і решіток. "
        + (f"Не повторюй ці теми: {'; '.join(recent)}." if recent else ""))
    text = gemini(prompt) or worker_text(prompt)       # Gemini, а якщо недоступний — модель Cloudflare
    if text:
        text = re.sub(r"[*#_`]+", "", text).strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        title, body = lines[0][:80], "\n".join(lines[1:])
    else:                                          # без ключа чи при відмові моделі — вбудована база
        j = st.get("para_fb", 0)
        title, body = PARA_FALLBACK[j % len(PARA_FALLBACK)]
        st["para_fb"] = j + 1
    st["para_titles"] = (recent + [title])[-30:]
    msg = (f"{header}\n"
           f"<blockquote><b>{esc(title)}</b>\n{esc(body)}</blockquote>")
    return rich(msg, para_visual(st, th, []))         # без «топів тижня» — лише порада і картинка


def worker_text(prompt: str) -> str | None:
    """Запасна текстова модель Cloudflare Workers AI (через ваш Worker)."""
    if not WORKER_MODE:
        return None
    try:
        r = http_json(f"{SYNC_URL}/txt?key={urllib.parse.quote(SYNC_KEY)}", {"prompt": prompt})
        t = (r.get("text") or "").strip()
        return re.sub(r"[*#_`]+", "", t) or None
    except Exception as e:
        print(f"Cloudflare AI: {err_text(e)}")
        return None


# ---------- «Стаття тижня 18+»: RSS журналів (розділи Sex & Relationships) ----------
ARTICLE_FEEDS = [
    ("Cosmopolitan", "https://www.cosmopolitan.com/rss/sex-love.xml/"),
    ("Cosmopolitan", "https://www.cosmopolitan.com/rss/all.xml/"),
    ("Men's Health", "https://www.menshealth.com/rss/sex-relationships.xml/"),
    ("Men's Health", "https://www.menshealth.com/rss/all.xml/"),
    ("Women's Health", "https://www.womenshealthmag.com/rss/sex-and-love.xml/"),
    ("Women's Health", "https://www.womenshealthmag.com/rss/all.xml/"),
    ("Gottman Institute", "https://www.gottman.com/blog/feed/"),
    ("Lovehoney", "https://www.lovehoney.co.uk/blog/feed/"),
    ("Lovehoney", "https://www.lovehoney.com/blog/feed/"),
    ("WhatToy", "https://www.whattoy.co.uk/feed/"),
    ("WhatToy", "https://whattoy.co.uk/feed/"),
]
ARTICLE_WORDS = re.compile(r"\bsex|orgasm|foreplay|position|intima|libido|bedroom|kiss|desire|arous|oral|"
                           r"pleasure|couple|relationship|marriage|partner|love life|kink|toy", re.I)


def strip_html(t: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", t or "")).split())


def collect_articles(st: dict, now: datetime) -> None:
    """Раз на день: свіжі статті журналів про секс і стосунки → пул у state (для розсилки й меню)."""
    import xml.etree.ElementTree as ET
    if st.get("art_day") == now.date().isoformat():
        return
    st["art_day"] = now.date().isoformat()
    seen = set(st.get("art_seen", []))
    pool, ok_feeds = [], []
    for src, url in ARTICLE_FEEDS:
        try:
            root = ET.fromstring(http_text(url))
        except Exception as e:
            print(f"Статті: {src} {url} → {str(e)[:60]}")
            continue
        ok_feeds.append(src)
        for it in list(root.iter("item"))[:40]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            body = it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description") or ""
            text = strip_html(body)
            if not (title and link) or link in seen or not ARTICLE_WORDS.search(title + " " + text[:300]):
                continue
            pool.append({"src": src, "title": title, "link": link, "text": text[:2500]})
    uniq = {a["link"]: a for a in pool}
    st["para_articles"] = list(uniq.values())[:15]
    print(f"Статті: працюють {sorted(set(ok_feeds))}, у пулі {len(st['para_articles'])}")


def build_article(st: dict) -> str | None:
    """«Стаття тижня 18+»: найкорисніша свіжа стаття, переказ українською, прев'ю оригіналу."""
    pool = [a for a in st.get("para_articles", []) if a["link"] not in set(st.get("art_seen", []))]
    if not pool:
        return None
    listing = [{"n": i, "title": a["title"], "source": a["src"], "text": a["text"][:1200]} for i, a in enumerate(pool[:10])]
    raw = gemini("Ти — редактор рубрики для дорослої подружньої пари. Ось свіжі статті журналів про секс і стосунки. "
                 "Обери ОДНУ найкориснішу й найцікавішу для пари (техніки, пози, близькість, бажання, дослідження) і "
                 "перекажи українською природно, по-людськи. Поверни лише JSON: {\"n\": номер, \"title\": \"заголовок "
                 "українською\", \"points\": [\"4–6 пунктів суті, кожен 1–2 речення\"], \"tip\": \"одна практична "
                 "порада для пари\"}. Без вульгарності та анатомічних подробиць.\n\n" + json.dumps(listing, ensure_ascii=False),
                 json_mode=True, relaxed=True, max_tokens=2000)
    try:
        r = json.loads(raw or "")
        a = pool[int(r["n"])]
    except Exception:
        print("Стаття: Gemini не повернув переказ")
        return None
    st["art_seen"] = (st.get("art_seen", []) + [a["link"]])[-300:]
    pts = "\n".join(f"• {esc(x)}" for x in (r.get("points") or [])[:6])
    msg = (f"📖 <b>СТАТТЯ ТИЖНЯ 18+</b> · {esc(a['src'])}\n"
           f"<blockquote><b>{esc(r.get('title'))}</b>\n{pts}</blockquote>\n"
           f"💡 {esc(r.get('tip'))}\n🔗 <a href=\"{html.escape(a['link'])}\">читати оригінал</a>")
    return rich(msg, {"type": "preview", "url": a["link"]})   # зверху — обкладинка статті


# ---------- Reddit: «Обговорення тижня 18+» (пост + 5 найкращих коментарів, переклад Gemini) ----------
def reddit_api(url: str, token: str | None = None, form: dict | None = None) -> dict:
    headers = {"User-Agent": f"python:svitlo-bot:1.0 (by /u/{REDDIT_USER})"}
    data = urllib.parse.urlencode(form).encode() if form else None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{REDDIT_ID}:{REDDIT_SECRET}".encode()).decode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def build_reddit(st: dict) -> str | None:
    if not (REDDIT_ID and REDDIT_SECRET and REDDIT_USER and REDDIT_PASS and GEMINI_KEY):
        return None
    try:
        tok = reddit_api("https://www.reddit.com/api/v1/access_token",
                         form={"grant_type": "password", "username": REDDIT_USER, "password": REDDIT_PASS})["access_token"]
        seen = set(st.get("reddit_seen", []))
        posts = []
        for sub in REDDIT_SUBS:
            lst = reddit_api(f"https://oauth.reddit.com/r/{sub}/top?t=week&limit=30&raw_json=1", tok)
            for ch in (lst.get("data") or {}).get("children", []):
                d = ch.get("data") or {}
                if (d.get("is_self") and not d.get("stickied") and d.get("id") not in seen
                        and len(d.get("selftext") or "") >= 200 and (d.get("num_comments") or 0) >= 15):
                    posts.append(d)
        if not posts:
            print("Reddit: немає нових постів")
            return None
        posts.sort(key=lambda d: (d.get("score") or 0) + 3 * (d.get("num_comments") or 0), reverse=True)
        post = posts[0]
        tree = reddit_api(f"https://oauth.reddit.com/r/{post['subreddit']}/comments/{post['id']}"
                          f"?sort=top&limit=30&depth=1&raw_json=1", tok)
        coms = [c["data"] for c in (tree[1]["data"]["children"] if len(tree) > 1 else [])
                if c.get("kind") == "t1" and c["data"].get("author") not in ("AutoModerator", "[deleted]")
                and len(c["data"].get("body") or "") >= 40 and c["data"].get("body") not in ("[deleted]", "[removed]")]
        coms = sorted(coms, key=lambda c: c.get("score") or 0, reverse=True)[:5]
    except Exception as e:
        print(f"Reddit: {err_text(e)}")
        return None
    src = {"title": post["title"], "post": (post.get("selftext") or "")[:3500],
           "comments": [(c.get("body") or "")[:1200] for c in coms]}
    prompt = (
        "Переклади українською обговорення з Reddit для дорослих читачів. Перекладай природно й по-людськи, "
        "як написала б жива людина, зберігаючи зміст, інтонацію й гумор; англійський сленг передавай доречними "
        "українськими відповідниками. Пост скороти до суті, але не сильно (до ~900 символів); кожен коментар — "
        "до ~350 символів, зберігаючи головну думку. Без вульгарності, без анатомічних подробиць, без "
        "особистих даних. Поверни лише JSON: {\"title\": \"...\", \"post\": \"...\", \"comments\": [\"...\"]}.\n\n"
        + json.dumps(src, ensure_ascii=False))
    raw = gemini(prompt, json_mode=True, relaxed=True, max_tokens=2500)
    try:
        tr = json.loads(raw or "")
    except Exception:
        print("Reddit: Gemini не повернув переклад")
        return None
    st["reddit_seen"] = (st.get("reddit_seen", []) + [post["id"]])[-300:]
    url = f"https://www.reddit.com{post.get('permalink', '')}"
    parts = [f"🔥 <b>ОБГОВОРЕННЯ ТИЖНЯ 18+</b> · r/{esc(post['subreddit'])}",
             f"<blockquote><b>{esc(tr.get('title'))}</b>\n{esc(tr.get('post'))}</blockquote>"]
    cm = [c for c in (tr.get("comments") or []) if c][:5]
    if cm:
        parts.append("💬 <b>Найкращі коментарі</b>")
        parts += [f"<blockquote>{n}. {esc(c)}</blockquote>" for n, c in enumerate(cm, 1)]
    parts.append(f"👍 {num(post.get('score') or 0)} · 💬 {num(post.get('num_comments') or 0)} коментарів · "
                 f'<a href="{html.escape(url)}">оригінал</a>')
    text = "\n".join(parts)
    while len(text) > 4000 and len(parts) > 4:     # ліміт Telegram — прибираємо останні коментарі
        parts.pop(-2)
        text = "\n".join(parts)
    return text


# ---------- SMM і таргет: дайджест тижня (Google News + Gemini) ----------
SMM_QUERIES = [("Instagram нова функція", "uk"), ("TikTok нова функція", "uk"), ("таргетована реклама Meta", "uk"),
               ("Instagram new feature", "en"), ("Meta ads update", "en"), ("TikTok ads new feature", "en"),
               ("Threads new feature", "en"), ("YouTube creators new feature", "en"),
               ("Instagram algorithm reach", "en"), ("Facebook Reels update", "en")]
SMM_TAGS = {"new": "🆕", "ads": "🎯", "algo": "📊", "rumor": "👀"}
# офіційні та галузеві джерела новин (RSS); The Keyword і Meta — лише матеріали про рекламу, соцмережі, креаторів
SMM_NEWS_FEEDS = [("Meta Newsroom", "https://about.fb.com/feed/"),
                  ("Meta Newsroom", "https://about.fb.com/news/feed/"),
                  ("Social Media Today", "https://www.socialmediatoday.com/feeds/news/"),
                  ("The Keyword (Google)", "https://blog.google/products/ads-commerce/rss/"),
                  ("The Keyword (Google)", "https://blog.google/products/youtube/rss/"),
                  ("The Keyword (Google)", "https://blog.google/rss/"),
                  ("Marketing Dive", "https://www.marketingdive.com/feeds/news/")]
SMM_WORDS = re.compile(r"instagram|facebook|threads|whatsapp|reels|tiktok|youtube|shorts|creator|advertis|\bads?\b|"
                       r"campaign|target|audience|algorithm|reach|feed|social|influencer|marketing|brand|commerce|"
                       r"shopping|search|gemini|ai\b|linkedin|snapchat|pinterest|x\.com|twitter", re.I)


def gnews(q: str, lang: str) -> list[dict]:
    import xml.etree.ElementTree as ET
    loc = {"uk": ("uk", "UA", "UA:uk"), "en": ("en-US", "US", "US:en")}[lang]
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:7d", "hl": loc[0], "gl": loc[1], "ceid": loc[2]})
    try:
        root = ET.fromstring(http_text(url))
    except Exception as e:
        print(f"Google News ({q}): {e}")
        return []
    return [{"title": (it.findtext("title") or "").strip(), "link": it.findtext("link") or "",
             "source": (it.findtext("source") or "").strip()} for it in root.iter("item")]


def build_smm(st: dict, now: datetime) -> str | None:
    seen = set(st.get("smm_links", []))
    items, titles = [], set()
    ok = []
    for src, url in SMM_NEWS_FEEDS:                   # спершу офіційні й галузеві джерела за 7 днів
        try:
            got = feed_items(url, src, days=7)
            ok.append(src)
        except Exception as e:
            print(f"SMM: {src} {url} → {str(e)[:60]}")
            continue
        for it in got[:25]:
            key = it["title"].lower()[:60]
            if (it["link"] not in seen and key not in titles
                    and (src in ("Social Media Today", "Marketing Dive") or SMM_WORDS.search(it["title"] + " " + it["text"][:300]))):
                titles.add(key)
                items.append({"title": it["title"], "link": it["link"], "source": src, "date": it.get("date", "")})
    print(f"SMM: працюють {sorted(set(ok))}, новин {len(items)}")
    for q, lang in SMM_QUERIES:                       # далі — Google News як доповнення
        for it in gnews(q, lang)[:8]:
            key = it["title"].lower()[:60]
            if it["link"] and it["title"] and it["link"] not in seen and key not in titles:
                titles.add(key)
                items.append(it)
    if not items:
        return None
    items = items[:60]
    label = f"{(now - timedelta(days=7)):%d.%m}–{now:%d.%m}"
    head = f"📈 <b>SMM І ТАРГЕТ · ДАЙДЖЕСТ ЗА ТИЖДЕНЬ</b> · {label}\n"
    listing = "\n".join(f"{n}. [{it.get('date') or '—'}] {it['title']} — {it['source']}" for n, it in enumerate(items))
    raw = gemini("Ти — редактор щотижневого дайджесту для SMM-спеціалістів і таргетологів. Ось новини за тиждень "
                 "(номер. [дата] заголовок — джерело). Обери 8–10 найважливіших і найсвіжіших для практиків: нові функції "
                 "соцмереж, зміни в рекламних кабінетах і таргетингу, алгоритми й охоплення, тести й чутки. Найвищий "
                 "пріоритет — темам, про які пишуть кілька джерел одразу (це головні новини тижня), і офіційним "
                 "анонсам Meta та Google. Для кожної теми обери одну найкращу новину; без повторів і без загальних "
                 "порад. Розташуй від найважливішої. Для кожної поверни об'єкт: n (номер новини), "
                 "tag (new|ads|algo|rumor), title (короткий заголовок українською), what (1–2 речення українською: "
                 "що сталося), tip (1 речення: як використати на практиці). Відповідь — лише JSON-масив.\n\n"
                 + listing, json_mode=True, max_tokens=3000)
    blocks = []
    try:
        picked = [p_ for p_ in json.loads(raw or "") if str(p_.get("n", "")).isdigit() and int(p_["n"]) < len(items)]
        if not picked:
            raise ValueError("порожній вибір")
        for n_, p_ in enumerate(picked[:10], 1):
            it = items[int(p_["n"])]
            blocks.append("<blockquote>" + f"{SMM_TAGS.get(p_.get('tag'), '🆕')} <b>{n_}. {esc(p_.get('title'))}</b>\n"
                          f"{esc(p_.get('what'))}\n💡 {esc(p_.get('tip'))}\n"
                          f'🔗 <a href="{html.escape(it["link"])}">{esc(it["source"] or "джерело")}</a></blockquote>')
            st.setdefault("smm_links", []).append(it["link"])
    except Exception:
        print("SMM: Gemini недоступний — надсилаю заголовки без обробки")
        for n_, it in enumerate(items[:8], 1):
            blocks.append(f'<blockquote>🆕 <b>{n_}.</b> <a href="{html.escape(it["link"])}">{esc(it["title"])}</a></blockquote>')
            st.setdefault("smm_links", []).append(it["link"])
    st["smm_links"] = st.get("smm_links", [])[-500:]
    if not blocks:
        return None
    text = head + "\n".join(blocks) + "\n<i>🆕 нова функція · 🎯 реклама · 📊 алгоритми · 👀 тести й чутки</i>"
    while len(text) > 4000 and len(blocks) > 3:
        blocks.pop()
        text = head + "\n".join(blocks) + "\n<i>🆕 нова функція · 🎯 реклама · 📊 алгоритми · 👀 тести й чутки</i>"
    practice = build_smm_practice(st, now)            # друге повідомлення — фішки практиків (Reddit, блоги, Medium)
    return text + (SPLIT + practice if practice else "")


def reddit_token() -> str | None:
    if not (REDDIT_ID and REDDIT_SECRET and REDDIT_USER and REDDIT_PASS):
        return None
    try:
        return reddit_api("https://www.reddit.com/api/v1/access_token",
                          form={"grant_type": "password", "username": REDDIT_USER, "password": REDDIT_PASS})["access_token"]
    except Exception as e:
        print(f"Reddit токен: {err_text(e)}")
        return None


# ---------- Загальні RSS-пули (блоги, Medium, подкасти) ----------
def feed_items(url: str, src: str, days: int = 10) -> list[dict]:
    """Елементи RSS/Atom за останні days днів: title, link, text, src."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    root = ET.fromstring(http_text(url))
    out, now_ = [], datetime.now(TZ)
    for it in list(root.iter("item"))[:40]:
        title, link = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip()
        body = (it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description")
                or it.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}summary") or "")
        date = ""
        try:
            d = parsedate_to_datetime(it.findtext("pubDate") or "")
            if d and (now_ - d.astimezone(TZ)).days > days:
                continue
            date = d.astimezone(TZ).strftime("%d.%m") if d else ""
        except Exception:
            pass
        if title and link:
            out.append({"src": src, "title": title, "link": link, "text": strip_html(body)[:2500], "date": date})
    return out


def collect_pool(st: dict, key: str, feeds: list, words: re.Pattern | None, now: datetime, limit: int = 20) -> None:
    """Раз на день оновлюємо пул st[key] зі списку RSS (лише нові, за темою)."""
    if st.get(f"{key}_day") == now.date().isoformat():
        return
    st[f"{key}_day"] = now.date().isoformat()
    seen = set(st.get(f"{key}_seen", []))
    pool, ok = [], []
    for src, url in feeds:
        try:
            items = feed_items(url, src)
            ok.append(src)
        except Exception as e:
            print(f"{key}: {src} {url} → {str(e)[:60]}")
            continue
        pool += [a for a in items if a["link"] not in seen and (not words or words.search(a["title"] + " " + a["text"][:400]))]
    st[key] = list({a["link"]: a for a in pool}.values())[:limit]
    print(f"{key}: працюють {sorted(set(ok))}, у пулі {len(st[key])}")


def podcast_feeds(terms: list[str], per_term: int = 3) -> list[tuple[str, str]]:
    """Адреси RSS подкастів за темою — через безкоштовний пошук Apple Podcasts."""
    out = []
    for t in terms:
        try:
            r = http_json("https://itunes.apple.com/search?" + urllib.parse.urlencode(
                {"term": t, "media": "podcast", "limit": per_term}))
            out += [(f"🎙 {x.get('collectionName')}", x["feedUrl"]) for x in r.get("results", []) if x.get("feedUrl")]
        except Exception as e:
            print(f"Подкасти ({t}): {err_text(e)[:60]}")
    return list(dict(out).items()) if out else []


SMM_FEEDS = [("Jon Loomer", "https://www.jonloomer.com/feed/"),
             ("Social Media Examiner", "https://www.socialmediaexaminer.com/feed/"),
             ("AdEspresso", "https://adespresso.com/feed/"),
             ("Hootsuite", "https://blog.hootsuite.com/feed/"),
             ("Buffer", "https://buffer.com/resources/rss/"),
             ("Later", "https://later.com/blog/feed/"),
             ("Medium · facebook-ads", "https://medium.com/feed/tag/facebook-ads"),
             ("Medium · tiktok-ads", "https://medium.com/feed/tag/tiktok-ads"),
             ("Medium · instagram-marketing", "https://medium.com/feed/tag/instagram-marketing"),
             ("Medium · ppc", "https://medium.com/feed/tag/ppc")]
EXP_FEEDS = [("Medium · sex", "https://medium.com/feed/tag/sex"),
             ("Medium · intimacy", "https://medium.com/feed/tag/intimacy"),
             ("Medium · sexuality", "https://medium.com/feed/tag/sexuality"),
             ("Medium · relationships", "https://medium.com/feed/tag/relationships")]


def build_smm_practice(st: dict, now: datetime | None = None) -> str | None:
    """«Фішки від практиків»: блоги практиків і Medium, переклад Gemini."""
    if not GEMINI_KEY:
        return None
    now = now or datetime.now(TZ)
    cands = []
    tok = None                                        # Reddit для SMM вимкнено — замість нього блоги та офіційні джерела
    if tok:
        seen = set(st.get("smm_reddit_seen", []))
        posts = []
        for sub in SMM_SUBS:
            try:
                lst = reddit_api(f"https://oauth.reddit.com/r/{sub}/top?t=week&limit=25&raw_json=1", tok)
            except Exception as e:
                print(f"Reddit r/{sub}: {err_text(e)}")
                continue
            posts += [ch["data"] for ch in (lst.get("data") or {}).get("children", [])
                      if ch.get("data", {}).get("is_self") and not ch["data"].get("stickied")
                      and ch["data"].get("id") not in seen and (ch["data"].get("num_comments") or 0) >= 10]
        posts.sort(key=lambda d: (d.get("score") or 0) + 3 * (d.get("num_comments") or 0), reverse=True)
        for d in posts[:3]:
            try:
                tree = reddit_api(f"https://oauth.reddit.com/r/{d['subreddit']}/comments/{d['id']}?sort=top&limit=10&depth=1&raw_json=1", tok)
                best = [c["data"].get("body", "")[:800] for c in tree[1]["data"]["children"]
                        if c.get("kind") == "t1" and c["data"].get("author") != "AutoModerator"
                        and len(c["data"].get("body") or "") >= 40][:2]
            except Exception:
                best = []
            cands.append({"src": f"r/{d['subreddit']}", "title": d["title"], "id": d["id"],
                          "link": f"https://www.reddit.com{d.get('permalink', '')}",
                          "text": (d.get("selftext") or "")[:1200] + "\nКоментарі: " + " | ".join(best),
                          "meta": f"👍 {num(d.get('score') or 0)} · 💬 {num(d.get('num_comments') or 0)} · "})
    collect_pool(st, "smm_pool", SMM_FEEDS, None, now)
    cands += [dict(a, meta="") for a in st.get("smm_pool", [])[:12]]
    if not cands:
        return None
    src = [{"n": i, "source": c["src"], "title": c["title"], "text": c["text"][:1200]} for i, c in enumerate(cands)]
    raw = gemini("Ти — редактор дайджесту для SMM-спеціалістів і таргетологів. Ось свіжі матеріали тижня від практиків "
                 "(обговорення Reddit, блоги, Medium). Обери 4 з найкориснішими практичними фішками (налаштування "
                 "реклами, креативи, охоплення, алгоритми, кейси з цифрами); без загальних порад і без повторів. Для "
                 "кожної поверни об'єкт: n, title (короткий заголовок українською), summary (2–3 речення: про що), "
                 "tip (1–2 речення: головна практична фішка). Перекладай природно, без кальок. Відповідь — лише JSON-масив.\n\n"
                 + json.dumps(src, ensure_ascii=False), json_mode=True, max_tokens=2500)
    try:
        items = [x for x in json.loads(raw or "") if str(x.get("n", "")).isdigit() and int(x["n"]) < len(cands)]
    except Exception:
        return None
    blocks = []
    for k, x in enumerate(items[:4], 1):
        c = cands[int(x["n"])]
        blocks.append("<blockquote>" + f"💬 <b>{k}. {esc(x.get('title'))}</b> · {esc(c['src'])}\n"
                      f"{esc(x.get('summary'))}\n💡 {esc(x.get('tip'))}\n"
                      f'{c["meta"]}<a href="{html.escape(c["link"])}">джерело</a></blockquote>')
        if c.get("id"):
            st.setdefault("smm_reddit_seen", []).append(c["id"])
        else:
            st.setdefault("smm_pool_seen", []).append(c["link"])
    st["smm_reddit_seen"] = st.get("smm_reddit_seen", [])[-300:]
    st["smm_pool_seen"] = st.get("smm_pool_seen", [])[-500:]
    return "💬 <b>ФІШКИ ВІД ПРАКТИКІВ</b> · за тиждень\n" + "\n".join(blocks) if blocks else None


# ---------- «Досвід тижня 18+»: Medium (sex, intimacy…) і подкасти ----------
def collect_experience(st: dict, now: datetime) -> None:
    if st.get("exp_pool_day") == now.date().isoformat():
        return
    feeds = EXP_FEEDS + podcast_feeds(["Sex Unwrapped", "sex relationships", "couples intimacy"])
    collect_pool(st, "exp_pool", feeds, ARTICLE_WORDS, now)


def build_experience(st: dict) -> str | None:
    pool = [a for a in st.get("exp_pool", []) if a["link"] not in set(st.get("exp_pool_seen", []))]
    if not pool:
        return None
    listing = [{"n": i, "title": a["title"], "source": a["src"], "text": a["text"][:1200]} for i, a in enumerate(pool[:10])]
    raw = gemini("Ти — редактор рубрики для дорослої подружньої пари. Ось свіжі особисті історії, есе й теми подкастів "
                 "про секс і близькість. Обери ОДНУ найцікавішу й найкориснішу для пари і перекажи українською природно, "
                 "по-людськи: що за досвід або тема, що з неї можна взяти. Поверни лише JSON: {\"n\": номер, "
                 "\"title\": \"заголовок українською\", \"points\": [\"3–5 пунктів, кожен 1–2 речення\"], "
                 "\"tip\": \"висновок або порада для пари\"}. Без вульгарності та анатомічних подробиць.\n\n"
                 + json.dumps(listing, ensure_ascii=False), json_mode=True, relaxed=True, max_tokens=2000)
    try:
        r = json.loads(raw or "")
        a = pool[int(r["n"])]
    except Exception:
        print("Досвід: Gemini не повернув переказ")
        return None
    st["exp_pool_seen"] = (st.get("exp_pool_seen", []) + [a["link"]])[-300:]
    pts = "\n".join(f"• {esc(x)}" for x in (r.get("points") or [])[:5])
    return (f"💬 <b>ДОСВІД ТИЖНЯ 18+</b> · {esc(a['src'])}\n"
            f"<blockquote><b>{esc(r.get('title'))}</b>\n{pts}</blockquote>\n"
            f"💡 {esc(r.get('tip'))}\n🔗 <a href=\"{html.escape(a['link'])}\">оригінал</a>")


# ---------- «Поради 18+»: свіжі матеріали — статті, огляди іграшок, досвід (по одному) ----------
# (джерело, адреса RSS, тип, скільки днів вважаємо свіжим, чи фільтрувати за темою)
CONTENT_FEEDS = [
    ("Cosmopolitan", "https://www.cosmopolitan.com/rss/sex-love.xml/", "article", 14, False),
    ("Cosmopolitan", "https://www.cosmopolitan.com/rss/all.xml/", "article", 14, True),
    ("Men's Health", "https://www.menshealth.com/rss/sex-relationships.xml/", "article", 14, False),
    ("Men's Health", "https://www.menshealth.com/rss/all.xml/", "article", 14, True),
    ("Women's Health", "https://www.womenshealthmag.com/rss/sex-and-love.xml/", "article", 14, False),
    ("Women's Health", "https://www.womenshealthmag.com/rss/all.xml/", "article", 14, True),
    ("Gottman Institute", "https://www.gottman.com/blog/feed/", "article", 30, True),
    ("Sex With Emily", "https://sexwithemily.com/feed/", "article", 30, False),
    ("Kinkly", "https://www.kinkly.com/feed", "article", 30, False),
    ("Lovehoney", "https://www.lovehoney.co.uk/blog/feed/", "review", 45, False),
    ("Lovehoney", "https://www.lovehoney.com/blog/feed/", "review", 45, False),
    ("WhatToy", "https://www.whattoy.co.uk/feed/", "review", 45, False),
    ("WhatToy", "https://whattoy.co.uk/feed/", "review", 45, False),
    ("Dildo or Dildon't", "https://www.dildoordildont.com/feed/", "review", 60, False),
    ("Hey Epiphora", "https://heyepiphora.com/feed/", "review", 60, False),
    ("Bedbible", "https://bedbible.com/feed/", "review", 45, False),
    ("Girl on the Net", "https://www.girlonthenet.com/feed/", "experience", 45, False),
    ("LELO", "https://www.lelo.com/blog/feed/", "review", 60, False),
    ("Good Vibrations", "https://www.goodvibes.com/blog/feed/", "review", 60, False),
    ("Autostraddle", "https://www.autostraddle.com/category/sex-and-dating/feed/", "article", 30, False),
    ("Glamour", "https://www.glamour.com/feed/tag/sex/latest/rss", "article", 21, False),
    ("Self", "https://www.self.com/feed/rss", "article", 21, True),
    ("Mashable", "https://mashable.com/feeds/rss/all", "review", 21, True),
    ("Refinery29", "https://www.refinery29.com/en-us/sex/rss.xml", "article", 21, False),
    ("Medium", "https://medium.com/feed/tag/sex-tips", "experience", 30, True),
    ("Medium", "https://medium.com/feed/tag/sex-toys", "review", 45, True),
    ("Medium", "https://medium.com/feed/tag/sex", "experience", 21, True),
    ("Medium", "https://medium.com/feed/tag/intimacy", "experience", 21, True),
    ("Medium", "https://medium.com/feed/tag/sexuality", "experience", 21, True),
    ("Medium", "https://medium.com/feed/tag/relationships", "experience", 21, True),
]
KIND_HEAD = {"tip": "💞 <b>ПОРАДА 18+</b>", "technique": "🔥 <b>ТЕХНІКА 18+</b>", "article": "📖 <b>СТАТТЯ 18+</b>",
             "review": "🧸 <b>ОГЛЯД 18+</b>", "experience": "💬 <b>ДОСВІД 18+</b>", "podcast": "🎙 <b>ПОДКАСТ 18+</b>",
             "discussion": "🗣 <b>ОБГОВОРЕННЯ 18+</b>"}


def norm_title(t: str) -> str:
    return re.sub(r"[^a-zа-яіїєґ0-9]+", " ", (t or "").lower()).strip()[:70]


def collect_content(st: dict, now: datetime) -> None:
    """Раз на день: свіжі матеріали з усіх джерел → пул st["content_pool"] (без повторів і дублікатів)."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    if st.get("content_day") == now.date().isoformat():
        return
    st["content_day"] = now.date().isoformat()
    seen = set(st.get("content_seen", [])) | set(st.get("art_seen", [])) | set(st.get("exp_pool_seen", []))
    feeds = list(CONTENT_FEEDS) + [(n, u, "podcast", 21, True) for n, u in podcast_feeds(
        ["Sex Unwrapped", "sex relationships", "couples intimacy"])]
    pool, titles, ok = [], set(), []
    for src, url, kind, days, filt in feeds:
        try:
            root = ET.fromstring(http_text(url))
            ok.append(src)
        except Exception as e:
            print(f"Поради 18+: {src} {url} → {str(e)[:60]}")
            continue
        for it in list(root.iter("item"))[:60]:
            title, link = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip()
            body = (it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or it.findtext("description")
                    or it.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}summary") or "")
            text = strip_html(body)
            try:
                d = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(TZ)
                age = (now - d).days
            except Exception:
                age = days // 2
            nt = norm_title(title)
            if (not title or not link or age > days or link in seen or nt in titles
                    or (filt and not ARTICLE_WORDS.search(title + " " + text[:400]))):
                continue
            com = it.findtext("{http://purl.org/rss/1.0/modules/slash/}comments") or "0"
            titles.add(nt)
            pool.append({"src": src.replace("🎙 ", ""), "kind": kind, "title": title, "link": link, "age": age,
                         "comments": int(com) if str(com).isdigit() else 0, "text": text[:900]})
    pool.sort(key=lambda a: a["age"])
    st["content_pool"] = pool[:80]
    print(f"Поради 18+: працюють {sorted(set(ok))}, у пулі {len(st['content_pool'])} "
          f"(статті {sum(a['kind'] == 'article' for a in pool)}, огляди {sum(a['kind'] == 'review' for a in pool)}, "
          f"досвід {sum(a['kind'] in ('experience', 'podcast') for a in pool)})")


PREFER = re.compile(r"position|technique|how to|toy|vibrat|dildo|wand|plug|lube|review|oral|blow|cunniling|"
                    r"orgasm|foreplay|anal|kink|bdsm|bondage|massage|handjob|fingering|masturbat|sex tip|sex move", re.I)
DOWNPLAY = re.compile(r"breakup|divorce|dating app|marriage advice|communicat|argument|therapy|mental health|"
                      r"relationship advice|loneliness|red flag|cheating", re.I)


def topic_weight(a: dict) -> float:
    """Техніка, пози, іграшки й девайси — частіше; проблеми стосунків — рідше."""
    t = a.get("title", "") + " " + a.get("text", "")[:300]
    return (2.5 if PREFER.search(t) else 1.0) * (0.35 if DOWNPLAY.search(t) else 1.0)


def pick_content(pool: list, seen: set, last: dict) -> dict | None:
    """Випадковий свіжий матеріал: важить свіжість і кількість коментарів; тип і джерело — не як минулого разу."""
    cands = [a for a in pool if a["link"] not in seen]
    if not cands:
        return None
    pref = [a for a in cands if a["kind"] != last.get("kind") and a["src"] != last.get("src")] or cands
    weights = [(1 + min(a.get("comments", 0), 60) / 10) * (1.5 if a["age"] <= 7 else 1.0) * topic_weight(a) for a in pref]
    return random.choices(pref, weights=weights, k=1)[0]


def build_content(st: dict) -> str | None:
    """Один свіжий матеріал (стаття / огляд / досвід / подкаст): переказ українською, тип — у заголовку."""
    seen = set(st.get("content_seen", []))
    a = pick_content(st.get("content_pool", []), seen, st.get("content_last") or {})
    if not a:
        return None
    raw = gemini("Ти — редактор рубрики «Поради 18+» для дорослих. Ось один свіжий матеріал (стаття, огляд інтимного "
                 "товару, особистий досвід або тема подкасту). Перекажи його українською природно, по-людськи, "
                 "зберігаючи головне: для огляду — що за товар, плюси, мінуси, кому підійде; для техніки — як саме "
                 "робити. Визнач тип: tip | technique | article | review | experience | podcast | discussion. "
                 "Поверни лише JSON: {\"kind\": \"тип\", \"title\": \"заголовок українською\", "
                 "\"points\": [\"4–6 пунктів, кожен 1–2 речення\"], \"tip\": \"практичний висновок\"}. "
                 "Без вульгарності та анатомічних подробиць.\n\n"
                 + json.dumps({"source": a["src"], "title": a["title"], "text": a["text"]}, ensure_ascii=False),
                 json_mode=True, relaxed=True, max_tokens=2000)
    try:
        r = json.loads(raw or "")
    except Exception:
        print("Поради 18+: Gemini не повернув переказ")
        return None
    st["content_seen"] = (st.get("content_seen", []) + [a["link"]])[-600:]
    st["content_last"] = {"kind": a["kind"], "src": a["src"]}
    head = KIND_HEAD.get(r.get("kind"), KIND_HEAD.get(a["kind"], "📖 <b>СТАТТЯ 18+</b>"))
    pts = "\n".join(f"• {esc(x)}" for x in (r.get("points") or [])[:6])
    com = f" · 💬 {a['comments']}" if a.get("comments") else ""
    msg = (f"{head} · {esc(a['src'])}{com}\n<blockquote><b>{esc(r.get('title'))}</b>\n{pts}</blockquote>\n"
           f"💡 {esc(r.get('tip'))}\n🔗 <a href=\"{html.escape(a['link'])}\">оригінал</a>")
    return rich(msg, {"type": "preview", "url": a["link"]})


# ---------- Фактична наявність світла (СвітлоБот каналу ЖК) — лише вам і дружині ----------
FACT_CHANNEL = "yaskravyi_power"


def fact_kind(text: str) -> str | None:
    t = text.lower()
    if "невдовзі" in t or "імовірн" in t:
        return None                                   # попередження, а не факт
    if "електроживлення відсутнє" in t:
        return "off"
    if "електроживлення відновлено" in t:
        return "on"
    return None


def fact_intervals(events: list, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Інтервали без світла у вікні [start, end] за подіями [[iso, kind], ...]."""
    ev = sorted((datetime.fromisoformat(t), k) for t, k in events)
    state = "on"
    for t, k in ev:                                   # стан на початок вікна
        if t <= start:
            state = k
    out, off_from = [], (start if state == "off" else None)
    for t, k in ev:
        if t <= start or t > end:
            continue
        if k == "off" and off_from is None:
            off_from = t
        elif k == "on" and off_from is not None:
            out.append((off_from, t))
            off_from = None
    if off_from is not None:
        out.append((off_from, end))
    return out


def fact_png(start: datetime, end: datetime, offs: list) -> bytes | None:
    """Тонка стрічка за добу: зелене — світло є, червоне — немає; позначки годин."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    W, H, X0, X1, Y0, Y1 = 1000, 120, 30, 970, 34, 66
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except Exception:
        font = ImageFont.load_default()
    span = (end - start).total_seconds()
    x = lambda t: X0 + (X1 - X0) * (t - start).total_seconds() / span
    d.rounded_rectangle([X0, Y0, X1, Y1], radius=8, fill=(72, 187, 120))
    for a, b in offs:
        d.rectangle([x(a), Y0, max(x(b), x(a) + 3), Y1], fill=(229, 62, 62))
    t = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while t < end:
        if t.hour % 3 == 0:
            xx = x(t)
            d.line([xx, Y1 + 2, xx, Y1 + 10], fill=(120, 120, 120), width=2)
            d.text((xx - 11, Y1 + 14), f"{t:%H}", fill=(80, 80, 80), font=font)
        t += timedelta(hours=1)
    d.text((X0, 6), f"{start:%d.%m %H:%M}", fill=(80, 80, 80), font=font)
    d.text((X1 - 150, 6), f"{end:%d.%m %H:%M}", fill=(80, 80, 80), font=font)
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def fact_update(st: dict, now: datetime, reason: str = "") -> list[str]:
    """Нові події «світло зникло / відновлено» з каналу ЖК → повідомлення зі стрічкою за добу."""
    try:
        posts = channel_posts(FACT_CHANNEL)
    except Exception as e:
        print(f"Факт світла: канал недоступний ({str(e)[:60]})")
        return []
    f = st.get("fact") or {}
    first = "events" not in f
    events, last_id, new = f.get("events", []), f.get("last", 0), []
    for pid, dt, text in posts:
        k = fact_kind(text)
        if k and pid > last_id:
            new.append((pid, dt, k))
            events.append([dt.isoformat(), k])
    if posts:
        f["last"] = max(last_id, posts[-1][0])
    f["events"] = sorted([e for e in events if now - datetime.fromisoformat(e[0]) < timedelta(hours=48)])
    st["fact"] = f
    if f["events"]:
        k_last = f["events"][-1][1]
        print(f"Факт світла: зараз {'НЕМАЄ' if k_last == 'off' else 'є'} з {f['events'][-1][0][11:16]} "
              f"(подій за 48 год: {len(f['events'])}, нових: {len(new)})")
    if first or not new:
        return []                                      # перший запуск — лише запам'ятовуємо
    msgs = []
    for pid, dt, k in new:
        if k == "off":
            msgs.append(f"🔴 <b>СВІТЛО ЗНИКЛО</b> о <b>{dt:%H:%M}</b>" + (f"\n{reason}" if reason else ""))
        else:
            prev = [datetime.fromisoformat(t) for t, kk in f["events"] if kk == "off" and datetime.fromisoformat(t) < dt]
            lasted = f" · не було <b>{dur(dt - prev[-1])}</b>" if prev else ""
            msgs.append(f"🟢 <b>СВІТЛО Є</b> з <b>{dt:%H:%M}</b>{lasted}")
    start, end = now - timedelta(hours=24), now
    offs = fact_intervals(f["events"], start, end)
    total = sum((b - a for a, b in offs), timedelta())
    lines = "\n".join(f"🔴 {a:%H:%M}–{b:%H:%M} · {dur(b - a)}" for a, b in offs) or "🟢 відключень не було"
    caption = ("\n".join(msgs) + f"\n<blockquote>За добу без світла: <b>{dur(total)}</b>\n{lines}</blockquote>\n"
               "<i>фактично, за даними СвітлоБота будинку</i>")
    png = fact_png(start, end, offs)
    return [rich(caption, {"type": "photo", "bytes": png}) if png else caption]


def fact_now_line(st: dict, now: datetime) -> str:
    ev = (st.get("fact") or {}).get("events") or []
    if not ev:
        return ""
    t, k = datetime.fromisoformat(ev[-1][0]), ev[-1][1]
    return (f"🔌 <b>Фактично:</b> світла немає з <b>{t:%H:%M}</b> (вже {dur(now - t)})\n" if k == "off"
            else f"🔌 <b>Фактично:</b> світло є з <b>{t:%H:%M}</b>\n")


# ---------- Акції Сільпо (магазин на Калнишевського, 2) ----------
SILPO_API = "https://sf-ecom-api.silpo.ua"
SKIP_PROMO = re.compile(r"\d\s*\+\s*\d|друг(ий|у)|при купівлі|при покупці|персональн", re.I)
CAT_EMOJI = [("м'яс", "🥩"), ("мяс", "🥩"), ("риб", "🐟"), ("молоч", "🥛"), ("сир", "🧀"), ("яйц", "🥚"),
             ("овоч", "🥦"), ("фрукт", "🍎"), ("хліб", "🍞"), ("випіч", "🥐"), ("солод", "🍫"), ("снек", "🍿"),
             ("кав", "☕"), ("чай", "🍵"), ("напо", "🥤"), ("вод", "💧"), ("алкогол", "🍷"), ("вин", "🍷"),
             ("пив", "🍺"), ("заморож", "🧊"), ("бакал", "🥫"), ("консерв", "🥫"), ("ковбас", "🌭"),
             ("кулінар", "🍱"), ("побут", "🧴"), ("гігієн", "🧼"), ("космет", "💄"), ("дит", "🧸"),
             ("тварин", "🐾"), ("дім", "🏠")]


def silpo_get(path: str, **query) -> dict:
    q = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None}, doseq=True)
    req = urllib.request.Request(f"{SILPO_API}{path}" + (f"?{q}" if q else ""), headers={
        "accept": "application/json", "accept-language": "uk,en;q=0.9", "origin": "https://silpo.ua",
        "referer": "https://silpo.ua/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _find_store(obj, needle: str, path: str = "") -> list[tuple[str, dict]]:
    """Рекурсивно шукає у відповіді API об'єкти, де будь-яке текстове поле містить needle."""
    out = []
    if isinstance(obj, dict):
        if any(isinstance(v, str) and needle.lower() in v.lower() for v in obj.values()):
            out.append((path, obj))
        for k, v in obj.items():
            out += _find_store(v, needle, f"{path}.{k}")
    elif isinstance(obj, list):
        for n_, v in enumerate(obj[:2000]):
            out += _find_store(v, needle, f"{path}[{n_}]")
    return out


def silpo_probe_pickup() -> tuple[str, str] | None:
    """Шукаємо фізичний магазин на Калнишевського серед точок самовивозу (кілька можливих запитів)."""
    ext = "https://sf-external-api.silpo.ua"
    cands = [(SILPO_API, "/v1/uk/branches", {"deliveryType": "SelfPickup"}),
             (SILPO_API, "/v1/branches", {"deliveryType": "SelfPickup"}),
             (SILPO_API, "/v1/self-pickup/branches", {}), (SILPO_API, "/v2/self-pickup/branches", {}),
             (SILPO_API, "/v1/uk/self-pickup/branches", {}), (SILPO_API, "/v1/branches/self-pickup", {}),
             (SILPO_API, "/v1/polygons/self-pickup", {"latitude": SILPO_LAT, "longitude": SILPO_LON}),
             (SILPO_API, "/v1/self-pickup", {"latitude": SILPO_LAT, "longitude": SILPO_LON}),
             (ext, "/v1/uk/stores", {}), (ext, "/v1/stores", {}), (ext, "/api/v1/stores", {}),
             (ext, "/v1/uk/shops", {}), (ext, "/v1/branches", {})]
    for base, path, q in cands:
        url = f"{base}{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")
        try:
            req = urllib.request.Request(url, headers={"accept": "application/json", "origin": "https://silpo.ua",
                                                       "referer": "https://silpo.ua/", "user-agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
        except Exception as e:
            print(f"Сільпо пошук магазину: {url} → {err_text(e)[:80]}")
            continue
        found = _find_store(data, SILPO_STORE)
        print(f"Сільпо пошук магазину: {url} → OK, знайдено «{SILPO_STORE}»: {len(found)}")
        for pth, o in found[:3]:
            print(f"   {pth}: {json.dumps(o, ensure_ascii=False)[:300]}")
        for _, o in found:
            bid = o.get("branchId") or o.get("id") or o.get("storeId") or o.get("filialId")
            if bid:
                return str(bid), "SelfPickup"
    return None


def silpo_branch(st: dict) -> tuple[str, str] | None:
    """Магазин: ручний SILPO_BRANCH → магазин на Калнишевського (самовивіз) → онлайн-магазин за адресою."""
    if SILPO_BRANCH:
        return SILPO_BRANCH, "SelfPickup"
    if st.get("silpo_branch") and (st["silpo_branch"].get("store") or st.get("silpo_probe") == today_str()):
        return st["silpo_branch"]["id"], st["silpo_branch"]["type"]
    if st.get("silpo_probe") != today_str():          # пошук фізичного магазину — не частіше разу на день
        st["silpo_probe"] = today_str()
        found = silpo_probe_pickup()
        if found:
            st["silpo_branch"] = {"id": found[0], "type": found[1], "name": "Сільпо, вул. Калнишевського, 2", "store": True}
            st.pop("silpo_last", None)
            st.pop("silpo_week", None)
            print("Сільпо: знайдено магазин на Калнишевського — перемикаюсь на нього")
            return found
    try:
        modes = silpo_get("/v1/polygons/contains/all", latitude=SILPO_LAT, longitude=SILPO_LON)
        modes = modes if isinstance(modes, list) else (modes.get("items") or [modes])
        print("Сільпо, магазини поруч: " + "; ".join(f"{m.get('name')} ({m.get('deliveryType')})" for m in modes))
        m = next((m for m in modes if SILPO_STORE.lower() in str(m.get("name", "")).lower()), None) or \
            next((m for m in modes if m.get("deliveryType") == "DeliveryHome"), modes[0])
        st["silpo_branch"] = {"id": m["branchId"], "type": m.get("deliveryType") or "DeliveryHome",
                              "name": "онлайн-магазин Сільпо (доставка на Мінський масив)", "store": False}
        print(f"Сільпо: магазин на Калнишевського не знайдено — беру «{m.get('name')}»")
        return m["branchId"], st["silpo_branch"]["type"]
    except Exception as e:
        print(f"Сільпо, магазин: {err_text(e)}")
        return None


def today_str() -> str:
    return datetime.now(TZ).date().isoformat()


def silpo_categories(branch: str) -> list[dict]:
    data = silpo_get(f"/v1/uk/branches/{branch}/categories")
    cats = data.get("items") if isinstance(data, dict) else data
    return [c for c in (cats or []) if not c.get("parentId")]


def silpo_products(branch: str, dtype: str, category: str, promo: bool, limit: int = 100, max_pages: int = 30) -> list:
    out = []
    for page in range(max_pages):
        data = silpo_get(f"/v1/uk/branches/{branch}/products", limit=limit, offset=page * limit, category=category,
                         includeChildCategories="true", deliveryType=dtype, sortBy="popularity",
                         sortDirection="desc", inStock="true", mustHavePromotion="true" if promo else None)
        items = data.get("items") or []
        out += items
        if len(items) < limit or len(out) >= (data.get("total") or 0):
            break
    return out


def p_price(p: dict) -> float:
    return float(p.get("displayPrice") or p.get("price") or 0)


def p_old(p: dict) -> float:
    return float(p.get("displayOldPrice") or p.get("oldPrice") or 0)


def silpo_snapshot(st: dict, now: datetime) -> None:
    """Щосереди: ціни всіх товарів магазину → silpo_prices.json (зберігаємо 5 тижнів)."""
    br = silpo_branch(st)
    if not br:
        return
    path = Path("silpo_prices.json")
    try:
        hist = json.loads(path.read_text("utf-8"))
    except Exception:
        hist = {}
    snap = {}
    try:
        for c in silpo_categories(br[0]):
            for p_ in silpo_products(br[0], br[1], c.get("slug"), promo=False):
                if p_.get("id") and p_price(p_):
                    snap[str(p_["id"])] = round(p_price(p_), 2)
    except Exception as e:
        print(f"Сільпо, знімок цін: {err_text(e)}")
    if len(snap) < 100:
        print(f"Сільпо: знімок неповний ({len(snap)} товарів) — не зберігаю")
        return
    hist[now.date().isoformat()] = snap
    for k in sorted(hist)[:-5]:
        del hist[k]
    path.write_text(json.dumps(hist, separators=(",", ":")), "utf-8")
    st["silpo_snap"] = now.date().isoformat()
    print(f"Сільпо: збережено ціни {len(snap)} товарів")


def build_silpo(st: dict, now: datetime) -> str | None:
    br = silpo_branch(st)
    if not br:
        return None
    try:
        hist = json.loads(Path("silpo_prices.json").read_text("utf-8"))
    except Exception:
        hist = {}
    weeks = sorted(hist)[-3:]                          # мінімальна ціна за останні 1–3 тижні
    sections, total = [], 0
    more: dict = {}                                   # «➕ ще 5»: решта позицій кожної категорії
    try:
        cats = silpo_categories(br[0])
    except Exception as e:
        print(f"Сільпо, категорії: {err_text(e)}")
        return None
    for c in cats:
        try:
            prods = silpo_products(br[0], br[1], c.get("slug"), promo=True, limit=100, max_pages=3)
        except Exception as e:
            print(f"Сільпо, {c.get('title')}: {err_text(e)}")
            continue
        cand = []
        for idx, p_ in enumerate(prods):
            price, old = p_price(p_), p_old(p_)
            promos = " ".join(str(x.get("title") or x.get("name") or x.get("type") or "")
                              for x in (p_.get("promotions") or []) if isinstance(x, dict))
            if not price or not old or old <= price or SKIP_PROMO.search(promos):
                continue                              # лише пряме зниження ціни, без «2+1»
            declared = (old - price) / old * 100
            past = [hist[w].get(str(p_.get("id"))) for w in weeks]
            past = [x for x in past if x]
            real = (min(past) - price) / min(past) * 100 if past else None
            rating = p_.get("guestProductRating")
            score = (real if real is not None else declared) + ((float(rating) - 4) * 5 if rating else 0) + max(0, 30 - idx) / 6
            if real is not None and real < 3:
                continue                              # «акція», яка насправді не дешевша — пропускаємо
            cand.append((score, p_, price, old, declared, real, rating, "ціна тижня" in promos.lower()))
        cand.sort(key=lambda x: x[0], reverse=True)
        if not cand:
            continue
        title = str(c.get("title") or c.get("name") or "")
        emo = next((e for k, e in CAT_EMOJI if k in title.lower()), "🛒")
        rows = []
        for n_, (_, p_, price, old, declared, real, rating, week) in enumerate(cand[:SILPO_TOP + 20], 1):
            ratio = f", {esc(p_.get('displayRatio') or p_.get('ratio'))}" if (p_.get("displayRatio") or p_.get("ratio")) else ""
            real_t = f" · <b>реально −{real:.0f}%</b>" if real is not None else ""
            rate_t = f" · ⭐ {float(rating):.1f}" if rating else ""
            rows.append(f"{n_}. {'🔥 ' if week else ''}{esc(p_.get('title'))}{ratio} — <b>{price:g} ₴</b> <s>{old:g} ₴</s> · "
                        f"заявлено −{declared:.0f}%{real_t}{rate_t}\n"
                        f'🔗 <a href="https://silpo.ua/product/{html.escape(str(p_.get("slug") or ""))}">відкрити</a>')
        ci = str(total)
        if len(rows) > SILPO_TOP:
            more[ci] = {"t": f"{emo} <b>{esc(title)}</b>", "b": f"{emo} {title[:16]}", "rows": rows[SILPO_TOP:]}
        sections.append((ci, "<blockquote>" + "\n".join([f"{emo} <b>{esc(title)}</b>"] + rows[:SILPO_TOP]) + "</blockquote>"))
        total += 1
    if not sections:
        return None
    head = (f"🛒 <b>АКЦІЇ СІЛЬПО · ТОП ЗА ТИЖДЕНЬ</b> · з {now:%d.%m}\n"
            f"<i>{'Ціни ' + esc((st.get('silpo_branch') or {}).get('name')) if not (st.get('silpo_branch') or {}).get('store') else 'Сільпо, вул. Калнишевського, 2'} · 🔥 — «Ціна тижня»</i>\n"
            + ("<i>Реальна вигода — порівняно з мінімальною ціною за останні тижні</i>" if weeks
               else "<i>Реальна вигода з'явиться з наступного тижня (бот ще збирає історію цін)</i>"))
    st["silpo_more"] = more
    msgs, cur, cur_ids = [], head, []                 # ділимо на повідомлення до 4000 символів
    for ci, sec in sections:
        if len(cur) + len(sec) + 1 > 3800:
            msgs.append(cur + kb_marker(cur_ids, more))
            cur, cur_ids = sec, [ci]
        else:
            cur += "\n" + sec
            cur_ids.append(ci)
    msgs.append(cur + kb_marker(cur_ids, more))
    return SPLIT.join(msgs)


def kb_marker(ids: list, more: dict) -> str:
    """Службова позначка кнопок «➕ ще 5» для категорій цього повідомлення (розбирається при надсиланні)."""
    ids = [i for i in ids if i in more]
    return f"\n§KB:{','.join(ids)}" if ids else ""


def split_kb(text: str, more: dict) -> tuple[str, dict | None]:
    """Прибирає позначку §KB і будує кнопки «➕ ще 5»."""
    m = re.search(r"\n§KB:([\d,]+)$", text)
    if not m:
        return text, None
    btns = [{"text": f"➕ {more[i]['b']}", "callback_data": f"sm:{i}:0"} for i in m.group(1).split(",") if i in more]
    return text[:m.start()], ({"inline_keyboard": [btns[k:k + 2] for k in range(0, len(btns), 2)]} if btns else None)


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
    """Український YouTube за останні 7 днів: «В тренді» в Україні + пошук за українськими словами; суворий фільтр мови."""
    if not YT_KEY:
        print("YT_KEY не задано — YouTube пропущено")
        return None
    start = now - timedelta(days=7)
    api = lambda path, **q: http_json(f"https://www.googleapis.com/youtube/v3/{path}?" + urllib.parse.urlencode({**q, "key": YT_KEY}))
    vids, token = [], ""
    for _ in range(4):
        try:
            r = api("videos", part="snippet,statistics,contentDetails,status", chart="mostPopular", regionCode="UA",
                    maxResults=50, **({"pageToken": token} if token else {}))
        except Exception as e:
            print(f"YouTube trending: {err_text(e)[:200]}")
            break
        vids += r.get("items", [])
        token = r.get("nextPageToken")
        if not token:
            break
    ids: list[str] = []
    for q in ("що|як|чому|це|коли", "україна|українською|київ|наші"):
        try:
            r = api("search", part="id", type="video", order="viewCount", regionCode="UA", relevanceLanguage="uk", q=q,
                    maxResults=50, publishedAfter=start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"))
            ids += [x["id"]["videoId"] for x in r.get("items", []) if x.get("id", {}).get("videoId")]
        except Exception as e:
            print(f"YouTube search: {err_text(e)[:200]}")
    have = {v.get("id") for v in vids}
    need = [i for i in dict.fromkeys(ids) if i not in have]
    for k in range(0, len(need), 50):
        try:
            vids += api("videos", part="snippet,statistics,contentDetails,status", id=",".join(need[k:k + 50])).get("items", [])
        except Exception as e:
            print(f"YouTube videos: {err_text(e)[:200]}")
    seen = set(st.get("yt_seen", []))
    good, ids_ok = [], set()
    for v in vids:
        sn = v.get("snippet", {})
        lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower()
        txt = sn.get("title", "") + " " + (sn.get("description") or "")[:300]
        try:
            pub = datetime.fromisoformat(sn.get("publishedAt", "").replace("Z", "+00:00"))
        except Exception:
            continue
        if (v.get("id") in ids_ok or v.get("id") in seen or pub < start or sn.get("categoryId") == "25"
                or (v.get("status") or {}).get("madeForKids")
                or iso_dur((v.get("contentDetails") or {}).get("duration")) <= 60):
            continue                                   # повтор, старе, новини, дитяче або Shorts
        if lang.startswith("ru") or re.search(r"[ыэъё]", txt, re.I):
            continue                                   # російське — ні
        if not (lang.startswith("uk") or UA_LETTERS.search(txt)):
            continue                                   # лише українська мова
        ids_ok.add(v.get("id"))
        good.append(v)
    print(f"YouTube: усього {len(vids)}, українських за тиждень {len(good)}")
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
    end = now + timedelta(days=1)                              # для підпису періоду (start … сьогодні)
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
    label = f"{start:%d.%m}–{now:%d.%m}"
    return f"▶️ <b>ТОП-{len(top)} УКРАЇНСЬКОГО YOUTUBE · {label}</b>\n" + "\n".join(items)


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


def channel_posts(channel: str | None = None) -> list[tuple[int, datetime, str]]:
    page = http_text(f"https://t.me/s/{channel or CHANNEL}")
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


CANCEL_WORDS = ("скасов", "відмін", "припин", "заверш", "не застосов", "більше не", "закінч", "відновлено графік",
                "повертаємось до граф", "повертаємося до граф")


def emerg_kind(text: str) -> str | None:
    t = text.lower()
    if "екстрен" not in t:
        return None
    return "off" if any(w in t for w in CANCEL_WORDS) else "on"


# Telegram-канали для екстрених: (ім'я, назва, лише пости про Київ?)
EMERG_CHANNELS = [("yaskravyi_power", "канал ЖК «Яскравий»", False),
                  ("Ukrenergo", "Укренерго", True),
                  ("dtek_kem", "ДТЕК Київські електромережі", False),
                  ("DTEKKyivskielectromerezhi", "ДТЕК Київські електромережі", False),
                  ("dtekkem", "ДТЕК Київські електромережі", False)]
EM_ON = re.compile(r"(застосову\w*|діють|запроваджен\w*|оголошен\w*|введен\w*)\s+(\w+\s+){0,2}(екстрен|аварійн)\w*\s+відключ|"
                   r"(екстрен|аварійн)\w*\s+відключення\s+(застосову|діють|запроваджен)|"
                   r"графік\w*\s+(стабілізаційн\w*\s+)?(погодинних\s+)?відключень\s+не\s+діють", re.I)
EM_OFF = re.compile(r"(екстрен|аварійн)\w*\s+відключення\s+(\w+\s+){0,2}(скасов|відмін|припин|заверш|закінч|не\s+застосову)|"
                    r"(скасов|відмін|припин|заверш|закінч)\w*\s+(\w+\s+){0,2}(екстрен|аварійн)|"
                    r"повертаємо\w*\s+до\s+графік|(знову\s+)?діють\s+(графіки|ГПВ)|відновлено\s+(застосування\s+)?графік", re.I)


def emergency_channels(st: dict, now: datetime) -> tuple[bool, list[str]]:
    """Останнє явне оголошення про екстрені (або їх скасування) у кожному каналі за добу; діє до 12 год."""
    on_any, notes, found = False, [], []
    for name, title, kyiv_only in EMERG_CHANNELS:
        try:
            posts = channel_posts(name)
        except Exception as e:
            continue
        if not posts:
            continue
        found.append(name)
        last = None
        for pid, dt, text in posts:
            if now - dt > timedelta(hours=24) or "екстрен" not in text.lower() and "аварійн" not in text.lower():
                continue
            if kyiv_only and not re.search(r"київ", text, re.I):
                continue
            # спершу перевіряємо скасування (в ньому теж є слово «екстрені»)
            kind = "off" if EM_OFF.search(text) else ("on" if EM_ON.search(text) else None)
            if kind:
                last = (kind, dt, text, pid)
        if last:
            kind, dt, text, pid = last
            active = kind == "on" and now - dt < timedelta(hours=12)
            on_any = on_any or active
            short = " ".join(text.split())[:140]
            print(f"Екстрені — {title} (@{name}, {dt:%d.%m %H:%M}): {'ТАК' if active else 'ні'} · «{short}»")
            if active:
                notes.append(f"{title}: {short}")
    print(f"Екстрені — канали знайдено: {found or 'жодного'}")
    return on_any, notes


def emergency_yasno(st: dict, now: datetime) -> list[str] | None:
    """Екстрені відключення за офіційним статусом YASNO (компанія ДТЕК у Києві). None — якщо YASNO недоступний."""
    data = None
    for attempt in range(2):
        try:
            data = http_json(API_URL)
            break
        except Exception as e:
            print(f"YASNO (екстрені), спроба {attempt + 1}: {err_text(e)[:80]}")
            _time.sleep(3)
    if not isinstance(data, dict):
        return None
    stats = {}
    for grp, g in data.items():
        if isinstance(g, dict):
            stt = ((g.get("today") or {}).get("status")) or "—"
            stats[stt] = stats.get(stt, 0) + 1
    on_y = stats.get("EmergencyShutdowns", 0) > 0
    addr = st.get("addr") or {}
    on_a = bool((addr.get("active") and addr.get("emergency")) or addr.get("city_emergency"))  # сайт ДТЕК — найточніше
    on_c, ch_notes = emergency_channels(st, now)     # Telegram: ЖК, Укренерго, ДТЕК (лише явні оголошення)
    on = on_y or on_a or on_c
    print(f"Екстрені: {'ТАК' if on else 'ні'} · YASNO {'так' if on_y else 'ні'} {stats} · "
          f"сайт ДТЕК {'так' if on_a else 'ні'} (адреса: {addr.get('reason') or '—'}; місто: {(addr.get('city_text') or '—')[:120]}) · "
          f"канали {'так' if on_c else 'ні'}")
    ch = st.get("emerg") or {}
    was = bool(ch.get("on"))
    out = []
    if on and not was:
        ch.update(on=True, since=now.isoformat(), src="yasno")
        srcs = ", ".join((["сайт ДТЕК"] if on_a else []) + (["YASNO"] if on_y else [])
                         + [n.split(":")[0] for n in ch_notes]) or "ДТЕК"
        note_t = addr.get("city_text") or (ch_notes[0] if ch_notes else "")
        note = f"<blockquote>{esc(note_t)}</blockquote>\n" if note_t else ""
        out.append("🚨🚨🚨 <b>ЕКСТРЕНІ ВІДКЛЮЧЕННЯ!</b> 🚨🚨🚨\n"
                   "Графік може не діяти · 🔋 зарядіть пристрої\n" + note + f"<i>джерела: {esc(srcs)}</i>")
    elif was and not on:
        ch.update(on=False, since=now.isoformat(), src="yasno")
        out.append("✅✅ <b>ЕКСТРЕНІ ВІДКЛЮЧЕННЯ СКАСОВАНО</b> ✅✅\n"
                   "Знову діє графік ДТЕК\n<i>жодне джерело (сайт ДТЕК, YASNO, канали) більше не повідомляє про екстрені</i>")
    st["emerg"] = ch
    return out


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


ADDR_NOW: dict = {}


def addr_line() -> str:
    """Що зараз за адресою (дані ДТЕК з перевірки адреси)."""
    a = ADDR_NOW
    if not a:
        return ""
    if a.get("active"):
        return (f"🏠 <b>За адресою зараз немає світла</b> · {esc(a.get('reason'))}\n"
                f"   з {esc(a.get('start'))}, орієнтовно до <b>{esc(a.get('end') or '—')}</b>\n")
    return "🏠 ДТЕК зараз не показує відключень за вашою адресою\n"


def summary(head: str, today_d, tomorrow_d, all_off, now) -> str:
    emerg = "🚨 <b>Діють екстрені відключення</b> — графік може не діяти\n" if EMERG_ACTIVE else ""
    return (f"{head}\n{emerg}{addr_line()}{now_line(all_off, now)}\n"
            + fmt_day(today_d, "Сьогодні") + "\n" + fmt_day(tomorrow_d, "Завтра"))


def settings_markup(on: list[str]) -> dict:
    b = [{"text": f"{'✅' if t in on else '⬜'} {label}", "callback_data": f"t:{t}"} for t, label in TOPICS]
    return {"inline_keyboard": [b[0:1], b[1:2], b[2:4], b[4:6], b[6:7]]}


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


def cleanup_expired(st: dict) -> None:
    """Видаляємо нагадування, подія яких уже минула (Telegram дозволяє протягом 48 год)."""
    now_ = datetime.now(TZ)
    keep = []
    for cid, mid, exp in st.get("to_delete", []):
        e = datetime.fromisoformat(exp)
        if e <= now_:
            if now_ - e < timedelta(hours=40):
                tg("deleteMessage", {"chat_id": cid, "message_id": mid})
        else:
            keep.append([cid, mid, exp])
    st["to_delete"] = keep


def main() -> None:
    check_config()
    st = load_state()
    cleanup_expired(st)
    GEMINI_BLOCK.update(st.get("gem_block") or {})
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
            worker_req = dict(ex.get("req") or {})
            worker_req["kino_used"] = ex.get("kino_used") or []
            subs = {str(k): list(v) for k, v in (ex.get("subs") or {}).items()}
            if ex.get("content_seen"):                # що вже показали з меню (Cloudflare) — не повторюємо
                st["content_seen"] = list(dict.fromkeys(st.get("content_seen", []) + list(ex["content_seen"])))[-600:]
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
    try:                                              # стан за адресою (окремий процес із браузером, кожні ~10 хв)
        st["addr"] = json.loads(Path("addr_state.json").read_text("utf-8"))
    except Exception:
        st.pop("addr", None)
    global ADDR_NOW
    ADDR_NOW = st.get("addr") or {}
    emerg_msgs = emergency_yasno(st, now)            # офіційний статус YASNO + дані ДТЕК за адресою
    if emerg_msgs is None:
        emerg_msgs = emergency_check(st, now) if CHANNEL else []
    EMERG_ACTIVE = bool((st.get("emerg") or {}).get("on"))
    msgs: list[tuple[str, str | None]] = [(m, None) for m in emerg_msgs]
    in_sched = any(a <= now < b for a, b in all_off)  # чи є зараз відключення за графіком
    if EMERG_ACTIVE:
        f_reason = "🚨 Причина: діють екстрені відключення"
    elif in_sched:
        f_reason = "📅 Причина: відключення за графіком"
    elif ADDR_NOW.get("active"):
        f_reason = f"🏠 ДТЕК за адресою: {esc(ADDR_NOW.get('reason'))}"
    else:
        f_reason = ("⚠️ <b>Фактично світла немає, але за джерелами (ДТЕК, YASNO, канали) немає ні екстрених, "
                    "ні планових відключень</b> — можлива аварія")
    fact_msgs = fact_update(st, now, f_reason)        # фактично є/немає світла (лише вам і дружині)
    # (текст для всіх, окремий текст для дружини або None)
    direct: list[tuple[str, str, bool, dict | None]] = []  # (chat_id, текст, це зведення?, кнопки)
    fam_msgs: list[str] = []                  # лише вам і дружині (запуск/ручна перевірка)

    # 0) меню команд і кнопки (для всіх; відповідь при наступному запуску)
    family = list(dict.fromkeys(CHAT_IDS + ([WIFE_ID] if WIFE_ID else [])))
    kino_req: list[str] = []
    yt_req: list[str] = []
    para_req: list[str] = []
    smm_req: list[str] = []
    silpo_req: list[str] = []
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
        if cmd == "/start":
            direct.append((cid, WELCOME, False, None))   # привітання англійською
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
            direct.append((cid, HELP_TEXT + SPLIT + SOURCES_TEXT, False, None))
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
        elif cmd in ("/porady", "/para", "/пара"):
            para_req.append(cid)
        elif cmd == "/smm":
            smm_req.append(cid)
        elif cmd == "/silpo":
            silpo_req.append(cid)

    if REQ_KINO and re.fullmatch(r"-?\d+", REQ_KINO):
        kino_req.append(REQ_KINO)
    if REQ_YT and re.fullmatch(r"-?\d+", REQ_YT):
        yt_req.append(REQ_YT)
    kino_req += [str(c) for c in worker_req.get("kino", [])]   # запити з меню, які прийняв Worker
    yt_req += [str(c) for c in worker_req.get("yt", [])]
    para_req += [str(c) for c in worker_req.get("para", [])]
    smm_req = list(dict.fromkeys(smm_req + [str(c) for c in worker_req.get("smm", [])]))
    silpo_req = list(dict.fromkeys(silpo_req + [str(c) for c in worker_req.get("silpo", [])]))
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
            msgs.append((expiring(text, t + timedelta(minutes=10)), None))   # сам видалиться після події
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
        k = build_kino(st, "🍿 <b>КІНО НА ВИХІДНІ</b>")
        if k:
            st["kino_last"] = {"text": k[0], "markup": k[1]}
            st["kino_week"] = week_key
            topic_msgs.append(("kino", k[0], k[1], k[0].replace(KINO_HINT, "")))
    used = set(worker_req.get("kino_used", [])) | set(st.get("kino_used", []))
    st["kino_q"] = [q for q in st.get("kino_q", []) if q["id"] not in used]    # видані з меню — прибираємо
    if TMDB_KEY and len(st["kino_q"]) < 2 and st.get("kino_q_try") != now.strftime("%Y-%m-%d %H:%M")[:15]:
        st["kino_q_try"] = now.strftime("%Y-%m-%d %H:%M")[:15]                  # не частіше ніж раз на 10 хв
        kq = build_kino(st)                            # готова підбірка «про запас» — меню віддає миттєво
        if kq:
            st["kino_q"].append({"id": hashlib.md5(kq[0].encode()).hexdigest()[:10], "text": kq[0], "markup": kq[1]})
    if kino_req:                                   # з меню — щоразу НОВА підбірка (без повторів)
        k_new = build_kino(st) if TMDB_KEY else None
        for c in kino_req:
            if not k_new:
                direct_m.append((c, "🍿 Кіно ще не налаштовано (немає ключа TMDB)." if not TMDB_KEY
                                 else "🍿 Не вдалося зібрати підбірку, спробуйте пізніше.", None))
                continue
            text = k_new[0].replace("КІНО НА ВИХІДНІ", "НОВА ПІДБІРКА КІНО", 1)
            direct_m.append((c, text, k_new[1]) if c in family else (c, text.replace(KINO_HINT, ""), None))
    if YT_KEY and not st.get("yt_last") and not st.get("yt_try") == now.strftime("%Y-%m-%d %H"):
        st["yt_try"] = now.strftime("%Y-%m-%d %H")      # топу ще немає — збираємо заздалегідь, без розсилки
        y0 = build_youtube(st, now)
        if y0:
            st["yt_last"] = y0
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
    collect_content(st, now)                          # раз на день оновлюємо пул свіжих матеріалів 18+
    if para_due:
        pm = build_para(st, now, header="💞 <b>ПОРАДА 18+</b>")
        topic_msgs.append(("para", pm, None, pm))
        extra = build_content(st) or build_reddit(st)  # друге — один свіжий матеріал (стаття, огляд, досвід…)
        if extra:
            topic_msgs.append(("para", extra, None, extra))
        st["para_sent"] = today_key
    if para_req:
        pm = build_para(st, now, with_news=False)
        direct_m += [(c, pm, None) for c in para_req]

    if st.get("yt_ver") != 2:                         # новий відбір YouTube (лише українське) — стару підбірку скидаємо
        st.pop("yt_last", None)
        st["yt_ver"] = 2
    if st.get("smm_ver") != 2:                        # нові джерела SMM — стару підбірку скидаємо, меню збере нову
        st.pop("smm_last", None)
        st["smm_ver"] = 2
    # 9) SMM і таргет — пн о 13:00 (вам, дружині та підписникам, які це ввімкнули); меню — той самий дайджест тижня
    smm_due = (now.weekday() == SMM_WEEKDAY and SMM_HOUR <= now.hour < SMM_HOUR + 4
               and st.get("smm_week") != week_key)
    if smm_due:
        sm = build_smm(st, now)
        if sm:
            st["smm_week"], st["smm_last"] = week_key, sm
            topic_msgs.append(("smm", sm, None, sm))
            smm_req = [c for c in smm_req if c not in audience("smm")]
    if not st.get("smm_last") and not st.get("smm_try") == now.strftime("%Y-%m-%d %H"):
        st["smm_try"] = now.strftime("%Y-%m-%d %H")   # дайджесту ще немає — збираємо заздалегідь, без розсилки
        sm = build_smm(st, now)
        if sm:
            st["smm_last"] = sm
    if smm_req and not st.get("smm_last"):
        sm = build_smm(st, now)
        if sm:
            st["smm_last"] = sm
    for c in smm_req:
        direct_m.append((c, st.get("smm_last") or "📈 Не вдалося зібрати дайджест, спробуйте пізніше.", None))

    # 10) Сільпо: ср — знімок цін; чт о 13:00 — топ акцій (вам, дружині, підписникам із темою); меню — топ тижня
    if (not SILPO_BRANCH and not (st.get("silpo_branch") or {}).get("store")
            and st.get("silpo_probe") != today_key):
        silpo_branch(st)                              # раз на день шукаємо фізичний магазин на Калнишевського
        if (st.get("silpo_branch") or {}).get("store") and now.weekday() == SILPO_WEEKDAY:
            st.pop("silpo_week", None)                # знайшли — сьогоднішню підбірку перебудуємо по ньому
    if (now.weekday() == SILPO_SNAP_WEEKDAY and now.hour >= SILPO_SNAP_HOUR
            and st.get("silpo_snap") != today_key):
        silpo_snapshot(st, now)
        st["silpo_snap"] = today_key                  # навіть якщо не вдалося — не повторюємо до наступної середи
    silpo_due = (now.weekday() == SILPO_WEEKDAY and SILPO_HOUR <= now.hour < SILPO_HOUR + 4
                 and st.get("silpo_week") != week_key)
    if silpo_due:
        sp = build_silpo(st, now)
        if sp:
            st["silpo_week"], st["silpo_last"] = week_key, sp
            topic_msgs.append(("silpo", sp, None, sp))
            silpo_req = [c for c in silpo_req if c not in audience("silpo")]
    if not st.get("silpo_last") and not st.get("silpo_try") == now.strftime("%Y-%m-%d %H"):
        st["silpo_try"] = now.strftime("%Y-%m-%d %H")  # акцій тижня ще немає — збираємо заздалегідь, без розсилки
        sp = build_silpo(st, now)
        if sp:
            st["silpo_last"] = sp
    if silpo_req and not st.get("silpo_last"):
        sp = build_silpo(st, now)
        if sp:
            st["silpo_last"] = sp
    for c in silpo_req:
        direct_m.append((c, st.get("silpo_last") or "🛒 Не вдалося отримати акції Сільпо, спробуйте пізніше.", None))

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
    jobs += [(cid, t, None) for t in fam_msgs + fact_msgs for cid in family]
    jobs += [(cid, wife_text if (wife_text and cid == WIFE_ID) else text, None)
             for text, wife_text in msgs for cid in svitlo_to]
    jobs += direct_m
    change_to = [] if worker_alive else svitlo_to      # працює Worker — зміни вже надіслав він
    jobs += [(cid, text, None) for text in change_msgs for cid in change_to]
    for topic, text, mk, plain in topic_msgs:
        jobs += [(cid, text, mk) if cid in family else (cid, plain, None) for cid in audience(topic)]
    expanded = []                                     # довгі підбірки (Сільпо, SMM) — кількома повідомленнями
    for cid, text, mk in jobs:
        if type(text) is str and (SPLIT in text or "\n§KB:" in text):
            for part in text.split(SPLIT):
                if part.strip():
                    t_, k_ = split_kb(part, st.get("silpo_more") or {})
                    expanded.append((cid, t_, k_))
        else:
            expanded.append((cid, text, mk))
    jobs = expanded
    removed: set[str] = set()
    for cid, text, mk in jobs:
        if cid in removed:
            continue
        try:
            mid = send(cid, text, mk)
            ok += 1
            if getattr(text, "expire", "") and mid:
                st.setdefault("to_delete", []).append([cid, mid, text.expire])
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

    st["gem_block"] = {m: t for m, t in GEMINI_BLOCK.items() if t > now.isoformat()}
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
