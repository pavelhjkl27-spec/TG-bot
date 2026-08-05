import asyncio

from aiogram import Bot, Dispatcher
from config import Config
from app.handlers import router
from aiogram.client.default import DefaultBotProperties
from app.database import init_db

bot = Bot(token=Config.BOT_TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()

dp.include_router(router)


async def main():
    print('Бот запускается...')

    await init_db()

    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())


