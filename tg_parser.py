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
from telethon.tl.types import Channel, DocumentAttributeVideo, MessageEntityMention, MessageEntityTextUrl, MessageEntityUrl, PeerChannel

SYNC_URL, SYNC_KEY = os.environ.get("SYNC_URL", "").rstrip("/"), os.environ.get("SYNC_KEY", "")
PER_DAY, FIRST_DAYS, PAUSE, RUN_SEC, MAX_SEC = 10, 3, 3, 10 * 60, 3 * 3600   # базово топ-10 кожного типу; перший збір — 3 останні дні; до 10 хв на запуск
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
    if lim and len(text.strip()) > lim:
        return f"довгий текст ({len(text.strip())} символів)"   # під справжнім контентом підпис зазвичай короткий
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
    ban_o = list(main_db.get("ban_o") or [])
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
        own = own_keys(ent, s["ref"])
        mode = int(s["ad"]) if s.get("ad") is not None else ad_mode
        ads = 0
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
            dl = [first + timedelta(days=n) for n in range((yday - first).days + 1)]
            days[t] = [d for d in dl if d < yday] if now.hour < 8 else dl
        all_days = sorted({d for dl in days.values() for d in dl})
        blocked = False
        pool = {t: {} for t in TYPES}                                # тип → день → [пости]
        taken = {t: {x["o"] for x in dbs[t]["items"] if x["k"] == k} | {int(b.split(":")[1]) for b in ban_o if b.split(":")[0] == k} for t in TYPES}
        want = {}                                                    # 🎯 скільки GIF / фото добрати з цього джерела до запасу (частка джерела, до ×3, якщо інші не дотягують)
        for t in tgt:
            cur = sum(1 for x in dbs[t]["items"] if x["k"] == k and x["i"] not in kill[t])
            want[t] = max(0, min(3 * share[t] - cur, tgt[t] - len(dbs[t]["items"]), 60))
        try:
            if all_days or any(want.values()):
                end = datetime.combine((all_days[-1] if all_days else yday) + timedelta(days=1), datetime.min.time(), KYIV)
                start = datetime.combine(all_days[0] - timedelta(days=2), datetime.min.time(), KYIV) if all_days else end   # +2 дні назад — для добору
                deep = datetime.combine((min(days["g"] + days["p"]) if days["g"] + days["p"] else yday) - timedelta(days=GIF_DEEP), datetime.min.time(), KYIV)
                since = {"v": start, "g": deep if (days["g"] or want["g"]) else start, "p": deep if want["p"] else start}   # GIF і фото — глибше, коли треба добирати
                albums: dict = {}
                async for m in client.iter_messages(ent, offset_date=end, limit=8000):
                    if m.date < min(since.values()):
                        break
                    t = kind_of(m)
                    if not t or m.date < since[t]:
                        continue
                    if t == "p" and m.grouped_id:
                        albums.setdefault(m.grouped_id, []).append(m); continue
                    if m.id not in taken[t]:
                        if ad_reason([m], own, mode, ad_words, bad, ad_lim):
                            ads += 1; continue
                        pool[t].setdefault(m.date.astimezone(KYIV).date(), []).append(Post([m], t))
                for ms in albums.values():
                    p = Post(ms, "p")
                    if p.id not in taken["p"] and p.date >= since["p"]:
                        if ad_reason(p.ms, own, mode, ad_words, bad, ad_lim):
                            ads += 1; continue
                        pool["p"].setdefault(p.date.astimezone(KYIV).date(), []).append(p)
        except Exception as e:
            st.update(ok=0, err=str(e)[:120]); log(f"⚠️ {st['t'][:30]}: {str(e)[:80]}"); continue
        for t, (tname, _f, qf, _bf, donef, _cap) in TYPES.items():
            if stop or blocked:
                break
            for day in days[t]:
                if stop or time.time() - t0 > RUN_SEC:
                    stop = True; break
                q = max(0, min(48, int(s.get(qf) or bases[t])))      # квота каналу для типу: базова (з адмінки) ± за 👍 / 👎 у боті
                already = sum(1 for x in dbs[t]["items"] if x["k"] == k and x.get("day") == day.isoformat())
                q = max(0, q - already)
                picks = pick(pool[t].get(day, []), q, t) if q else []
                if len(picks) < q and (pool[t].get(day) or t == "g"):   # бракує — добір: відео й фото — з 2 попередніх днів (якщо за день щось було); GIF — найкращі за 30 днів
                    back = range(1, (GIF_DEEP if t == "g" else 2) + 1)
                    extra = sorted((p for n_ in back for p in pool[t].get(day - timedelta(days=n_), []) if p.id not in taken[t] and p not in picks),
                                   key=lambda p: -p.sc)
                    picks += extra[:q - len(picks)]
                added = 0
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
                    await asyncio.sleep(PAUSE)
                st[donef] = day.isoformat()
                if added:
                    log(f"🍓 {st['t'][:24]} · {day:%d.%m} · {tname}: топ-{added} з {len(pool[t].get(day, []))}")
            if want.get(t) and not stop and not blocked and time.time() - t0 < RUN_SEC:   # 🎯 запас: найкращі за 30 днів, яких ще немає
                cur = sum(1 for x in dbs[t]["items"] if x["k"] == k and x["i"] not in kill[t])
                need = max(0, min(3 * share[t] - cur, tgt[t] - len(dbs[t]["items"]), 60))
                cands = sorted((p for dl in pool[t].values() for p in dl if p.id not in taken[t]), key=lambda p: -p.sc)[:need]
                if cands:
                    try:
                        new = await forward_batch(client, dest, ent, cands)
                        n_ = 0
                        for p, ids in zip(cands, new):
                            if ids:
                                dbs[t]["items"].append(item_of(p, ids, k, p.date.astimezone(KYIV).date(), 50 + n_, ent)); taken[t].add(p.id); n_ += 1
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
                                if ad_reason([m], own, mode, ad_words, bad, ad_lim):
                                    kill[t].add(x["i"]); ads += 1
        except Exception as e:
            print(f"refresh {k}: {e}")
        st["ads"] = ads; ad_total += ads
        if ads:
            log(f"🚫 {st['t'][:24]}: відсіяно рекламу — {ads}")
    main_db["ad_un"] = sorted(bad)[-400:]; main_db["adn"] = ad_total; main_db["ban_o"] = list(dict.fromkeys(ban_o))[-3000:]
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
