import asyncio
import os
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class Form(StatesGroup):
    name = State()
    birthday = State()
    text = State()


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


@dp.message(F.text == 'Оставить заявку')
async def set_name(message: types.Message, state: FSMContext):
    await state.set_state(Form.name)

    await message.answer(text='Для того, чтобы оставить заявку. введите ваше имя:',
                         reply_markup=types.ReplyKeyboardRemove())


@dp.message(Form.name)
async def set_birthday(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(Form.birthday)

    await message.answer(
        text='Теперь введите вашу дату рождения в формате ...:'
    )


@dp.message(Form.birthday)
async def set_text(message: types.Message, state: FSMContext):
    await state.update_data(birthday=message.text)
    await state.set_state(Form.text)

    await message.answer(
        text='Напишите ваш текст обращения:'
    )


@dp.message(Form.text)
async def save_statement(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    data = await state.get_data()

    print(data)

    await state.clear()
    await message.answer(
        text='Спасибо, ваша заявка отправлена администратору. Чтобы написать еще раз, составьте новую заявку.'
    )

async def main():
    print('Бот запускается...')

    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())


