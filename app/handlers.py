import re

from aiogram import Router, types, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext

from app.keyboards import (get_main_keyboard,
                           get_cancel_keyboard,
                           get_back_cancel_keyboard,)
from app.states import Form
from config import Config
from app.db_requests import add_user, save_user_appeal

router = Router()


@router.message(CommandStart())
async def cmd_start(message: types.Message):
    keyboard = get_main_keyboard()
    user = message.from_user

    if not message or not user:
        return

    await add_user(user.id)

    await message.answer(text=(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "💰 Наш текущий прайс: <b>100 рублей</b>\n\n"
        "👇 <i>Выберите нужное действие в меню ниже:</i>"),
        reply_markup=keyboard)


@router.message(Command('help'))
async def cmd_help(message: types.Message):
    await message.answer(text=(
        "ℹ️ <b>Справочная информация</b>\n\n"
        "Я бот для приема заявок и обращений. Воспользуйтесь клавиатурой внизу, чтобы начать работу.")
    )


@router.message(F.text == 'Оставить заявку')
async def set_name(message: types.Message, state: FSMContext):
    await state.set_state(Form.name)

    await message.answer(text=(
        "📝 <b>Оформление заявки</b>\n\n"
        "Пожалуйста, введите ваше <b>имя</b>:"),
        reply_markup=get_cancel_keyboard()
    )


@router.message(F.text == 'Меню')
async def menu(message: types.Message, state: FSMContext):
    await state.clear()

    await message.answer(text=(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "💰 Наш текущий прайс: <b>100 рублей</b>\n\n"
        "👇 <i>Выберите нужное действие в меню ниже:</i>"),
        reply_markup=get_main_keyboard())


@router.message(F.text == 'Назад')
async def back(message: types.Message, state: FSMContext):
    current_state = await state.get_state()

    if current_state == Form.text.state:
        await state.set_state(Form.birthday)

        await message.answer(text='Введите вашу дату рождения в формате DD/MM/YYYY:')

    elif current_state == Form.birthday.state:
        await state.set_state(Form.name)

        await message.answer(text=(
            "📝 <b>Оформление заявки</b>\n\n"
            "Пожалуйста, введите ваше <b>имя</b>:"),
            reply_markup=get_cancel_keyboard()
        )

@router.message(F.reply_to_message, F.from_user.id == Config.ADMIN_ID)
async def reply_to_message(message: types.Message, bot: Bot):
    original_message = message.reply_to_message

    if not original_message or not original_message.text or '#id' not in original_message.text or not message.text:
        return

    user_id = int(original_message.text.split('id')[-1])

    await bot.send_message(chat_id=user_id, text=message.text)


@router.message(Form.name)
async def set_birthday(message: types.Message, state: FSMContext):
    if not message or not message.text:
        await message.answer(text="⚠️ <i>Вы ничего не написали. Пожалуйста, введите ваше имя:</i>")

        return

    await state.update_data(name=message.text)
    await state.set_state(Form.birthday)

    await message.answer(
        text=(
            "📅 Отлично! Теперь введите вашу <b>дату рождения</b>.\n\n"
            "Используйте формат <code>ДД/ММ/ГГГГ</code> (например, <i>05/12/1984</i>):"),
        reply_markup=get_back_cancel_keyboard()
    )


@router.message(Form.birthday)
async def set_text(message: types.Message, state: FSMContext):
    if not message or not message.text or not re.fullmatch(
            r'^(0?[1-9]|[12][0-9]|3[01])/(0?[1-9]|1[0-2])/\d{4}$', message.text):
        await message.answer(text=(
            "⚠️ <b>Ошибка формата</b>\n\n"
            "Пожалуйста, введите дату строго в формате <code>ДД/ММ/ГГГГ</code>\n"
            "<i>Пример: 05/12/1984</i>")
        )

        return

    await state.update_data(birthday=message.text)
    await state.set_state(Form.text)

    await message.answer(
        text=(
            "✍️ <b>Текст обращения</b>\n\n"
            "Напишите суть вашей заявки или задайте вопрос в свободной форме:"),
        reply_markup=get_back_cancel_keyboard()
    )


@router.message(Form.text)
async def save_statement(message: types.Message, state: FSMContext, bot: Bot):
    if not message or not message.text:
        await message.answer(text="⚠️ <i>Текст не распознан. Пожалуйста, напишите ваше обращение:</i>")

        return

    await state.update_data(text=message.text)
    data = await state.get_data()

    user = message.from_user

    if not user:
        return

    result = await save_user_appeal(user.id, message.text)

    if result == 'ERROR':
        await message.answer(
            text='Произошла ошибка на стороне сервер. Пожалуйста, напишите \\start'
        )
        
        return

    await bot.send_message(chat_id=Config.ADMIN_ID,
                           text=(
                               f"🔔 <b>НОВАЯ ЗАЯВКА</b>\n\n"
                               f"👤 <b>Имя:</b> {data['name']}\n"
                               f"📅 <b>Дата рождения:</b> {data['birthday']}\n\n"
                               f"💬 <b>Обращение:</b>\n"
                               f"<i>{data['text']}</i>\n\n"
                               f"#id{user.id}")
                           )
    await message.answer(
        text=(
            "✅ <b>Ваша заявка успешно отправлена!</b>\n\n"
            "Администратор ознакомится с ней и ответит вам прямо здесь.\n\n"
            "<i>Чтобы написать еще раз, выберите действие в меню.</i>"),
        reply_markup=get_main_keyboard()
    )
    await state.clear()


