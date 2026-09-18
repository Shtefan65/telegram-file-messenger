import asyncio
import logging
import os
import re
import secrets
import string
from datetime import datetime

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession


# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

BOT_API_BASE_URL = os.getenv("BOT_API_BASE_URL")

PORT = int(os.getenv("PORT", "10000"))

MAX_FILE_SIZE = 2_000_000_000  # 2 GB


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# =========================
# DATABASE
# =========================

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


# =========================
# CODE GENERATOR
# =========================

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
                "SELECT 1 FROM files WHERE code = $1",
                code
            )

        if not exists:
            return code


# =========================
# BOT
# =========================

dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "📁 <b>Файловый менеджер</b>\n\n"
        "Отправь мне файл, и я сохраню его в системе.\n\n"
        "Доступные команды:\n"
        "/myfiles — мои файлы\n"
        "/get КОД — получить файл\n"
        "/info КОД — информация о файле\n"
        "/delete КОД — удалить файл",
        parse_mode="HTML"
    )


# =========================
# FILE UPLOAD
# =========================

@dp.message(F.document)
async def handle_document(message: Message, bot: Bot):

    document = message.document

    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await message.answer(
            "❌ Файл слишком большой.\n"
            "Максимальный размер: 2 ГБ."
        )
        return

    await message.answer("⏳ Сохраняю файл...")

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
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            code,
            message.from_user.id,
            document.file_id,
            document.file_unique_id,
            document.file_name,
            document.file_size,
            document.mime_type
        )

    size_mb = (
        document.file_size / 1024 / 1024
        if document.file_size
        else 0
    )

    await message.answer(
        "✅ <b>Файл сохранён</b>\n\n"
        f"📄 Имя: <code>{document.file_name}</code>\n"
        f"📦 Размер: {size_mb:.2f} MB\n\n"
        f"🔑 Код файла:\n"
        f"<code>{code}</code>\n\n"
        "Чтобы получить файл:\n"
        f"<code>/get {code}</code>",
        parse_mode="HTML"
    )


# =========================
# GET FILE
# =========================

@dp.message(Command("get"))
async def cmd_get(message: Message, bot: Bot):

    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            "Использование:\n"
            "<code>/get A7K2M-91QPX</code>",
            parse_mode="HTML"
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
            code
        )

    if not row:
        await message.answer("❌ Файл с таким кодом не найден.")
        return

    try:
        await bot.send_document(
            message.chat.id,
            row["file_id"],
            caption=(
                f"📄 {row['file_name'] or 'Файл'}\n"
                f"🔑 Код: {row['code']}"
            )
        )

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE files
                SET downloads = downloads + 1
                WHERE code = $1
                """,
                code
            )

    except Exception as e:
        logger.exception("Failed to send file: %s", e)

        await message.answer(
            "❌ Не удалось отправить файл."
        )


# =========================
# MY FILES
# =========================

@dp.message(Command("myfiles"))
async def cmd_myfiles(message: Message):

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
            message.from_user.id
        )

    if not rows:
        await message.answer(
            "📂 У тебя пока нет сохранённых файлов."
        )
        return

    text = "📂 <b>Твои файлы</b>\n\n"

    for row in rows[:50]:

        size_mb = (
            row["file_size"] / 1024 / 1024
            if row["file_size"]
            else 0
        )

        text += (
            f"📄 <b>{row['file_name'] or 'Без имени'}</b>\n"
            f"🔑 <code>{row['code']}</code>\n"
            f"📦 {size_mb:.2f} MB\n"
            f"⬇️ Скачиваний: {row['downloads']}\n\n"
        )

    if len(rows) > 50:
        text += "Показаны последние 50 файлов."

    await message.answer(
        text,
        parse_mode="HTML"
    )


# =========================
# INFO
# =========================

@dp.message(Command("info"))
async def cmd_info(message: Message):

    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            "Использование:\n"
            "<code>/info КОД</code>",
            parse_mode="HTML"
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
            code
        )

    if not row:
        await message.answer("❌ Файл не найден.")
        return

    size_mb = (
        row["file_size"] / 1024 / 1024
        if row["file_size"]
        else 0
    )

    await message.answer(
        "ℹ️ <b>Информация о файле</b>\n\n"
        f"📄 Имя: <code>{row['file_name'] or 'Без имени'}</code>\n"
        f"🔑 Код: <code>{row['code']}</code>\n"
        f"📦 Размер: {size_mb:.2f} MB\n"
        f"⬇️ Скачиваний: {row['downloads']}\n"
        f"📅 Загружен: {row['created_at']}",
        parse_mode="HTML"
    )


# =========================
# DELETE
# =========================

@dp.message(Command("delete"))
async def cmd_delete(message: Message):

    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer(
            "Использование:\n"
            "<code>/delete КОД</code>",
            parse_mode="HTML"
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
            code
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
            code
        )

    await message.answer(
        "🗑 Файл удалён."
    )


# =========================
# HEALTH SERVER
# =========================

async def health(request):
    return web.json_response(
        {
            "status": "ok",
            "service": "telegram-file-messenger",
            "time": datetime.utcnow().isoformat()
        }
    )


async def root(request):
    return web.json_response(
        {
            "status": "running",
            "service": "Telegram File Manager"
        }
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get("/", root)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT
    )

    await site.start()

    logger.info(
        "HTTP server started on port %s",
        PORT
    )

    return runner


# =========================
# MAIN
# =========================

async def main():

    logger.info("Starting Telegram File Manager...")

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
            session=session
        )
    else:
        bot = Bot(
            token=BOT_TOKEN
        )

    try:

        logger.info("Bot started")

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )

    finally:

        await bot.session.close()

        if web_runner:
            await web_runner.cleanup()

        if db_pool:
            await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())