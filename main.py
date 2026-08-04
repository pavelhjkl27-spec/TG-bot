import asyncio
import os
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    KeyBoard = types.ReplyKeyboardMarkup(keyboard=[
        [
            types.KeyboardButton(text='Задать вопрос'),
            types.KeyboardButton(text='Оставить заявку'),
        ],
        [
            types.KeyboardButton(text='О нас')
        ]
    ], resize_keyboard=True)

    await message.answer(text='Привет, новый пользователь! Наш прайс: 100 рублей. Выбери один из вариантов:',
                         reply_markup=KeyBoard)


@dp.message(Command('help'))
async def cmd_help(message: types.Message):
    await message.answer('Я бот для приема заявок. Скоро здесь будет меню.')


async def main():
    print('Бот запускается...')
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())


