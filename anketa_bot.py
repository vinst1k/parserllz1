"""
anketa_bot.py — бот-парсер «Ищу работу» на lolz.live.
Извлекает TG-контакты авторов тем.

Запуск: python anketa_bot.py
"""
import asyncio
import json
import logging
import os
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8977938351:AAEvSZwKKPksvhYhmDNsNBRtnSQYZahZYB0")
OWNER_ID = int(os.getenv("OWNER_ID", "8341333063"))
LOLZ_LOGIN = os.getenv("LOLZ_LOGIN", "gsertoifur@gmail.com")
LOLZ_PASSWORD = os.getenv("LOLZ_PASSWORD", "vinstik123")

FORUM_BASE = "https://lolz.live"
FORUM_URL = "https://lolz.live/forums/832/"
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("anketa")

router = Router()

BOT_LINKS = {"lolz_news", "lolz_legal", "lolz_guru", "lolzteam_dev_blog", "lolzteam", "ulta_games"}
CHAT_PREFIXES = ("https://t.me/+Lr_o08HwF8NkYTEy", "https://t.me/+e_mGvWWzQp40ZjMy",
                 "https://t.me/joinchat/AAAAAFNLmVP0ZCy51tNOig")

parse_state = {"cancel": False, "threads": [], "running": False, "mode": "all", "count": 20}


# ── HTTP-парсер ────────────────────────────────────────────────────────────

def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UserAgent().random},
        follow_redirects=True,
        timeout=30,
        verify=False,
    )


async def lolz_login(client: httpx.AsyncClient) -> bool:
    # Получаем страницу логина для csrf
    resp = await client.get("https://lolz.live/login/")
    soup = BeautifulSoup(resp.text, "lxml")
    token_el = soup.select_one('input[name="_xfToken"]')
    token = token_el["value"] if token_el else ""

    # Логинимся
    data = {
        "login": LOLZ_LOGIN,
        "password": LOLZ_PASSWORD,
        "remember": "1",
        "_xfToken": token,
    }
    resp = await client.post("https://lolz.live/login/", data=data)
    if "logout" in resp.text.lower() or resp.url.path == "/":
        log.info("Логин выполнен")
        return True
    log.warning("Логин не подтверждён, пробуем продолжить")
    return True


def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    threads = []
    for el in soup.select("div.discussionListItem"):
        try:
            link = el.select_one("a.listBlock.main")
            if not link:
                continue
            href = link.get("href", "")
            title_el = link.select_one("h3.title .spanTitle") or link.select_one("h3.title")
            title = title_el.get_text(strip=True) if title_el else "N/A"
            author_el = (link.select_one(".username.threadCreator .styleUserNickname")
                         or link.select_one(".threadCreator"))
            author = author_el.get_text(strip=True) if author_el else "N/A"
            date_el = link.select_one(".startDate")
            date = date_el.get_text(strip=True) if date_el else ""
            url = href if href.startswith("http") else f"{FORUM_BASE}/{href.lstrip('/')}"
            threads.append({
                "title": title,
                "author": author,
                "url": url,
                "date": date,
                "tg": [],
            })
        except Exception as e:
            log.warning("parse err: %s", e)
    return threads


def get_page_count(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    nav = soup.select_one(".PageNav")
    if not nav:
        return 1
    last = nav.select("a")[-1] if nav.select("a") else None
    if last:
        t = last.get_text(strip=True)
        if t.isdigit():
            return int(t)
    return 1


def extract_tg(html: str) -> list[str]:
    raw = set()

    for m in re.findall(r'tg://resolve\?domain=([a-zA-Z0-9_]+)', html):
        raw.add(f"https://t.me/{m}")

    for m in re.findall(r'https?://t\.me/([a-zA-Z0-9_]+)', html):
        raw.add(f"https://t.me/{m}")

    for m in re.findall(r'@[a-zA-Z0-9_]{3,40}', html):
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


async def scrape(max_pages: int = 5, stop_event: asyncio.Event | None = None,
                 progress_callback=None) -> list[dict]:
    all_threads = []
    client = make_client()
    try:
        log.info("Логинюсь на lolz.live...")
        await lolz_login(client)

        resp = await client.get(FORUM_URL)
        if resp.status_code != 200:
            log.error("Не удалось открыть форум: %d", resp.status_code)
            return []

        debug_path = os.path.join(OUTPUT_DIR, "debug_railway.html")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(resp.text)
        log.info("HTML сохранён: %s", debug_path)

        total = min(get_page_count(resp.text), max_pages)
        log.info("Страниц: %d", total)

        for pg in range(1, total + 1):
            if stop_event and stop_event.is_set():
                break
            url = FORUM_URL if pg == 1 else f"{FORUM_URL}page-{pg}"
            if pg > 1:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
            threads = parse_listing(resp.text)
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
            try:
                resp = await client.get(t["url"])
                if resp.status_code == 200:
                    t["tg"] = extract_tg(resp.text)
                    if t["tg"]:
                        log.info("  TG: %s", t["tg"])
            except Exception as e:
                log.warning("extract err: %s", e)
            if progress_callback and (i + 1) % 5 == 0:
                tg_count = sum(1 for x in all_threads[:i+1] if x.get("tg"))
                await progress_callback(f"[{i+1}/{len(all_threads)}] TG найдено: {tg_count}")
            await asyncio.sleep(0.5)
    finally:
        await client.aclose()

    fpath = os.path.join(OUTPUT_DIR, "ankety_ishu_rabotu.json")
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(all_threads, f, ensure_ascii=False, indent=2)
    log.info("Сохранено %d анкет -> %s", len(all_threads), fpath)
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
    fpath = os.path.join(OUTPUT_DIR, "ankety_ishu_rabotu.json")
    if not os.path.exists(fpath):
        await message.answer("Сначала /scrape")
        return
    with open(fpath, encoding="utf-8") as f:
        threads = json.load(f)
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
    fpath = os.path.join(OUTPUT_DIR, "ankety_ishu_rabotu.json")
    if not os.path.exists(fpath):
        await message.answer("Сначала /scrape")
        return
    with open(fpath, encoding="utf-8") as f:
        threads = json.load(f)
    try:
        idx = int((command.args or "").strip())
        t = threads[idx - 1]
    except (ValueError, IndexError):
        await message.answer("Укажите номер: /anketa 1")
        return
    await message.answer(fmt_one(t, idx))


# ── Запуск ─────────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=BOT_TOKEN)
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
