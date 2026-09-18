import asyncio
import html
import logging
import os
import secrets
import string
from datetime import datetime

import asyncpg
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_API_BASE_URL = os.getenv("BOT_API_BASE_URL")

PORT = int(os.getenv("PORT", "10000"))

# Telegram Bot API поддерживает большие файлы в зависимости
# от используемого API. Ограничение нашего менеджера:
MAX_FILE_SIZE = 2_000_000_000

FILES_PER_PAGE = 5


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

db_pool: asyncpg.Pool | None = None


async def init_db():
    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
    )

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id BIGSERIAL PRIMARY KEY,
                code VARCHAR(20) UNIQUE NOT NULL,
                user_id BIGINT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT,
                file_name TEXT,
                file_size BIGINT,
                mime_type TEXT,
                downloads INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_files_user_id
            ON files(user_id);
            """
        )

        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_files_code
            ON files(code);
            """
        )

    logger.info("Database initialized")


# ============================================================
# CODE GENERATOR
# ============================================================

ALPHABET = string.ascii_uppercase + string.digits


def generate_code() -> str:
    part1 = "".join(
        secrets.choice(ALPHABET)
        for _ in range(5)
    )

    part2 = "".join(
        secrets.choice(ALPHABET)
        for _ in range(5)
    )

    return f"{part1}-{part2}"


async def create_unique_code() -> str:
    while True:

        code = generate_code()

        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                """
                SELECT 1
                FROM files
                WHERE code = $1
                """,
                code,
            )

        if not exists:
            return code


# ============================================================
# HELPERS
# ============================================================

def format_size(size: int | None) -> str:

    if not size:
        return "Неизвестно"

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ]

    value = float(size)

    for unit in units:

        if value < 1024:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} PB"


def escape(value) -> str:
    return html.escape(str(value)) if value is not None else ""


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="📤 Загрузить файл",
                    callback_data="upload",
                )
            ],

            [
                InlineKeyboardButton(
                    text="📂 Мои файлы",
                    callback_data="files:0",
                ),
                InlineKeyboardButton(
                    text="🔎 Найти файл",
                    callback_data="search",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="stats",
                ),
                InlineKeyboardButton(
                    text="ℹ️ Помощь",
                    callback_data="help",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="⚙️ Настройки",
                    callback_data="settings",
                )
            ],
        ]
    )


def back_home_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="home",
                )
            ]
        ]
    )


def cancel_upload_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="home",
                )
            ]
        ]
    )


def file_keyboard(
    code: str,
    owner: bool = True,
) -> InlineKeyboardMarkup:

    buttons = [
        [
            InlineKeyboardButton(
                text="📥 Получить",
                callback_data=f"get:{code}",
            ),
            InlineKeyboardButton(
                text="ℹ️ Подробнее",
                callback_data=f"info:{code}",
            ),
        ]
    ]

    if owner:

        buttons.append(
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"delete:{code}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ К списку",
                callback_data="files:0",
            ),
            InlineKeyboardButton(
                text="🏠 Главная",
                callback_data="home",
            ),
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


# ============================================================
# MAIN MENU
# ============================================================

async def show_main_menu(message: Message):

    text = (
        "📁 <b>FILE MANAGER</b>\n\n"
        "Добро пожаловать в файловый менеджер.\n\n"
        "Здесь ты можешь безопасно хранить свои "
        "файлы и получать их по уникальному коду.\n\n"
        "Выбери нужное действие ниже 👇"
    )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def edit_to_main_menu(callback: CallbackQuery):

    text = (
        "📁 <b>FILE MANAGER</b>\n\n"
        "Главное меню\n\n"
        "Выбери нужное действие 👇"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


# ============================================================
# START
# ============================================================

dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):

    await show_main_menu(message)


# ============================================================
# UPLOAD SCREEN
# ============================================================

@dp.callback_query(F.data == "upload")
async def callback_upload(callback: CallbackQuery):

    await callback.answer()

    text = (
        "📤 <b>ЗАГРУЗКА ФАЙЛА</b>\n\n"
        "Отправь мне файл следующим сообщением.\n\n"
        "После загрузки я:\n"
        "• сохраню файл;\n"
        "• создам уникальный код;\n"
        "• покажу информацию о файле.\n\n"
        f"📦 Максимальный размер: "
        f"{format_size(MAX_FILE_SIZE)}"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=cancel_upload_keyboard(),
    )


# ============================================================
# FILE UPLOAD
# ============================================================

@dp.message(F.document)
async def handle_document(
    message: Message,
    bot: Bot,
):

    document: Document = message.document

    if (
        document.file_size
        and document.file_size > MAX_FILE_SIZE
    ):

        await message.answer(
            "❌ <b>Файл слишком большой</b>\n\n"
            f"Максимальный размер: "
            f"{format_size(MAX_FILE_SIZE)}",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )

        return

    status_message = await message.answer(
        "⏳ <b>Сохраняю файл...</b>",
        parse_mode="HTML",
    )

    try:

        code = await create_unique_code()

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
                    mime_type
                )
                VALUES (
                    $1,
                    $2,
                    $3,
                    $4,
                    $5,
                    $6,
                    $7
                )
                """,
                code,
                message.from_user.id,
                document.file_id,
                document.file_unique_id,
                document.file_name,
                document.file_size,
                document.mime_type,
            )

        text = (
            "✅ <b>ФАЙЛ СОХРАНЁН</b>\n\n"
            f"📄 <b>Имя:</b> "
            f"<code>{escape(document.file_name)}</code>\n"
            f"📦 <b>Размер:</b> "
            f"{format_size(document.file_size)}\n"
            f"🔑 <b>Код:</b> "
            f"<code>{code}</code>\n\n"
            "Файл готов к использованию."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[

                [
                    InlineKeyboardButton(
                        text="📥 Получить файл",
                        callback_data=f"get:{code}",
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="ℹ️ Информация",
                        callback_data=f"info:{code}",
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="📂 Мои файлы",
                        callback_data="files:0",
                    ),
                    InlineKeyboardButton(
                        text="🏠 Главная",
                        callback_data="home",
                    ),
                ],
            ]
        )

        await status_message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception:

        logger.exception("File save error")

        await status_message.edit_text(
            "❌ <b>Не удалось сохранить файл.</b>\n\n"
            "Попробуй ещё раз.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )


# ============================================================
# MY FILES
# ============================================================

async def show_files(
    callback: CallbackQuery,
    page: int = 0,
):

    user_id = callback.from_user.id

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT
                code,
                file_name,
                file_size,
                downloads,
                created_at
            FROM files
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id,
        )

    total = len(rows)

    if total == 0:

        await callback.message.edit_text(
            "📂 <b>МОИ ФАЙЛЫ</b>\n\n"
            "Здесь пока пусто.\n\n"
            "Загрузи первый файл, чтобы он появился здесь.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📤 Загрузить файл",
                            callback_data="upload",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🏠 Главная",
                            callback_data="home",
                        )
                    ],
                ]
            ),
        )

        return

    start = page * FILES_PER_PAGE
    end = start + FILES_PER_PAGE

    page_rows = rows[start:end]

    total_pages = (
        total + FILES_PER_PAGE - 1
    ) // FILES_PER_PAGE

    text = (
        "📂 <b>МОИ ФАЙЛЫ</b>\n\n"
        f"Всего файлов: <b>{total}</b>\n\n"
    )

    buttons = []

    for row in page_rows:

        name = row["file_name"] or "Без имени"

        text += (
            f"📄 <b>{escape(name)}</b>\n"
            f"🔑 <code>{row['code']}</code>\n"
            f"📦 {format_size(row['file_size'])}\n"
            f"⬇️ Скачиваний: {row['downloads']}\n\n"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {name[:30]}",
                    callback_data=f"file:{row['code']}",
                )
            ]
        )

    navigation = []

    if page > 0:

        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"files:{page - 1}",
            )
        )

    navigation.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="noop",
        )
    )

    if page + 1 < total_pages:

        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"files:{page + 1}",
            )
        )

    if navigation:
        buttons.append(navigation)

    buttons.append(
        [
            InlineKeyboardButton(
                text="🏠 Главная",
                callback_data="home",
            )
        ]
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )


@dp.callback_query(F.data.startswith("files:"))
async def callback_files(callback: CallbackQuery):

    await callback.answer()

    page = int(
        callback.data.split(":")[1]
    )

    await show_files(
        callback,
        page,
    )


# ============================================================
# FILE DETAILS
# ============================================================

@dp.callback_query(F.data.startswith("file:"))
async def callback_file(callback: CallbackQuery):

    await callback.answer()

    code = callback.data.split(":", 1)[1]

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code,
        )

    if not row:

        await callback.message.edit_text(
            "❌ Файл не найден.",
            reply_markup=back_home_keyboard(),
        )

        return

    owner = (
        row["user_id"]
        == callback.from_user.id
    )

    text = (
        "📄 <b>ФАЙЛ</b>\n\n"
        f"📄 <b>Имя:</b> "
        f"<code>{escape(row['file_name'] or 'Без имени')}</code>\n"
        f"📦 <b>Размер:</b> "
        f"{format_size(row['file_size'])}\n"
        f"🔑 <b>Код:</b> "
        f"<code>{row['code']}</code>\n"
        f"⬇️ <b>Скачиваний:</b> "
        f"{row['downloads']}\n"
        f"📅 <b>Дата:</b> "
        f"{row['created_at']}"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=file_keyboard(
            code,
            owner,
        ),
    )


# ============================================================
# GET FILE
# ============================================================

@dp.callback_query(F.data.startswith("get:"))
async def callback_get_file(
    callback: CallbackQuery,
    bot: Bot,
):

    code = callback.data.split(":", 1)[1]

    await callback.answer(
        "⏳ Отправляю файл..."
    )

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code,
        )

    if not row:

        await callback.message.answer(
            "❌ Файл не найден."
        )

        return

    try:

        await bot.send_document(
            callback.message.chat.id,
            row["file_id"],
            caption=(
                f"📄 {row['file_name'] or 'Файл'}\n"
                f"🔑 Код: {row['code']}"
            ),
        )

        async with db_pool.acquire() as conn:

            await conn.execute(
                """
                UPDATE files
                SET downloads = downloads + 1
                WHERE code = $1
                """,
                code,
            )

    except Exception:

        logger.exception(
            "Failed to send file"
        )

        await callback.message.answer(
            "❌ Не удалось отправить файл."
        )


# ============================================================
# FILE INFO
# ============================================================

@dp.callback_query(F.data.startswith("info:"))
async def callback_info(callback: CallbackQuery):

    await callback.answer()

    code = callback.data.split(":", 1)[1]

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code,
        )

    if not row:

        await callback.message.edit_text(
            "❌ Файл не найден.",
            reply_markup=back_home_keyboard(),
        )

        return

    text = (
        "ℹ️ <b>ИНФОРМАЦИЯ О ФАЙЛЕ</b>\n\n"
        f"📄 <b>Имя:</b>\n"
        f"<code>{escape(row['file_name'] or 'Без имени')}</code>\n\n"
        f"🔑 <b>Код:</b>\n"
        f"<code>{row['code']}</code>\n\n"
        f"📦 <b>Размер:</b> "
        f"{format_size(row['file_size'])}\n"
        f"⬇️ <b>Скачиваний:</b> "
        f"{row['downloads']}\n"
        f"🗂 <b>MIME:</b> "
        f"{escape(row['mime_type'] or 'Не указан')}\n"
        f"📅 <b>Загружен:</b> "
        f"{row['created_at']}"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📥 Получить",
                        callback_data=f"get:{code}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 Удалить",
                        callback_data=f"delete:{code}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"file:{code}",
                    )
                ],
            ]
        ),
    )


# ============================================================
# DELETE CONFIRMATION
# ============================================================

@dp.callback_query(F.data.startswith("delete:"))
async def callback_delete(callback: CallbackQuery):

    await callback.answer()

    code = callback.data.split(":", 1)[1]

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code,
        )

    if not row:

        await callback.message.edit_text(
            "❌ Файл уже удалён.",
            reply_markup=back_home_keyboard(),
        )

        return

    if row["user_id"] != callback.from_user.id:

        await callback.message.edit_text(
            "❌ У тебя нет доступа к удалению этого файла.",
            reply_markup=back_home_keyboard(),
        )

        return

    name = row["file_name"] or "Без имени"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить",
                    callback_data=f"confirm_delete:{code}",
                )
            ],

            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"file:{code}",
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "⚠️ <b>Удаление файла</b>\n\n"
        f"📄 <code>{escape(name)}</code>\n\n"
        "Ты действительно хочешь удалить этот файл?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@dp.callback_query(
    F.data.startswith("confirm_delete:")
)
async def callback_confirm_delete(
    callback: CallbackQuery
):

    await callback.answer()

    code = callback.data.split(":", 1)[1]

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code,
        )

        if not row:

            await callback.message.edit_text(
                "❌ Файл уже удалён.",
                reply_markup=back_home_keyboard(),
            )

            return

        if row["user_id"] != callback.from_user.id:

            await callback.message.edit_text(
                "❌ У тебя нет доступа.",
                reply_markup=back_home_keyboard(),
            )

            return

        await conn.execute(
            """
            DELETE FROM files
            WHERE code = $1
            """,
            code,
        )

    await callback.message.edit_text(
        "🗑 <b>ФАЙЛ УДАЛЁН</b>\n\n"
        "Файл успешно удалён из твоего хранилища.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📂 Мои файлы",
                        callback_data="files:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🏠 Главная",
                        callback_data="home",
                    )
                ],
            ]
        ),
    )


# ============================================================
# SEARCH
# ============================================================

@dp.callback_query(F.data == "search")
async def callback_search(callback: CallbackQuery):

    await callback.answer()

    await callback.message.edit_text(
        "🔎 <b>ПОИСК ФАЙЛА</b>\n\n"
        "Для поиска отправь название файла "
        "или его код следующим сообщением.\n\n"
        "Например:\n"
        "<code>photo</code>\n"
        "<code>A7K2M</code>",
        parse_mode="HTML",
        reply_markup=back_home_keyboard(),
    )


@dp.message(
    F.text,
    ~F.text.startswith("/")
)
async def handle_text_search(message: Message):

    query = message.text.strip()

    if not query:
        return

    if len(query) > 100:
        return

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            """
            SELECT
                code,
                file_name,
                file_size,
                downloads
            FROM files
            WHERE user_id = $1
              AND (
                    file_name ILIKE $2
                    OR code ILIKE $2
                  )
            ORDER BY created_at DESC
            LIMIT 20
            """,
            message.from_user.id,
            f"%{query}%",
        )

    if not rows:

        await message.answer(
            "🔎 <b>Ничего не найдено</b>\n\n"
            f"По запросу <code>{escape(query)}</code> "
            "файлов нет.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )

        return

    text = (
        "🔎 <b>РЕЗУЛЬТАТЫ ПОИСКА</b>\n\n"
        f"Запрос: <code>{escape(query)}</code>\n\n"
    )

    buttons = []

    for row in rows:

        name = row["file_name"] or "Без имени"

        text += (
            f"📄 <b>{escape(name)}</b>\n"
            f"🔑 <code>{row['code']}</code>\n"
            f"📦 {format_size(row['file_size'])}\n\n"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {name[:30]}",
                    callback_data=f"file:{row['code']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="🏠 Главная",
                callback_data="home",
            )
        ]
    )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )


# ============================================================
# STATISTICS
# ============================================================

@dp.callback_query(F.data == "stats")
async def callback_stats(callback: CallbackQuery):

    await callback.answer()

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS files_count,
                COALESCE(SUM(file_size), 0) AS total_size,
                COALESCE(SUM(downloads), 0) AS downloads
            FROM files
            WHERE user_id = $1
            """,
            callback.from_user.id,
        )

    text = (
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"📁 Файлов: <b>{row['files_count']}</b>\n"
        f"💾 Использовано: "
        f"<b>{format_size(row['total_size'])}</b>\n"
        f"⬇️ Скачиваний: "
        f"<b>{row['downloads']}</b>\n\n"
        "Статистика относится только к твоим файлам."
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_home_keyboard(),
    )


# ============================================================
# HELP
# ============================================================

@dp.callback_query(F.data == "help")
async def callback_help(callback: CallbackQuery):

    await callback.answer()

    text = (
        "ℹ️ <b>ПОМОЩЬ</b>\n\n"
        "📤 <b>Загрузить файл</b>\n"
        "Отправь документ боту — он сохранит его "
        "и выдаст уникальный код.\n\n"
        "📂 <b>Мои файлы</b>\n"
        "Просмотр всех загруженных тобой файлов.\n\n"
        "🔎 <b>Поиск</b>\n"
        "Поиск по имени или коду файла.\n\n"
        "📥 <b>Получить</b>\n"
        "Бот повторно отправит сохранённый файл.\n\n"
        "🗑 <b>Удалить</b>\n"
        "Удаление файла с подтверждением.\n\n"
        "📊 <b>Статистика</b>\n"
        "Размер хранилища, количество файлов "
        "и скачиваний."
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_home_keyboard(),
    )


# ============================================================
# SETTINGS
# ============================================================

@dp.callback_query(F.data == "settings")
async def callback_settings(callback: CallbackQuery):

    await callback.answer()

    text = (
        "⚙️ <b>НАСТРОЙКИ</b>\n\n"
        "Сейчас доступны основные настройки "
        "файлового менеджера.\n\n"
        "🔐 Доступ к файлам определяется "
        "владельцем файла.\n\n"
        "📦 Максимальный размер файла: "
        f"<b>{format_size(MAX_FILE_SIZE)}</b>\n\n"
        "Дополнительные настройки можем добавить "
        "в следующей версии."
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_home_keyboard(),
    )


# ============================================================
# HOME
# ============================================================

@dp.callback_query(F.data == "home")
async def callback_home(callback: CallbackQuery):

    await callback.answer()

    await edit_to_main_menu(callback)


# ============================================================
# NOOP
# ============================================================

@dp.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery):

    await callback.answer()


# ============================================================
# OLD COMMANDS — RESERVE
# ============================================================

@dp.message(Command("myfiles"))
async def old_myfiles(message: Message):

    fake_callback = None

    await message.answer(
        "📂 Открой раздел «Мои файлы» через меню:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📂 Мои файлы",
                        callback_data="files:0",
                    )
                ]
            ]
        ),
    )


@dp.message(Command("get"))
async def old_get(message: Message):

    args = message.text.split(maxsplit=1)

    if len(args) < 2:

        await message.answer(
            "Используй меню → 📂 Мои файлы."
        )

        return

    code = args[1].strip().upper()

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code,
        )

    if not row:

        await message.answer(
            "❌ Файл не найден."
        )

        return

    try:

        await message.bot.send_document(
            message.chat.id,
            row["file_id"],
        )

        async with db_pool.acquire() as conn:

            await conn.execute(
                """
                UPDATE files
                SET downloads = downloads + 1
                WHERE code = $1
                """,
                code,
            )

    except Exception:

        logger.exception(
            "Old get command failed"
        )

        await message.answer(
            "❌ Не удалось отправить файл."
        )


@dp.message(Command("info"))
async def old_info(message: Message):

    args = message.text.split(maxsplit=1)

    if len(args) < 2:

        await message.answer(
            "Используй меню → 📂 Мои файлы."
        )

        return

    code = args[1].strip().upper()

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code,
        )

    if not row:

        await message.answer(
            "❌ Файл не найден."
        )

        return

    await message.answer(
        "ℹ️ <b>Информация</b>\n\n"
        f"📄 {escape(row['file_name'] or 'Без имени')}\n"
        f"🔑 <code>{row['code']}</code>\n"
        f"📦 {format_size(row['file_size'])}\n"
        f"⬇️ {row['downloads']} скачиваний",
        parse_mode="HTML",
    )


@dp.message(Command("delete"))
async def old_delete(message: Message):

    args = message.text.split(maxsplit=1)

    if len(args) < 2:

        await message.answer(
            "Используй меню → 📂 Мои файлы."
        )

        return

    code = args[1].strip().upper()

    async with db_pool.acquire() as conn:

        row = await conn.fetchrow(
            """
            SELECT *
            FROM files
            WHERE code = $1
            """,
            code,
        )

        if not row:

            await message.answer(
                "❌ Файл не найден."
            )

            return

        if row["user_id"] != message.from_user.id:

            await message.answer(
                "❌ Ты не можешь удалить этот файл."
            )

            return

        await conn.execute(
            """
            DELETE FROM files
            WHERE code = $1
            """,
            code,
        )

    await message.answer(
        "🗑 Файл удалён.",
        reply_markup=main_menu_keyboard(),
    )


# ============================================================
# HEALTH SERVER
# ============================================================

async def health(request):

    return web.json_response(
        {
            "status": "ok",
            "service": "telegram-file-manager",
            "time": datetime.utcnow().isoformat(),
        }
    )


async def root(request):

    return web.json_response(
        {
            "status": "running",
            "service": "Telegram File Manager",
        }
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/",
        root,
    )

    app.router.add_get(
        "/health",
        health,
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    logger.info(
        "HTTP server started on port %s",
        PORT,
    )

    return runner


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "Starting Telegram File Manager..."
    )

    await init_db()

    web_runner = await start_web_server()

    session = None

    if BOT_API_BASE_URL:

        session = AiohttpSession(
            api=BOT_API_BASE_URL
        )

    if session:

        bot = Bot(
            token=BOT_TOKEN,
            session=session,
        )

    else:

        bot = Bot(
            token=BOT_TOKEN,
        )

    try:

        logger.info(
            "Telegram bot started"
        )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:

        await bot.session.close()

        if web_runner:

            await web_runner.cleanup()

        if db_pool:

            await db_pool.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(main())