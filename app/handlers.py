import re
import html
import asyncio

from aiogram import Router, types, F, Bot
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.keyboards import (get_main_keyboard,
                           get_cancel_keyboard,
                           get_back_cancel_keyboard,
                           get_admin_keyboard, get_sure_keyboard)
from app.states import Form, Question, Newsletter, ChangeAboutUs
from config import Config
from app.db_requests import (add_user,
                             save_user_appeal,
                             get_user_thread_id,
                             get_topic_name,
                             set_user_thread_id, get_user_id,
                             save_group_id, get_group_id,
                             get_about_us, get_users,
                             activated_user, deactivated_user,
                             set_about_us_text)

router = Router()


@router.message(CommandStart(), F.chat.type == 'private')
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()

    user = message.from_user

    if not message or not user:
        return

    if user.id != Config.ADMIN_ID:

        keyboard = get_main_keyboard()

        await add_user(user.id)

        try:
            await message.answer(text=(
                "👋 <b>Добро пожаловать!</b>\n\n"
                "💰 Наш текущий прайс: <b>100 рублей</b>\n\n"
                "👇 <i>Выберите нужное действие в меню ниже:</i>"),
                reply_markup=keyboard)
        except TelegramForbiddenError:
            pass

        return

    admin_keyboard = get_admin_keyboard()

    try:
        await message.answer(text='Здравствуйте, Екатерина!.\n\n'
                                  'Вам доступен уникальный функционал ниже:',
                             reply_markup=admin_keyboard)
    except TelegramForbiddenError:
        pass


@router.message(Command('help'), F.chat.type == 'private')
async def cmd_help(message: types.Message):
    await message.answer(text=(
        "ℹ️ <b>Справочная информация</b>\n\n"
        "Я бот для приема заявок и обращений. Воспользуйтесь клавиатурой внизу, чтобы начать работу.")
    )


@router.message(Command('bind'))
async def cmd_bind(message: types.Message, bot: Bot):
    user = message.from_user

    if user is None:
        return

    if message.chat.type == 'private':
        await message.answer(text='Вы не можете выполнить это действие здесь!')

        return

    bot_member = await bot.get_chat_member(chat_id=message.chat.id, user_id=bot.id)

    if not isinstance(bot_member, types.ChatMemberAdministrator):
        try:
            await message.answer(text='Сделайте бота администратором!')
        except TelegramBadRequest:
            pass

        return

    if not bot_member.can_manage_topics:
        try:
            await message.answer(text='Разрешите боту управлять темами!')
        except TelegramBadRequest:
            pass

        return

    if user.id != Config.ADMIN_ID:
        try:
            await message.answer(text='Вы не являетесь админом этого бота, '
                                    'поэтому его функционал вам не доступен!')
        except TelegramBadRequest:
            pass

        return

    if not message.chat.is_forum:
        try:
            await message.answer(text='Добавьте бота в форум/супергруппу!')
        except TelegramBadRequest:
            pass

        return

    is_saved = await save_group_id(message.chat.id)

    if not is_saved:
        try:
            await message.answer(
                text='Бот уже привязан к другой группе!'
            )
        except TelegramBadRequest:
            pass

        return

    await message.answer(text='Бот был успешно привязан к группе!')


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_chat(event: types.ChatMemberUpdated, bot: Bot):
    user = event.from_user

    if event.chat.type == 'private':
        status = await activated_user(user.id)

        if not status:
            try:
                await bot.send_message(chat_id=user.id, text='Чтобы начать пользоваться ботом, напишите /start')
            except TelegramForbiddenError:
                pass

        return

    if user is None or user.id != Config.ADMIN_ID:
        await bot.leave_chat(chat_id=event.chat.id)

        return

    if not event.chat.is_forum:
        try:
            await bot.send_message(chat_id=event.chat.id, text='Включите темы в группе!')
        except TelegramBadRequest:
            pass
        finally:
            await bot.leave_chat(chat_id=event.chat.id)

        return

    group_id = await get_group_id()

    if group_id is not None and group_id != event.chat.id:
        try:
            await bot.send_message(chat_id=event.chat.id, text='Бот уже привязан к другой группе!')
        except TelegramBadRequest:
            pass
        finally:
            await bot.leave_chat(chat_id=event.chat.id)

        return

    if event.chat.id != group_id:
        await bot.send_message(chat_id=event.chat.id, text='Используйте команду /bind, чтобы привязать бота к этой группе.')


@router.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER),
                    F.chat.type == 'private')
async def bot_removed_from_chat(event: types.ChatMemberUpdated, bot: Bot):
    await deactivated_user(event.from_user.id)


@router.message(F.text == 'Оставить заявку',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def set_name(message: types.Message, state: FSMContext):
    await state.set_state(Form.name)

    try:
        await message.answer(text=(
            "📝 <b>Оформление заявки</b>\n\n"
            "Пожалуйста, введите ваше <b>имя</b>:"),
            reply_markup=get_cancel_keyboard()
        )
    except TelegramForbiddenError:
        pass


@router.message(F.text == 'О нас',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def about_us(message: types.Message):
    user = message.from_user

    if user is None:
        return

    about_us_text = await get_about_us()

    if about_us_text is None:
        try:
            await message.answer(text='Описание уточняется у администратора или ошибка на сервере.')
        except TelegramForbiddenError:
            pass

        return

    try:
        await message.answer(
            text=html.escape(about_us_text)
        )
    except TelegramForbiddenError:
        pass


@router.message(F.text == 'Задать вопрос',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def question_text(message: types.Message, state: FSMContext):
    await state.set_state(Question.question)

    try:
        await message.answer(
            text='Задайте ваш вопрос:',
            reply_markup=get_cancel_keyboard()
        )
    except TelegramForbiddenError:
        pass


@router.message(F.text == 'Сделать рассылку',
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def newsletter(message: types.Message, state: FSMContext):
    await state.set_state(Newsletter.text)

    try:
        await message.answer(
            text='Введите текст рассылки:',
            reply_markup=get_cancel_keyboard()
        )
    except TelegramForbiddenError:
        pass


@router.message(F.text == 'Изменить "О нас"',
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def change_about_us(message: types.Message, state: FSMContext):
    await state.set_state(ChangeAboutUs.about_us_text)

    try:
        await message.answer(text='Напишите описание вашего сервиса и предоставляемых вами услуг:')
    except TelegramForbiddenError:
        pass


@router.message(F.text == 'Меню', F.chat.type == 'private')
async def menu(message: types.Message, state: FSMContext):
    await state.clear()

    user = message.from_user

    if user is None:
        return

    if user.id != Config.ADMIN_ID:
        try:
            await message.answer(text=(
                "👋 <b>Добро пожаловать!</b>\n\n"
                "💰 Наш текущий прайс: <b>100 рублей</b>\n\n"
                "👇 <i>Выберите нужное действие в меню ниже:</i>"),
                reply_markup=get_main_keyboard())
        except TelegramForbiddenError:
            pass

        return

    try:
        await message.answer(text='Вы в меню.', reply_markup=get_admin_keyboard())
    except TelegramForbiddenError:
        pass


@router.message(F.text == 'Назад', F.chat.type == 'private')
async def back(message: types.Message, state: FSMContext):
    current_state = await state.get_state()

    if current_state is None:
        await message.answer(text=(
            "👋 <b>Добро пожаловать!</b>\n\n"
            "💰 Наш текущий прайс: <b>100 рублей</b>\n\n"
            "👇 <i>Выберите нужное действие в меню ниже:</i>"),
            reply_markup=get_main_keyboard()
        )

        return

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
        try:
            await message.answer(text='Бот не привязан к группе!')
        except TelegramBadRequest:
            pass

        return

    if group_id != message.chat.id:
        try:
            await message.answer(text='Бот привязан к другой группе!')
        except TelegramBadRequest:
            pass

        return

    original_message = message.reply_to_message

    if not original_message or not original_message.text or not message.text:
        return

    user_id = await get_user_id(message.message_thread_id)

    if not user_id:
        try:
            await message.answer(text='Данный пользователь не зарегистрирован в боте!')
        except TelegramBadRequest:
            pass

        return

    reply_text = (
        f"📩 <b>Ответ на вашу заявку:</b>\n"
        f"<i>{html.escape(original_message.text)}</i>\n\n"
        f"💬 <b>Сообщение от администратора:</b>\n"
        f"{html.escape(message.text)}"
    )

    try:
        await bot.send_message(chat_id=user_id, text=reply_text)
    except TelegramForbiddenError:
        await message.answer(text='Пользователь только что заблокировал бота, поэтому ваше сообщение не доставлено.')


@router.message(Form.name,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
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


@router.message(Form.birthday,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
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


@router.message(Form.text,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def save_statement(message: types.Message, state: FSMContext, bot: Bot):
    if not message or not message.text:
        await message.answer(text="⚠️ <i>Текст не распознан. Пожалуйста, напишите ваше обращение:</i>")

        return

    await state.update_data(text=message.text)
    data = await state.get_data()

    await state.clear()

    user = message.from_user

    if not user:
        return

    group_id = await get_group_id()

    if group_id is None:
        await message.answer(text='Бот временно не работает, попробуйте позже.')

        return

    topic_id = await get_user_thread_id(user.id)

    if topic_id is None:
        topic_name = await get_topic_name(user.id)

        if topic_name is None:
            try:
                await message.answer(text='Вы не зарегистрированы в боте. Напишите /start')
            except TelegramForbiddenError:
                pass

            return

        try:
            topic = await bot.create_forum_topic(chat_id=group_id, name=topic_name)
        except TelegramBadRequest:
            await message.answer(text='Ошибка на стороне сервера. Попробуйте создать заявку позже.')

            return

        topic_id = topic.message_thread_id

        status = await set_user_thread_id(user.id, topic_id)

        if not status:
            await message.answer(text='Вы не зарегистрированы в боте. Напишите /start')

            return

    try:
        await bot.send_message(chat_id=group_id,
                                text=(
                                    f"🔔 <b>НОВАЯ ЗАЯВКА</b>\n\n"
                                    f"👤 <b>Имя:</b> {html.escape(data['name'])}\n"
                                    f"📅 <b>Дата рождения:</b> {html.escape(data['birthday'])}\n\n"
                                    f"💬 <b>Обращение:</b>\n"
                                    f"<i>{html.escape(data['text'])}</i>"),
                                message_thread_id=topic_id
                                )
    except TelegramBadRequest:
        await message.answer(text='Ваша заявка не была доставлена. Ошибка на сервере.')

        return

    result = await save_user_appeal(user.id, message.text, 'Bid')

    if not result:
        try:
            await message.answer(
                text='Произошла ошибка на стороне сервер. Пожалуйста, напишите \\start'
            )
        except TelegramForbiddenError:
            pass

        return

    try:
        await message.answer(
            text=(
                "✅ <b>Ваша заявка успешно отправлена!</b>\n\n"
                "Администратор ознакомится с ней и ответит вам прямо здесь.\n\n"
                "<i>Чтобы написать еще раз, выберите действие в меню.</i>"),
            reply_markup=get_main_keyboard()
        )
    except TelegramForbiddenError:
        pass


@router.message(Question.question,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def save_question(message: types.Message, state: FSMContext, bot: Bot):
    if not message or not message.text:
        try:
            await message.answer(text='Текст не распознан. Пожалуйста, напишите вопрос текстом!')
        except TelegramForbiddenError:
            pass

        return

    await state.update_data(question=message.text)
    data = await state.get_data()

    await state.clear()

    user = message.from_user

    if user is None:
        return

    group_id = await get_group_id()

    if group_id is None:
        try:
            await message.answer(text='Ошибка на сервере. Попробуйте позже.')
        except TelegramForbiddenError:
            pass

        return

    topic_id = await get_user_thread_id(user.id)

    if topic_id is None:
        topic_name = await get_topic_name(user.id)

        if topic_name is None:
            try:
                await message.answer(text='Вы не зарегистрированы в боте. Пожалуйста, напишите /start')
            except TelegramForbiddenError:
                pass

            return

        try:
            topic = await bot.create_forum_topic(chat_id=group_id, name=topic_name)
        except TelegramBadRequest:
            await message.answer(text='Ошибка на стороне сервера. Попробуйте задать вопрос позже.')

            return

        topic_id = topic.message_thread_id

        status = await set_user_thread_id(user.id, topic_id)

        if not status:
            try:
                await message.answer(text='Вы не зарегистрированы в боте. Пожалуйста, напишите /start')
            except TelegramForbiddenError:
                pass

            return

    try:
        await bot.send_message(chat_id=group_id,
                                text=f'Новый вопрос!\n\n'
                                    f'{html.escape(data["question"])}',
                                message_thread_id=topic_id)
    except TelegramBadRequest:
        await message.answer(text='Ваш вопрос не был доставлен. Ошибка на сервере.')

        return

    result = await save_user_appeal(user.id, message.text, 'Question')

    if not result:
        try:
            await message.answer(
                text='Произошла ошибка на стороне сервер. Пожалуйста, напишите /start'
            )
        except TelegramForbiddenError:
            pass

        return

    try:
        await message.answer(
            text=(
                "✅ <b>Ваш вопрос успешно отправлен!</b>\n\n"
                "Администратор ознакомится с ним и ответит вам прямо здесь.\n\n"
                "<i>Чтобы написать еще раз, выберите действие в меню.</i>"),
            reply_markup=get_main_keyboard()
        )
    except TelegramForbiddenError:
        pass


@router.message(Newsletter.text,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def send_newsletter(message: types.Message, state: FSMContext):
    if not message or not message.text:
        try:
            await message.answer(text='Текст не распознан. Пожалуйста, напишите текст снова.')
        except TelegramForbiddenError:
            pass

        return

    if len(message.text) > 4096:
        try:
            await message.answer(text='Ваше сообщение слишком длинное! Пожалуйста, учтите ограничение в 4096 символов.')
        except TelegramForbiddenError:
            pass

        return

    await state.update_data(newsletter=message.text)
    await state.set_state(Newsletter.sure)

    try:
        await message.answer(text=f'Вы уверены?\n\n'
                                  f'Вот так выглядит ваше сообщение сейчас:\n',
                             reply_markup=get_sure_keyboard())
        await message.answer(text=f'{html.escape(message.text)}')
    except TelegramForbiddenError:
        pass


@router.message(Newsletter.sure,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def accept_newsletter(message: types.Message, state: FSMContext, bot: Bot):
    if not message or not message.text or message.text not in ['Подтвердить', 'Изменить']:
        try:
            await message.answer(text='Такого варианта нету!')
        except TelegramForbiddenError:
            pass

        return

    elif message.text == 'Подтвердить':
        data = await state.get_data()
        await state.clear()

        users = await get_users()

        if users is None:
            try:
                await  message.answer(text='У бота нету пользователей. Пока что сделать рассылку нельзя.',
                                      reply_markup=get_admin_keyboard())
            except TelegramForbiddenError:
                pass

            return

        sent = 0
        not_sent = 0

        for telegram_id, is_active in users:
            if is_active:
                while True:
                    try:
                        await bot.send_message(chat_id=telegram_id,
                                               text=html.escape(data['newsletter']))
                        sent += 1

                        break
                    except TelegramForbiddenError:
                        not_sent += 1
                        await deactivated_user(telegram_id)

                        break
                    except TelegramBadRequest:
                        not_sent += 1

                        break
                    except TelegramRetryAfter as error:
                        await asyncio.sleep(error.retry_after)

        try:
            await message.answer(
                text=f'Рассылка завершена\n\n'
                     f'Отправлено: {sent}\n'
                     f'Не отправлено: {not_sent}\n\n'
                     f'Всего человек: {len(users)}\n'
                     f'Активных из них: {sent + not_sent}',
                reply_markup=get_admin_keyboard()
            )
        except TelegramForbiddenError:
            pass

        return

    elif message.text == 'Изменить':
        await state.set_state(Newsletter.text)

        try:
            await message.answer(
                text='Введите текст рассылки:',
                reply_markup=get_cancel_keyboard()
            )
        except TelegramForbiddenError:
            pass

        return


@router.message(ChangeAboutUs.about_us_text,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def set_about_us(message: types.Message, state: FSMContext):
    if not message or not message.text:
        try:
            await message.answer(text='⚠️ <i>Текст не распознан. Пожалуйста, напишите ваше описание:</i>')
        except TelegramForbiddenError:
            pass

        return

    await state.update_data(about_us=message.text)
    data = await state.get_data()

    await state.clear()

    status = await set_about_us_text(data['about_us'])

    if not status:
        try:
            await message.answer(text='Текст не был сохранен! Пожалуйста, попробуйте снова.')
        except TelegramForbiddenError:
            pass

        return

    try:
        await message.answer(text='Текст был успешно изменен!',
                             reply_markup=get_admin_keyboard())
    except TelegramForbiddenError:
        pass
