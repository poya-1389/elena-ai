"""
bot.py — نقطه‌ی ورود ربات النا (Elena)

Stack: Python 3.12+ / aiogram >= 3.31 (Bot API 10.3) / PostgreSQL (Railway) / Gemini 2.5 Flash

تصمیم Polling در برابر Webhook:
    Polling انتخاب شد. روی Railway، پروسه به‌صورت دائمی (Long-running worker) اجرا
    می‌شود نه Serverless، پس مزیت اصلی Webhook (بیدار شدن on-demand) اینجا بی‌فایده
    است؛ در عوض Polling نیاز به سرور HTTP/SSL/دامنه ندارد، پیاده‌سازی‌اش خطای کمتری
    دارد و برای یک پروژه با محدودیت «حداکثر ۴ فایل» ساده‌تر و پایدارتر است.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import ai
from database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("elena.bot")

AMBIENT_PROBABILITY = 0.3
AMBIENT_KEYWORDS = ("نظرت", "به نظر", "نظر شما", "نظرتون", "فکر می‌کنی", "فکر میکنی", "نظرتو")
EDIT_THROTTLE_CHARS = 48  # حداقل رشد متن بین دو ویرایش پیام، برای جلوگیری از Flood

db: Database
bot_id: int = 0
bot_username: str = ""


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ گفتگوی جدید", callback_data="new_chat")],
            [InlineKeyboardButton(text="📊 وضعیت", callback_data="stats")],
            [InlineKeyboardButton(text="⭐ Premium", callback_data="premium")],
        ]
    )


def memory_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🗑 پاک کردن حافظه", callback_data="clear_memory")]]
    )


# ---------------------------------------------------------------------------
# تبدیل خروجی Markdown-ایِ مدل به HTML موردقبول تلگرام (فرمت رسمی)
# ---------------------------------------------------------------------------
def markdown_to_telegram_html(text: str) -> str:
    # ابتدا escape کاراکترهای خاص HTML
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # بلوک کد ```lang\n...\n```
    out = re.sub(
        r"```(\w*)\n(.*?)```",
        lambda m: f"<pre><code>{m.group(2)}</code></pre>",
        out,
        flags=re.DOTALL,
    )
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r"<i>\1</i>", out)
    out = re.sub(r"__(.+?)__", r"<u>\1</u>", out)
    out = re.sub(r"~~(.+?)~~", r"<s>\1</s>", out)
    out = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", out)
    out = re.sub(r"(?m)^&gt;\s?(.+)$", r"<blockquote>\1</blockquote>", out)
    return out


async def download_bytes(bot: Bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    buf = await bot.download_file(tg_file.file_path)
    return buf.read()


def is_direct_mode(message: Message) -> bool:
    if message.chat.type == "private":
        return True
    text = message.text or message.caption or ""
    if message.reply_to_message and message.reply_to_message.from_user and (
        message.reply_to_message.from_user.id == bot_id
    ):
        return True
    if bot_username and f"@{bot_username.lower()}" in text.lower():
        return True
    if ai.message_calls_elena(text):
        return True
    return False


def is_ambient_worthy(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 8:
        return False
    if t.endswith("?") or t.endswith("؟"):
        return True
    return any(k in t for k in AMBIENT_KEYWORDS)


async def stream_reply(message: Message, user_text: str, media: list[tuple[bytes, str]] | None = None) -> None:
    chat_id = message.chat.id
    user = message.from_user
    author_name = user.first_name if message.chat.type != "private" else None

    allowed, used_today, limit_today, used_4h, limit_4h = await db.register_message_and_check_limit(
        chat_id, user.id
    )
    if not allowed:
        if message.chat.type != "private":
            return  # در گروه، بی‌سروصدا رد شو تا اسپم نشه
        await message.answer(
            "امروز به سقف پیام‌هات رسیدی. یکم بعد دوباره بیا 🙂\n"
            f"({used_today}/{limit_today} امروز — {used_4h}/{limit_4h} این ۴ ساعت اخیر)"
        )
        return

    await db.upsert_user(user.id, user.first_name, user.username)
    history = await db.get_history(chat_id)
    memory_notes = await db.get_memory_notes(user.id) if message.chat.type == "private" else None

    try:
        await message.bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:  # noqa: BLE001
        pass

    placeholder = await message.answer("…")
    buffer = ""
    last_edit_len = 0
    had_error = False

    async for chunk in ai.generate_reply_stream(
        history=history,
        user_text=user_text,
        author_name=author_name,
        media=media,
        memory_notes=memory_notes,
    ):
        if chunk == "__ELENA_AI_ERROR__":
            had_error = True
            break
        buffer += chunk
        if len(buffer) - last_edit_len >= EDIT_THROTTLE_CHARS:
            try:
                await placeholder.edit_text(buffer)
                last_edit_len = len(buffer)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramBadRequest:
                pass  # متن تغییری نکرده یا خطای بی‌اهمیت

    if had_error or not buffer.strip():
        try:
            await placeholder.edit_text("یه مشکلی پیش اومد. یه‌بار دیگه امتحان کن.")
        except TelegramBadRequest:
            pass
        return

    final_html = markdown_to_telegram_html(buffer)
    try:
        await placeholder.edit_text(final_html, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        try:
            await placeholder.edit_text(buffer)
        except TelegramBadRequest:
            pass

    await db.add_turn(chat_id, "user", user_text, author_name=author_name)
    await db.add_turn(chat_id, "model", buffer)

    if message.chat.type != "private":
        await db.mark_ambient_reply(chat_id)


def register_handlers(dp: Dispatcher) -> None:

    @dp.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        await db.upsert_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
        name = message.from_user.first_name
        text = (
            f"سلام، {name}\n\n"
            "من النا - Elena هستم. می‌تونی اینجا بهم پیام بدی یا من رو به یک گروه اضافه کنی.\n\n"
            "من پیام‌های صوتی، عکس‌ها و فایل‌های PDF رو می‌فهمم. درباره هر چیزی که می‌خوای بنویس."
        )
        await message.answer(text, reply_markup=main_menu_kb())

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        used_today, used_4h = await db.get_usage(message.chat.id, message.from_user.id)
        text = (
            "این چت:\n"
            f"- امروز: {used_today} از {75}\n"
            f"- این ۴ ساعت اخیر: {used_4h} از {30}"
        )
        await message.answer(text)

    @dp.message(Command("premium"))
    async def cmd_premium(message: Message) -> None:
        await message.answer("فعلاً فروش نسخه‌ی Premium در دسترس نیست.")

    @dp.message(Command("forget"))
    async def cmd_forget(message: Message) -> None:
        await db.forget_user(message.from_user.id)
        await message.answer("هر چی ازت یادم بود پاک شد.")

    @dp.callback_query(F.data == "new_chat")
    async def cb_new_chat(call: CallbackQuery) -> None:
        await db.pool.execute("DELETE FROM conversation WHERE chat_id=$1", call.message.chat.id)
        await call.answer("گفتگوی جدید شروع شد.")

    @dp.callback_query(F.data == "stats")
    async def cb_stats(call: CallbackQuery) -> None:
        used_today, used_4h = await db.get_usage(call.message.chat.id, call.from_user.id)
        await call.message.answer(f"این چت:\n- امروز: {used_today} از 75\n- این ۴ ساعت اخیر: {used_4h} از 30")
        await call.answer()

    @dp.callback_query(F.data == "premium")
    async def cb_premium(call: CallbackQuery) -> None:
        await call.message.answer("فعلاً فروش نسخه‌ی Premium در دسترس نیست.")
        await call.answer()

    @dp.callback_query(F.data == "clear_memory")
    async def cb_clear_memory(call: CallbackQuery) -> None:
        await db.forget_user(call.from_user.id)
        await call.answer("حافظه پاک شد.")

    @dp.message(F.voice)
    async def on_voice(message: Message) -> None:
        if message.chat.type != "private" and not is_direct_mode(message):
            return
        data = await download_bytes(message.bot, message.voice.file_id)
        await stream_reply(message, message.caption or "به این پیام صوتی پاسخ بده.", media=[(data, "audio/ogg")])

    @dp.message(F.photo)
    async def on_photo(message: Message) -> None:
        if message.chat.type != "private" and not is_direct_mode(message):
            return
        data = await download_bytes(message.bot, message.photo[-1].file_id)
        await stream_reply(message, message.caption or "این عکس رو ببین و نظرت رو بگو.", media=[(data, "image/jpeg")])

    @dp.message(F.document)
    async def on_document(message: Message) -> None:
        if message.chat.type != "private" and not is_direct_mode(message):
            return
        doc = message.document
        if doc.mime_type != "application/pdf":
            if message.chat.type == "private":
                await message.answer("فعلاً فقط PDF رو می‌فهمم، نه این نوع فایل رو.")
            return
        data = await download_bytes(message.bot, doc.file_id)
        await stream_reply(
            message, message.caption or "این PDF رو بخون و خلاصه‌اش کن.", media=[(data, "application/pdf")]
        )

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        text = message.text
        chat_type = message.chat.type

        if chat_type == "private":
            await stream_reply(message, text)
            return

        direct = is_direct_mode(message)
        if direct:
            await stream_reply(message, text)
            return

        # Ambient Mode: فقط پیام را برای Context ذخیره کن، بدون فراخوانی AI
        await db.add_turn(message.chat.id, "user", text, author_name=message.from_user.first_name)

        if not is_ambient_worthy(text):
            return
        if not await db.ambient_cooldown_ok(message.chat.id):
            return
        if random.random() > AMBIENT_PROBABILITY:
            return

        # این پیام از دید کاربر «معمولی» است، نه خطاب به النا؛ آخرین turn ای که
        # همین الان ذخیره کردیم را حذف می‌کنیم چون stream_reply خودش دوباره ثبتش می‌کند
        await db.pool.execute(
            """
            DELETE FROM conversation WHERE id = (
                SELECT id FROM conversation WHERE chat_id=$1 ORDER BY created_at DESC LIMIT 1
            )
            """,
            message.chat.id,
        )
        await stream_reply(message, text)


async def main() -> None:
    global db, bot_id, bot_username

    bot_token = os.environ["BOT_TOKEN"]
    database_url = os.environ["DATABASE_URL"]

    ai.init_client()
    db = Database(database_url)
    await db.connect()

    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=None))
    me = await bot.get_me()
    bot_id, bot_username = me.id, (me.username or "")

    await bot.set_my_commands(
        [
            {"command": "start", "description": "شروع کار با Elena"},
            {"command": "stats", "description": "وضعیت مصرف"},
            {"command": "premium", "description": "وضعیت Premium"},
            {"command": "forget", "description": "پاک کردن حافظه‌ی من"},
        ]
    )

    dp = Dispatcher()
    register_handlers(dp)

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
