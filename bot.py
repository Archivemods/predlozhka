import asyncio
import json
import logging
import os
import html
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_IDS = {int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()}
MOD_CHAT_ID = int(os.getenv("MODERATION_CHAT_ID", "0"))
DB_PATH = os.getenv("DATABASE_PATH", "/app/data/bot.db")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
router = Router()
dp = Dispatcher()
dp.include_router(router)


class AdminFlow(StatesGroup):
    setting_text = State()
    manual_reply = State()


def now() -> str:
    try:
        return datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds")
    except Exception:
        return datetime.now().astimezone().isoformat(timespec="seconds")


@asynccontextmanager
async def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


async def init_db() -> None:
    async with db() as c:
        await c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
          user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, message_count INTEGER NOT NULL DEFAULT 0,
          accepted_count INTEGER NOT NULL DEFAULT 0, rejected_count INTEGER NOT NULL DEFAULT 0,
          is_blocked INTEGER NOT NULL DEFAULT 0, blocked_at TEXT, blocked_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS submissions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          sender_name TEXT, username TEXT, text_content TEXT, content_type TEXT,
          created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
          media_group_id TEXT, source_message_ids TEXT NOT NULL DEFAULT '[]',
          media_metadata TEXT NOT NULL DEFAULT '[]', mod_message_ids TEXT NOT NULL DEFAULT '[]',
          FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS replies (
          id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL, reply_type TEXT NOT NULL,
          text_content TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id);
        """)
        cur = await c.execute("PRAGMA table_info(users)")
        user_columns = {row["name"] for row in await cur.fetchall()}
        if "accepted_count" not in user_columns:
            await c.execute("ALTER TABLE users ADD COLUMN accepted_count INTEGER NOT NULL DEFAULT 0")
        if "rejected_count" not in user_columns:
            await c.execute("ALTER TABLE users ADD COLUMN rejected_count INTEGER NOT NULL DEFAULT 0")
        for key, value in (("welcome_text", "Привет! Отправь сюда трек или любую предложку."),
                           ("ack_text", "Спасибо! Ваша предложка получена."),
                           ("accept_text", "Ваш трек принят! Спасибо за предложку."),
                           ("reject_text", "К сожалению, этот трек не подойдёт. Спасибо за предложку.")):
            await c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))
        await c.commit()


async def get_setting(key: str) -> str:
    async with db() as c:
        cur = await c.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else ""


async def set_setting(key: str, value: str) -> None:
    async with db() as c:
        await c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await c.commit()


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить приветствие /start", callback_data="cfg:welcome_text")],
        [InlineKeyboardButton(text="Изменить подтверждение", callback_data="cfg:ack_text")],
        [InlineKeyboardButton(text="Изменить текст принятия", callback_data="cfg:accept_text")],
        [InlineKeyboardButton(text="Изменить текст отказа", callback_data="cfg:reject_text")],
        [InlineKeyboardButton(text="Заблокированные пользователи", callback_data="blocked:list")],
    ])


async def blocked_users_page(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 8
    async with db() as c:
        cur = await c.execute("SELECT COUNT(*) AS total FROM users WHERE is_blocked=1")
        total = (await cur.fetchone())["total"]
        max_page = max(0, (total - 1) // page_size)
        page = max(0, min(page, max_page))
        cur = await c.execute(
            "SELECT user_id,username,first_name,last_name FROM users WHERE is_blocked=1 ORDER BY blocked_at DESC LIMIT ? OFFSET ?",
            (page_size, page * page_size),
        )
        users = await cur.fetchall()
    rows = []
    for user in users:
        name = " ".join(filter(None, (user["first_name"], user["last_name"]))) or user["username"] or str(user["user_id"])
        name = name[:32]
        rows.append([InlineKeyboardButton(
            text=f"{name} · Разблокировать",
            callback_data=f"unblock_user:{user['user_id']}:{page}",
        )])
    if max_page:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"blocked:page:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="blocked:noop"))
        if page < max_page:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"blocked:page:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Закрыть", callback_data="blocked:close")])
    text = f"Заблокированные пользователи · {total}" if total else "Заблокированных пользователей нет."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def control_keyboard(sub_id: int, blocked: bool = False, answered: bool = False) -> InlineKeyboardMarkup:
    if blocked:
        rows = [[InlineKeyboardButton(text="Разблокировать пользователя", callback_data=f"unblock:{sub_id}")]]
    elif answered:
        rows = [[InlineKeyboardButton(text="Ответить по новой", callback_data=f"reopen:{sub_id}")]]
    else:
        rows = [
            [InlineKeyboardButton(text="Принять трек", callback_data=f"accept:{sub_id}"),
             InlineKeyboardButton(text="Отклонить", callback_data=f"reject:{sub_id}")],
            [InlineKeyboardButton(text="Ответить самому", callback_data=f"manual:{sub_id}")],
            [InlineKeyboardButton(text="Заблокировать пользователя", callback_data=f"block:{sub_id}")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def message_text(m: Message) -> str:
    return m.text or m.caption or ""


def media_info(m: Message) -> dict:
    item = {"message_id": m.message_id, "type": "other"}
    if m.photo:
        item.update(type="photo", file_id=m.photo[-1].file_id, file_unique_id=m.photo[-1].file_unique_id)
    elif m.document:
        item.update(type="document", file_id=m.document.file_id, file_unique_id=m.document.file_unique_id, file_name=m.document.file_name)
    elif m.audio:
        item.update(type="audio", file_id=m.audio.file_id, file_unique_id=m.audio.file_unique_id, file_name=m.audio.file_name)
    elif m.video:
        item.update(type="video", file_id=m.video.file_id, file_unique_id=m.video.file_unique_id)
    elif m.voice:
        item.update(type="voice", file_id=m.voice.file_id, file_unique_id=m.voice.file_unique_id)
    elif m.video_note:
        item.update(type="video_note", file_id=m.video_note.file_id, file_unique_id=m.video_note.file_unique_id)
    elif m.sticker:
        item.update(type="sticker", file_id=m.sticker.file_id, file_unique_id=m.sticker.file_unique_id)
    return item


album_buffers: dict[tuple[int, str], list[Message]] = defaultdict(list)
album_tasks: dict[tuple[int, str], asyncio.Task] = {}
album_locks: dict[tuple[int, str], asyncio.Lock] = defaultdict(asyncio.Lock)
pending_manual_replies: dict[tuple[int, int], tuple[int, int]] = {}


async def save_submission(messages: list[Message]) -> None:
    messages.sort(key=lambda m: m.message_id)
    first = messages[0]
    user = first.from_user
    if user is None:
        return
    async with db() as c:
        cur = await c.execute("SELECT is_blocked FROM users WHERE user_id=?", (user.id,))
        existing = await cur.fetchone()
        blocked = bool(existing["is_blocked"]) if existing else False
        if existing:
            await c.execute("UPDATE users SET username=?,first_name=?,last_name=?,last_seen=?,message_count=message_count+? WHERE user_id=?",
                            (user.username, user.first_name, user.last_name, now(), len(messages), user.id))
        else:
            await c.execute("INSERT INTO users(user_id,username,first_name,last_name,first_seen,last_seen,message_count) VALUES(?,?,?,?,?,?,?)",
                            (user.id, user.username, user.first_name, user.last_name, now(), now(), len(messages)))
        if blocked:
            await c.commit()
            return
        media = [media_info(m) for m in messages]
        texts = [message_text(m) for m in messages if message_text(m)]
        content = "\n".join(texts)
        types = [x["type"] for x in media]
        content_type = "album" if first.media_group_id else (types[0] if types else "text")
        cur = await c.execute("INSERT INTO submissions(user_id,sender_name,username,text_content,content_type,created_at,media_group_id,source_message_ids,media_metadata) VALUES(?,?,?,?,?,?,?,?,?)",
                              (user.id, " ".join(filter(None, [user.first_name, user.last_name])), user.username, content, content_type,
                               now(), first.media_group_id, json.dumps([m.message_id for m in messages]), json.dumps(media, ensure_ascii=False)))
        sub_id = cur.lastrowid
        await c.commit()

    date = now().replace("T", " ")
    display_name = html.escape(user.full_name or "Без имени")
    profile_url = f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"
    caption = (f"👤 <a href=\"{profile_url}\">{display_name}</a> · <code>ID {user.id}</code>\n"
               f"🕒 {html.escape(date)}\n"
               f"💬 Сообщение: {html.escape(content[:2500]) if content else 'медиа / вложение'}")
    copied_ids: list[int] = []
    try:
        if len(messages) > 1:
            copied = await messages[0].bot.copy_messages(chat_id=MOD_CHAT_ID, from_chat_id=user.id, message_ids=[m.message_id for m in messages])
            copied_ids = [x.message_id for x in copied]
        else:
            copied = await messages[0].bot.copy_message(chat_id=MOD_CHAT_ID, from_chat_id=user.id, message_id=messages[0].message_id)
            copied_ids = [copied.message_id]
    except Exception as e:
        log.warning("Cannot copy submission %s as group, falling back: %s", sub_id, e)
        for m in messages:
            try:
                copied = await m.bot.copy_message(chat_id=MOD_CHAT_ID, from_chat_id=user.id, message_id=m.message_id)
                copied_ids.append(copied.message_id)
            except Exception:
                log.exception("Failed to copy message %s", m.message_id)
    try:
        info = await messages[0].bot.send_message(
            chat_id=MOD_CHAT_ID, text=caption, parse_mode="HTML",
            reply_markup=control_keyboard(sub_id, blocked=blocked),
        )
        copied_ids = [info.message_id]
        async with db() as c:
            await c.execute("UPDATE submissions SET mod_message_ids=? WHERE id=?", (json.dumps(copied_ids), sub_id))
            await c.commit()
    except Exception:
        log.exception("Could not post submission #%s to moderation chat %s", sub_id, MOD_CHAT_ID)
    try:
        await messages[0].answer(await get_setting("ack_text"), reply_to_message_id=messages[0].message_id)
    except Exception:
        log.exception("Could not acknowledge submission #%s to user %s", sub_id, user.id)


async def flush_album(key: tuple[int, str]) -> None:
    await asyncio.sleep(1.2)
    async with album_locks[key]:
        batch = album_buffers.pop(key, [])
        album_tasks.pop(key, None)
    if batch:
        await save_submission(batch)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user:
        async with db() as c:
            cur = await c.execute("SELECT is_blocked FROM users WHERE user_id=?", (message.from_user.id,))
            row = await cur.fetchone()
        if row and row["is_blocked"] and message.from_user.id not in OWNER_IDS:
            return
    if message.from_user and message.from_user.id in OWNER_IDS:
        await message.answer("Панель суперпользователя:", reply_markup=admin_menu())
    else:
        await message.answer(await get_setting("welcome_text"))


@router.message(F.chat.type == "private")
async def incoming(message: Message, state: FSMContext):
    user = message.from_user
    if not user:
        return
    current = await state.get_state()
    if user.id in OWNER_IDS and current == AdminFlow.setting_text.state:
        text = message.text or message.caption
        if not text:
            await message.answer("Нужно отправить текстовое сообщение.")
            return
        data = await state.get_data()
        await set_setting(data["setting_key"], text)
        await state.clear()
        await message.answer("Текст сохранён.", reply_markup=admin_menu())
        return
    if message.media_group_id:
        key = (user.id, message.media_group_id)
        async with album_locks[key]:
            album_buffers[key].append(message)
            old = album_tasks.get(key)
            if old:
                old.cancel()
            album_tasks[key] = asyncio.create_task(flush_album(key))
        return
    await save_submission([message])


async def update_mod_controls(bot: Bot, sub_id: int, user_id: int, answered: bool = False, blocked: bool = False) -> None:
    async with db() as c:
        cur = await c.execute("SELECT mod_message_ids,user_id,sender_name,username,created_at,text_content FROM submissions WHERE id=?", (sub_id,))
        row = await cur.fetchone()
    if not row:
        return
    for mid in json.loads(row["mod_message_ids"]):
        try:
            if mid == json.loads(row["mod_message_ids"])[0] and answered and not blocked:
                async with db() as c:
                    cur = await c.execute("SELECT reply_type,text_content FROM replies WHERE submission_id=? ORDER BY id DESC LIMIT 1", (sub_id,))
                    reply = await cur.fetchone()
                if reply:
                    if reply["reply_type"] == "accepted":
                        status_text = "\n\n✅ Было принято"
                    elif reply["reply_type"] == "rejected":
                        status_text = "\n\n❌ Отклонено"
                    else:
                        sent_text = html.escape(reply["text_content"] or "")
                        status_text = f"\n\n✍️ Отправлен свой текст:\n<blockquote>{sent_text}</blockquote>"
                    name = html.escape(row["sender_name"] or "Без имени")
                    profile = f"https://t.me/{row['username']}" if row["username"] else f"tg://user?id={row['user_id']}"
                    date = html.escape(row["created_at"].replace("T", " "))
                    content = html.escape((row["text_content"] or "медиа / вложение")[:2500])
                    card_text = (f"👤 <a href=\"{profile}\">{name}</a> · <code>ID {row['user_id']}</code>\n"
                                 f"🕒 {date}\n💬 Сообщение: {content}{status_text}")
                    await bot.edit_message_text(
                        chat_id=MOD_CHAT_ID, message_id=mid,
                        text=card_text, parse_mode="HTML",
                        reply_markup=control_keyboard(sub_id, blocked=blocked, answered=answered),
                    )
                    continue
            await bot.edit_message_reply_markup(chat_id=MOD_CHAT_ID, message_id=mid, reply_markup=control_keyboard(sub_id, blocked=blocked, answered=answered))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                log.debug("Keyboard update failed: %s", e)


async def send_response(bot: Bot, sub_id: int, moderator_id: int, kind: str, text: str) -> None:
    async with db() as c:
        cur = await c.execute("SELECT user_id,status FROM submissions WHERE id=?", (sub_id,))
        row = await cur.fetchone()
        if not row:
            return
        user_id = row["user_id"]
        cur = await c.execute("SELECT is_blocked FROM users WHERE user_id=?", (user_id,))
        user = await cur.fetchone()
        if user and user["is_blocked"]:
            return
        await c.execute("INSERT INTO replies(submission_id,user_id,moderator_id,reply_type,text_content,created_at) VALUES(?,?,?,?,?,?)",
                        (sub_id, user_id, moderator_id, kind, text, now()))
        old_status = row["status"]
        new_status = "answered" if kind == "manual" else kind
        if kind == "accepted" and old_status != "accepted":
            await c.execute("UPDATE users SET accepted_count=accepted_count+1 WHERE user_id=?", (user_id,))
        elif kind == "rejected" and old_status != "rejected":
            await c.execute("UPDATE users SET rejected_count=rejected_count+1 WHERE user_id=?", (user_id,))
        await c.execute("UPDATE submissions SET status=? WHERE id=?", (new_status, sub_id))
        await c.commit()
    async with db() as c:
        cur = await c.execute("SELECT source_message_ids FROM submissions WHERE id=?", (sub_id,))
        source_row = await cur.fetchone()
    source_ids = json.loads(source_row["source_message_ids"]) if source_row else []
    try:
        await bot.send_message(chat_id=user_id, text=text, reply_to_message_id=source_ids[0] if source_ids else None)
    except TelegramBadRequest:
        await bot.send_message(chat_id=user_id, text=text)
    await update_mod_controls(bot, sub_id, user_id, answered=True)


async def send_manual_reply(message: Message, state: FSMContext, sub_id: int) -> None:
    text = message.text or message.caption
    if not text:
        await message.answer("Пока принимаю текстовый ответ. Напишите текст или /cancel.")
        return
    await send_response(message.bot, sub_id, message.from_user.id, "manual", text)
    await state.clear()
    await message.answer("Ответ отправлен.")


@router.message(F.text == "/cancel")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.")


@router.message(F.chat.id == MOD_CHAT_ID, F.reply_to_message)
async def moderator_chat_reply(message: Message):
    if not message.from_user or not message.text:
        return
    key = (message.chat.id, message.reply_to_message.message_id)
    pending = pending_manual_replies.get(key)
    if not pending:
        return
    submission_id, moderator_id = pending
    if message.from_user.id != moderator_id:
        return
    await send_response(message.bot, submission_id, moderator_id, "manual", message.text)
    pending_manual_replies.pop(key, None)
    await message.reply("Ответ отправлен.")


@router.callback_query(F.data.startswith("cfg:"))
async def config_callback(call: CallbackQuery, state: FSMContext):
    if not call.from_user or call.from_user.id not in OWNER_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    labels = {
        "welcome_text": "приветствие после /start",
        "ack_text": "подтверждение получения предложки",
        "accept_text": "текст принятия",
        "reject_text": "текст отказа",
    }
    if key not in labels:
        await call.answer("Неизвестная настройка", show_alert=True)
        return
    await state.set_state(AdminFlow.setting_text)
    await state.update_data(setting_key=key)
    await call.message.answer(f"Отправьте новый текст: {labels[key]}. Для отмены /cancel.")
    await call.answer()


@router.callback_query(F.data == "blocked:list")
async def blocked_list(call: CallbackQuery):
    if not call.from_user or call.from_user.id not in OWNER_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return
    text, keyboard = await blocked_users_page()
    await call.message.edit_text(text, reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data.startswith("blocked:page:"))
async def blocked_page_callback(call: CallbackQuery):
    if not call.from_user or call.from_user.id not in OWNER_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return
    page = int(call.data.rsplit(":", 1)[1])
    text, keyboard = await blocked_users_page(page)
    await call.message.edit_text(text, reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data == "blocked:noop")
async def blocked_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data == "blocked:close")
async def blocked_close(call: CallbackQuery):
    if call.from_user and call.from_user.id in OWNER_IDS:
        await call.message.edit_text("Панель суперпользователя:", reply_markup=admin_menu())
    await call.answer()


@router.callback_query(F.data.startswith("unblock_user:"))
async def unblock_from_list(call: CallbackQuery):
    if not call.from_user or call.from_user.id not in OWNER_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return
    _, user_id_raw, page_raw = call.data.split(":")
    user_id, page = int(user_id_raw), int(page_raw)
    async with db() as c:
        await c.execute("UPDATE users SET is_blocked=0,blocked_at=NULL,blocked_by=NULL WHERE user_id=?", (user_id,))
        await c.execute("UPDATE submissions SET status='new' WHERE user_id=? AND status='blocked'", (user_id,))
        await c.commit()
        cur = await c.execute("SELECT id,status FROM submissions WHERE user_id=?", (user_id,))
        submissions = await cur.fetchall()
    for submission in submissions:
        await update_mod_controls(call.bot, submission["id"], user_id,
                                  answered=submission["status"] in ("answered", "accepted", "rejected"))
    text, keyboard = await blocked_users_page(page)
    await call.message.edit_text(text, reply_markup=keyboard)
    await call.answer("Разблокирован")


@router.message(F.chat.type == "private", F.text.startswith("/unblock"))
async def manual_unblock(message: Message):
    if not message.from_user or message.from_user.id not in OWNER_IDS:
        await message.answer("Команда доступна только суперпользователям.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /unblock <telegram_id>")
        return
    user_id = int(parts[1])
    async with db() as c:
        cur = await c.execute("SELECT is_blocked FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            await message.answer("Пользователь не найден в базе.")
            return
        await c.execute("UPDATE users SET is_blocked=0,blocked_at=NULL,blocked_by=NULL WHERE user_id=?", (user_id,))
        await c.execute("UPDATE submissions SET status='new' WHERE user_id=? AND status='blocked'", (user_id,))
        await c.commit()
        cur = await c.execute("SELECT id,status FROM submissions WHERE user_id=?", (user_id,))
        submissions = await cur.fetchall()
    for submission in submissions:
        await update_mod_controls(
            message.bot, submission["id"], user_id,
            answered=submission["status"] in ("answered", "accepted", "rejected"),
        )
    await message.answer(f"Пользователь {user_id} разблокирован.")


@router.callback_query(F.data.startswith("manual:"))
async def manual_callback(call: CallbackQuery, state: FSMContext):
    sub_id = int(call.data.split(":")[1])
    async with db() as c:
        cur = await c.execute("SELECT s.user_id,u.is_blocked FROM submissions s JOIN users u USING(user_id) WHERE s.id=?", (sub_id,))
        row = await cur.fetchone()
    if not row or row["is_blocked"]:
        await call.answer("Пользователь заблокирован или предложка не найдена", show_alert=True)
        return
    prompt = await call.message.answer(
        f"Напишите ответ пользователю {row['user_id']} ответом на это сообщение:",
        reply_markup=ForceReply(selective=True),
    )
    pending_manual_replies[(prompt.chat.id, prompt.message_id)] = (sub_id, call.from_user.id)
    await call.answer()


@router.callback_query(F.data.startswith("reopen:"))
async def reopen_callback(call: CallbackQuery):
    sub_id = int(call.data.split(":")[1])
    async with db() as c:
        cur = await c.execute("SELECT user_id FROM submissions WHERE id=?", (sub_id,))
        row = await cur.fetchone()
        cur = await c.execute("SELECT is_blocked FROM users WHERE user_id=?", (row["user_id"],)) if row else None
        user = await cur.fetchone() if cur else None
    if row and user and not user["is_blocked"]:
        await update_mod_controls(call.bot, sub_id, row["user_id"])
    await call.answer()


@router.callback_query(F.data.startswith("accept:") | F.data.startswith("reject:"))
async def decision_callback(call: CallbackQuery):
    action, sid = call.data.split(":")
    sub_id = int(sid)
    kind = "accepted" if action == "accept" else "rejected"
    text = await get_setting("accept_text" if action == "accept" else "reject_text")
    try:
        await send_response(call.bot, sub_id, call.from_user.id, kind, text)
        await call.answer("Отправлено пользователю")
    except TelegramForbiddenError:
        await call.answer("Не удалось написать пользователю", show_alert=True)


@router.callback_query(F.data.startswith("block:"))
async def block_callback(call: CallbackQuery):
    sub_id = int(call.data.split(":")[1])
    async with db() as c:
        cur = await c.execute("SELECT user_id FROM submissions WHERE id=?", (sub_id,))
        row = await cur.fetchone()
        if not row:
            await call.answer("Предложка не найдена", show_alert=True)
            return
        user_id = row["user_id"]
        await c.execute("UPDATE users SET is_blocked=1,blocked_at=?,blocked_by=? WHERE user_id=?", (now(), call.from_user.id, user_id))
        await c.execute("UPDATE submissions SET status='blocked' WHERE user_id=? AND status IN ('new','answered','accepted','rejected')", (user_id,))
        await c.commit()
        cur = await c.execute("SELECT id FROM submissions WHERE user_id=?", (user_id,))
        subs = await cur.fetchall()
    for item in subs:
        await update_mod_controls(call.bot, item["id"], user_id, blocked=True)
    await call.answer("Пользователь заблокирован")


@router.callback_query(F.data.startswith("unblock:"))
async def unblock_callback(call: CallbackQuery):
    sub_id = int(call.data.split(":")[1])
    async with db() as c:
        cur = await c.execute("SELECT user_id FROM submissions WHERE id=?", (sub_id,))
        row = await cur.fetchone()
        if not row:
            await call.answer("Предложка не найдена", show_alert=True)
            return
        user_id = row["user_id"]
        await c.execute("UPDATE users SET is_blocked=0,blocked_at=NULL,blocked_by=NULL WHERE user_id=?", (user_id,))
        await c.execute("UPDATE submissions SET status='new' WHERE id=? AND status='blocked'", (sub_id,))
        await c.commit()
        cur = await c.execute("SELECT id,status FROM submissions WHERE user_id=?", (user_id,))
        subs = await cur.fetchall()
    for item in subs:
        await update_mod_controls(call.bot, item["id"], user_id, answered=item["status"] == "answered")
    await call.answer("Пользователь разблокирован")


async def main():
    if not TOKEN:
        raise RuntimeError("Set BOT_TOKEN in .env")
    if not MOD_CHAT_ID:
        raise RuntimeError("Set MODERATION_CHAT_ID in .env")
    await init_db()
    bot = Bot(TOKEN)
    me = await bot.get_me()
    log.info("Started as @%s (id=%s); moderation chat=%s; database=%s", me.username, me.id, MOD_CHAT_ID, DB_PATH)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
