import asyncio
import logging
import os
import random
import string
from datetime import datetime
from math import ceil

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

# Пользователь -> ждём файл
waiting_for_file = set()

# Пользователь -> ждём код поиска
searching_users = set()

# Пользователь -> сообщения текущей страницы
# Нужно для удаления предыдущей страницы перед показом новой
page_messages = {}


# ============================================================
# КОНСТАНТЫ
# ============================================================

FILES_PER_PAGE = 10


CATEGORY_NAMES = {
    "photo": "🖼️ Изображения",
    "video": "🎥 Видео",
    "gif": "🎞️ GIF",
    "audio": "🎵 Аудио",
    "voice": "🎤 Голосовые",
    "document": "📄 Документы",
    "archive": "📦 Архивы",
}


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

        # Для старой базы
        await conn.execute("""
            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS file_type TEXT
        """)

        await conn.execute("""
            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS file_unique_id TEXT
        """)

        await conn.execute("""
            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS file_name TEXT
        """)

        await conn.execute("""
            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS file_size BIGINT
        """)

        await conn.execute("""
            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS mime_type TEXT
        """)

        await conn.execute("""
            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS downloads INTEGER DEFAULT 0
        """)

        await conn.execute("""
            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_user_id
            ON files(user_id)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_code
            ON files(code)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_type
            ON files(user_id, file_type)
        """)

    logger.info("Database initialized")


# ============================================================
# CODE
# ============================================================

async def generate_code():

    while True:

        letters = "".join(
            random.choices(string.ascii_uppercase, k=5)
        )

        numbers = "".join(
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

def escape_html(text):

    if not text:
        return ""

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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


def get_icon(file_type):

    return {
        "photo": "🖼️",
        "video": "🎥",
        "gif": "🎞️",
        "audio": "🎵",
        "voice": "🎤",
        "document": "📄",
        "archive": "📦",
    }.get(file_type, "📁")


def normalize_file_type(file_type, file_name=None):

    if file_type == "animation":
        return "gif"

    if file_type in CATEGORY_NAMES:
        return file_type

    name = (file_name or "").lower()

    archive_extensions = (
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".tar.gz",
        ".tar.xz",
        ".tar.bz2",
    )

    if name.endswith(archive_extensions):
        return "archive"

    return "document"


def get_file_data(message: Message):

    # --------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------

    if message.photo:

        photo = message.photo[-1]

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

        return {
            "file_type": "photo",
            "file_id": photo.file_id,
            "file_unique_id": photo.file_unique_id,
            "file_name": f"photo_{timestamp}.jpg",
            "file_size": None,
            "mime_type": "image/jpeg",
        }

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    if message.video:

        video = message.video

        return {
            "file_type": "video",
            "file_id": video.file_id,
            "file_unique_id": video.file_unique_id,
            "file_name": video.file_name or "video.mp4",
            "file_size": video.file_size,
            "mime_type": video.mime_type or "video/mp4",
        }

    # --------------------------------------------------------
    # GIF / ANIMATION
    # --------------------------------------------------------

    if message.animation:

        animation = message.animation

        return {
            "file_type": "gif",
            "file_id": animation.file_id,
            "file_unique_id": animation.file_unique_id,
            "file_name": animation.file_name or "animation.gif",
            "file_size": animation.file_size,
            "mime_type": animation.mime_type or "image/gif",
        }

    # --------------------------------------------------------
    # AUDIO
    # --------------------------------------------------------

    if message.audio:

        audio = message.audio

        return {
            "file_type": "audio",
            "file_id": audio.file_id,
            "file_unique_id": audio.file_unique_id,
            "file_name": audio.file_name or "audio.mp3",
            "file_size": audio.file_size,
            "mime_type": audio.mime_type or "audio/mpeg",
        }

    # --------------------------------------------------------
    # VOICE
    # --------------------------------------------------------

    if message.voice:

        voice = message.voice

        return {
            "file_type": "voice",
            "file_id": voice.file_id,
            "file_unique_id": voice.file_unique_id,
            "file_name": "voice.ogg",
            "file_size": voice.file_size,
            "mime_type": "audio/ogg",
        }

    # --------------------------------------------------------
    # DOCUMENT
    # --------------------------------------------------------

    if message.document:

        document = message.document

        file_name = document.file_name or "document"

        file_type = normalize_file_type(
            "document",
            file_name
        )

        return {
            "file_type": file_type,
            "file_id": document.file_id,
            "file_unique_id": document.file_unique_id,
            "file_name": file_name,
            "file_size": document.file_size,
            "mime_type": document.mime_type,
        }

    return None


# ============================================================
# MAIN MENU
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


def home_keyboard():

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


# ============================================================
# START
# ============================================================

async def cmd_start(message: Message):

    text = (
        "📁 <b>Файловый Менеджер</b>\n\n"
        "Добро пожаловать!\n\n"
        "Я могу сохранять практически любые файлы:\n\n"
        "🖼️ Изображения\n"
        "🎥 Видео\n"
        "🎞️ GIF\n"
        "🎵 Аудио\n"
        "🎤 Голосовые\n"
        "📄 Документы\n"
        "📦 Архивы\n\n"
        "Выбери нужное действие:"
    )

    await message.answer(
        text,
        reply_markup=main_menu()
    )


# ============================================================
# HOME
# ============================================================

async def show_home(callback: CallbackQuery):

    waiting_for_file.discard(callback.from_user.id)
    searching_users.discard(callback.from_user.id)

    await callback.message.edit_text(
        "📁 <b>Файловый Менеджер</b>\n\n"
        "Выбери действие:",
        reply_markup=main_menu()
    )

    await callback.answer()


# ============================================================
# UPLOAD
# ============================================================

async def upload_start(callback: CallbackQuery):

    user_id = callback.from_user.id

    waiting_for_file.add(user_id)

    await callback.message.edit_text(
        "📤 <b>Загрузка файла</b>\n\n"
        "Отправь мне файл.\n\n"
        "Поддерживаются:\n"
        "🖼️ Фото\n"
        "🎥 Видео\n"
        "🎞️ GIF\n"
        "🎵 Аудио\n"
        "🎤 Голосовые\n"
        "📄 Документы\n"
        "📦 Архивы",
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

    data = get_file_data(message)

    if not data:
        return False

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
            VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8
            )
            """,
            code,
            user_id,
            data["file_id"],
            data["file_unique_id"],
            data["file_name"],
            data["file_size"],
            data["mime_type"],
            data["file_type"],
        )

    waiting_for_file.discard(user_id)

    icon = get_icon(data["file_type"])

    text = (
        f"{icon} <b>Файл сохранён!</b>\n\n"
        f"📄 <b>Имя:</b> "
        f"{escape_html(data['file_name'])}\n"
        f"📦 <b>Размер:</b> "
        f"{format_size(data['file_size'])}\n"
        f"🔑 <b>Код:</b> <code>{code}</code>\n\n"
        "Файл теперь находится в разделе "
        f"{CATEGORY_NAMES[data['file_type']]}."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Получить",
                    callback_data=f"get:{code}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📂 Мои файлы",
                    callback_data="myfiles"
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

    await message.answer(
        text,
        reply_markup=keyboard
    )

    logger.info(
        "File saved: user=%s code=%s type=%s name=%s",
        user_id,
        code,
        data["file_type"],
        data["file_name"]
    )

    return True


# ============================================================
# FILE HANDLER
# ============================================================

async def handle_file(message: Message):

    user_id = message.from_user.id

    if user_id not in waiting_for_file:

        await message.answer(
            "📁 Я получил файл.\n\n"
            "Чтобы сохранить его, нажми "
            "«📤 Загрузить файл».",
            reply_markup=main_menu()
        )

        return

    await save_file(message)


# ============================================================
# CATEGORY MENU
# ============================================================

async def my_files(callback: CallbackQuery):

    user_id = callback.from_user.id

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT file_type, COUNT(*) AS count
            FROM files
            WHERE user_id = $1
            GROUP BY file_type
            """,
            user_id
        )

    counts = {}

    for row in rows:

        normalized = normalize_file_type(
            row["file_type"]
        )

        counts[normalized] = counts.get(
            normalized,
            0
        ) + int(row["count"])

    buttons = []

    for file_type in (
        "photo",
        "video",
        "gif",
        "audio",
        "voice",
        "document",
        "archive",
    ):

        count = counts.get(file_type, 0)

        buttons.append([
            InlineKeyboardButton(
                text=f"{CATEGORY_NAMES[file_type]} — {count}",
                callback_data=f"category:{file_type}:1"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="home"
        )
    ])

    await callback.message.edit_text(
        "📂 <b>Мои файлы</b>\n\n"
        "Выбери категорию:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )

    await callback.answer()


# ============================================================
# DELETE OLD PAGE
# ============================================================

async def clear_page_messages(bot: Bot, user_id: int):

    ids = page_messages.get(user_id, [])

    if not ids:
        return

    for message_id in ids:

        try:

            await bot.delete_message(
                chat_id=user_id,
                message_id=message_id
            )

        except Exception:
            pass

    page_messages[user_id] = []


# ============================================================
# CATEGORY PAGE
# ============================================================

async def show_category_page(
    callback: CallbackQuery,
    file_type: str,
    page: int
):

    user_id = callback.from_user.id

    # Сначала отвечаем на callback
    await callback.answer()

    # Удаляем старую страницу
    await clear_page_messages(
        callback.bot,
        user_id
    )

    offset = (page - 1) * FILES_PER_PAGE

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT
                id,
                code,
                file_id,
                file_name,
                file_size,
                mime_type,
                file_type,
                downloads,
                created_at
            FROM files
            WHERE user_id = $1
            AND file_type = $2
            ORDER BY created_at DESC
            LIMIT $3
            OFFSET $4
            """,
            user_id,
            file_type,
            FILES_PER_PAGE,
            offset
        )

        total = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM files
            WHERE user_id = $1
            AND file_type = $2
            """,
            user_id,
            file_type
        )

    total = int(total or 0)

    pages = max(
        1,
        ceil(total / FILES_PER_PAGE)
    )

    if page < 1:
        page = 1

    if page > pages:
        page = pages

    if not rows:

        sent = await callback.bot.send_message(
            user_id,
            f"{CATEGORY_NAMES[file_type]}\n\n"
            "📭 Здесь пока нет файлов.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ Назад",
                            callback_data="myfiles"
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

        page_messages[user_id] = [sent.message_id]

        return

    sent_ids = []

    # --------------------------------------------------------
    # ОТПРАВЛЯЕМ ФАЙЛЫ
    # --------------------------------------------------------

    for row in rows:

        real_type = normalize_file_type(
            row["file_type"],
            row["file_name"]
        )

        icon = get_icon(real_type)

        name = row["file_name"] or "Без имени"

        if len(name) > 80:
            name = name[:77] + "..."

        created = row["created_at"]

        if isinstance(created, datetime):
            created = created.strftime(
                "%d.%m.%Y %H:%M"
            )

        caption = (
            f"{icon} <b>{escape_html(name)}</b>\n\n"
            f"📦 Размер: {format_size(row['file_size'])}\n"
            f"🔑 Код: <code>{row['code']}</code>\n"
            f"📥 Скачиваний: {row['downloads']}\n"
            f"📅 {created}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📥 Получить",
                        callback_data=f"get:{row['code']}"
                    ),
                    InlineKeyboardButton(
                        text="ℹ️ Инфо",
                        callback_data=f"info:{row['code']}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑️ Удалить",
                        callback_data=f"delete:{row['code']}"
                    )
                ]
            ]
        )

        try:

            if real_type == "photo":

                sent = await callback.bot.send_photo(
                    user_id,
                    row["file_id"],
                    caption=caption,
                    reply_markup=keyboard
                )

            elif real_type == "video":

                sent = await callback.bot.send_video(
                    user_id,
                    row["file_id"],
                    caption=caption,
                    reply_markup=keyboard
                )

            elif real_type == "gif":

                sent = await callback.bot.send_animation(
                    user_id,
                    row["file_id"],
                    caption=caption,
                    reply_markup=keyboard
                )

            elif real_type == "audio":

                sent = await callback.bot.send_audio(
                    user_id,
                    row["file_id"],
                    caption=caption,
                    reply_markup=keyboard
                )

            elif real_type == "voice":

                sent = await callback.bot.send_voice(
                    user_id,
                    row["file_id"],
                    reply_markup=keyboard
                )

            else:

                sent = await callback.bot.send_document(
                    user_id,
                    row["file_id"],
                    caption=caption,
                    reply_markup=keyboard
                )

            sent_ids.append(sent.message_id)

        except Exception as e:

            logger.exception(
                "Failed to display file %s",
                row["code"]
            )

            sent = await callback.bot.send_message(
                user_id,
                "⚠️ Не удалось показать файл\n\n"
                f"🔑 Код: <code>{row['code']}</code>\n"
                f"📄 {escape_html(name)}",
                reply_markup=keyboard
            )

            sent_ids.append(sent.message_id)

    # --------------------------------------------------------
    # НАВИГАЦИЯ
    # --------------------------------------------------------

    nav_buttons = []

    if page > 1:

        nav_buttons.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"category:{file_type}:{page - 1}"
            )
        )

    nav_buttons.append(
        InlineKeyboardButton(
            text=f"{page}/{pages}",
            callback_data="noop"
        )
    )

    if page < pages:

        nav_buttons.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"category:{file_type}:{page + 1}"
            )
        )

    nav_keyboard = [
        nav_buttons,
        [
            InlineKeyboardButton(
                text="📂 Категории",
                callback_data="myfiles"
            )
        ],
        [
            InlineKeyboardButton(
                text="🏠 Главное меню",
                callback_data="home"
            )
        ]
    ]

    nav = await callback.bot.send_message(
        user_id,
        f"{CATEGORY_NAMES[file_type]}\n\n"
        f"📄 Показано {offset + 1}–"
        f"{min(offset + len(rows), total)} "
        f"из {total}\n\n"
        f"Страница <b>{page}</b> из <b>{pages}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=nav_keyboard
        )
    )

    sent_ids.append(nav.message_id)

    page_messages[user_id] = sent_ids


# ============================================================
# NOOP
# ============================================================

async def noop(callback: CallbackQuery):

    await callback.answer()


# ============================================================
# GET FILE
# ============================================================

async def get_file(callback: CallbackQuery):

    code = callback.data.split(":", 1)[1]

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

        await callback.answer(
            "❌ Файл не найден",
            show_alert=True
        )

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

    file_type = normalize_file_type(
        row["file_type"],
        row["file_name"]
    )

    try:

        if file_type == "photo":

            await callback.bot.send_photo(
                callback.from_user.id,
                row["file_id"],
                caption=(
                    f"🖼️ <b>"
                    f"{escape_html(row['file_name'])}"
                    f"</b>\n"
                    f"🔑 <code>{row['code']}</code>"
                )
            )

        elif file_type == "video":

            await callback.bot.send_video(
                callback.from_user.id,
                row["file_id"],
                caption=(
                    f"🎥 <b>"
                    f"{escape_html(row['file_name'])}"
                    f"</b>\n"
                    f"🔑 <code>{row['code']}</code>"
                )
            )

        elif file_type == "gif":

            await callback.bot.send_animation(
                callback.from_user.id,
                row["file_id"],
                caption=(
                    f"🎞️ <b>"
                    f"{escape_html(row['file_name'])}"
                    f"</b>\n"
                    f"🔑 <code>{row['code']}</code>"
                )
            )

        elif file_type == "audio":

            await callback.bot.send_audio(
                callback.from_user.id,
                row["file_id"],
                caption=(
                    f"🎵 <b>"
                    f"{escape_html(row['file_name'])}"
                    f"</b>\n"
                    f"🔑 <code>{row['code']}</code>"
                )
            )

        elif file_type == "voice":

            await callback.bot.send_voice(
                callback.from_user.id,
                row["file_id"]
            )

        else:

            await callback.bot.send_document(
                callback.from_user.id,
                row["file_id"],
                caption=(
                    f"📄 <b>"
                    f"{escape_html(row['file_name'])}"
                    f"</b>\n"
                    f"🔑 <code>{row['code']}</code>"
                )
            )

        await callback.answer("✅ Файл отправлен")

    except Exception:

        logger.exception(
            "Failed to send file %s",
            code
        )

        await callback.answer(
            "❌ Не удалось отправить файл",
            show_alert=True
        )


# ============================================================
# INFO
# ============================================================

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

    real_type = normalize_file_type(
        row["file_type"],
        row["file_name"]
    )

    icon = get_icon(real_type)

    created = row["created_at"]

    if isinstance(created, datetime):

        created = created.strftime(
            "%d.%m.%Y %H:%M"
        )

    text = (
        f"{icon} <b>Информация о файле</b>\n\n"
        f"📄 <b>Имя:</b> "
        f"{escape_html(row['file_name'] or 'Без имени')}\n"
        f"📦 <b>Размер:</b> "
        f"{format_size(row['file_size'])}\n"
        f"🔧 <b>Тип:</b> "
        f"{escape_html(real_type)}\n"
        f"📝 <b>MIME:</b> "
        f"{escape_html(row['mime_type'] or 'Неизвестно')}\n"
        f"🔑 <b>Код:</b> "
        f"<code>{row['code']}</code>\n"
        f"📥 <b>Скачиваний:</b> "
        f"{row['downloads']}\n"
        f"📅 <b>Добавлен:</b> "
        f"{created}"
    )

    await callback.message.edit_caption(
        caption=text,
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
                        text="📂 Категория",
                        callback_data=f"backfile:{real_type}"
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

    await callback.message.edit_caption(
        caption=(
            "⚠️ <b>Удаление файла</b>\n\n"
            f"Ты действительно хочешь удалить "
            f"<code>{code}</code>?"
        ),
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
            SELECT user_id, file_name, file_type
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

    await callback.message.edit_caption(
        caption=(
            "🗑️ <b>Файл удалён</b>\n\n"
            f"<code>{code}</code> больше недоступен."
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📂 Мои файлы",
                        callback_data="myfiles"
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

    await callback.answer("Удалено")


# ============================================================
# SEARCH
# ============================================================

async def search_start(callback: CallbackQuery):

    searching_users.add(
        callback.from_user.id
    )

    await callback.message.edit_text(
        "🔎 <b>Поиск файла</b>\n\n"
        "Отправь код файла.\n\n"
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
            SELECT *
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

    await send_found_file(
        message,
        row
    )

    return True


async def send_found_file(
    message: Message,
    row
):

    file_type = normalize_file_type(
        row["file_type"],
        row["file_name"]
    )

    icon = get_icon(file_type)

    text = (
        f"{icon} <b>Файл найден</b>\n\n"
        f"📄 <b>Имя:</b> "
        f"{escape_html(row['file_name'])}\n"
        f"📦 <b>Размер:</b> "
        f"{format_size(row['file_size'])}\n"
        f"🔑 <b>Код:</b> "
        f"<code>{row['code']}</code>\n"
        f"📥 <b>Скачиваний:</b> "
        f"{row['downloads']}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Получить",
                    callback_data=f"get:{row['code']}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Удалить",
                    callback_data=f"delete:{row['code']}"
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

    await message.answer(
        text,
        reply_markup=keyboard
    )


# ============================================================
# TEXT
# ============================================================

async def handle_text(message: Message):

    user_id = message.from_user.id

    if user_id in searching_users:

        handled = await process_search(message)

        if handled:
            return

    if user_id in waiting_for_file:

        await message.answer(
            "⚠️ Отправь именно файл.\n\n"
            "Можно отправить фото, видео, GIF, "
            "аудио, голосовое, документ или архив."
        )

        return

    await message.answer(
        "Выбери действие в меню:",
        reply_markup=main_menu()
    )


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

    text = (
        "📊 <b>Статистика</b>\n\n"
        f"📁 Всего файлов: <b>{total}</b>\n"
        f"📦 Общий размер: "
        f"<b>{format_size(total_size)}</b>\n"
        f"📥 Скачиваний: <b>{downloads}</b>\n\n"
        "Разделы находятся в «📂 Мои файлы»."
    )

    await callback.message.edit_text(
        text,
        reply_markup=home_keyboard()
    )

    await callback.answer()


# ============================================================
# HELP
# ============================================================

async def help_page(callback: CallbackQuery):

    text = (
        "ℹ️ <b>Помощь</b>\n\n"
        "📤 <b>Загрузить файл</b>\n"
        "Сохраняет файл и выдаёт уникальный код.\n\n"
        "📂 <b>Мои файлы</b>\n"
        "Файлы разделены по категориям.\n\n"
        "🖼️ Фото, видео и GIF отображаются "
        "не только по названию, а прямо в Telegram.\n\n"
        "🔢 В каждой категории максимум "
        "<b>10 файлов на страницу</b>.\n\n"
        "🔎 <b>Найти файл</b>\n"
        "Позволяет получить файл по его коду."
    )

    await callback.message.edit_text(
        text,
        reply_markup=home_keyboard()
    )

    await callback.answer()


# ============================================================
# SETTINGS
# ============================================================

async def settings(callback: CallbackQuery):

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        "📁 Хранилище: PostgreSQL\n"
        "☁️ Файлы: Telegram File ID\n"
        "🔑 Коды: автоматически\n"
        "📄 Страница: до 10 файлов\n"
        "🛡️ Удаление: только владелец"
    )

    await callback.message.edit_text(
        text,
        reply_markup=home_keyboard()
    )

    await callback.answer()


# ============================================================
# COMMANDS
# ============================================================

async def cmd_myfiles(message: Message):

    user_id = message.from_user.id

    async with db_pool.acquire() as conn:

        total = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM files
            WHERE user_id = $1
            """,
            user_id
        )

    await message.answer(
        f"📂 У тебя сохранено файлов: <b>{total}</b>\n\n"
        "Открой категории:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📂 Открыть мои файлы",
                        callback_data="myfiles"
                    )
                ]
            ]
        )
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

        await message.answer(
            "❌ Файл не найден."
        )

        return

    await send_file_by_row(
        message.bot,
        message.chat.id,
        row
    )


async def send_file_by_row(
    bot: Bot,
    chat_id: int,
    row
):

    file_type = normalize_file_type(
        row["file_type"],
        row["file_name"]
    )

    if file_type == "photo":

        await bot.send_photo(
            chat_id,
            row["file_id"]
        )

    elif file_type == "video":

        await bot.send_video(
            chat_id,
            row["file_id"]
        )

    elif file_type == "gif":

        await bot.send_animation(
            chat_id,
            row["file_id"]
        )

    elif file_type == "audio":

        await bot.send_audio(
            chat_id,
            row["file_id"]
        )

    elif file_type == "voice":

        await bot.send_voice(
            chat_id,
            row["file_id"]
        )

    else:

        await bot.send_document(
            chat_id,
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
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code
        )

    if not row:

        await message.answer(
            "❌ Файл не найден."
        )

        return

    file_type = normalize_file_type(
        row["file_type"],
        row["file_name"]
    )

    await message.answer(
        f"{get_icon(file_type)} <b>Файл</b>\n\n"
        f"📄 {escape_html(row['file_name'])}\n"
        f"📦 {format_size(row['file_size'])}\n"
        f"🔧 {escape_html(file_type)}\n"
        f"🔑 <code>{row['code']}</code>\n"
        f"📥 Скачиваний: {row['downloads']}"
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

            await message.answer(
                "❌ Файл не найден."
            )

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
# HTTP SERVER — RENDER
# ============================================================

async def index(request):

    return web.json_response({
        "status": "online",
        "service": "Telegram File Manager"
    })


async def health(request):

    return web.json_response({
        "status": "ok",
        "service": "telegram-file-manager",
        "time": datetime.utcnow().isoformat()
    })


async def start_http_server():

    app = web.Application()

    app.router.add_get(
        "/",
        index
    )

    app.router.add_get(
        "/health",
        health
    )

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
# CALLBACKS
# ============================================================

async def callback_handler(callback: CallbackQuery):

    data = callback.data or ""

    # Главное меню
    if data == "home":

        await show_home(callback)
        return

    # Загрузка
    if data == "upload":

        await upload_start(callback)
        return

    # Мои файлы
    if data == "myfiles":

        await my_files(callback)
        return

    # Категория
    if data.startswith("category:"):

        parts = data.split(":")

        file_type = parts[1]
        page = int(parts[2])

        await show_category_page(
            callback,
            file_type,
            page
        )

        return

    # Неактивная кнопка страницы
    if data == "noop":

        await noop(callback)
        return

    # Поиск
    if data == "search":

        await search_start(callback)
        return

    # Статистика
    if data == "stats":

        await statistics(callback)
        return

    # Помощь
    if data == "help":

        await help_page(callback)
        return

    # Настройки
    if data == "settings":

        await settings(callback)
        return

    # Получение
    if data.startswith("get:"):

        await get_file(callback)
        return

    # Информация
    if data.startswith("info:"):

        await info_file(callback)
        return

    # Удаление
    if data.startswith("delete:"):

        await delete_start(callback)
        return

    # Подтверждение удаления
    if data.startswith("confirm_delete:"):

        await confirm_delete(callback)
        return

    # Вернуться в категорию
    if data.startswith("backfile:"):

        file_type = data.split(":", 1)[1]

        await callback.answer()

        await clear_page_messages(
            callback.bot,
            callback.from_user.id
        )

        # Показываем первую страницу через временный callback
        # напрямую
        class TempCallback:
            pass

        temp = TempCallback()
        temp.bot = callback.bot
        temp.from_user = callback.from_user

        await show_category_page(
            callback,
            file_type,
            1
        )

        return

    await callback.answer()


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "Starting Telegram File Manager..."
    )

    await init_db()

    await start_http_server()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    dp = Dispatcher()

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    dp.callback_query.register(
        callback_handler
    )

    # --------------------------------------------------------
    # FILES
    # --------------------------------------------------------

    dp.message.register(
        handle_file,
        F.photo
        | F.video
        | F.animation
        | F.audio
        | F.voice
        | F.document
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    dp.message.register(
        handle_text,
        F.text
    )

    logger.info(
        "Telegram bot started"
    )

    logger.info(
        "Start polling"
    )

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

        logger.info(
            "Bot stopped"
        )