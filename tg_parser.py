"""🍓 Відео, GIF і фото з каналів: від імені окремого акаунта раз на день бере з кожного джерела топ за вчора окремо для кожного типу
(відео — 40% до 5 хв, 30% до 15 хв, 30% довші; GIF і фото — просто найкращі; альбом фото — один пост) за оцінкою
«перегляди × залученість» (пересилання, коментарі, реакції), пересилає їх ПАЧКАМИ в канал-сховище й передає боту опис.
Джерела, канал-сховище й квоти задаються в боті: 👑 Адмін → 🍓: джерела. Канали із забороною пересилання пропускаються.
Секрети: TG_API_ID, TG_API_HASH, TG_SESSION, SYNC_URL, SYNC_KEY."""
import asyncio, json, math, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, DocumentAttributeVideo, InputMessagesFilterGif, MessageEntityMention, MessageEntityTextUrl, MessageEntityUrl, PeerChannel

SYNC_URL, SYNC_KEY = os.environ.get("SYNC_URL", "").rstrip("/"), os.environ.get("SYNC_KEY", "")
PER_DAY, FIRST_DAYS, PAUSE, RUN_SEC, MAX_SEC = 10, 3, 3, 10 * 60, 3 * 3600   # базово топ-10 кожного типу; перший збір — 3 останні дні; до 10 хв на запуск
GIF_ALL, GIF_IDX, GIF_RESCAN = 5000, 1500, 7                       # 🎞 архів GIF: уся історія каналу (до 5000 GIF), індекс найкращих — 1500, переглядати раз на 7 днів
GIF_DEEP = 30                                                       # GIF трапляються рідко: якщо за день бракує — добір найкращими за 30 днів (для відео й фото — за 2)
# тип: (назва, файл у боті, поле квоти, поле базової квоти, поле «зібрано до», ліміт записів)
TYPES = {"v": ("відео", "vids.json", "q", "base", "done", 2000),
         "g": ("GIF", "vid_g.json", "qg", "base_g", "done_g", 1500),
         "p": ("фото", "vid_p.json", "qp", "base_p", "done_p", 1500)}
KYIV = timezone(timedelta(hours=2))
LOG: list = []


def http_json(url: str, body=None):
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                                 headers={"content-type": "application/json", "User-Agent": "svitlo-tg-parser"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "null")


def fget(name: str, default):
    try:
        return http_json(f"{SYNC_URL}/fileget?key={urllib.parse.quote(SYNC_KEY)}&name={name}") or default
    except Exception as e:
        print(f"fileget {name}: {e}")
        return default


def fpush(name: str, data) -> bool:
    try:
        http_json(f"{SYNC_URL}/filepush?key={urllib.parse.quote(SYNC_KEY)}&name={name}", data)
        return True
    except Exception as e:
        print(f"filepush {name}: {e}")
        return False


def log(s: str):
    print(s)
    LOG.append(f"{datetime.now(KYIV):%d.%m %H:%M} {s}"[:160])


def stats(m) -> tuple:
    """(перегляди, реакції, пересилання, коментарі) — чого немає (напр., у чатах), те 0."""
    r = sum(int(getattr(x, "count", 0) or 0) for x in (getattr(getattr(m, "reactions", None), "results", None) or []))
    return int(m.views or 0), r, int(getattr(m, "forwards", 0) or 0), int(getattr(getattr(m, "replies", None), "replies", 0) or 0)


def score(m) -> float:
    """Оцінка: охоплення (перегляди) × залученість. Пересилання важать найбільше (ним діляться), далі коментарі, далі реакції.
    У чатах без лічильника переглядів працює лише залученість."""
    v, r, f, c = stats(m)
    eng = r + 3 * f + 2 * c
    base = math.log10(1 + v + 25 * eng)
    rate = min(eng / v, 0.3) if v >= 50 else 0.0
    return base * (1 + 3 * rate)


def _dur(m):
    doc = getattr(m, "document", None)
    a = next((x for x in (doc.attributes if doc else []) if isinstance(x, DocumentAttributeVideo)), None)
    return a


def video_of(m):
    """Звичайне відео (не кружечок, не GIF, не стікер) → (тривалість, ширина, висота) або None."""
    if not getattr(m, "document", None) or not m.video or m.gif or m.video_note or m.sticker:
        return None
    a = _dur(m)
    if not a or getattr(a, "round_message", False) or not a.duration or a.duration > MAX_SEC:
        return None
    return int(a.duration), a.w, a.h


def gif_of(m):
    """GIF (у Telegram — беззвучна анімація) → тривалість або None."""
    if not getattr(m, "document", None) or not m.gif or m.sticker:
        return None
    a = _dur(m)
    return int(a.duration) if a and a.duration else 1


def kind_of(m):
    if getattr(m, "noforwards", False):
        return None
    if video_of(m):
        return "v"
    if gif_of(m):
        return "g"
    if getattr(m, "photo", None) and not getattr(m, "document", None):
        return "p"
    return None


# ---------- 🚫 Фільтр реклами ----------
AD_MARK = re.compile(r"(#\s?реклам|\bреклама\b|на правах реклам|#ads?\b|#promo|#промо|\berid\b|sponsored|спонсор|партнерськ|партнерск|#partner|взаимопиар|взаємопіар|\bпиар\b|\bпіар\b|промокод|promo ?code)", re.I)
AD_HARD = re.compile(r"(казино|casino|букмекер|1win|1xbet|pin-?up|\bслоты?\b|заработ|заробіт|пассивн\w* доход|пасивн\w* дохід|\bкрипт|crypto|airdrop)", re.I)
AD_CALL = re.compile(r"(подпис|підпис|переход|перейд|жми|тисни|клик|клікай|заход|вступа|присоедин|приєдн|ссылк|посиланн|канал|\bбот\b|полн\w* видео|повн\w* відео|продолжени|продовженн|full ?video|\bfull\b|\blink\b|\bjoin\b|subscribe|👉|👇|⬇|➡|🔗)", re.I)
AD_SHOP = re.compile(r"(промокод|promo ?code|скидк|знижк|купи|закаж|замов|доставк|магазин|прайс|\bцена\b|\bціна\b|\d+\s?(грн|₴|₽|руб|\$|usd|€))", re.I)
AD_DOM = ("onlyfans.com", "fansly.com", "boosty.to", "patreon.com", "fanvue.com", "linktr.ee")


def tkey(u: str) -> str:
    """Посилання / @згадка → ключ цілі: назва каналу, +запрошення, c/<id> або домен сайту."""
    u = (u or "").strip().lower()
    m = re.search(r"(?:t\.me|telegram\.me|telegram\.dog)/(\+[\w-]+|joinchat/[\w-]+|c/\d+|s/\w+|\w+)", u)
    if m:
        k = m.group(1)
        return "+" + k[9:] if k.startswith("joinchat/") else k[2:] if k.startswith("s/") else k
    m = re.search(r"domain=(\w+)", u) or re.search(r"https?://(?:www\.)?([^/\s?#]+)", u)
    if m:
        return m.group(1)
    return u.lstrip("@")


def own_keys(ent, ref: str) -> set:
    """Власні ідентифікатори джерела: підпис каналу посиланням на себе — не реклама."""
    own = {f"c/{ent.id}"}
    if getattr(ent, "username", None):
        own.add(ent.username.lower())
    for u in getattr(ent, "usernames", None) or []:
        own.add(str(getattr(u, "username", "")).lower())
    m = re.search(r"t\.me/(?:\+|joinchat/)([\w-]+)", ref or "")
    if m:
        own.add("+" + m.group(1).lower())
    return own


def targets_of(ms: list) -> tuple:
    """(усі цілі посилань/згадок, цілі з кнопок-посилань)."""
    tg_, btn = set(), set()
    for m in ms:
        try:
            for e, t in (m.get_entities_text() if m.entities else []):
                if isinstance(e, MessageEntityTextUrl):
                    tg_.add(tkey(e.url))
                elif isinstance(e, MessageEntityUrl):
                    tg_.add(tkey(t))
                elif isinstance(e, MessageEntityMention):
                    tg_.add(t.lstrip("@").lower())
        except Exception:
            pass
        for u in re.findall(r"(?:https?://|t\.me/)\S+|@\w{4,}", m.message or ""):
            tg_.add(tkey(u))
        for row in getattr(getattr(m, "reply_markup", None), "rows", None) or []:
            for b in getattr(row, "buttons", []) or []:
                u = getattr(b, "url", None) or getattr(getattr(b, "type", None), "url", None)   # старий і новий формат кнопок Telethon
                if u:
                    btn.add(tkey(u))
    return {k for k in tg_ | btn if k}, btn


def ad_reason(ms: list, own: set, mode: int, words: list, bad: set, lim: int = 300) -> str:
    """Причина вважати пост рекламою або "" (mode: 0 — фільтр вимкнено, 1 — звичайний, 2 — суворий; lim — макс. довжина підпису, 0 — без обмеження)."""
    if not mode:
        return ""
    text = "\n".join(m.message or "" for m in ms)
    low = text.lower()
    allt, btn = targets_of(ms)
    ext, ebtn = allt - own, btn - own
    if ext & bad:
        return "посилання з чорного списку"
    if ebtn:
        return "кнопка-посилання"
    if any(k.endswith(d) for k in ext for d in AD_DOM) or any(d.split(".")[0] in low for d in AD_DOM):
        return "платний контент (OnlyFans тощо)"
    if AD_MARK.search(low):
        return "позначка реклами"
    plain = re.sub(r"(https?://\S+|t\.me/\S+|@\w+|#\w+|[^\w\s])", " ", text)     # без посилань, згадок, хештегів, емодзі
    plain = re.sub(r"\s+", " ", plain).strip()
    if lim and len(plain) > lim:
        return f"довгий текст ({len(plain)} символів)"          # під справжнім контентом підпис зазвичай короткий
    hit = next((w for w in words if w and w in low), None)
    if hit:
        return f"стоп-слово «{hit}»"
    if AD_HARD.search(low):
        return "казино / ставки / заробіток"
    if ext:
        if mode >= 2:
            return "посилання на інший канал"
        if len(ext) >= 2:
            return "кілька посилань на інші канали"
        if AD_CALL.search(low) or AD_SHOP.search(low):
            return "заклик перейти на інший канал"
        if len(re.sub(r"(https?://\S+|t\.me/\S+|@\w+|\s|[^\w])", "", text)) < 25:
            return "лише посилання на інший канал"
    elif mode >= 2 and AD_SHOP.search(low):
        return "комерційні слова"
    return ""


def media_keys(ms: list) -> set:
    """♻️ «Відбитки» публікації: id файлу (у пересланих копіях той самий), розмір+тривалість (той самий файл, завантажений наново), оригінал пересилання."""
    ks = set()
    for m in ms:
        d, ph = getattr(m, "document", None), getattr(m, "photo", None)
        if d is not None and getattr(d, "id", None):
            ks.add(f"d{d.id}")
        if d is not None and getattr(d, "size", None):
            a = _dur(m)
            ks.add(f"s{d.size}:{int(a.duration) if a and a.duration else 0}")
        if ph is not None and getattr(ph, "id", None):
            ks.add(f"p{ph.id}")
        f = getattr(m, "fwd_from", None)
        cid = getattr(getattr(f, "from_id", None), "channel_id", None) if f else None
        if cid and getattr(f, "channel_post", None):
            ks.add(f"f{cid}:{f.channel_post}")
    return ks


async def gif_index(client, ent, own: set, mode: int, words: list, bad: set, lim: int) -> list:
    """🎞 Уся історія GIF каналу (серверний фільтр — лише GIF, швидко) → [[id, оцінка]] від найкращих.
    Принцип: популярність ВІДНОСНО СВОГО ЧАСУ — оцінка поділена на медіану GIF того ж місяця
    (старі пости набрали більше переглядів просто за роки, а канал міг вирости — так нові й старі змагаються чесно)."""
    rows = []
    async for m in client.iter_messages(ent, filter=InputMessagesFilterGif, limit=GIF_ALL):
        if kind_of(m) != "g" or ad_reason([m], own, mode, words, bad, lim):
            continue                                                  # (повтори відсіюються вже під час добору — історія відбитків змінюється щодня)
        rows.append((m.id, score(m), m.date.astimezone(KYIV).strftime("%Y-%m")))
    if not rows:
        return []
    allv = sorted(r[1] for r in rows)
    gmed = allv[len(allv) // 2] or 1
    med = {}
    for mon in {r[2] for r in rows}:
        v = sorted(r[1] for r in rows if r[2] == mon)
        med[mon] = (v[len(v) // 2] if len(v) >= 5 else gmed) or 1      # мало GIF за місяць — порівнюємо із загальною медіаною
    out = sorted(([o, round(0.7 * sc / med[mon] + 0.3 * sc / gmed, 3)] for o, sc, mon in rows), key=lambda r: -r[1])
    return out[:GIF_IDX]


SOFT = ("довгий", "лише посилання", "заклик", "кілька посилань", "посилання на інший", "комерційні")   # евристики: уже зібране за ними НЕ видаляємо


def learn_sig(msgs: list, own: set) -> set:
    """✍️ Підпис каналу: посилання/згадка, що стоїть у ≥25% постів (своє запрошення, резервний канал, бот зворотного звʼязку) — це не реклама."""
    from collections import Counter
    c = Counter()
    for m in msgs:
        c.update(targets_of([m])[0] - own)
    n = len(msgs)
    return {k for k, v in c.items() if n >= 4 and v >= max(2, 0.25 * n)}   # і для малих каналів (мережі на кшталт «… x GREHOGRAM» підписують кожен пост посиланнями на «сестер»)


class Post:
    """Одиниця добірки: відео, GIF або пост із фото (альбом до 10 фото — один пост)."""
    def __init__(self, ms: list, t: str):
        self.ms, self.t = sorted(ms, key=lambda x: x.id), t
        self.m = max(ms, key=score)                              # лічильники альбому — з найактивнішого фото
        self.id, self.date = self.ms[0].id, self.ms[0].date
        self.sc = max(score(x) for x in ms)


def pick_video(posts: list, q: int) -> list:
    """Відео: 40% до 5 хв, 30% — від 5 до 15 хв, 30% — довші (для 10 це 4 / 3 / 3); якщо в групі бракує — добір найкращими з інших."""
    ranked = sorted(posts, key=lambda p: -p.sc)
    dur = lambda p: video_of(p.m)[0]
    n1, n2 = round(q * 0.4), round(q * 0.3)
    groups = [([p for p in ranked if dur(p) < 300], n1), ([p for p in ranked if 300 <= dur(p) < 900], n2), ([p for p in ranked if dur(p) >= 900], q - n1 - n2)]
    out = [p for g, n in groups for p in g[:n]]
    out += [p for p in ranked if p not in out][:q - len(out)]
    return sorted(out, key=lambda p: -p.sc)[:q]


def gate(posts: list, q: int, thr) -> tuple:
    """🏅 Поріг якості відносно самого каналу (thr = [нижній квартиль, медіана, верхній квартиль] його оцінок):
    беремо не нижче медіани; якщо так не набирається й половини квоти — помʼякшуємо до нижнього квартиля; нижче — ніколи."""
    if not thr or not q:
        return posts, ""
    ok = [p for p in posts if p.sc >= thr[1]]
    if len(ok) * 2 >= q or len(ok) >= len(posts):
        return ok, "≥ медіани каналу"
    return [p for p in posts if p.sc >= thr[0]], "помʼякшено до нижнього квартиля"


def pick(posts: list, q: int, t: str) -> list:
    return pick_video(posts, q) if t == "v" else sorted(posts, key=lambda p: -p.sc)[:q]


async def resolve(client, ref: str, dialogs: list):
    """Посилання / @назва / запрошення / t.me/c/… / точна назва → сутність Telegram."""
    ref = ref.strip()
    m = re.search(r"t\.me/c/(\d+)", ref)
    if m:
        return await client.get_entity(PeerChannel(int(m.group(1))))
    m = re.search(r"t\.me/(?:\+|joinchat/)([\w-]+)", ref)
    if m:
        inv = await client(CheckChatInviteRequest(m.group(1)))
        chat = getattr(inv, "chat", None)
        if chat is None:
            raise ValueError("акаунт не учасник цього каналу — спершу приєднайтесь за запрошенням")
        return chat
    if re.fullmatch(r"-?\d{6,}", ref):
        i = int(ref)
        return await client.get_entity(PeerChannel(int(str(i)[4:])) if str(i).startswith("-100") else i)
    m = re.search(r"(?:t\.me/|@)([A-Za-z]\w{3,})", ref)
    if m:
        return await client.get_entity(m.group(1))
    hit = [d.entity for d in dialogs if (d.name or "").strip().lower() == ref.lower()]
    if not hit:
        raise ValueError("не знайдено серед чатів акаунта — перевірте назву або дайте посилання")
    return hit[0]


def link_of(ent, mid: int) -> str:
    un = getattr(ent, "username", None)
    return f"https://t.me/{un}/{mid}" if un else ""             # закриті — без посилання (його відкриють лише учасники)


def item_of(post: Post, new_ids: list, k: str, day, rank: int, ent) -> dict:
    v, r, fw, c = stats(post.m)
    x = {"i": new_ids[0], "k": k, "o": post.id, "v": v, "r": r, "f": fw, "c": c, "sc": round(post.sc, 2), "day": day.isoformat(), "rk": rank,
         "dt": post.date.astimezone(KYIV).strftime("%Y-%m-%d %H:%M"), "at": datetime.now(KYIV).strftime("%Y-%m-%d %H:%M"), "l": link_of(ent, post.id)}   # at — коли зібрано (від нього рахується строк зберігання)
    if post.t == "v":
        x["d"] = video_of(post.m)[0]; x["t"] = (post.m.message or "")[:140]
    elif post.t == "g":
        x["d"] = gif_of(post.m)
    else:
        x["ids"] = new_ids; x["n"] = len(new_ids)
    return x


async def forward_batch(client, dest, ent, posts: list) -> list:
    """Пачкою: усі вибрані пости одного каналу за день — одним запитом (до 100 повідомлень). → [нові id для кожного поста]."""
    out, chunk = [], []
    async def flush(ch):
        ids = [m.id for p in ch for m in p.ms]
        res = await client.forward_messages(dest, ids, from_peer=ent)
        res = res if isinstance(res, list) else [res]
        byorig, n = {}, 0
        for p in ch:
            got = [r.id for r in res[n:n + len(p.ms)] if r]
            n += len(p.ms)
            out.append(got)
        return byorig
    for p in posts:
        if sum(len(x.ms) for x in chunk) + len(p.ms) > 100:
            await flush(chunk); chunk = []
            await asyncio.sleep(PAUSE)
        chunk.append(p)
    if chunk:
        await flush(chunk)
    return out


async def main():
    if not (SYNC_URL and SYNC_KEY and os.environ.get("TG_SESSION")):
        sys.exit("Немає секретів TG_SESSION / SYNC_URL / SYNC_KEY")
    cfg = fget("vid_cfg.json", {})
    if cfg.get("pause"):                                              # ⏸ збір вимкнено адміном — навіть не підключаємось до Telegram
        print("⏸ Збір вимкнено в боті (👑 → 🍓: джерела) — нічого не роблю"); return
    wipe0 = cfg.get("wipe_at") or 0
    dbs = {t: fget(TYPES[t][1], {}) for t in TYPES}
    for d in dbs.values():
        d.setdefault("items", [])
    main_db = fget("vid_meta.json", {}) or {}                     # службове (статуси джерел, канал-сховище, лог) — окремим малим файлом
    if not main_db.get("st") and dbs["v"].get("st"):              # перехід зі старого формату
        main_db = {k_: dbs["v"].pop(k_) for k_ in ("st", "dest_id", "dest_t", "log") if k_ in dbs["v"]}
    for k_ in ("st", "dest_id", "dest_t", "log"):
        dbs["v"].pop(k_, None)
    main_db.setdefault("st", {})
    dbs["meta"] = main_db
    LOG.extend(main_db.get("log") or [])
    src = [s for s in cfg.get("src") or [] if s.get("on", 1) != 0]
    ad_mode, ad_words = int(cfg.get("ad", 1) if cfg.get("ad") is not None else 1), [str(w).lower().strip() for w in cfg.get("ad_w") or [] if str(w).strip()]
    bans = fget("vid_ban.json", {}) or {}                           # 🚫 «Це реклама» з бота (адмін): {тип: [id у сховищі]}
    ban_ids = {t: {int(i) for i in bans.get(t) or []} for t in TYPES}
    bad = set(main_db.get("ad_un") or [])                          # 🚫 чорний список цілей (навчається з 🚫 у боті)
    kill = {t: set() for t in TYPES}                                # що прибрати зі сховища цього запуску (реклама)
    ad_total = 0
    ad_lim = int(cfg.get("ad_len") if cfg.get("ad_len") is not None else 300)
    ban_o = list(main_db.pop("ban_o", None) or [])
    if not cfg.get("dest") or not src:
        print("Не задано канал-сховище або джерела — налаштуйте в боті: 👑 Адмін → 🍓: джерела")
        return
    client = TelegramClient(StringSession(os.environ["TG_SESSION"]), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        log("⚠️ сесія недійсна — створіть TG_SESSION заново (tg_login.py)")
        await push(dbs); return
    dialogs = await client.get_dialogs(limit=600)
    try:
        dest = await resolve(client, cfg["dest"], dialogs)
        main_db["dest_id"], main_db["dest_t"] = int(f"-100{dest.id}") if isinstance(dest, Channel) else -dest.id, getattr(dest, "title", "")
    except Exception as e:
        log(f"⚠️ канал-сховище: {str(e)[:90]}")
        await push(dbs); await client.disconnect(); return
    now = datetime.now(KYIV)
    yday = (now - timedelta(days=1)).date()
    t0, stop = time.time(), False
    bases = {t: max(3, min(40, int(cfg.get(TYPES[t][3]) or PER_DAY))) for t in TYPES}
    tgt = {t: int(cfg.get("tgt_" + t) or 300) for t in ("g", "p")}       # 🎯 запас GIF і фото в боті (адмінка); добирається найкращими за 30 днів
    share = {t: math.ceil(tgt[t] / max(1, len(src))) for t in tgt}
    gidx = fget("vid_gidx.json", {}) or {}                              # 🎞 архіви GIF по джерелах (окремий файл — бот його не читає)
    gidx_ch = False
    hist = fget("vid_hist.json", {}) or {}                            # ♻️ історія (окремий файл — бот його не читає, щоб не гальмувати)
    mh = dict(hist.get("mh") or {})                                   # відбиток → id у сховищі: та сама публікація не потрапить удруге
    ban_o = list(hist.get("ban_o") or []) + ban_o
    rb = cfg.get("rebuild") or 0
    if rb and rb != hist.get("rb"):                                   # 🔄 «перезібрати з нуля» з адмінки: забути, що вже бачили й збирали (позначене 🚫 як реклама — памʼятаємо)
        hist["rb"] = rb; mh.clear()
        for g_ in gidx.values():
            g_["seen"] = []
        gidx_ch = True
        for st_ in main_db["st"].values():
            for f_ in [TYPES[t][4] for t in TYPES]:
                st_.pop(f_, None)                                     # → перший збір знову візьме топ за 3 останні дні
        log("🔄 перезбір з нуля: історію повторів і дат скинуто")
    alive = {x["i"] for t in TYPES for x in dbs[t]["items"]}
    dup_total = 0

    def fresh(posts: list) -> list:                                   # ♻️ прибрати повтори (з історії й у межах пачки)
        nonlocal_seen, out = set(), []
        for p in posts:
            ks = media_keys(p.ms)
            if any(k_ in mh for k_ in ks) or ks & nonlocal_seen:
                continue
            nonlocal_seen |= ks; out.append(p)
        return out
    for s in src:
        if stop or time.time() - t0 > RUN_SEC:
            break
        k = s["k"]
        st = main_db["st"].setdefault(k, {})
        try:
            ent = await resolve(client, s["ref"], dialogs)
        except Exception as e:
            st.update(ok=0, err=str(e)[:120]); log(f"⚠️ {s.get('t') or s['ref'][:30]}: {str(e)[:80]}"); continue
        st["t"] = getattr(ent, "title", "") or s["ref"]
        own = own_keys(ent, s["ref"]) | set(st.get("sig") or [])         # + вивчений підпис каналу
        mode = int(s["ad"]) if s.get("ad") is not None else ad_mode
        ads = 0
        dups = 0
        from collections import Counter
        adr = Counter()
        try:                                                      # 🚫 позначені в боті як реклама: запамʼятати, куди вони ведуть, і прибрати
            for t in TYPES:
                hit = [x for x in dbs[t]["items"] if x["k"] == k and x["i"] in ban_ids[t] and x["i"] not in kill[t]]
                if hit:
                    for m in await client.get_messages(ent, ids=[x["o"] for x in hit]):
                        if m:
                            bad |= (targets_of([m])[0] - own)
                    kill[t] |= {x["i"] for x in hit}
                    ban_o.extend(f"{k}:{x['o']}" for x in hit)          # більше не брати цей оригінал (навіть добором)
        except Exception as e:
            print(f"ban {k}: {e}")
        if getattr(ent, "noforwards", False):
            if not st.get("err"):
                log(f"⏭ {st['t'][:30]}: заборона пересилання — пропускаю")
            st.update(ok=0, err="у каналі заборонено пересилання — пропущено"); continue
        # які дні ще не зібрано (окремо для кожного типу): з наступного після останнього зібраного (перший раз — 3 останні дні) по вчора; «вчора» — після 08:00
        days = {}
        for t in TYPES:
            done = st.get(TYPES[t][4]) or ""
            first = max(datetime.strptime(done, "%Y-%m-%d").date() + timedelta(days=1) if done else yday - timedelta(days=FIRST_DAYS - 1), yday - timedelta(days=6))
            dl = sorted({first + timedelta(days=n) for n in range((yday - first).days + 1)} | {yday - timedelta(days=n) for n in range(3)})   # + останні 3 дні: добрати, якщо бракує
            days[t] = [d for d in dl if d < yday] if now.hour < 8 else dl
        all_days = sorted({d for dl in days.values() for d in dl})
        blocked = False
        pool = {t: {} for t in TYPES}                                # тип → день → [пости]
        taken = {t: {x["o"] for x in dbs[t]["items"] if x["k"] == k} | {int(b.split(":")[1]) for b in ban_o if b.split(":")[0] == k} for t in TYPES}
        want = {}                                                    # 🎯 скільки GIF / фото добрати з цього джерела до запасу (фото — частка джерела до ×3; GIF — скільки бракує, бо їх мало)
        for t in tgt:
            cur = sum(1 for x in dbs[t]["items"] if x["k"] == k and x["i"] not in kill[t])
            want[t] = max(0, min((tgt[t] if t == "g" else 3 * share[t]) - cur, tgt[t] - len(dbs[t]["items"]), 60))
        try:
            if all_days or any(want.values()):
                end = datetime.combine((all_days[-1] if all_days else yday) + timedelta(days=1), datetime.min.time(), KYIV)
                start = datetime.combine(all_days[0] - timedelta(days=2), datetime.min.time(), KYIV) if all_days else end   # +2 дні назад — для добору
                deep = datetime.combine((min(days["g"] + days["p"]) if days["g"] + days["p"] else yday) - timedelta(days=GIF_DEEP), datetime.min.time(), KYIV)
                since = {"v": start, "g": deep if (days["g"] or want["g"]) else start, "p": deep if want["p"] else start}   # GIF і фото — глибше, коли треба добирати
                albums: dict = {}
                raw = []
                async for m in client.iter_messages(ent, offset_date=end, limit=8000):
                    if m.date < min(since.values()):
                        break
                    t = kind_of(m)
                    if not t or m.date < since[t]:
                        continue
                    raw.append((m, t))
                sig = learn_sig([m for m, _t in raw], own)              # ✍️ підпис каналу — не реклама
                for t_ in TYPES:                                         # 🏅 «типова якість» каналу для кожного типу — з усього переглянутого вікна
                    sc_ = sorted(score(m) for m, tt in raw if tt == t_)
                    if len(sc_) >= 15:
                        st.setdefault("thr", {})[t_] = [round(sc_[int(len(sc_) * f_)], 3) for f_ in (0.25, 0.5, 0.75)]
                if sig:
                    st["sig"] = sorted(sig | set(st.get("sig") or []))[:20]
                own |= set(st.get("sig") or [])
                for m, t in raw:
                    if t == "p" and m.grouped_id:
                        albums.setdefault(m.grouped_id, []).append(m); continue
                    if m.id not in taken[t]:
                        r_ = ad_reason([m], own, mode, ad_words, bad, ad_lim)
                        if r_:
                            ads += 1; adr[r_.split(" (")[0]] += 1; continue
                        if any(k_ in mh for k_ in media_keys([m])):
                            dups += 1; continue
                        pool[t].setdefault(m.date.astimezone(KYIV).date(), []).append(Post([m], t))
                for ms in albums.values():
                    p = Post(ms, "p")
                    if p.id not in taken["p"] and p.date >= since["p"]:
                        r_ = ad_reason(p.ms, own, mode, ad_words, bad, ad_lim)
                        if r_:
                            ads += 1; adr[r_.split(" (")[0]] += 1; continue
                        if any(k_ in mh for k_ in media_keys(p.ms)):
                            dups += 1; continue
                        pool["p"].setdefault(p.date.astimezone(KYIV).date(), []).append(p)
        except Exception as e:
            st.update(ok=0, err=str(e)[:120]); log(f"⚠️ {st['t'][:30]}: {str(e)[:80]}"); continue
        gi = gidx.get(k) or {}
        if want["g"] and (gi.get("v") != 2 or gi.get("at", "") < f"{now - timedelta(days=GIF_RESCAN):%Y-%m-%d}") and time.time() - t0 < RUN_SEC:   # v2 — з урахуванням підпису каналу   # 🎞 переглянути всю історію GIF (раз на тиждень)
            try:
                gi = gidx[k] = {"v": 2, "at": f"{now:%Y-%m-%d}", "o": await gif_index(client, ent, own, mode, ad_words, bad, ad_lim), "seen": gi.get("seen") or []}
                gidx_ch = True
                log(f"🎞 {st['t'][:24]}: архів GIF за весь час — {len(gi['o'])}")
            except Exception as e:
                print(f"gif index {k}: {e}")
        st["gall"] = len(gi.get("o") or [])
        for t, (tname, _f, qf, _bf, donef, _cap) in TYPES.items():
            if stop or blocked:
                break
            for day in days[t]:
                if stop or time.time() - t0 > RUN_SEC:
                    stop = True; break
                q = max(0, min(48, int(s.get(qf) or bases[t])))      # квота каналу для типу: базова (з адмінки) ± за 👍 / 👎 у боті
                already = sum(1 for x in dbs[t]["items"] if x["k"] == k and x.get("day") == day.isoformat() and x["i"] not in kill[t])
                q = max(0, q - already)
                thr = (st.get("thr") or {}).get(t)
                okp, lvl = gate(pool[t].get(day, []), q, thr)
                picks = pick(okp, q, t) if q else []
                if len(picks) < q and (pool[t].get(day) or t == "g"):   # бракує — добір: відео й фото — з 2 попередніх днів (якщо за день щось було); GIF — найкращі за 30 днів
                    back = range(1, (GIF_DEEP if t == "g" else 2) + 1)
                    extra = sorted((p for n_ in back for p in pool[t].get(day - timedelta(days=n_), []) if p.id not in taken[t] and p not in picks and (not thr or p.sc >= thr[1])),
                                   key=lambda p: -p.sc)                    # добір — лише не гірше медіани каналу
                    picks += extra[:q - len(picks)]
                added = 0
                n0 = len(picks); picks = fresh(picks); dups += n0 - len(picks)
                if picks:
                    try:
                        new = await forward_batch(client, dest, ent, picks)
                    except errors.FloodWaitError as e:
                        log(f"⏸ Telegram просить паузу {e.seconds} с — продовжу наступного запуску"); stop = True; break
                    except errors.ChatForwardsRestrictedError:
                        st.update(ok=0, err="у каналі заборонено пересилання — пропущено"); blocked = True; break
                    except Exception as e:
                        log(f"⚠️ {st['t'][:28]} · {tname}: {str(e)[:70]}"); stop = True; break
                    for rank, (p, ids) in enumerate(zip(picks, new), already + 1):
                        if ids:
                            dbs[t]["items"].append(item_of(p, ids, k, day, rank, ent)); taken[t].add(p.id); added += 1
                            mh.update({k_: ids[0] for k_ in media_keys(p.ms)}); alive.add(ids[0])
                    await asyncio.sleep(PAUSE)
                st[donef] = day.isoformat()
                if added:
                    log(f"🍓 {st['t'][:24]} · {day:%d.%m} · {tname}: топ-{added} з {len(pool[t].get(day, []))}{' · ' + lvl if lvl else ''}")
            if want.get(t) and not stop and not blocked and time.time() - t0 < RUN_SEC:   # 🎯 запас: найкращі за 30 днів, яких ще немає
                cur = sum(1 for x in dbs[t]["items"] if x["k"] == k and x["i"] not in kill[t])
                need = max(0, min(3 * share[t] - cur, tgt[t] - len(dbs[t]["items"]), 60))
                if t == "g" and gi.get("o"):                         # 🎞 GIF — з архіву за весь час: найкращі відносно свого часу, яких ще не показували
                    seen = set(gi.get("seen") or [])
                    order = [o for o, _ in gi["o"] if o not in taken["g"] and o not in seen]
                    if len(order) < need and seen:                   # архів пройдено — починаємо по колу
                        gi["seen"], seen = [], set()
                        order = [o for o, _ in gi["o"] if o not in taken["g"]]
                    ids_ = order[:need]
                    try:
                        got = {m.id: m for m in await client.get_messages(ent, ids=ids_) if m and kind_of(m) == "g"} if ids_ else {}
                    except Exception:
                        got = {}
                    cands = [Post([got[o]], "g") for o in ids_ if o in got]
                else:
                    cands = sorted((p for dl in pool[t].values() for p in dl if p.id not in taken[t]), key=lambda p: -p.sc)[:need]
                n0 = len(cands); cands = fresh(cands); dups += n0 - len(cands)
                if cands:
                    try:
                        new = await forward_batch(client, dest, ent, cands)
                        n_ = 0
                        for p, ids in zip(cands, new):
                            if ids:
                                dbs[t]["items"].append(item_of(p, ids, k, p.date.astimezone(KYIV).date(), 50 + n_, ent)); taken[t].add(p.id); n_ += 1
                                mh.update({k_: ids[0] for k_ in media_keys(p.ms)}); alive.add(ids[0])
                                if t == "g" and gi.get("o") is not None:
                                    gi["seen"] = (gi.get("seen") or [])[-4999:] + [p.id]; gidx_ch = True
                        if n_:
                            log(f"🎯 {st['t'][:24]} · {tname}: запас +{n_}")
                        await asyncio.sleep(PAUSE)
                    except errors.FloodWaitError as e:
                        log(f"⏸ Telegram просить паузу {e.seconds} с — продовжу наступного запуску"); stop = True
                    except errors.ChatForwardsRestrictedError:
                        st.update(ok=0, err="у каналі заборонено пересилання — пропущено"); blocked = True
                    except Exception as e:
                        log(f"⚠️ {st['t'][:28]} · {tname}: {str(e)[:70]}"); stop = True
        for t, (_n, _f, _q, _b, _d, _c) in TYPES.items():
            st["n" if t == "v" else "n" + t] = sum(1 for x in dbs[t]["items"] if x["k"] == k)
        if not blocked:
            st.update(ok=1, err="")
        try:                                                      # оновити лічильники постів за останні 8 днів (для «топу тижня» й «вистрілило») + перевірити вже зібране на рекламу
            since = f"{now - timedelta(days=8):%Y-%m-%d}"
            rec = [(t, x) for t in TYPES for x in dbs[t]["items"] if x["k"] == k and x["dt"][:10] >= since and x["i"] not in kill[t]][-300:]
            for n in range(0, len(rec), 100):
                part = rec[n:n + 100]
                for m in await client.get_messages(ent, ids=[x["o"] for _t, x in part]):
                    if m:
                        for t, x in part:
                            if x["o"] == m.id:
                                x["v"], x["r"], x["f"], x["c"] = stats(m)
                                r_ = ad_reason([m], own, mode, ad_words, bad, ad_lim)
                                if r_ and not r_.startswith(SOFT):            # уже зібране видаляємо лише за явною рекламою (кнопка, позначка, чорний список…)
                                    kill[t].add(x["i"]); ads += 1; adr[r_.split(" (")[0]] += 1; continue
                                ks = media_keys([m]); other = {mh[k_] for k_ in ks if k_ in mh and mh[k_] != x["i"] and mh[k_] in alive}
                                if other and min(other) < x["i"]:
                                    kill[t].add(x["i"]); dups += 1          # ♻️ повтор уже зібраного — лишаємо раніший
                                else:
                                    mh.update({k_: x["i"] for k_ in ks})
        except Exception as e:
            print(f"refresh {k}: {e}")
        st["ads"] = ads; ad_total += ads; st["adr"] = [f"{r_}: {n_}" for r_, n_ in adr.most_common(4)]
        st["dups"] = dups; dup_total += dups
        if ads:
            log(f"🚫 {st['t'][:24]}: відсіяно рекламу — {ads} ({adr.most_common(1)[0][0]}…)")
        if dups:
            log(f"♻️ {st['t'][:24]}: пропущено повторів — {dups}")
    if gidx_ch:
        fpush("vid_gidx.json", gidx)
    killed = {i for t in TYPES for i in kill[t]}
    hist["mh"] = dict(list({k_: v for k_, v in mh.items() if v not in killed}.items())[-30000:])   # видалене як реклама/повтор — не «бачене»                       # останні 30 тис. відбитків
    main_db["dupn"] = dup_total
    main_db["ad_un"] = sorted(bad)[-400:]; main_db["adn"] = ad_total; hist["ban_o"] = list(dict.fromkeys(ban_o))[-3000:]
    fpush("vid_hist.json", hist)
    # прибирання: прострочені (відео — cfg.keep днів, GIF і фото — cfg.keep_gp) і понад ліміт; збережені ⭐ — не чіпаємо
    live = {s["k"] for s in cfg.get("src") or []}
    keep = {int(i) for i, n in (fget("vid_keep.json", {}) or {}).items() if n}
    for t, (tname, _f, _q, _b, _d, cap) in TYPES.items():
        if t not in dbs:
            continue
        days_keep = int(cfg.get("keep") or 14) if t == "v" else int(cfg.get("keep_gp") or 7)
        cut = f"{now - timedelta(days=days_keep):%Y-%m-%d %H:%M}"
        items = dbs[t]["items"]
        old = [x for x in items if x["k"] not in live or x["i"] in kill[t] or x["i"] in ban_ids[t] or (max(x["dt"], x.get("at") or "") < cut and x["i"] not in keep)]   # 🚫 реклама — навіть зі збережених
        rest = [x for x in items if x not in old]
        if len(rest) > cap:
            old += [x for x in rest if x["i"] not in keep][:len(rest) - cap]
        if old:
            try:
                ids = [i for x in old for i in (x.get("ids") or [x["i"]])]
                for n in range(0, len(ids), 100):
                    await client.delete_messages(dest, ids[n:n + 100])
                log(f"🧹 {tname}: прибрано (старі й реклама) — {len(old)}")
            except Exception as e:
                log(f"⚠️ очищення {tname}: {str(e)[:60]}")
            gone = {x["i"] for x in old}
            dbs[t]["items"] = [x for x in items if x["i"] not in gone]
    if ((fget("vid_cfg.json", {}) or {}).get("wipe_at") or 0) != wipe0:   # 🗑 поки йшов збір, адмін видалив усе — не повертаємо зібране, прибираємо й нове
        ids = [i for t in TYPES for x in dbs[t]["items"] for i in (x.get("ids") or [x["i"]])]
        try:
            for n in range(0, len(ids), 100):
                await client.delete_messages(dest, ids[n:n + 100])
        except Exception as e:
            print(f"wipe: {e}")
        for t in TYPES:
            dbs[t]["items"] = []
        log("🗑 адмін видалив усе зібране — базу очищено")
    await push(dbs)
    await client.disconnect()


async def push(dbs: dict):
    at = f"{datetime.now(KYIV):%d.%m %H:%M}"
    meta = dbs.get("meta") or {}
    meta["log"], meta["at"] = LOG[-14:], at
    for t in TYPES:
        if t in dbs:
            dbs[t]["at"] = at
            meta.setdefault("cnt", {})[t] = len(dbs[t]["items"])
            if fpush(TYPES[t][1], {"at": at, "items": dbs[t]["items"]}):
                print(f"→ бот: {TYPES[t][0]} — {len(dbs[t]['items'])}")
    fpush("vid_meta.json", meta)


if __name__ == "__main__":
    asyncio.run(main())
