import asyncio

from aiogram import Bot, Dispatcher
from config import Config
from app.handlers import router

bot = Bot(token=Config.BOT_TOKEN)
dp = Dispatcher()

dp.include_router(router)


async def main():
    print('Бот запускается...')

    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())


