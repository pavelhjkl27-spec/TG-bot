import re

from aiogram import Router, types, F, Bot
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.fsm.context import FSMContext

from app.keyboards import (get_main_keyboard,
                           get_cancel_keyboard,
                           get_back_cancel_keyboard,)
from app.states import Form
from config import Config
from app.db_requests import (add_user,
                             save_user_appeal,
                             get_user_thread_id,
                             get_topic_name,
                             set_user_thread_id, get_user_id,
                             save_group_id, get_group_id)

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


@router.message(Command('bind'))
async def cmd_bind(message: types.Message):
    user = message.from_user

    if user is None:
        return

    if user.id != Config.ADMIN_ID:
        await message.answer(text='Вы не являетесь админом этого бота, '
                                  'поэтому его функционал вам не доступен!')

        return

    if not message.chat.is_forum:
        await message.answer(text='Добавьте бота в форум/супергруппу!')

        return

    is_saved = await save_group_id(message.chat.id)

    if not is_saved:
        await message.answer(
            text='Бот уже привязан к другой группе!'
        )

        return

    await message.answer(text='Бот был успешно привязан к группе!')


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_chat(event: types.ChatMemberUpdated, bot: Bot):
    user = event.from_user

    if event.chat.type == 'private':
        return

    if user is None or user.id != Config.ADMIN_ID:
        await bot.leave_chat(chat_id=event.chat.id)

        return

    if not event.chat.is_forum:
        await bot.leave_chat(chat_id=event.chat.id)

        return

    group_id = await get_group_id()

    if group_id is not None and group_id != event.chat.id:
        await bot.leave_chat(chat_id=event.chat.id)


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

@router.message(F.reply_to_message,
                F.from_user.id == Config.ADMIN_ID,
                F.message_thread_id.is_not(None))
async def reply_to_message(message: types.Message, bot: Bot):
    group_id = await get_group_id()

    if group_id is None:
        await message.answer(text='Бот не привязан к группе!')

        return

    if group_id != message.chat.id:
        await message.answer(text='Бот привязан к другой группе!')

        return

    original_message = message.reply_to_message

    if not original_message or not original_message.text or not message.text:
        return

    user_id = await get_user_id(message.message_thread_id)

    if not user_id:
        return

    reply_text = (
        f"📩 <b>Ответ на вашу заявку:</b>\n"
        f"<i>{original_message.text}</i>\n\n"
        f"💬 <b>Сообщение от администратора:</b>\n"
        f"{message.text}"
    )

    await bot.send_message(chat_id=user_id, text=reply_text)


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

    group_id = await get_group_id()

    if group_id is None:
        await message.answer(text='Бот временно не работает, попробуйте позже.')

        return

    result = await save_user_appeal(user.id, message.text)

    if result == 'ERROR':
        await message.answer(
            text='Произошла ошибка на стороне сервер. Пожалуйста, напишите \\start'
        )

        return

    topic_id = await get_user_thread_id(user.id)

    if topic_id is None:
        topic_name = await get_topic_name(user.id)

        topic = await bot.create_forum_topic(chat_id=group_id, name=topic_name)
        topic_id = topic.message_thread_id

        await set_user_thread_id(user.id, topic_id)

    await bot.send_message(chat_id=group_id,
                            text=(
                                f"🔔 <b>НОВАЯ ЗАЯВКА</b>\n\n"
                                f"👤 <b>Имя:</b> {data['name']}\n"
                                f"📅 <b>Дата рождения:</b> {data['birthday']}\n\n"
                                f"💬 <b>Обращение:</b>\n"
                                f"<i>{data['text']}</i>"),
                            message_thread_id=topic_id
                            )

    await message.answer(
        text=(
            "✅ <b>Ваша заявка успешно отправлена!</b>\n\n"
            "Администратор ознакомится с ней и ответит вам прямо здесь.\n\n"
            "<i>Чтобы написать еще раз, выберите действие в меню.</i>"),
        reply_markup=get_main_keyboard()
    )
    await state.clear()


