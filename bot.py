import asyncio
import logging
import os
import secrets
import string

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandStart
from aiogram.types import Message


BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

# Для Local Bot API:
BOT_API_BASE_URL = os.getenv(
    "BOT_API_BASE_URL",
    "http://telegram-local-api:8081"
)

# Официальный лимит Local Bot API
MAX_FILE_SIZE = 2_000_000_000


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

dp = Dispatcher()
pool = None


def create_code():
    """
    Создаёт код вида:
    A7K2M-91QPX
    """

    alphabet = string.ascii_uppercase + string.digits

    return (
        "".join(secrets.choice(alphabet) for _ in range(5))
        + "-"
        + "".join(secrets.choice(alphabet) for _ in range(5))
    )


def create_bot():

    api = TelegramAPIServer.from_base(
        BOT_API_BASE_URL.rstrip("/")
    )

    session = AiohttpSession(api=api)

    return Bot(
        BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(
            parse_mode="HTML"
        )
    )


bot = create_bot()


async def init_database():

    global pool

    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5
    )

    async with pool.acquire() as conn:

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TIMESTAMPTZ
                NOT NULL DEFAULT NOW()
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id BIGSERIAL PRIMARY KEY,

            owner_id BIGINT
                NOT NULL
                REFERENCES users(tg_id)
                ON DELETE CASCADE,

            file_id TEXT NOT NULL,

            file_unique_id TEXT,

            file_name TEXT,

            mime_type TEXT,

            file_size BIGINT,

            code TEXT UNIQUE NOT NULL,

            downloads BIGINT
                NOT NULL DEFAULT 0,

            created_at TIMESTAMPTZ
                NOT NULL DEFAULT NOW()
        );
        """)

        await conn.execute("""
        CREATE INDEX IF NOT EXISTS
        files_owner_index
        ON files(owner_id);
        """)


async def register_user(message: Message):

    user = message.from_user

    async with pool.acquire() as conn:

        await conn.execute(
            """
            INSERT INTO users
            (
                tg_id,
                username,
                first_name
            )
            VALUES ($1, $2, $3)

            ON CONFLICT (tg_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name
            """,

            user.id,
            user.username,
            user.first_name
        )


def format_size(size):

    if not size:
        return "неизвестно"

    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"

    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.2f} MB"

    if size >= 1024:
        return f"{size / 1024:.2f} KB"

    return f"{size} B"


@dp.message(CommandStart())
async def start(message: Message):

    await register_user(message)

    await message.answer(
        "<b>📁 Telegram File Messenger</b>\n\n"

        "Отправь мне файл — я создам "
        "уникальный код для него.\n\n"

        "Например:\n"
        "<code>A7K2M-91QPX</code>\n\n"

        "Другой пользователь сможет получить "
        "этот файл командой:\n"

        "<code>/get A7K2M-91QPX</code>\n\n"

        "<b>Команды:</b>\n"

        "/myfiles — мои файлы\n"
        "/get КОД — получить файл\n"
        "/info КОД — информация о файле\n"
        "/delete КОД — удалить файл\n"
        "/help — помощь"
    )


@dp.message(Command("help"))
async def help_command(message: Message):

    await start(message)


@dp.message(F.document)
async def upload_file(message: Message):

    await register_user(message)

    document = message.document

    size = document.file_size or 0

    if size > MAX_FILE_SIZE:

        await message.answer(
            "❌ Файл слишком большой.\n\n"
            "Максимальный размер: <b>2000 MB</b>."
        )

        return


    # Генерируем уникальный код

    for _ in range(20):

        code = create_code()

        try:

            async with pool.acquire() as conn:

                await conn.execute(
                    """
                    INSERT INTO files
                    (
                        owner_id,
                        file_id,
                        file_unique_id,
                        file_name,
                        mime_type,
                        file_size,
                        code
                    )

                    VALUES
                    (
                        $1,
                        $2,
                        $3,
                        $4,
                        $5,
                        $6,
                        $7
                    )
                    """,

                    message.from_user.id,

                    document.file_id,

                    document.file_unique_id,

                    document.file_name,

                    document.mime_type,

                    size,

                    code
                )

            break

        except asyncpg.UniqueViolationError:

            continue

    else:

        await message.answer(
            "❌ Не удалось создать код."
        )

        return


    await message.answer(
        "✅ <b>Файл сохранён!</b>\n\n"

        f"📄 <b>{document.file_name or 'Без имени'}</b>\n"

        f"📦 Размер: {format_size(size)}\n\n"

        f"🔑 Код файла:\n"
        f"<code>{code}</code>\n\n"

        "Передай этот код другому пользователю.\n\n"

        f"Получение:\n"
        f"<code>/get {code}</code>"
    )


@dp.message(Command("get"))
async def get_file(message: Message):

    await register_user(message)

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2:

        await message.answer(
            "Использование:\n"
            "<code>/get A7K2M-91QPX</code>"
        )

        return


    code = parts[1].strip().upper()


    async with pool.acquire() as conn:

        file = await conn.fetchrow(
            """
            SELECT
                file_id,
                file_name,
                file_size
            FROM files
            WHERE code = $1
            """,

            code
        )


    if not file:

        await message.answer(
            "❌ Файл с таким кодом не найден."
        )

        return


    await message.answer(
        "⏳ Отправляю файл..."
    )


    try:

        await bot.send_document(
            chat_id=message.chat.id,

            document=file["file_id"],

            caption=(
                f"📁 {file['file_name'] or 'Файл'}\n"
                f"🔑 <code>{code}</code>"
            )
        )


        async with pool.acquire() as conn:

            await conn.execute(
                """
                UPDATE files
                SET downloads = downloads + 1
                WHERE code = $1
                """,

                code
            )


    except Exception:

        logging.exception(
            "Ошибка отправки файла"
        )

        await message.answer(
            "❌ Не удалось отправить файл."
        )


@dp.message(Command("myfiles"))
async def my_files(message: Message):

    await register_user(message)

    async with pool.acquire() as conn:

        files = await conn.fetch(
            """
            SELECT
                file_name,
                file_size,
                code,
                downloads
            FROM files
            WHERE owner_id = $1
            ORDER BY created_at DESC
            LIMIT 50
            """,

            message.from_user.id
        )


    if not files:

        await message.answer(
            "📂 У тебя пока нет файлов."
        )

        return


    result = [
        "<b>📂 Твои файлы:</b>\n"
    ]


    for file in files:

        result.append(
            f"📄 <b>{file['file_name'] or 'Без имени'}</b>\n"
            f"📦 {format_size(file['file_size'])}\n"
            f"🔑 <code>{file['code']}</code>\n"
            f"📥 Скачиваний: {file['downloads']}"
        )


    await message.answer(
        "\n\n".join(result)
    )


@dp.message(Command("info"))
async def file_info(message: Message):

    await register_user(message)

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2:

        await message.answer(
            "Использование:\n"
            "<code>/info КОД</code>"
        )

        return


    code = parts[1].strip().upper()


    async with pool.acquire() as conn:

        file = await conn.fetchrow(
            """
            SELECT
                file_name,
                file_size,
                mime_type,
                downloads,
                created_at
            FROM files
            WHERE code = $1
            """,

            code
        )


    if not file:

        await message.answer(
            "❌ Файл не найден."
        )

        return


    await message.answer(
        "<b>📄 Информация</b>\n\n"

        f"Имя: "
        f"<code>{file['file_name'] or 'Без имени'}</code>\n"

        f"Размер: "
        f"{format_size(file['file_size'])}\n"

        f"Тип: "
        f"{file['mime_type'] or 'неизвестно'}\n"

        f"Скачиваний: "
        f"{file['downloads']}\n"

        f"Код: "
        f"<code>{code}</code>"
    )


@dp.message(Command("delete"))
async def delete_file(message: Message):

    await register_user(message)

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2:

        await message.answer(
            "Использование:\n"
            "<code>/delete КОД</code>"
        )

        return


    code = parts[1].strip().upper()


    async with pool.acquire() as conn:

        result = await conn.execute(
            """
            DELETE FROM files

            WHERE code = $1
            AND owner_id = $2
            """,

            code,
            message.from_user.id
        )


    if result == "DELETE 0":

        await message.answer(
            "❌ Файл не найден "
            "или он тебе не принадлежит."
        )

    else:

        await message.answer(
            "🗑 Файл удалён из каталога."
        )


async def main():

    await init_database()

    me = await bot.get_me()

    logging.info(
        "Bot started: @%s",
        me.username
    )

    try:

        await dp.start_polling(bot)

    finally:

        await bot.session.close()

        if pool:

            await pool.close()


if __name__ == "__main__":

    asyncio.run(main())