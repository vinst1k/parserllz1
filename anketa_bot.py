"""
anketa_bot.py — бот-парсер «Ищу работу» на lolz.live.
Извлекает TG-контакты авторов тем.

Запуск: python anketa_bot.py
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from fake_useragent import UserAgent
from playwright.async_api import async_playwright, Page
from playwright_stealth import Stealth

BOT_TOKEN = os.getenv("BOT_TOKEN", "8977938351:AAEvSZwKKPksvhYhmDNsNBRtnSQYZahZYB0")
OWNER_ID = int(os.getenv("OWNER_ID", "8341333063"))
LOLZ_LOGIN = os.getenv("LOLZ_LOGIN", "gsertoifur@gmail.com")
LOLZ_PASSWORD = os.getenv("LOLZ_PASSWORD", "vinstik123")
USE_PROXY = os.getenv("USE_PROXY", "0") == "1"
PROXY_SERVER = os.getenv("PROXY_SERVER", "http://127.0.0.1:10808")

FORUM_BASE = "https://lolz.live"
FORUM_URL = "https://lolz.live/forums/832/"
USER_DATA_DIR = Path(os.getenv("BROWSER_PROFILE", "./browser_profile_lolz"))
OUTPUT_DIR = Path("./output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

STEALTH = Stealth(
    navigator_languages_override=("ru-RU", "ru"),
    navigator_platform_override="Win32",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anketa")

router = Router()

BOT_LINKS = {"lolz_news", "lolz_legal", "lolz_guru", "lolzteam_dev_blog", "lolzteam", "ulta_games"}
CHAT_PREFIXES = ("https://t.me/+Lr_o08HwF8NkYTEy", "https://t.me/+e_mGvWWzQp40ZjMy",
                 "https://t.me/joinchat/AAAAAFNLmVP0ZCy51tNOig")

parse_state = {"cancel": False, "threads": [], "running": False, "mode": "all", "count": 20}


# ── Playwright ─────────────────────────────────────────────────────────────

async def nav(page: Page, url: str) -> bool:
    for attempt in range(1, 4):
        try:
            r = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if r and r.status in (403, 429):
                await asyncio.sleep(3 * attempt)
                continue
            await asyncio.sleep(1)
            return True
        except Exception as e:
            log.warning("nav err %d: %s", attempt, e)
            await asyncio.sleep(2 * attempt)
    return False


async def lolz_login(page: Page) -> bool:
    if not await nav(page, "https://lolz.live/login/"):
        return False
    await asyncio.sleep(2)
    email_input = (await page.query_selector('input[name="login"]')
                   or await page.query_selector('input[type="email"]'))
    if not email_input:
        avatar = await page.query_selector('.avatar, .username, a[href*="account"]')
        if avatar:
            log.info("Уже залогинены")
            return True
        return False
    await email_input.click()
    await email_input.fill(LOLZ_LOGIN)
    await asyncio.sleep(0.3)
    pass_input = (await page.query_selector('input[name="password"]')
                  or await page.query_selector('input[type="password"]'))
    if not pass_input:
        return False
    await pass_input.click()
    await pass_input.fill(LOLZ_PASSWORD)
    await asyncio.sleep(0.3)
    submit = (await page.query_selector('button[type="submit"]')
              or await page.query_selector('input[type="submit"]'))
    if submit:
        await submit.click()
        await asyncio.sleep(3)
    log.info("Логин выполнен")
    return True


async def get_pages(page: Page) -> int:
    try:
        nav_el = await page.query_selector(".PageNav")
        if not nav_el:
            return 1
        last = await nav_el.query_selector("a:last-child")
        if last:
            t = (await last.inner_text()).strip()
            if t.isdigit():
                return int(t)
    except Exception:
        pass
    return 1


EXTRACT_JS = """
() => {
    const allLinks = [];
    document.querySelectorAll('a[href*="t.me/"]').forEach(a => {
        allLinks.push({href: a.href, text: a.textContent.trim()});
    });
    const tgResolve = [];
    document.querySelectorAll('a[href*="tg://resolve"]').forEach(a => {
        tgResolve.push(a.href);
    });
    document.querySelectorAll('[data-value*="tg://resolve"]').forEach(el => {
        tgResolve.push(el.getAttribute('data-value'));
    });
    const firstMsg = document.querySelector('.messageList .message');
    const firstMsgText = firstMsg ? (firstMsg.textContent || '') : '';
    const atMentions = (firstMsgText.match(/@[a-zA-Z0-9_]{3,40}/g) || []);
    const tgFromAttrs = [];
    document.querySelectorAll('[data-tg-url], [data-telegram-url]').forEach(el => {
        const v = el.getAttribute('data-tg-url') || el.getAttribute('data-telegram-url');
        if (v) tgFromAttrs.push(v);
    });
    const bbCode = (document.body.innerHTML.match(/https?:\\/\\/t\\.me\\/[a-zA-Z0-9_]+/g) || []);
    return {
        allLinks,
        tgResolve: [...new Set(tgResolve)],
        atMentions: [...new Set(atMentions)],
        tgFromAttrs,
        bbCode: [...new Set(bbCode)]
    };
}
"""


def filter_contacts(result: dict) -> list[str]:
    raw = set()

    for link in result.get("allLinks", []):
        href = link["href"]
        raw.add(href)

    for url in result.get("tgResolve", []):
        if url:
            if "domain=" in url:
                username = url.split("domain=")[-1].split("&")[0]
                raw.add(f"https://t.me/{username}")
            else:
                raw.add(url)

    for href in result.get("tgFromAttrs", []):
        if href:
            raw.add(href)

    for url in result.get("bbCode", []):
        raw.add(url)

    for m in result.get("atMentions", []):
        raw.add(m)

    contacts = []
    for href in raw:
        text = href.lower()
        if any(ch in href for ch in BOT_LINKS):
            continue
        if any(href.startswith(p) for p in CHAT_PREFIXES):
            continue
        if "joinchat" in href:
            continue
        if href.endswith("_bot") or "bot" in text:
            continue
        contacts.append(href)

    return list(dict.fromkeys(contacts))


async def extract_tg_from_thread(page: Page, url: str) -> list[str]:
    if not await nav(page, url):
        return []
    try:
        result = await page.evaluate(EXTRACT_JS)
        return filter_contacts(result)
    except Exception as e:
        log.warning("extract err: %s", e)
        return []


async def parse_listing(page: Page) -> list[dict]:
    threads = []
    for el in await page.query_selector_all("div.discussionListItem"):
        try:
            link = await el.query_selector("a.listBlock.main")
            if not link:
                continue
            href = await link.get_attribute("href") or ""
            title_el = (await link.query_selector("h3.title .spanTitle")
                        or await link.query_selector("h3.title"))
            title = await title_el.inner_text() if title_el else "N/A"
            author_el = (await link.query_selector(".username.threadCreator .styleUserNickname")
                         or await link.query_selector(".threadCreator"))
            author = await author_el.inner_text() if author_el else "N/A"
            date_el = await link.query_selector(".startDate")
            date = await date_el.inner_text() if date_el else ""
            url_full = href if href.startswith("http") else f"{FORUM_BASE}/{href.lstrip('/')}"
            threads.append({
                "title": title.strip(),
                "author": author.strip(),
                "url": url_full,
                "date": date.strip(),
                "tg": [],
            })
        except Exception as e:
            log.warning("list parse err: %s", e)
    return threads


async def scrape(max_pages: int = 5, stop_event: asyncio.Event | None = None,
                 progress_callback=None) -> list[dict]:
    all_threads = []
    async with STEALTH.use_async(async_playwright()) as p:
        launch_args = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox",
                     "--disable-dev-shm-usage"],
        }
        if USE_PROXY:
            launch_args["proxy"] = {"server": PROXY_SERVER}

        browser = await p.chromium.launch(**launch_args)
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            user_agent=UserAgent().random,
        )
        page = await ctx.new_page()

        log.info("Логинюсь на lolz.live...")
        await lolz_login(page)

        if not await nav(page, FORUM_URL):
            log.error("Не удалось открыть форум")
            await browser.close()
            return []

        total = min(await get_pages(page), max_pages)
        log.info("Страниц: %d", total)

        for pg in range(1, total + 1):
            if stop_event and stop_event.is_set():
                break
            url = FORUM_URL if pg == 1 else f"{FORUM_URL}page-{pg}"
            if pg > 1 and not await nav(page, url):
                continue
            await page.mouse.wheel(0, 400)
            await asyncio.sleep(0.5)
            threads = await parse_listing(page)
            all_threads.extend(threads)
            log.info("Стр %d/%d: %d тем", pg, total, len(threads))
            if progress_callback:
                await progress_callback(f"Страница {pg}/{total}: {len(all_threads)} тем")
            if pg < total:
                await asyncio.sleep(1)

        log.info("Извлекаю TG из %d тем...", len(all_threads))
        for i, t in enumerate(all_threads):
            if stop_event and stop_event.is_set():
                break
            log.info("[%d/%d] %s", i + 1, len(all_threads), t["title"][:50])
            t["tg"] = await extract_tg_from_thread(page, t["url"])
            if t["tg"]:
                log.info("  TG: %s", t["tg"])
            if progress_callback and (i + 1) % 5 == 0:
                tg_count = sum(1 for x in all_threads[:i+1] if x.get("tg"))
                await progress_callback(f"[{i+1}/{len(all_threads)}] TG найдено: {tg_count}")
            await asyncio.sleep(0.8)

        await browser.close()

    fname = OUTPUT_DIR / "ankety_ishu_rabotu.json"
    fname.write_text(json.dumps(all_threads, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Сохранено %d анкет -> %s", len(all_threads), fname)
    return all_threads


# ── Форматирование ────────────────────────────────────────────────────────

def fmt_text(threads: list[dict], mode: str = "all") -> str:
    if mode == "tg_only":
        threads = [t for t in threads if t.get("tg")]
    if not threads:
        return "Анкет не найдено."
    lines = [f"📋 Ищу работу — {len(threads)} анкет\n"]
    for i, t in enumerate(threads, 1):
        tg = t.get("tg", [])
        tg_str = tg[0] if tg else "нет TG"
        lines.append(
            f"#{i}  {t['title']}\n"
            f"   Автор: {t['author']}\n"
            f"   TG: {tg_str}\n"
            f"   {t['url']}\n"
        )
    return "\n".join(lines)


def fmt_one(t: dict, idx: int) -> str:
    tg = t.get("tg", [])
    tg_lines = "\n".join(f"  {c}" for c in tg) if tg else "  нет TG"
    return (
        f"👤 Анкета #{idx}\n"
        f"Название: {t['title']}\n"
        f"Автор: {t['author']}\n"
        f"Дата: {t['date']}\n\n"
        f"TG-контакты:\n{tg_lines}\n\n"
        f"Тема: {t['url']}"
    )


# ── Клавиатуры ────────────────────────────────────────────────────────────

def kb_size() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10", callback_data="scrape:10"),
         InlineKeyboardButton(text="20", callback_data="scrape:20"),
         InlineKeyboardButton(text="50", callback_data="scrape:50"),
         InlineKeyboardButton(text="300", callback_data="scrape:300")],
    ])


def kb_mode(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Только с TG", callback_data=f"mode:tg_only:{count}"),
         InlineKeyboardButton(text="📋 Без разницы", callback_data=f"mode:all:{count}")],
    ])


def kb_stop() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Остановить и скинуть", callback_data="stop:now")],
    ])


# ── Команды ────────────────────────────────────────────────────────────────

@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    if message.from_user.id != OWNER_ID:
        return
    await message.answer(
        "🤖 Бот анкет «Ищу работу» — lolz.live\n\n"
        "/scrape — начать парсинг\n"
        "/list — список анкет\n"
        "/anketa <номер> — одна анкета с TG"
    )


@router.message(Command("scrape"))
async def cmd_scrape(message: Message) -> None:
    if message.from_user.id != OWNER_ID:
        return
    await message.answer("Сколько тем парсить?", reply_markup=kb_size())


@router.callback_query(lambda c: c.data and c.data.startswith("scrape:"))
async def on_scrape_size(cb: CallbackQuery) -> None:
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Только для владельца")
        return
    count = int(cb.data.split(":")[1])
    parse_state["count"] = count
    parse_state["threads"] = []
    parse_state["cancel"] = False
    await cb.message.edit_text(f"Парсить {count} тем. Режим:", reply_markup=kb_mode(count))
    await cb.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("mode:"))
async def on_mode(cb: CallbackQuery) -> None:
    if cb.from_user.id != OWNER_ID:
        return
    parts = cb.data.split(":")
    mode = parts[1]
    count = int(parts[2])
    parse_state["mode"] = mode
    parse_state["threads"] = []
    parse_state["cancel"] = False
    parse_state["running"] = True

    mode_label = "только с TG" if mode == "tg_only" else "все"
    await cb.message.edit_text(
        f"Режим: {mode_label} | Тем: {count}\n⏳ Парсинг запущен...",
        reply_markup=kb_stop()
    )
    await cb.answer()

    stop_event = asyncio.Event()
    parse_state["_stop_event"] = stop_event

    async def progress_cb(text: str):
        try:
            await cb.message.edit_text(
                f"Режим: {mode_label} | Тем: {count}\n⏳ {text}\n\nНажмите «Остановить» для остановки",
                reply_markup=kb_stop()
            )
        except Exception:
            pass

    asyncio.create_task(_run_scrape(cb.message, count, mode, stop_event, progress_cb))


async def _run_scrape(message: Message, count: int, mode: str,
                      stop_event: asyncio.Event, progress_cb):
    try:
        threads = await scrape(max_pages=count, stop_event=stop_event,
                               progress_callback=progress_cb)
        parse_state["threads"] = threads
        parse_state["running"] = False

        if mode == "tg_only":
            filtered = [t for t in threads if t.get("tg")]
        else:
            filtered = threads

        if not filtered:
            await message.answer("Анкет не найдено.")
            return

        text = fmt_text(filtered, mode)
        for i in range(0, len(text), 4000):
            await message.answer(text[i:i+4000])
    except Exception as e:
        log.error("Scrape error: %s", e)
        await message.answer(f"Ошибка: {e}")
        parse_state["running"] = False


@router.callback_query(lambda c: c.data == "stop:now")
async def on_stop(cb: CallbackQuery) -> None:
    if cb.from_user.id != OWNER_ID:
        return
    stop_event = parse_state.get("_stop_event")
    if stop_event:
        stop_event.set()
    parse_state["cancel"] = True
    parse_state["running"] = False

    threads = parse_state["threads"]
    mode = parse_state["mode"]

    if not threads:
        await cb.message.edit_text("Парсинг остановлен. Ничего не собрано.")
        await cb.answer()
        return

    if mode == "tg_only":
        filtered = [t for t in threads if t.get("tg")]
    else:
        filtered = threads

    await cb.message.edit_text(f"⏹ Остановлено. Собрано {len(filtered)} анкет. Отправляю...")
    await cb.answer()

    text = fmt_text(filtered, mode)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000])


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    if message.from_user.id != OWNER_ID:
        return
    fpath = OUTPUT_DIR / "ankety_ishu_rabotu.json"
    if not fpath.exists():
        await message.answer("Сначала /scrape")
        return
    threads = json.loads(fpath.read_text(encoding="utf-8"))
    if not threads:
        await message.answer("Пусто.")
        return
    lines = []
    for i, t in enumerate(threads[:50], 1):
        tg = t.get("tg", [])
        tg_str = tg[0] if tg else "—"
        lines.append(f"{i}. {t['author']}: {tg_str}")
    if len(threads) > 50:
        lines.append(f"\n...ещё {len(threads)-50}")
    await message.answer("\n".join(lines))


@router.message(Command("anketa"))
async def cmd_anketa(message: Message, command: CommandObject) -> None:
    if message.from_user.id != OWNER_ID:
        return
    fpath = OUTPUT_DIR / "ankety_ishu_rabotu.json"
    if not fpath.exists():
        await message.answer("Сначала /scrape")
        return
    threads = json.loads(fpath.read_text(encoding="utf-8"))
    try:
        idx = int((command.args or "").strip())
        t = threads[idx - 1]
    except (ValueError, IndexError):
        await message.answer("Укажите номер: /anketa 1")
        return
    await message.answer(fmt_one(t, idx))


# ── Запуск ─────────────────────────────────────────────────────────────────

async def main():
    bot_kwargs = {"token": BOT_TOKEN}
    if USE_PROXY:
        bot_kwargs["proxy"] = PROXY_SERVER
    bot = Bot(**bot_kwargs)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Бот запущен, владелец: %s", OWNER_ID)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, handle_signals=False)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
