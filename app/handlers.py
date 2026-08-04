from aiogram import Router, types, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from app.keyboards import get_reply_keyboard
from app.states import Form
from config import Config

router = Router()


@router.message(CommandStart())
async def cmd_start(message: types.Message):
    keyboard = get_reply_keyboard()

    await message.answer(text='Привет, новый пользователь! Наш прайс: 100 рублей. Выбери один из вариантов:',
                         reply_markup=keyboard)


@router.message(Command('help'))
async def cmd_help(message: types.Message):
    await message.answer('Я бот для приема заявок. Скоро здесь будет меню.')


@router.message(F.text == 'Оставить заявку')
async def set_name(message: types.Message, state: FSMContext):
    await state.set_state(Form.name)

    await message.answer(text='Для того, чтобы оставить заявку. введите ваше имя:')


@router.message(F.reply_to_message, F.from_user.id == int(Config.ADMIN_ID))
async def reply_to_message(message: types.Message, bot: Bot):
    original_message = message.reply_to_message

    if not original_message or not original_message.text or '#id' not in original_message.text or not message.text:
        return

    user_id = int(original_message.text.split('id')[-1])

    await bot.send_message(chat_id=user_id, text=message.text)


@router.message(Form.name)
async def set_birthday(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(Form.birthday)

    await message.answer(
        text='Теперь введите вашу дату рождения в формате ...:'
    )


@router.message(Form.birthday)
async def set_text(message: types.Message, state: FSMContext):
    await state.update_data(birthday=message.text)
    await state.set_state(Form.text)

    await message.answer(
        text='Напишите ваш текст обращения:'
    )


@router.message(Form.text)
async def save_statement(message: types.Message, state: FSMContext, bot: Bot):
    await state.update_data(text=message.text)
    data = await state.get_data()

    if not message.from_user:
        return

    user_id = message.from_user.id

    await bot.send_message(chat_id=Config.ADMIN_ID,
        text=f'🔔 Новая заявка!\n\n'
             f'Имя: {data["name"]}\n'
             f'Дата рождения: {data["birthday"]}\n'
             f'Текст: {data["text"]}\n'
             f'#id{user_id}'
    )
    await message.answer(
        text='Спасибо, ваша заявка отправлена администратору! Чтобы написать еще раз, составьте новую заявку'
    )
    await state.clear()


