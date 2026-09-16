import asyncio
from datetime import datetime
import html
import logging

from aiogram import Router, types, F, Bot
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.exc import SQLAlchemyError

from app.keyboards import (get_main_keyboard,
                           get_cancel_keyboard,
                           get_back_cancel_keyboard,
                           get_admin_keyboard, get_sure_keyboard)
from app.states import Form, Question, Newsletter, ChangeAboutUs, ChangePrice
from config import Config
from app.db_requests import (add_user,
                             save_user_appeal,
                             get_user_thread_id,
                             get_topic_name,
                             set_user_thread_id, get_user_id,
                             save_group_id, get_group_id,
                             get_about_us, get_users,
                             activated_user, deactivated_user,
                             set_about_us_text, get_price,
                             set_price)
from app.utils import exceeds_telegram_limit, safe_answer, safe_send_message

logger = logging.getLogger(__name__)

router = Router()

NAME_MAX_LENGTH = 200
BIRTHDAY_FORMAT = '%d/%m/%Y'

SET_THREAD_ID_ATTEMPTS = 3
SET_THREAD_ID_RETRY_DELAY_SECONDS = 0.5
SAVE_APPEAL_ATTEMPTS = 3
SAVE_APPEAL_RETRY_DELAY_SECONDS = 0.5

_pending_topic_ids: dict[int, int] = {}
"""
Тема, которая реально создана в Telegram, но ещё не подтверждена записью в
Users.topic_id из-за сбоя set_user_thread_id (даже после ретраев). Пока
процесс бота жив, следующая попытка того же клиента переиспользует этот
topic_id вместо создания ещё одной темы в группе — не более одной
осиротевшей темы на клиента за инцидент, а не по одной на каждый повтор.
Как и FSM-хранилка бота (тоже in-memory), это состояние не переживает
перезапуск процесса — приемлемый компромисс, раз без похода в ту же самую
недоступную сейчас БД персистентную альтернативу всё равно не сделать.
"""


class TopicResolutionError(Exception):
    """
    Поднимается, если тему клиента в рабочей группе не удалось получить или
    создать. reason — machine-readable причина ('not_registered',
    'telegram_error', 'db_error'), по которой вызывающий код сам выбирает
    подходящий пользователю текст (тексты для заявки и вопроса исторически
    отличаются, поэтому текст не зашит внутрь исключения).
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def resolve_client_topic(bot: Bot, user: types.User, group_id: int) -> int:
    topic_id = await get_user_thread_id(user.id)

    if topic_id is not None:
        _pending_topic_ids.pop(user.id, None)
        return topic_id

    topic_id = _pending_topic_ids.get(user.id)

    if topic_id is None:
        topic_name = await get_topic_name(user.id)

        if topic_name is None:
            raise TopicResolutionError('not_registered')

        try:
            topic = await bot.create_forum_topic(chat_id=group_id, name=topic_name)
        except (TelegramBadRequest, TelegramRetryAfter):
            raise TopicResolutionError('telegram_error')

        topic_id = topic.message_thread_id
    else:
        logger.info(
            "Переиспользуем ранее созданную тему topic_id=%s для user_id=%s вместо повторного "
            "создания (прошлая попытка привязать её в БД не удалась)",
            topic_id, user.id
        )

    status = None
    link_error = None

    for attempt in range(1, SET_THREAD_ID_ATTEMPTS + 1):
        try:
            status = await set_user_thread_id(user.id, topic_id)
            link_error = None
            break
        except SQLAlchemyError as error:
            link_error = error

            if attempt < SET_THREAD_ID_ATTEMPTS:
                logger.warning(
                    "Не удалось привязать topic_id=%s к user_id=%s (попытка %s/%s), повтор через %sс: %s",
                    topic_id, user.id, attempt, SET_THREAD_ID_ATTEMPTS,
                    SET_THREAD_ID_RETRY_DELAY_SECONDS, error
                )
                await asyncio.sleep(SET_THREAD_ID_RETRY_DELAY_SECONDS)

    if link_error is not None:
        _pending_topic_ids[user.id] = topic_id
        logger.error(
            "Тема topic_id=%s создана в Telegram для user_id=%s, но привязать её в БД не удалось "
            "после %s попыток (ошибка БД) — тема временно осиротела; следующая попытка клиента "
            "переиспользует этот topic_id вместо создания новой: %s",
            topic_id, user.id, SET_THREAD_ID_ATTEMPTS, link_error
        )
        raise TopicResolutionError('db_error')

    if not status:
        _pending_topic_ids.pop(user.id, None)
        logger.error(
            "Тема topic_id=%s создана в Telegram, но не привязана к user_id=%s (пользователь не найден) — тема осиротела",
            topic_id, user.id
        )
        raise TopicResolutionError('not_registered')

    _pending_topic_ids.pop(user.id, None)
    return topic_id


async def _deliver_client_submission(
    message: types.Message,
    state: FSMContext,
    bot: Bot,
    *,
    user: types.User,
    group_id: int,
    final_text: str,
    appeal_type: str,
    save_text: str,
    telegram_error_text: str,
    db_error_text: str,
    not_registered_text: str,
    undelivered_text: str,
    success_text: str,
    name: str | None = None,
    birthday: str | None = None,
) -> None:
    """
    Общий хвост отправки заявки/вопроса: резолвит (или создаёт) тему клиента,
    шлёт итоговый текст в группу, сохраняет обращение в БД и подтверждает
    клиенту. Используется и save_statement, и save_question, чтобы у обоих
    сценариев было гарантированно одинаковое поведение при любых сбоях.
    """
    try:
        topic_id = await resolve_client_topic(bot, user, group_id)
    except TopicResolutionError as error:
        if error.reason == 'telegram_error':
            text = telegram_error_text
        elif error.reason == 'db_error':
            text = db_error_text
        else:
            text = not_registered_text

        await safe_answer(message, context=f'невозможность создать тему user_id={user.id}', text=text)
        return

    delivered = await safe_send_message(
        bot, group_id,
        context=f'обращение (type={appeal_type}) в группу user_id={user.id} topic_id={topic_id}',
        text=final_text, message_thread_id=topic_id
    )

    if not delivered:
        await safe_answer(message, context=f'уведомление о недоставке user_id={user.id}', text=undelivered_text)
        return

    result = None
    save_error = None

    for attempt in range(1, SAVE_APPEAL_ATTEMPTS + 1):
        try:
            result = await save_user_appeal(user.id, save_text, appeal_type, name=name, birthday=birthday)
            save_error = None
            break
        except SQLAlchemyError as error:
            save_error = error

            if attempt < SAVE_APPEAL_ATTEMPTS:
                logger.warning(
                    "Не удалось сохранить обращение (type=%s) для user_id=%s в БД (попытка %s/%s), "
                    "повтор через %sс: %s",
                    appeal_type, user.id, attempt, SAVE_APPEAL_ATTEMPTS,
                    SAVE_APPEAL_RETRY_DELAY_SECONDS, error
                )
                await asyncio.sleep(SAVE_APPEAL_RETRY_DELAY_SECONDS)

    if save_error is not None:
        logger.error(
            "Обращение (type=%s) отправлено в группу, но не сохранено в БД для user_id=%s "
            "после %s попыток (ошибка БД): %s",
            appeal_type, user.id, SAVE_APPEAL_ATTEMPTS, save_error
        )
        await safe_answer(
            message,
            context=f'ошибка сохранения обращения в БД user_id={user.id}',
            text='Не получилось сохранить обращение из-за временного сбоя. Пожалуйста, отправьте его ещё раз.'
        )
        return

    if not result:
        await safe_answer(
            message,
            context=f'пользователь не найден при сохранении обращения user_id={user.id}',
            text='Не получилось сохранить обращение. Пожалуйста, напишите /start и попробуйте снова.'
        )
        return

    await state.clear()

    await safe_answer(
        message,
        context=f'подтверждение отправки обращения user_id={user.id}',
        text=success_text,
        reply_markup=get_main_keyboard()
    )


WELCOME_MENU_TEXT = (
    "👋 <b>Добро пожаловать!</b>\n\n"
    "💰 Актуальные цены на разборы:\n<b>{price}</b>\n\n"
    "👇 Выберите нужное действие в меню ниже:"
)
DEFAULT_PRICE_FALLBACK = 'Актуальные цены уточняются — напишите нам, и мы подскажем.'


async def send_welcome_menu(message: types.Message, log_context: str) -> None:
    price = await get_price()

    if price is None:
        price = DEFAULT_PRICE_FALLBACK

    await safe_answer(
        message,
        context=f'{log_context} user_id={message.from_user.id}',
        text=WELCOME_MENU_TEXT.format(price=html.escape(price)),
        reply_markup=get_main_keyboard()
    )


@router.message(CommandStart(), F.chat.type == 'private')
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()

    user = message.from_user

    if not message or not user:
        return

    if user.id != Config.ADMIN_ID:
        await add_user(user.id)
        await send_welcome_menu(message, 'приветствие')

        return

    await safe_answer(
        message,
        context=f'приветствие администратора admin_id={user.id}',
        text='Здравствуйте!\n\n'
             'Вам доступны следующие функции:',
        reply_markup=get_admin_keyboard()
    )


@router.message(Command('help'), F.chat.type == 'private')
async def cmd_help(message: types.Message):
    await safe_answer(
        message,
        context=f'справка user_id={message.from_user.id}',
        text=(
            "ℹ️ <b>Справочная информация</b>\n\n"
            "Здесь вы можете оставить заявку на разбор, задать вопрос или узнать о наших услугах. Воспользуйтесь кнопками внизу экрана.")
    )


@router.message(Command('bind'))
async def cmd_bind(message: types.Message, bot: Bot):
    user = message.from_user

    if user is None:
        return

    if message.chat.type == 'private':
        await safe_answer(
            message,
            context=f'/bind из личного чата user_id={user.id}',
            text='Эту команду нужно выполнить в рабочей группе, а не в личном чате.'
        )

        return

    try:
        bot_member = await bot.get_chat_member(chat_id=message.chat.id, user_id=bot.id)
    except TelegramBadRequest as error:
        logger.warning("Не удалось проверить статус бота в чате chat_id=%s: %s", message.chat.id, error)

        await safe_answer(
            message,
            context=f'проверка статуса бота chat_id={message.chat.id}',
            text='Не удалось проверить бота в этом чате. Убедитесь, что бот добавлен в группу, и повторите /bind.'
        )

        return

    if not isinstance(bot_member, types.ChatMemberAdministrator):
        await safe_answer(
            message,
            context=f'запрос прав администратора chat_id={message.chat.id}',
            text='Пожалуйста, сделайте бота администратором группы и повторите /bind.'
        )

        return

    if not bot_member.can_manage_topics:
        await safe_answer(
            message,
            context=f'запрос прав на управление темами chat_id={message.chat.id}',
            text='Пожалуйста, разрешите боту управлять темами (в правах администратора группы) и повторите /bind.'
        )

        return

    if user.id != Config.ADMIN_ID:
        await safe_answer(
            message,
            context=f'запрет /bind для user_id={user.id}',
            text='Эта команда доступна только администратору бота.'
        )

        return

    if not message.chat.is_forum:
        await safe_answer(
            message,
            context=f'запрос на форум/супергруппу chat_id={message.chat.id}',
            text='Пожалуйста, включите темы (Topics) в настройках группы и повторите /bind.'
        )

        return

    is_saved = await save_group_id(message.chat.id)

    if not is_saved:
        await safe_answer(
            message,
            context=f'конфликт привязки группы chat_id={message.chat.id}',
            text='Бот уже привязан к другой группе. Переносить привязку на новую группу через /bind сейчас не поддерживается.'
        )

        return

    await safe_answer(
        message,
        context=f'успешная привязка группы chat_id={message.chat.id}',
        text='Бот успешно привязан к группе!'
    )


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_chat(event: types.ChatMemberUpdated, bot: Bot):
    user = event.from_user

    if user is None:
        return

    if event.chat.type == 'private':
        status = await activated_user(user.id)

        if not status:
            await safe_send_message(
                bot, user.id,
                context=f'приглашение после добавления в чат user_id={user.id}',
                text='Чтобы начать пользоваться ботом, напишите /start'
            )

        return

    if user.id != Config.ADMIN_ID:
        await bot.leave_chat(chat_id=event.chat.id)

        return

    if not event.chat.is_forum:
        await safe_send_message(
            bot, event.chat.id,
            context=f'запрос на включение тем chat_id={event.chat.id}',
            text='Чтобы бот мог работать в этой группе, нужно включить темы (Topics) в её настройках. Бот сейчас выйдет из группы — включите темы и добавьте его снова.'
        )
        await bot.leave_chat(chat_id=event.chat.id)

        return

    group_id = await get_group_id()

    if group_id is not None and group_id != event.chat.id:
        await safe_send_message(
            bot, event.chat.id,
            context=f'конфликт привязки группы chat_id={event.chat.id}',
            text='Бот уже привязан к другой группе, поэтому он сейчас покинет эту.'
        )
        await bot.leave_chat(chat_id=event.chat.id)

        return

    if event.chat.id != group_id:
        await safe_send_message(
            bot, event.chat.id,
            context=f'приглашение выполнить /bind chat_id={event.chat.id}',
            text='Используйте команду /bind, чтобы привязать бота к этой группе.'
        )


@router.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER),
                    F.chat.type == 'private')
async def bot_removed_from_chat(event: types.ChatMemberUpdated):
    user = event.from_user

    if user is None:
        return

    await deactivated_user(user.id)


@router.message(F.text == 'Оставить заявку',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(None))
async def set_name(message: types.Message, state: FSMContext):
    await state.set_state(Form.name)

    await safe_answer(
        message,
        context=f'запрос имени user_id={message.from_user.id}',
        text=(
            "📝 <b>Оформление заявки</b>\n\n"
            "Пожалуйста, введите ваше <b>имя</b>:"),
        reply_markup=get_cancel_keyboard()
    )


@router.message(F.text == 'О нас',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(None))
async def about_us(message: types.Message):
    user = message.from_user

    if user is None:
        return

    about_us_text = await get_about_us()

    if about_us_text is None:
        await safe_answer(
            message,
            context=f'отсутствие описания "О нас" user_id={user.id}',
            text='Информация о нас пока не заполнена. Пожалуйста, задайте вопрос — мы ответим лично.'
        )

        return

    await safe_answer(
        message,
        context=f'текст "О нас" user_id={user.id}',
        text=f'ℹ️ <b>О нас</b>\n\n{html.escape(about_us_text)}'
    )


@router.message(F.text == 'Задать вопрос',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(None))
async def question_text(message: types.Message, state: FSMContext):
    await state.set_state(Question.question)

    await safe_answer(
        message,
        context=f'запрос вопроса user_id={message.from_user.id}',
        text='❓ <b>Ваш вопрос</b>\n\nНапишите, что вас интересует:',
        reply_markup=get_cancel_keyboard()
    )


@router.message(F.text == 'Сделать рассылку',
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID,
                StateFilter(None))
async def newsletter(message: types.Message, state: FSMContext):
    await state.set_state(Newsletter.text)

    await safe_answer(
        message,
        context=f'запрос текста рассылки admin_id={message.from_user.id}',
        text='Введите текст рассылки:',
        reply_markup=get_cancel_keyboard()
    )


@router.message(F.text == 'Изменить «О нас»',
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID,
                StateFilter(None))
async def change_about_us(message: types.Message, state: FSMContext):
    await state.set_state(ChangeAboutUs.about_us_text)

    await safe_answer(
        message,
        context=f'запрос нового описания admin_id={message.from_user.id}',
        text='Напишите новый текст раздела «О нас» — описание вашего сервиса и услуг:'
    )


@router.message(F.text == 'Изменить прайс',
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID,
                StateFilter(None))
async def change_price(message: types.Message, state: FSMContext):
    await state.set_state(ChangePrice.price)

    await safe_answer(
        message,
        context=f'запрос нового прайса admin_id={message.from_user.id}',
        text='Напишите ваш прайс:'
    )


@router.message(F.text == 'Как пользоваться ботом?',
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID,
                StateFilter(None))
async def admin_instruction(message: types.Message):
    instruction = (
        '👩‍💼 <b>Как пользоваться ботом</b>\n\n'

        '<b>1. Подключение рабочей группы</b>\n'
        '• Добавьте бота в Telegram-группу с включёнными темами.\n'
        '• Сделайте бота администратором и разрешите ему управлять темами.\n'
        '• В этой группе отправьте команду /bind.\n'
        '• После успешной привязки заявки и вопросы клиентов будут приходить туда.\n\n'

        '<b>2. Работа с клиентами</b>\n'
        '• Каждый клиент после /start регистрируется в базе бота.\n'
        '• Для каждого клиента бот создаёт отдельную тему в рабочей группе '
        'и затем использует её повторно.\n'
        '• Тексты заявок и вопросов сохраняются в базе данных.\n'
        '• Чтобы ответить клиенту, откройте его тему и используйте '
        '<b>«Ответить / Reply»</b> именно на сообщение бота с заявкой или вопросом.\n'
        '• Обычное сообщение в теме клиенту автоматически не отправляется.\n\n'

        '<b>3. Прайс и раздел «О нас»</b>\n'
        '• Кнопка «Изменить прайс» меняет прайс, который видят пользователи.\n'
        '• Кнопка Изменить «О нас» меняет описание сервиса.\n'
        '• Эти настройки хранятся в базе данных и не удаляются при обычном '
        'перезапуске самого бота.\n\n'

        '<b>4. Рассылка</b>\n'
        '• Нажмите «Сделать рассылку» и отправьте текст.\n'
        '• Бот покажет предпросмотр. После этого рассылку можно подтвердить '
        'или вернуться к изменению текста.\n'
        '• Рассылка отправляется активным пользователям.\n'
        '• Если пользователь заблокировал бота, он помечается неактивным.\n'
        '• Если Telegram временно ограничит скорость отправки, бот подождёт '
        'и повторит попытку автоматически.\n\n'

        '<b>5. Важно про группу и темы</b>\n'
        '⚠️ В текущей версии автоматическая смена привязанной группы не предусмотрена.\n'
        'Удаление бота из группы <b>не удаляет</b> пользователей, обращения, '
        'прайс и описание из базы, но привязка к старой группе остаётся сохранённой.\n\n'
        'Если бота случайно удалили, лучше добавить его обратно <b>в ту же группу</b> '
        'и снова выдать права администратора и право управления темами.\n\n'
        '⚠️ <b>Не удаляйте темы клиентов вручную.</b> '
        'Бот запоминает тему клиента и рассчитывает, что она продолжает существовать. '
        'Если рабочую группу нужно полностью заменить или тема клиента была удалена, '
        'потребуется техническая перенастройка.\n\n'

        '<b>6. Какие данные сохраняются</b>\n'
        'Бот хранит пользователей, их активность, ID клиентских тем, '
        'тексты заявок и вопросов, текущий прайс, текст «О нас» '
        'и ID рабочей группы.\n\n'

        'Если бот перезапустится, данные из базы сохранятся. '
        'Пользователь, который в этот момент находился посередине заполнения формы, '
        'может потерять только текущий незавершённый шаг и начать заполнение заново.'
    )

    await safe_answer(
        message,
        context=f'инструкция admin_id={message.from_user.id}',
        text=instruction,
        reply_markup=get_admin_keyboard()
    )


@router.message(F.text == 'Меню', F.chat.type == 'private')
async def menu(message: types.Message, state: FSMContext):
    await state.clear()

    user = message.from_user

    if user is None:
        return

    if user.id != Config.ADMIN_ID:
        await send_welcome_menu(message, 'меню')

        return

    await safe_answer(
        message,
        context=f'меню администратора admin_id={user.id}',
        text='Главное меню администратора.',
        reply_markup=get_admin_keyboard()
    )


@router.message(F.text == 'Назад',
                F.chat.type == 'private',
                StateFilter(None, Form.text, Form.birthday))
async def back(message: types.Message, state: FSMContext):
    current_state = await state.get_state()

    if current_state is None:
        await send_welcome_menu(message, 'меню (Назад)')

        return

    if current_state == Form.text.state:
        await state.set_state(Form.birthday)

        await safe_answer(
            message,
            context=f'Назад к дате рождения user_id={message.from_user.id}',
            text='📅 Введите вашу <b>дату рождения</b> в формате <code>ДД/ММ/ГГГГ</code> (например, <i>05/12/1984</i>):'
        )

    elif current_state == Form.birthday.state:
        await state.set_state(Form.name)

        await safe_answer(
            message,
            context=f'Назад к имени user_id={message.from_user.id}',
            text=(
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
        await safe_answer(
            message,
            context=f'отсутствие привязки группы admin_id={message.from_user.id}',
            text='Бот пока не привязан ни к одной группе. Выполните /bind.'
        )

        return

    if group_id != message.chat.id:
        await safe_answer(
            message,
            context=f'несовпадение группы admin_id={message.from_user.id}',
            text='Этот чат — не та группа, к которой привязан бот, поэтому ответ не будет доставлен клиенту.'
        )

        return

    original_message = message.reply_to_message

    if original_message is None:
        return

    if original_message.from_user is None or original_message.from_user.id != bot.id:
        await safe_answer(
            message,
            context=f'reply не на сообщение бота admin_id={message.from_user.id}',
            text='Чтобы ответ дошёл клиенту, используйте Reply именно на сообщение бота с заявкой или вопросом.'
        )
        return

    if not original_message.text:
        await safe_answer(
            message,
            context=f'reply на нетекстовое сообщение бота admin_id={message.from_user.id}',
            text='Сообщение, на которое вы ответили, не содержит текста. Чтобы ответ дошёл клиенту, '
                 'используйте Reply на текстовое сообщение бота с заявкой или вопросом клиента.'
        )
        return

    user_id = await get_user_id(message.message_thread_id)

    if not user_id:
        await safe_answer(
            message,
            context=f'незарегистрированный пользователь thread_id={message.message_thread_id}',
            text='Клиент, привязанный к этой теме, не найден в базе бота — ответ не может быть доставлен.'
        )

        return

    original_text = original_message.text

    if len(original_text) > 2500:
        original_text = original_text[:2500] + '…'

    context_text = (
        f"📩 <b>Ответ администратора на ваше обращение:</b>\n"
        f"<i>{html.escape(original_text)}</i>\n\n"
        f"💬 <b>Сообщение администратора:</b>"
    )

    try:
        await bot.send_message(chat_id=user_id, text=context_text)
    except TelegramForbiddenError as error:
        logger.error("Доставка ответа админа пользователю user_id=%s не удалась (пользователь заблокировал бота): %s", user_id, error)
        await safe_answer(message, context=f'уведомление о блокировке admin_id={message.from_user.id}',
                           text='Пользователь заблокировал бота, поэтому ваше сообщение не доставлено.')
        return
    except TelegramAPIError as error:
        logger.error("Доставка ответа админа пользователю user_id=%s не удалась: %s", user_id, error)
        await safe_answer(message, context=f'уведомление о недоставке admin_id={message.from_user.id}',
                           text='Не удалось доставить сообщение клиенту. Попробуйте отправить его ещё раз чуть позже.')
        return

    try:
        await message.copy_to(chat_id=user_id)
    except TelegramForbiddenError as error:
        logger.error("Доставка ответа админа пользователю user_id=%s не удалась (пользователь заблокировал бота): %s", user_id, error)
        await safe_answer(message, context=f'уведомление о блокировке (содержимое) admin_id={message.from_user.id}',
                           text='Пользователь заблокировал бота, поэтому ваше сообщение не доставлено.')
    except TelegramAPIError as error:
        logger.error(
            "Доставка содержимого ответа админа пользователю user_id=%s не удалась после отправки заголовка: %s",
            user_id, error
        )
        await safe_answer(message, context=f'уведомление о недоставке содержимого admin_id={message.from_user.id}',
                           text='Не удалось доставить сообщение клиенту. Попробуйте отправить его ещё раз чуть позже.')

        await safe_send_message(
            bot, user_id,
            context=f'уведомление клиента о частичной недоставке user_id={user_id}',
            text='⚠️ К сожалению, основное содержимое ответа администратора не удалось доставить. '
                 'Пожалуйста, напишите нам ещё раз, если вопрос остался открытым.'
        )


@router.message(Form.name,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def set_birthday(message: types.Message, state: FSMContext):
    if not message or not message.text:
        await safe_answer(
            message,
            context=f'нераспознанное имя user_id={message.from_user.id}',
            text="⚠️ <i>Вы ничего не написали. Пожалуйста, введите ваше имя:</i>"
        )

        return

    if len(message.text) > NAME_MAX_LENGTH:
        await safe_answer(
            message,
            context=f'слишком длинное имя user_id={message.from_user.id}',
            text=(
                f"⚠️ <i>Введённое имя слишком длинное (максимум {NAME_MAX_LENGTH} "
                f"символов). Пожалуйста, введите имя короче:</i>")
        )

        return

    await state.update_data(name=message.text)
    await state.set_state(Form.birthday)

    await safe_answer(
        message,
        context=f'запрос даты рождения user_id={message.from_user.id}',
        text=(
            "📅 Отлично! Теперь введите вашу <b>дату рождения</b>.\n\n"
            "Используйте формат <code>ДД/ММ/ГГГГ</code> (например, <i>05/12/1984</i>):"),
        reply_markup=get_back_cancel_keyboard()
    )


@router.message(Form.birthday,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def set_text(message: types.Message, state: FSMContext):
    if not message or not message.text:
        await safe_answer(
            message,
            context=f'нераспознанная дата рождения user_id={message.from_user.id}',
            text=(
                "⚠️ <b>Неверный формат даты</b>\n\n"
                "Пожалуйста, введите дату в формате <code>ДД/ММ/ГГГГ</code>, например: <i>05/12/1984</i>")
        )
        return

    try:
        birthday = datetime.strptime(message.text, BIRTHDAY_FORMAT)
    except ValueError:
        await safe_answer(
            message,
            context=f'некорректная дата рождения user_id={message.from_user.id}',
            text=(
                "⚠️ <b>Некорректная дата</b>\n\n"
                "Пожалуйста, введите существующую дату в формате <code>ДД/ММ/ГГГГ</code>\n"
                "<i>Пример: 05/12/1984</i>")
        )
        return

    # Сохраняем дату в каноническом виде strftime, а не сырой ввод пользователя:
    # %Y при разборе не ограничен строго 4 цифрами, а Requests.birthday — String(10).
    await state.update_data(birthday=birthday.strftime(BIRTHDAY_FORMAT))
    await state.set_state(Form.text)

    await safe_answer(
        message,
        context=f'запрос текста обращения user_id={message.from_user.id}',
        text=(
            "✍️ <b>Текст обращения</b>\n\n"
            "Опишите, пожалуйста, суть вашей заявки — какой разбор вас интересует и что важно учесть:"),
        reply_markup=get_back_cancel_keyboard()
    )


@router.message(Form.text,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def save_statement(message: types.Message, state: FSMContext, bot: Bot):
    if not message or not message.text:
        await safe_answer(
            message,
            context=f'нераспознанный текст заявки user_id={message.from_user.id}',
            text="⚠️ <i>Пожалуйста, отправьте текст обращения сообщением (не фото и не файлом):</i>"
        )
        return

    await state.update_data(text=message.text)
    data = await state.get_data()

    final_text = (
        f"🔔 <b>НОВАЯ ЗАЯВКА</b>\n\n"
        f"👤 <b>Имя:</b> {html.escape(data['name'])}\n"
        f"📅 <b>Дата рождения:</b> {html.escape(data['birthday'])}\n\n"
        f"💬 <b>Обращение:</b>\n"
        f"<i>{html.escape(data['text'])}</i>"
    )

    overflow = exceeds_telegram_limit(final_text)

    if overflow:
        await safe_answer(
            message,
            context=f'превышен лимит длины заявки user_id={message.from_user.id}',
            text=(
                f"⚠️ Текст обращения слишком длинный (примерно на {overflow} символов). "
                f"Пожалуйста, сократите его и отправьте ещё раз."
            )
        )
        return

    user = message.from_user

    if not user:
        return

    group_id = await get_group_id()

    if group_id is None:
        await safe_answer(
            message,
            context=f'группа не привязана (заявка) user_id={user.id}',
            text='Бот временно не работает, попробуйте позже.'
        )
        return

    await _deliver_client_submission(
        message, state, bot,
        user=user, group_id=group_id, final_text=final_text,
        appeal_type='Bid', save_text=message.text,
        name=data['name'], birthday=data['birthday'],
        telegram_error_text='К сожалению, сейчас не получилось отправить заявку. Пожалуйста, попробуйте ещё раз чуть позже.',
        db_error_text='Не получилось обработать вашу заявку. Пожалуйста, отправьте её ещё раз через несколько минут.',
        not_registered_text='Вы ещё не зарегистрированы в боте. Пожалуйста, напишите /start, чтобы начать.',
        undelivered_text='Ваша заявка не была доставлена. Попробуйте отправить её ещё раз чуть позже.',
        success_text=(
            "✅ <b>Ваша заявка успешно отправлена!</b>\n\n"
            "Администратор ознакомится с ней и ответит вам прямо здесь.\n\n"
            "<i>Чтобы написать еще раз, выберите действие в меню.</i>"
        ),
    )


@router.message(Question.question,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def save_question(message: types.Message, state: FSMContext, bot: Bot):
    if not message or not message.text:
        await safe_answer(
            message,
            context=f'нераспознанный текст вопроса user_id={message.from_user.id}',
            text='Пожалуйста, отправьте вопрос текстовым сообщением.'
        )
        return

    await state.update_data(question=message.text)
    data = await state.get_data()

    final_text = f'Новый вопрос!\n\n{html.escape(data["question"])}'

    overflow = exceeds_telegram_limit(final_text)

    if overflow:
        await safe_answer(
            message,
            context=f'превышен лимит длины вопроса user_id={message.from_user.id}',
            text=(
                f"⚠️ Текст вопроса слишком длинный (примерно на {overflow} символов). "
                f"Пожалуйста, сократите его и отправьте ещё раз."
            )
        )
        return

    user = message.from_user

    if user is None:
        return

    group_id = await get_group_id()

    if group_id is None:
        await safe_answer(
            message,
            context=f'группа не привязана (вопрос) user_id={user.id}',
            text='Бот временно не работает, попробуйте позже.'
        )
        return

    await _deliver_client_submission(
        message, state, bot,
        user=user, group_id=group_id, final_text=final_text,
        appeal_type='Question', save_text=message.text,
        telegram_error_text='К сожалению, сейчас не получилось отправить вопрос. Пожалуйста, попробуйте ещё раз чуть позже.',
        db_error_text='Не получилось обработать ваш вопрос. Пожалуйста, отправьте его ещё раз через несколько минут.',
        not_registered_text='Вы ещё не зарегистрированы в боте. Пожалуйста, напишите /start, чтобы начать.',
        undelivered_text='Ваш вопрос не был доставлен. Попробуйте отправить его ещё раз чуть позже.',
        success_text=(
            "✅ <b>Ваш вопрос успешно отправлен!</b>\n\n"
            "Администратор ознакомится с ним и ответит вам прямо здесь.\n\n"
            "<i>Чтобы написать еще раз, выберите действие в меню.</i>"
        ),
    )


@router.message(Newsletter.text,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def send_newsletter(message: types.Message, state: FSMContext):
    if not message or not message.text:
        await safe_answer(
            message,
            context=f'нераспознанный текст рассылки admin_id={message.from_user.id}',
            text='Пожалуйста, отправьте текст рассылки текстовым сообщением.'
        )

        return

    overflow = exceeds_telegram_limit(html.escape(message.text))

    if overflow:
        await safe_answer(
            message,
            context=f'превышен лимит длины рассылки admin_id={message.from_user.id}',
            text=(
                f"⚠️ Текст рассылки слишком длинный (примерно на {overflow} символов сверх лимита Telegram). "
                f"Пожалуйста, сократите текст."
            )
        )

        return

    await state.update_data(newsletter=message.text)
    await state.set_state(Newsletter.sure)

    await safe_answer(
        message,
        context=f'запрос подтверждения рассылки admin_id={message.from_user.id}',
        text=f'Подтвердите отправку рассылки.\n\n'
             f'Сообщение будет отправлено всем активным клиентам бота. Вот как оно выглядит:\n',
        reply_markup=get_sure_keyboard()
    )
    await safe_answer(
        message,
        context=f'предпросмотр рассылки admin_id={message.from_user.id}',
        text=f'{html.escape(message.text)}'
    )


@router.message(Newsletter.sure,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def accept_newsletter(message: types.Message, state: FSMContext, bot: Bot):
    if not message or not message.text or message.text not in ['Подтвердить', 'Изменить']:
        await safe_answer(
            message,
            context=f'некорректный вариант подтверждения рассылки admin_id={message.from_user.id}',
            text='Пожалуйста, воспользуйтесь кнопками «Подтвердить» или «Изменить».'
        )

        return

    elif message.text == 'Подтвердить':
        data = await state.get_data()
        await state.clear()

        users = await get_users()

        if users is None:
            await safe_answer(
                message,
                context=f'отсутствие пользователей для рассылки admin_id={message.from_user.id}',
                text='У бота пока нет ни одного пользователя, поэтому рассылку отправить некому.',
                reply_markup=get_admin_keyboard()
            )

            return

        sent = 0
        not_sent = 0

        for telegram_id, is_active in users:
            if not is_active:
                continue

            recipient_key = StorageKey(bot_id=bot.id, chat_id=telegram_id, user_id=telegram_id)
            recipient_state = await state.storage.get_state(recipient_key)
            keyboard = get_main_keyboard() if recipient_state is None else None

            try:
                await bot.send_message(chat_id=telegram_id,
                                       text=html.escape(data['newsletter']), reply_markup=keyboard)
                sent += 1
            except TelegramForbiddenError as error:
                logger.info("Рассылка: доставка пользователю user_id=%s не удалась (заблокировал бота): %s", telegram_id, error)
                not_sent += 1
                await deactivated_user(telegram_id)
            except TelegramAPIError as error:
                logger.warning("Рассылка: доставка пользователю user_id=%s не удалась: %s", telegram_id, error)
                not_sent += 1

            await asyncio.sleep(0.05)

        await safe_answer(
            message,
            context=f'итоги рассылки admin_id={message.from_user.id}',
            text=f'Рассылка завершена\n\n'
                 f'Отправлено: {sent}\n'
                 f'Не доставлено (не отправлено): {not_sent}\n\n'
                 f'Всего пользователей в базе: {len(users)}\n'
                 f'Из них активных: {sent + not_sent}',
            reply_markup=get_admin_keyboard()
        )

        return

    elif message.text == 'Изменить':
        await state.set_state(Newsletter.text)

        await safe_answer(
            message,
            context=f'повторный запрос текста рассылки admin_id={message.from_user.id}',
            text='Введите текст рассылки:',
            reply_markup=get_cancel_keyboard()
        )

        return


@router.message(ChangeAboutUs.about_us_text,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def set_about_us(message: types.Message, state: FSMContext):
    if not message or not message.text:
        await safe_answer(
            message,
            context=f'нераспознанный текст "О нас" admin_id={message.from_user.id}',
            text='⚠️ <i>Пожалуйста, отправьте описание текстовым сообщением:</i>'
        )

        return

    await state.update_data(about_us=message.text)
    data = await state.get_data()

    await state.clear()

    status = await set_about_us_text(data['about_us'])

    if not status:
        await safe_answer(
            message,
            context=f'ошибка сохранения "О нас" admin_id={message.from_user.id}',
            text='Текст не удалось сохранить. Пожалуйста, попробуйте ещё раз.'
        )

        return

    await safe_answer(
        message,
        context=f'успешное изменение "О нас" admin_id={message.from_user.id}',
        text='Текст успешно изменён!',
        reply_markup=get_admin_keyboard()
    )


@router.message(ChangePrice.price,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def set_price_text(message: types.Message, state: FSMContext):
    if not message or not message.text:
        await safe_answer(
            message,
            context=f'нераспознанный текст прайса admin_id={message.from_user.id}',
            text='⚠️ <i>Пожалуйста, отправьте прайс текстовым сообщением:</i>'
        )

        return

    await state.update_data(price=message.text)
    data = await state.get_data()

    await state.clear()

    status = await set_price(data['price'])

    if not status:
        await safe_answer(
            message,
            context=f'ошибка сохранения прайса admin_id={message.from_user.id}',
            text='Прайс не удалось сохранить. Пожалуйста, попробуйте ещё раз.'
        )

        return

    await safe_answer(
        message,
        context=f'успешное изменение прайса admin_id={message.from_user.id}',
        text='Прайс успешно изменён!',
        reply_markup=get_admin_keyboard()
    )
