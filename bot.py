import asyncio
import logging
import os
import random
import string
from datetime import datetime

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не найден")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# GLOBAL
# ============================================================

db_pool = None

# Пользователи, которые сейчас ожидают файл
waiting_for_file = set()

# Временное состояние поиска
searching_users = set()


# ============================================================
# DATABASE
# ============================================================

async def init_db():
    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5
    )

    async with db_pool.acquire() as conn:

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id BIGSERIAL PRIMARY KEY,
                code VARCHAR(20) UNIQUE NOT NULL,
                user_id BIGINT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT,
                file_name TEXT,
                file_size BIGINT,
                mime_type TEXT,
                file_type TEXT,
                downloads INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Если база была создана старой версией бота
        # добавляем новые колонки
        await conn.execute("""
            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS file_type TEXT
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_user_id
            ON files(user_id)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_code
            ON files(code)
        """)

    logger.info("Database initialized")


# ============================================================
# CODE GENERATOR
# ============================================================

async def generate_code():
    while True:

        letters = ''.join(
            random.choices(string.ascii_uppercase, k=5)
        )

        numbers = ''.join(
            random.choices(string.digits, k=5)
        )

        code = f"{letters}-{numbers}"

        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM files WHERE code = $1",
                code
            )

        if not exists:
            return code


# ============================================================
# HELPERS
# ============================================================

def format_size(size):
    if size is None:
        return "Неизвестно"

    size = float(size)

    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} PB"


def escape_html(text):
    if not text:
        return ""

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def get_file_icon(file_type):
    icons = {
        "photo": "🖼️",
        "video": "🎥",
        "audio": "🎵",
        "voice": "🎤",
        "document": "📄",
        "animation": "🎞️",
    }

    return icons.get(file_type, "📁")


def get_file_type(message: Message):
    """
    Определяет тип файла Telegram-сообщения.
    Возвращает:
        file_type
        file_id
        file_unique_id
        file_name
        file_size
        mime_type
    """

    # PHOTO
    if message.photo:
        photo = message.photo[-1]

        return (
            "photo",
            photo.file_id,
            photo.file_unique_id,
            "photo.jpg",
            None,
            "image/jpeg"
        )

    # VIDEO
    if message.video:
        video = message.video

        return (
            "video",
            video.file_id,
            video.file_unique_id,
            video.file_name or "video.mp4",
            video.file_size,
            video.mime_type or "video/mp4"
        )

    # AUDIO
    if message.audio:
        audio = message.audio

        return (
            "audio",
            audio.file_id,
            audio.file_unique_id,
            audio.file_name or "audio.mp3",
            audio.file_size,
            audio.mime_type or "audio/mpeg"
        )

    # VOICE
    if message.voice:
        voice = message.voice

        return (
            "voice",
            voice.file_id,
            voice.file_unique_id,
            "voice.ogg",
            voice.file_size,
            "audio/ogg"
        )

    # ANIMATION / GIF
    if message.animation:
        animation = message.animation

        return (
            "animation",
            animation.file_id,
            animation.file_unique_id,
            animation.file_name or "animation.gif",
            animation.file_size,
            animation.mime_type or "image/gif"
        )

    # DOCUMENT
    if message.document:
        document = message.document

        return (
            "document",
            document.file_id,
            document.file_unique_id,
            document.file_name or "document",
            document.file_size,
            document.mime_type
        )

    return None


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Загрузить файл",
                    callback_data="upload"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📂 Мои файлы",
                    callback_data="myfiles"
                ),
                InlineKeyboardButton(
                    text="🔎 Найти файл",
                    callback_data="search"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="stats"
                )
            ],
            [
                InlineKeyboardButton(
                    text="ℹ️ Помощь",
                    callback_data="help"
                ),
                InlineKeyboardButton(
                    text="⚙️ Настройки",
                    callback_data="settings"
                )
            ]
        ]
    )


def back_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="home"
                )
            ]
        ]
    )


def file_actions(code):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Получить файл",
                    callback_data=f"get:{code}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Удалить",
                    callback_data=f"delete:{code}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="home"
                )
            ]
        ]
    )


# ============================================================
# START
# ============================================================

async def cmd_start(message: Message):

    text = (
        "📁 <b>Файловый Менеджер</b>\n\n"
        "Добро пожаловать!\n\n"
        "Я могу сохранять практически любые файлы "
        "и выдавать каждому уникальный код.\n\n"
        "🖼️ Фото\n"
        "🎥 Видео\n"
        "🎵 Аудио\n"
        "🎤 Голосовые\n"
        "🎞️ GIF\n"
        "📄 Документы\n"
        "📦 Архивы\n"
        "и многое другое.\n\n"
        "Выберите действие:"
    )

    await message.answer(
        text,
        reply_markup=main_menu()
    )


# ============================================================
# HOME
# ============================================================

async def show_home(target):

    text = (
        "📁 <b>Файловый Менеджер</b>\n\n"
        "Выберите действие:"
    )

    if isinstance(target, CallbackQuery):

        await target.message.edit_text(
            text,
            reply_markup=main_menu()
        )

        await target.answer()

    else:

        await target.answer(
            text,
            reply_markup=main_menu()
        )


# ============================================================
# UPLOAD
# ============================================================

async def upload_start(callback: CallbackQuery):

    user_id = callback.from_user.id

    waiting_for_file.add(user_id)

    text = (
        "📤 <b>Загрузка файла</b>\n\n"
        "Отправь мне файл.\n\n"
        "Поддерживаются:\n"
        "🖼️ Фото\n"
        "🎥 Видео\n"
        "🎵 Аудио\n"
        "🎤 Голосовые\n"
        "🎞️ GIF\n"
        "📄 Документы\n"
        "📦 Архивы\n\n"
        "После получения я сохраню его и выдам уникальный код."
    )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data="home"
                    )
                ]
            ]
        )
    )

    await callback.answer()


# ============================================================
# SAVE FILE
# ============================================================

async def save_file(message: Message):

    user_id = message.from_user.id

    file_data = get_file_type(message)

    if not file_data:
        return False

    (
        file_type,
        file_id,
        file_unique_id,
        file_name,
        file_size,
        mime_type
    ) = file_data

    code = await generate_code()

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            INSERT INTO files (
                code,
                user_id,
                file_id,
                file_unique_id,
                file_name,
                file_size,
                mime_type,
                file_type
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            code,
            user_id,
            file_id,
            file_unique_id,
            file_name,
            file_size,
            mime_type,
            file_type
        )

    waiting_for_file.discard(user_id)

    icon = get_file_icon(file_type)

    text = (
        "✅ <b>Файл сохранён!</b>\n\n"
        f"{icon} <b>Тип:</b> {escape_html(file_type)}\n"
        f"📄 <b>Имя:</b> {escape_html(file_name)}\n"
        f"📦 <b>Размер:</b> {format_size(file_size)}\n"
        f"🔑 <b>Код:</b> <code>{code}</code>\n\n"
        "Сохрани этот код — по нему можно получить файл."
    )

    await message.answer(
        text,
        reply_markup=file_actions(code)
    )

    logger.info(
        "File saved: user=%s code=%s type=%s name=%s",
        user_id,
        code,
        file_type,
        file_name
    )

    return True


# ============================================================
# FILE MESSAGE HANDLER
# ============================================================

async def handle_file(message: Message):

    user_id = message.from_user.id

    # Если пользователь специально находится в режиме загрузки
    # или прислал поддерживаемый файл — сохраняем его.
    file_data = get_file_type(message)

    if not file_data:
        return

    if user_id not in waiting_for_file:
        # Если файл отправлен просто так,
        # тоже предлагаем сохранить его.
        await message.answer(
            "📁 Я получил файл.\n\n"
            "Чтобы сохранить его, нажми "
            "«📤 Загрузить файл», затем отправь файл ещё раз.",
            reply_markup=main_menu()
        )
        return

    await save_file(message)


# ============================================================
# MY FILES
# ============================================================

async def my_files(callback: CallbackQuery):

    user_id = callback.from_user.id

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT
                code,
                file_name,
                file_size,
                file_type,
                downloads,
                created_at
            FROM files
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 30
            """,
            user_id
        )

    if not rows:

        await callback.message.edit_text(
            "📂 <b>Мои файлы</b>\n\n"
            "У тебя пока нет сохранённых файлов.",
            reply_markup=back_menu()
        )

        await callback.answer()
        return

    text = "📂 <b>Мои файлы</b>\n\n"

    buttons = []

    for row in rows:

        icon = get_file_icon(row["file_type"])

        name = row["file_name"] or "Без имени"

        if len(name) > 25:
            name = name[:22] + "..."

        text += (
            f"{icon} <b>{escape_html(name)}</b>\n"
            f"🔑 <code>{row['code']}</code>  "
            f"📦 {format_size(row['file_size'])}\n\n"
        )

        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {name}",
                callback_data=f"get:{row['code']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="home"
        )
    ])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )

    await callback.answer()


# ============================================================
# SEARCH
# ============================================================

async def search_start(callback: CallbackQuery):

    searching_users.add(callback.from_user.id)

    await callback.message.edit_text(
        "🔎 <b>Поиск файла</b>\n\n"
        "Отправь мне код файла.\n\n"
        "Например:\n"
        "<code>ABCDE-12345</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data="home"
                    )
                ]
            ]
        )
    )

    await callback.answer()


async def process_search(message: Message):

    user_id = message.from_user.id

    if user_id not in searching_users:
        return False

    searching_users.discard(user_id)

    code = message.text.strip().upper()

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT
                code,
                user_id,
                file_id,
                file_name,
                file_size,
                mime_type,
                file_type,
                downloads,
                created_at
            FROM files
            WHERE code = $1
            """,
            code
        )

    if not row:

        await message.answer(
            "❌ <b>Файл не найден</b>\n\n"
            "Проверь код и попробуй ещё раз.",
            reply_markup=main_menu()
        )

        return True

    await send_file_info(
        message,
        row
    )

    return True


# ============================================================
# GET FILE
# ============================================================

async def get_file(callback: CallbackQuery):

    code = callback.data.split(":", 1)[1]

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT
                id,
                code,
                user_id,
                file_id,
                file_name,
                file_size,
                mime_type,
                file_type,
                downloads
            FROM files
            WHERE code = $1
            """,
            code
        )

    if not row:

        await callback.answer(
            "Файл не найден",
            show_alert=True
        )

        return

    # Увеличиваем счётчик скачиваний
    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            UPDATE files
            SET downloads = downloads + 1
            WHERE code = $1
            """,
            code
        )

    file_type = row["file_type"]

    try:

        if file_type == "photo":

            await callback.message.answer_photo(
                row["file_id"],
                caption=(
                    f"🖼️ <b>{escape_html(row['file_name'])}</b>\n"
                    f"🔑 Код: <code>{row['code']}</code>"
                )
            )

        elif file_type == "video":

            await callback.message.answer_video(
                row["file_id"],
                caption=(
                    f"🎥 <b>{escape_html(row['file_name'])}</b>\n"
                    f"🔑 Код: <code>{row['code']}</code>"
                )
            )

        elif file_type == "audio":

            await callback.message.answer_audio(
                row["file_id"],
                caption=(
                    f"🎵 <b>{escape_html(row['file_name'])}</b>\n"
                    f"🔑 Код: <code>{row['code']}</code>"
                )
            )

        elif file_type == "voice":

            await callback.message.answer_voice(
                row["file_id"]
            )

        elif file_type == "animation":

            await callback.message.answer_animation(
                row["file_id"],
                caption=(
                    f"🎞️ <b>{escape_html(row['file_name'])}</b>\n"
                    f"🔑 Код: <code>{row['code']}</code>"
                )
            )

        else:

            await callback.message.answer_document(
                row["file_id"],
                caption=(
                    f"📄 <b>{escape_html(row['file_name'])}</b>\n"
                    f"🔑 Код: <code>{row['code']}</code>"
                )
            )

        await callback.answer("✅ Файл отправлен")

    except Exception as e:

        logger.exception("Failed to send file")

        await callback.answer(
            "❌ Не удалось отправить файл",
            show_alert=True
        )


# ============================================================
# FILE INFO
# ============================================================

async def send_file_info(message: Message, row):

    icon = get_file_icon(row["file_type"])

    text = (
        f"{icon} <b>Информация о файле</b>\n\n"
        f"📄 <b>Имя:</b> "
        f"{escape_html(row['file_name'] or 'Без имени')}\n"
        f"📦 <b>Размер:</b> {format_size(row['file_size'])}\n"
        f"🔧 <b>Тип:</b> {escape_html(row['file_type'])}\n"
        f"🔑 <b>Код:</b> <code>{row['code']}</code>\n"
        f"📥 <b>Скачиваний:</b> {row['downloads']}\n\n"
        "Нажми кнопку ниже, чтобы получить файл."
    )

    await message.answer(
        text,
        reply_markup=file_actions(row["code"])
    )


async def info_file(callback: CallbackQuery):

    code = callback.data.split(":", 1)[1]

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT
                code,
                file_name,
                file_size,
                mime_type,
                file_type,
                downloads,
                created_at
            FROM files
            WHERE code = $1
            """,
            code
        )

    if not row:

        await callback.answer(
            "Файл не найден",
            show_alert=True
        )

        return

    icon = get_file_icon(row["file_type"])

    created = row["created_at"]

    if isinstance(created, datetime):
        created = created.strftime("%d.%m.%Y %H:%M")

    text = (
        f"{icon} <b>Информация о файле</b>\n\n"
        f"📄 <b>Имя:</b> "
        f"{escape_html(row['file_name'] or 'Без имени')}\n"
        f"📦 <b>Размер:</b> {format_size(row['file_size'])}\n"
        f"🔧 <b>Тип:</b> {escape_html(row['file_type'])}\n"
        f"📝 <b>MIME:</b> "
        f"{escape_html(row['mime_type'] or 'Неизвестно')}\n"
        f"🔑 <b>Код:</b> <code>{row['code']}</code>\n"
        f"📥 <b>Скачиваний:</b> {row['downloads']}\n"
        f"📅 <b>Дата:</b> {created}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📥 Получить",
                        callback_data=f"get:{code}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑️ Удалить",
                        callback_data=f"delete:{code}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🏠 Главное меню",
                        callback_data="home"
                    )
                ]
            ]
        )
    )

    await callback.answer()


# ============================================================
# DELETE
# ============================================================

async def delete_start(callback: CallbackQuery):

    code = callback.data.split(":", 1)[1]

    await callback.message.edit_text(
        "⚠️ <b>Удаление файла</b>\n\n"
        f"Ты действительно хочешь удалить файл "
        f"<code>{code}</code>?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да, удалить",
                        callback_data=f"confirm_delete:{code}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"get:{code}"
                    )
                ]
            ]
        )
    )

    await callback.answer()


async def confirm_delete(callback: CallbackQuery):

    code = callback.data.split(":", 1)[1]

    user_id = callback.from_user.id

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT user_id, file_name
            FROM files
            WHERE code = $1
            """,
            code
        )

        if not row:

            await callback.answer(
                "Файл уже удалён",
                show_alert=True
            )

            return

        # Только владелец может удалить файл
        if row["user_id"] != user_id:

            await callback.answer(
                "❌ Это не твой файл",
                show_alert=True
            )

            return

        await conn.execute(
            """
            DELETE FROM files
            WHERE code = $1
            """,
            code
        )

    await callback.message.edit_text(
        "🗑️ <b>Файл удалён</b>\n\n"
        f"Файл <code>{code}</code> больше недоступен.",
        reply_markup=main_menu()
    )

    await callback.answer("Удалено")


# ============================================================
# STATISTICS
# ============================================================

async def statistics(callback: CallbackQuery):

    user_id = callback.from_user.id

    async with db_pool.acquire() as conn:

        total = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM files
            WHERE user_id = $1
            """,
            user_id
        )

        downloads = await conn.fetchval(
            """
            SELECT COALESCE(SUM(downloads), 0)
            FROM files
            WHERE user_id = $1
            """,
            user_id
        )

        total_size = await conn.fetchval(
            """
            SELECT COALESCE(SUM(file_size), 0)
            FROM files
            WHERE user_id = $1
            """,
            user_id
        )

        photos = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM files
            WHERE user_id = $1
            AND file_type = 'photo'
            """,
            user_id
        )

        videos = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM files
            WHERE user_id = $1
            AND file_type = 'video'
            """,
            user_id
        )

        documents = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM files
            WHERE user_id = $1
            AND file_type = 'document'
            """,
            user_id
        )

    text = (
        "📊 <b>Статистика</b>\n\n"
        f"📁 Всего файлов: <b>{total}</b>\n"
        f"🖼️ Фото: <b>{photos}</b>\n"
        f"🎥 Видео: <b>{videos}</b>\n"
        f"📄 Документы: <b>{documents}</b>\n"
        f"📦 Общий размер: <b>{format_size(total_size)}</b>\n"
        f"📥 Скачиваний: <b>{downloads}</b>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_menu()
    )

    await callback.answer()


# ============================================================
# HELP
# ============================================================

async def help_page(callback: CallbackQuery):

    text = (
        "ℹ️ <b>Помощь</b>\n\n"
        "📤 <b>Загрузить файл</b>\n"
        "Отправляет файл в хранилище и выдаёт уникальный код.\n\n"
        "🔎 <b>Найти файл</b>\n"
        "Введи код, чтобы найти сохранённый файл.\n\n"
        "📂 <b>Мои файлы</b>\n"
        "Показывает твои сохранённые файлы.\n\n"
        "📊 <b>Статистика</b>\n"
        "Показывает количество файлов, их размер и скачивания.\n\n"
        "Поддерживаются фото, видео, аудио, голосовые, GIF, "
        "документы и архивы."
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_menu()
    )

    await callback.answer()


# ============================================================
# SETTINGS
# ============================================================

async def settings(callback: CallbackQuery):

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        "В этой версии основные параметры работают "
        "автоматически.\n\n"
        "📁 Хранилище: PostgreSQL\n"
        "☁️ Файлы: Telegram File ID\n"
        "🔑 Коды: автоматически генерируются\n"
        "🛡️ Доступ к удалению: только владелец"
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_menu()
    )

    await callback.answer()


# ============================================================
# TEXT HANDLER
# ============================================================

async def handle_text(message: Message):

    user_id = message.from_user.id

    # Поиск
    if user_id in searching_users:

        handled = await process_search(message)

        if handled:
            return

    # Если ждём файл
    if user_id in waiting_for_file:

        await message.answer(
            "⚠️ Пожалуйста, отправь именно файл.\n\n"
            "Можно отправить фото, видео, документ, "
            "аудио, голосовое или GIF."
        )

        return

    await message.answer(
        "Выбери действие в меню:",
        reply_markup=main_menu()
    )


# ============================================================
# COMMANDS
# ============================================================

async def cmd_myfiles(message: Message):

    fake_callback = None

    user_id = message.from_user.id

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT
                code,
                file_name,
                file_size,
                file_type
            FROM files
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 30
            """,
            user_id
        )

    if not rows:

        await message.answer(
            "📂 У тебя пока нет файлов.",
            reply_markup=main_menu()
        )

        return

    text = "📂 <b>Мои файлы</b>\n\n"

    for row in rows:

        icon = get_file_icon(row["file_type"])

        text += (
            f"{icon} "
            f"<b>{escape_html(row['file_name'] or 'Без имени')}</b>\n"
            f"🔑 <code>{row['code']}</code>\n"
            f"📦 {format_size(row['file_size'])}\n\n"
        )

    await message.answer(
        text,
        reply_markup=main_menu()
    )


async def cmd_get(message: Message):

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:

        await message.answer(
            "Использование:\n"
            "<code>/get ABCDE-12345</code>"
        )

        return

    code = parts[1].strip().upper()

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code
        )

    if not row:

        await message.answer("❌ Файл не найден.")
        return

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            UPDATE files
            SET downloads = downloads + 1
            WHERE code = $1
            """,
            code
        )

    file_type = row["file_type"]

    if file_type == "photo":

        await message.answer_photo(
            row["file_id"]
        )

    elif file_type == "video":

        await message.answer_video(
            row["file_id"]
        )

    elif file_type == "audio":

        await message.answer_audio(
            row["file_id"]
        )

    elif file_type == "voice":

        await message.answer_voice(
            row["file_id"]
        )

    elif file_type == "animation":

        await message.answer_animation(
            row["file_id"]
        )

    else:

        await message.answer_document(
            row["file_id"]
        )


async def cmd_info(message: Message):

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:

        await message.answer(
            "Использование:\n"
            "<code>/info ABCDE-12345</code>"
        )

        return

    code = parts[1].strip().upper()

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT
                code,
                file_name,
                file_size,
                mime_type,
                file_type,
                downloads,
                created_at
            FROM files
            WHERE code = $1
            """,
            code
        )

    if not row:

        await message.answer("❌ Файл не найден.")
        return

    icon = get_file_icon(row["file_type"])

    await message.answer(
        f"{icon} <b>Файл</b>\n\n"
        f"📄 {escape_html(row['file_name'])}\n"
        f"📦 {format_size(row['file_size'])}\n"
        f"🔧 {escape_html(row['file_type'])}\n"
        f"🔑 <code>{row['code']}</code>\n"
        f"📥 Скачиваний: {row['downloads']}",
        reply_markup=file_actions(row["code"])
    )


async def cmd_delete(message: Message):

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:

        await message.answer(
            "Использование:\n"
            "<code>/delete ABCDE-12345</code>"
        )

        return

    code = parts[1].strip().upper()

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT user_id
            FROM files
            WHERE code = $1
            """,
            code
        )

        if not row:

            await message.answer("❌ Файл не найден.")
            return

        if row["user_id"] != message.from_user.id:

            await message.answer(
                "❌ Ты не можешь удалить чужой файл."
            )

            return

        await conn.execute(
            """
            DELETE FROM files
            WHERE code = $1
            """,
            code
        )

    await message.answer(
        "🗑️ Файл удалён.",
        reply_markup=main_menu()
    )


# ============================================================
# HTTP SERVER FOR RENDER
# ============================================================

async def health(request):

    return web.json_response({
        "status": "ok",
        "service": "telegram-file-manager",
        "time": datetime.utcnow().isoformat()
    })


async def index(request):

    return web.json_response({
        "status": "online",
        "service": "Telegram File Manager"
    })


async def start_http_server():

    app = web.Application()

    app.router.add_get("/", index)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logger.info(
        "HTTP server started on port %s",
        PORT
    )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callback_handler(callback: CallbackQuery):

    data = callback.data or ""

    if data == "home":

        # Сбрасываем состояния
        waiting_for_file.discard(
            callback.from_user.id
        )

        searching_users.discard(
            callback.from_user.id
        )

        await show_home(callback)
        return

    if data == "upload":

        await upload_start(callback)
        return

    if data == "myfiles":

        await my_files(callback)
        return

    if data == "search":

        await search_start(callback)
        return

    if data == "stats":

        await statistics(callback)
        return

    if data == "help":

        await help_page(callback)
        return

    if data == "settings":

        await settings(callback)
        return

    if data.startswith("get:"):

        await get_file(callback)
        return

    if data.startswith("info:"):

        await info_file(callback)
        return

    if data.startswith("delete:"):

        await delete_start(callback)
        return

    if data.startswith("confirm_delete:"):

        await confirm_delete(callback)
        return

    await callback.answer()


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info("Starting Telegram File Manager...")

    await init_db()

    await start_http_server()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    dp = Dispatcher()

    # Commands
    dp.message.register(
        cmd_start,
        Command("start")
    )

    dp.message.register(
        cmd_myfiles,
        Command("myfiles")
    )

    dp.message.register(
        cmd_get,
        Command("get")
    )

    dp.message.register(
        cmd_info,
        Command("info")
    )

    dp.message.register(
        cmd_delete,
        Command("delete")
    )

    # Callbacks
    dp.callback_query.register(
        callback_handler
    )

    # Files
    dp.message.register(
        handle_file,
        F.photo
        | F.video
        | F.audio
        | F.voice
        | F.animation
        | F.document
    )

    # Text
    dp.message.register(
        handle_text,
        F.text
    )

    logger.info("Telegram bot started")
    logger.info("Start polling")

    try:

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )

    finally:

        await bot.session.close()

        if db_pool:
            await db_pool.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info("Bot stopped")