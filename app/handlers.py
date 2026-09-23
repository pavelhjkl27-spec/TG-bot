import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from datetime import datetime, timedelta, timezone
import html
import logging
import secrets

from aiogram import Router, types, F, Bot
from aiogram.enums import ContentType
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy.exc import SQLAlchemyError

from app.callbacks import DialogCallback, NewsletterCallback, OrderStatusCallback, SettingsCallback
from app.keyboards import (get_main_keyboard,
                           get_cancel_keyboard,
                           get_back_cancel_keyboard,
                           get_admin_keyboard, get_newsletter_confirm_markup,
                           get_dialog_waiting_keyboard, get_dialog_active_keyboard,
                           get_dialog_request_markup, get_dialog_status_markup,
                           get_order_status_markup, get_settings_confirm_markup)
from app.states import Form, Question, Newsletter, ChangeAboutUs, ChangePrice, Dialog
from config import Config
from app.db_requests import (add_user,
                             save_user_appeal,
                             get_user_thread_id,
                             get_topic_name,
                             set_user_thread_id, clear_user_thread_id, get_user_id,
                             save_group_id, get_group_id,
                             get_about_us, get_users,
                             activated_user, deactivated_user,
                             set_about_us_text, get_price,
                             set_price, get_reply_target_by_group_message_id,
                             get_bid_history_by_thread_id,
                             transition_request_status, get_bid_card_by_group_message_id,
                             get_idle_fsm_rows)
from app.utils import (TELEGRAM_MESSAGE_LIMIT, exceeds_telegram_limit, is_dead_topic_error, safe_answer,
                       safe_send_message, send_message_capturing_error, safe_edit_message_text,
                       safe_answer_callback, telegram_text_length, format_client_date)

logger = logging.getLogger(__name__)

router = Router()

NAME_MAX_LENGTH = 200
BIRTHDAY_FORMAT = '%d/%m/%Y'

SET_THREAD_ID_ATTEMPTS = 3
SET_THREAD_ID_RETRY_DELAY_SECONDS = 0.5
SAVE_APPEAL_ATTEMPTS = 3
SAVE_APPEAL_RETRY_DELAY_SECONDS = 0.5

CLIENT_QUOTE_MAX_LENGTH = 150
HISTORY_PREVIEW_MAX_LENGTH = 150

# Типы контента, для которых Bot API вообще поддерживает caption у copyMessage —
# для остальных (стикеры, video_note и т.п.) caption физически некуда передать.
CAPTION_CAPABLE_CONTENT_TYPES = {
    ContentType.DOCUMENT, ContentType.PHOTO, ContentType.VIDEO,
    ContentType.AUDIO, ContentType.VOICE, ContentType.ANIMATION,
}
DOCUMENT_FALLBACK_CAPTION = 'Ваш разбор готов, файл прикреплён ниже 📎'
NEUTRAL_FALLBACK_CAPTION = 'Сообщение от администратора'

_pending_topic_ids: dict[int, int] = {}
"""
Тема, которая реально создана в Telegram, но ещё не подтверждена записью в
Users.topic_id из-за сбоя set_user_thread_id (даже после ретраев). Пока
процесс бота жив, следующая попытка того же клиента переиспользует этот
topic_id вместо создания ещё одной темы в группе — не более одной
осиротевшей темы на клиента за инцидент, а не по одной на каждый повтор.
В отличие от FSM-хранилища бота (оно в Postgres), это состояние живёт только в памяти
и не переживает перезапуск процесса — приемлемый компромисс, раз без похода в ту же
самую недоступную сейчас БД персистентную альтернативу всё равно не сделать.
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
        except TelegramAPIError as error:
            # Именно TelegramAPIError, а не (TelegramBadRequest, TelegramRetryAfter):
            # TelegramForbiddenError (бота выгнали из группы), TelegramNotFound (группы
            # больше нет), TelegramNetworkError (обычный таймаут до api.telegram.org) и
            # TelegramServerError — БРАТЬЯ TelegramBadRequest в иерархии aiogram, а не его
            # наследники, поэтому раньше пролетали мимо и оставляли клиента вообще без
            # ответа. TelegramRetryAfter отдельной ветки не требует: ожидание и до трёх
            # повторов делает retry_after_middleware на уровне сессии бота (run.py), и
            # сюда исключение доходит уже с исчерпанными попытками.
            logger.error(
                "Не удалось создать тему '%s' для user_id=%s в группе group_id=%s: %s: %s",
                topic_name, user.id, group_id, type(error).__name__, error
            )
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


DEAD_TOPIC_ADMIN_NOTICE = (
    "⚠️ <b>Тема клиента недоступна</b>\n\n"
    "Обращение клиента <b>{client}</b> (id <code>{user_id}</code>) не удалось доставить: его тема "
    "в группе больше не существует — скорее всего, её удалили вручную.\n\n"
    "Привязка к удалённой теме сброшена: при следующей попытке клиента бот создаст ему новую тему. "
    "Клиента уже попросили отправить обращение ещё раз, само обращение сейчас <b>не сохранено</b>.\n\n"
    "<i>Переписка из удалённой темы не восстанавливается.</i>"
)
DEAD_TOPIC_TIMEOUT_ADMIN_NOTICE = (
    "⚠️ <b>Тема клиента недоступна</b>\n\n"
    "{event}, но уведомление об этом в его тему не доставлено: темы в группе больше не существует — "
    "скорее всего, её удалили вручную.\n\n"
    "Привязка к удалённой теме сброшена: при следующем обращении клиента бот создаст ему новую тему. "
    "Клиенту о закрытии бот сообщает в личке, как обычно.\n\n"
    "<i>Переписка из удалённой темы не восстанавливается.</i>"
)
ATTACHMENT_UNDELIVERED_CARD_NOTE = (
    '⚠️ Вложение не доставлено — обращение не сохранено, клиента попросили отправить его ещё раз.'
)


async def _heal_dead_topic(bot: Bot, client_id: int, topic_id: int, admin_notice: str) -> None:
    """
    Тема клиента есть в БД, но в Telegram её уже нет (удалили вручную). Без
    сброса привязки каждое следующее обращение этого клиента падало бы в ту же
    несуществующую тему — бесконечный цикл «не доставлено» без шанса
    самовосстановиться. Сбрасываем Users.topic_id (следующая попытка пойдёт
    штатным путём resolve_client_topic и создаст новую тему) и сообщаем админу,
    чтобы он не узнал о поломке от клиента. admin_notice — готовый текст этого
    сообщения: у каждого вызывающего своя ситуация (DEAD_TOPIC_*_ADMIN_NOTICE).
    """
    logger.error(
        "Тема topic_id=%s клиента user_id=%s недоступна в Telegram (удалена?) — сбрасываем привязку, "
        "следующее обращение создаст новую тему",
        topic_id, client_id
    )

    try:
        cleared = await clear_user_thread_id(client_id, topic_id)
    except SQLAlchemyError as error:
        logger.error(
            "Не удалось сбросить topic_id=%s у user_id=%s после недоступной темы (ошибка БД): %s",
            topic_id, client_id, error
        )
        return

    if not cleared:
        # Привязку уже сбросила (или заменила) параллельная попытка того же клиента —
        # второе уведомление админу о том же инциденте не нужно.
        logger.info(
            "topic_id=%s у user_id=%s к моменту сброса уже был изменён — уведомление админу не дублируем",
            topic_id, client_id
        )
        return

    await safe_send_message(
        bot, Config.ADMIN_ID,
        context=f'уведомление админу о недоступной теме user_id={client_id} topic_id={topic_id}',
        text=admin_notice
    )


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
    reply_markup: types.InlineKeyboardMarkup | None = None,
    attachment: types.Message | None = None,
) -> None:
    """
    Общий хвост отправки заявки/вопроса: резолвит (или создаёт) тему клиента,
    шлёт итоговый текст в группу, сохраняет обращение в БД и подтверждает
    клиенту. Используется и save_statement, и save_question, чтобы у обоих
    сценариев было гарантированно одинаковое поведение при любых сбоях.
    attachment — сообщение клиента с вложением (фото/документ вопроса): копируется
    в тему ответом на карточку; не дошло — обращение не сохраняется, как при недоставке карточки.
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

    delivered_message, send_error = await send_message_capturing_error(
        bot, group_id,
        context=f'обращение (type={appeal_type}) в группу user_id={user.id} topic_id={topic_id}',
        text=final_text, message_thread_id=topic_id, reply_markup=reply_markup
    )

    if not delivered_message:
        if send_error is not None and is_dead_topic_error(send_error):
            await _heal_dead_topic(
                bot, user.id, topic_id,
                DEAD_TOPIC_ADMIN_NOTICE.format(client=html.escape(user.full_name), user_id=user.id)
            )

        await safe_answer(message, context=f'уведомление о недоставке user_id={user.id}', text=undelivered_text)
        return

    if attachment is not None:
        try:
            await attachment.copy_to(
                chat_id=group_id, message_thread_id=topic_id,
                reply_parameters=types.ReplyParameters(message_id=delivered_message.message_id)
            )
        except TelegramAPIError as error:
            logger.warning(
                "Вложение обращения (type=%s) user_id=%s не скопировано в тему topic_id=%s: %s",
                appeal_type, user.id, topic_id, error
            )
            # Карточка уже в теме, но без вложения и без записи в БД — помечаем, чтобы админ не отвечал на неё.
            await safe_edit_message_text(
                bot, group_id, delivered_message.message_id,
                context=f'пометка карточки без вложения user_id={user.id}',
                text=f'{final_text}\n\n{ATTACHMENT_UNDELIVERED_CARD_NOTE}'
            )
            await safe_answer(message, context=f'вложение не доставлено user_id={user.id}', text=undelivered_text)
            return

    result = None
    save_error = None

    for attempt in range(1, SAVE_APPEAL_ATTEMPTS + 1):
        try:
            result = await save_user_appeal(
                user.id, save_text, appeal_type, name=name, birthday=birthday,
                group_message_id=delivered_message.message_id
            )
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


BID_CARD_HEADER = "🔔 <b>НОВАЯ ЗАЯВКА</b>\n\n"
BID_CARD_TRUNCATED = '…'

ORDER_STATUS_LABELS = {
    'new': '🆕 Новая',
    'in_progress': '🛠 В работе',
    'done': '✔️ Готово',
}


def _bid_card_text(name: str, birthday: str, text: str, status: str | None = None) -> str:
    """
    Карточка заявки в теме клиента. Без status (или со status='new') — ровно тот текст, что
    уходит при подаче заявки. Для in_progress/done добавляется строка статуса; если с ней карточка
    перестаёт влезать в лимит Telegram (текст заявки проверен на лимит без неё), обрезается только
    показ текста клиента — по исходным символам, чтобы не разорвать HTML-сущность.
    """
    prefix = (
        f"{BID_CARD_HEADER}"
        f"👤 <b>Имя:</b> {html.escape(name)}\n"
        f"📅 <b>Дата рождения:</b> {html.escape(birthday)}\n\n"
        f"💬 <b>Обращение:</b>\n"
    )
    status_part = '' if status in (None, 'new') \
        else f"\n\n📌 <b>Статус:</b> {ORDER_STATUS_LABELS.get(status, status)}"
    escaped = html.escape(text)

    # Без строки статуса текст не обрезается: save_statement проверяет лимит именно на нём.
    if not status_part or telegram_text_length(f"{prefix}<i>{escaped}</i>{status_part}") <= TELEGRAM_MESSAGE_LIMIT:
        return f"{prefix}<i>{escaped}</i>{status_part}"

    budget = TELEGRAM_MESSAGE_LIMIT - telegram_text_length(f"{prefix}<i>{BID_CARD_TRUNCATED}</i>{status_part}")
    pieces = []
    used = 0

    for char in text:
        escaped_char = html.escape(char)

        if used + telegram_text_length(escaped_char) > budget:
            break

        pieces.append(escaped_char)
        used += telegram_text_length(escaped_char)

    return f"{prefix}<i>{''.join(pieces)}{BID_CARD_TRUNCATED}</i>{status_part}"


WELCOME_MENU_TEXT = (
    "👋 <b>Добро пожаловать!</b>\n\n"
    "💰 Актуальные цены на разборы:\n<b>{price}</b>\n\n"
    "📝 <b>Как это работает:</b> вы оставляете заявку — администратор напишет вам в этот чат, "
    "чтобы уточнить детали и прислать реквизиты для оплаты. Разбор обычно готов в течение нескольких часов.\n\n"
    "👇 Выберите нужное действие в меню ниже:"
)
DEFAULT_PRICE_FALLBACK = 'Актуальные цены уточняются — напишите нам, и мы подскажем.'


PRICE_TOO_LONG_TEXT = (
    '⚠️ Текст слишком длинный, сообщение с прайсом не поместится в лимит Telegram — '
    'сократите текст и отправьте заново.'
)


def render_welcome_text(price: str | None) -> str:
    """
    Единственная сборка текста «приветствие + прайс»: её используют отправка клиенту
    (приветствие и «Показать прайс») и проверка длины при сохранении нового прайса.
    price=None — прайс не задан, подставляется DEFAULT_PRICE_FALLBACK.
    """
    if price is None:
        price = DEFAULT_PRICE_FALLBACK

    return WELCOME_MENU_TEXT.format(price=html.escape(price))


def exceeds_welcome_limit(price: str) -> bool:
    return telegram_text_length(render_welcome_text(price)) > TELEGRAM_MESSAGE_LIMIT


ABOUT_US_FALLBACK = 'Информация о нас пока не заполнена. Пожалуйста, задайте вопрос — мы ответим лично.'
ABOUT_US_TOO_LONG_TEXT = (
    '⚠️ Текст слишком длинный, раздел «О нас» не поместится в лимит Telegram — '
    'сократите текст и отправьте заново.'
)


def render_about_us_text(about_us: str | None) -> str:
    """
    Единственная сборка раздела «О нас» так, как его видит клиент (кнопка «О нас»); ею же
    строится превью при правке и проверка длины. about_us=None — описание не задано.
    """
    if about_us is None:
        return ABOUT_US_FALLBACK

    return f'ℹ️ <b>О нас</b>\n\n{html.escape(about_us)}'


def exceeds_about_us_limit(about_us: str) -> bool:
    return telegram_text_length(render_about_us_text(about_us)) > TELEGRAM_MESSAGE_LIMIT


async def send_welcome_menu(message: types.Message, log_context: str) -> None:
    price = await get_price()

    await safe_answer(
        message,
        context=f'{log_context} user_id={message.from_user.id}',
        text=render_welcome_text(price),
        reply_markup=get_main_keyboard()
    )


# ------------------------------------------------------------ Диалог клиент↔админ
#
# Источник правды — FSM клиента (Dialog.waiting / Dialog.active, в data: dialog_id,
# request_message_id, status_message_id). Тема клиента — существующая привязка
# Users.topic_id. Все переходы, которые может одновременно сделать другая сторона
# (клиент в личке и админ в группе живут под разными блокировками SimpleEventIsolation),
# идут через атомарный PostgresStorage.transition_state: уведомления шлёт только тот, кто
# выиграл переход, проигравший молчит (callback при этом всё равно получает answer).
#
# Клиентские хендлеры диалога зарегистрированы ДО cmd_start и menu: в диалоге /start —
# это корректный выход с уведомлением админа, а любой другой текст (включая тексты кнопок
# меню) — содержимое диалога, а не команда.
#
# Срок: ожидание и активный диалог закрываются фоновой задачей (close_idle_dialogs) после
# DIALOG_*_TIMEOUT бездействия. «Давность» — fsm_storage.updated_at (его сдвигает любая запись
# PostgresStorage), а каждое пересылаемое сообщение перед copy_to «касается» диалога
# (_touch_dialog): меняет data['activity_id']. Автозакрытие требует в transition_state тот
# activity_id, который видело при выборке, поэтому сообщение, успевшее коснуться диалога, не даст
# закрыть его у себя за спиной, а опоздавшее не уйдёт после уведомления о закрытии.

DIALOG_WAITING_TIMEOUT = timedelta(hours=4)
DIALOG_ACTIVE_TIMEOUT = timedelta(hours=12)

DIALOG_REQUEST_TEXT = (
    '💬 <b>Клиент запрашивает диалог</b>\n\n'
    'После подтверждения все ваши сообщения в этой теме будут уходить клиенту напрямую, без Reply.'
)
DIALOG_ACTIVE_TEXT = (
    '🟢 <b>Диалог активен</b>\n\n'
    'Пишите в эту тему — сообщения уходят клиенту напрямую. Сообщения клиента появятся здесь.'
)
DIALOG_WAITING_NOTE_TEXT = '✉️ Клиент пишет, пока ждёт подтверждения диалога:'
DIALOG_WAITING_FORWARDED_TEXT = (
    '✉️ Сообщение передано администратору. Запрос на диалог ждёт его решения — '
    'если передумали, нажмите «Отменить запрос».'
)
DIALOG_WAITING_MENU_HINT_TEXT = (
    '⏳ Ваш запрос на диалог ожидает решения администратора. '
    'Чтобы оставить заявку или задать вопрос, сначала нажмите «Отменить запрос».'
)
DIALOG_REQUEST_ALREADY_CLOSED_TEXT = 'Запрос на диалог уже закрыт — сообщение не отправлено. Воспользуйтесь меню.'
DIALOG_ALREADY_FINISHED_TEXT = 'Диалог уже завершён — сообщение не отправлено. Воспользуйтесь меню.'
DIALOG_ALREADY_FINISHED_ADMIN_TEXT = 'Диалог уже завершён — сообщение клиенту не отправлено.'

# Тексты кнопок главного меню клиента (и «Меню»), набранные в ожидании, — команды, а не
# сообщения админу: их не пересылаем, а подсказываем сначала отменить запрос.
CLIENT_MENU_TEXTS = frozenset(
    {button.text for row in get_main_keyboard().keyboard for button in row} | {'Меню'}
)


def _client_key(bot: Bot, client_id: int) -> StorageKey:
    return StorageKey(bot_id=bot.id, chat_id=client_id, user_id=client_id)


async def _touch_dialog(bot: Bot, storage, client_id: int, dialog_id: str) -> bool:
    """
    Отметка активности диалога перед пересылкой сообщения: новый activity_id (и свежий updated_at).
    False — диалога dialog_id уже нет (закрыт любым способом), пересылать нельзя. update_data_if не
    воскрешает удалённую запись; из-за FOR UPDATE в transition_state касание и автозакрытие
    сериализуются по строке — побеждает ровно одно.
    """
    return await storage.update_data_if(
        _client_key(bot, client_id),
        match={'dialog_id': dialog_id}, patch={'activity_id': secrets.token_hex(4)}
    )


async def _notify_client(bot: Bot, client_id: int, *, context: str, **send_kwargs) -> bool:
    """Сообщение клиенту о смене статуса диалога или заказа; блокировка бота — deactivated_user, как везде."""
    try:
        await bot.send_message(chat_id=client_id, **send_kwargs)
        return True
    except TelegramForbiddenError as error:
        logger.warning("Не удалось отправить [%s] — клиент user_id=%s заблокировал бота: %s", context, client_id, error)
        await deactivated_user(client_id)
    except TelegramAPIError as error:
        logger.warning("Не удалось отправить [%s] user_id=%s: %s", context, client_id, error)

    return False


async def _drop_stale_buttons(bot: Bot, callback: types.CallbackQuery) -> None:
    """Снимает inline-кнопки с сообщения, по которому пришёл уже неактуальный callback."""
    if callback.message is None:
        return

    try:
        await bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id, message_id=callback.message.message_id, reply_markup=None
        )
    except TelegramAPIError as error:
        # Чаще всего "message is not modified": кнопки уже сняты победившей стороной.
        logger.info("Не удалось снять устаревшие кнопки message_id=%s: %s", callback.message.message_id, error)


async def _close_dialog_request(bot: Bot, storage, client_id: int, dialog_id: str, *, closed_by: str) -> bool:
    """
    Dialog.waiting → None: отклонение админом (closed_by='admin') или отмена клиентом ('client').
    Возвращает False, если переход уже сделала другая сторона — тогда ничего не шлёт.
    """
    old_data = await storage.transition_state(
        _client_key(bot, client_id),
        from_state=Dialog.waiting, match={'dialog_id': dialog_id}, to_state=None, to_data={}
    )

    if old_data is None:
        return False

    await _announce_request_closed(bot, client_id, old_data, closed_by=closed_by)

    return True


async def _announce_request_closed(bot: Bot, client_id: int, old_data: dict, *, closed_by: str) -> None:
    """
    Уведомления о закрытом запросе на диалог (переход waiting → None уже сделан вызывающим).
    closed_by: 'admin' | 'client' | 'timeout' (автозакрытие, см. close_idle_dialogs).
    """
    group_id = await get_group_id()
    topic_id = await get_user_thread_id(client_id)
    request_message_id = old_data.get('request_message_id')

    # (итог на карточке запроса, уведомление в тему или None, ответ клиенту)
    request_result_text, topic_text, client_text = {
        'admin': (
            '❌ Запрос отклонён администратором.',
            None,
            'К сожалению, сейчас администратор не может начать диалог. '
            'Вы можете задать вопрос или оставить заявку — мы обязательно ответим.',
        ),
        'client': (
            '❌ Клиент отменил запрос на диалог.',
            '❌ Клиент отменил запрос на диалог.',
            'Запрос на диалог отменён.',
        ),
        'timeout': (
            '⌛️ Запрос закрыт автоматически — не было ответа.',
            '⌛️ Запрос клиента на диалог закрыт автоматически: его долго не подтверждали. '
            'Клиент может запросить диалог заново.',
            '⌛️ Запрос на диалог закрыт: администратор так и не смог ответить. '
            'Вы можете задать вопрос или оставить заявку — мы обязательно ответим, — или запросить диалог заново.',
        ),
    }[closed_by]

    if group_id is not None and request_message_id is not None:
        await safe_edit_message_text(
            bot, group_id, request_message_id,
            context=f'итог запроса на диалог user_id={client_id}',
            text=f'{DIALOG_REQUEST_TEXT}\n\n{request_result_text}'
        )

    if topic_text is not None and group_id is not None and topic_id is not None:
        context = f'уведомление о закрытии запроса на диалог ({closed_by}) user_id={client_id}'

        if closed_by == 'timeout':
            await _send_timeout_topic_notice(
                bot, group_id, client_id, topic_id, topic_text, context=context,
                event='Запрос на диалог клиента {client} закрыт автоматически по неактивности'
            )
        else:
            await safe_send_message(bot, group_id, context=context, text=topic_text, message_thread_id=topic_id)

    await _notify_client(
        bot, client_id,
        context='закрытие запроса на диалог',
        text=client_text, reply_markup=get_main_keyboard()
    )


async def _finish_active_dialog(bot: Bot, storage, client_id: int, dialog_id: str, *, ended_by: str) -> bool:
    """
    Dialog.active → None. ended_by: 'client' | 'admin' | 'blocked' (бот заблокирован
    клиентом — выяснилось при пересылке). Возвращает False (и ничего не шлёт), если
    диалог уже завершила другая сторона.
    """
    old_data = await storage.transition_state(
        _client_key(bot, client_id),
        from_state=Dialog.active, match={'dialog_id': dialog_id}, to_state=None, to_data={}
    )

    if old_data is None:
        return False

    await _announce_dialog_finished(bot, client_id, old_data, ended_by=ended_by)

    return True


async def _announce_dialog_finished(bot: Bot, client_id: int, old_data: dict, *, ended_by: str) -> None:
    """
    Уведомления о завершённом диалоге (переход active → None уже сделан вызывающим).
    ended_by: 'client' | 'admin' | 'blocked' | 'timeout' (автозакрытие, см. close_idle_dialogs).
    Статус-сообщение в теме в любом случае теряет кнопку «Завершить»; вторая сторона
    (при таймауте — обе) получает явное уведомление.
    """
    group_id = await get_group_id()
    topic_id = await get_user_thread_id(client_id)
    status_message_id = old_data.get('status_message_id')

    status_texts = {
        'client': '🔴 <b>Диалог завершён клиентом</b>',
        'admin': '🔴 <b>Диалог завершён администратором</b>',
        'blocked': '🔴 <b>Диалог завершён</b>: клиент заблокировал бота',
        'timeout': '🔴 <b>Диалог завершён</b>: долго не было сообщений',
    }

    if group_id is not None and status_message_id is not None:
        await safe_edit_message_text(
            bot, group_id, status_message_id,
            context=f'статус завершённого диалога user_id={client_id}',
            text=status_texts[ended_by]
        )

    if ended_by == 'admin':
        await _notify_client(
            bot, client_id,
            context='завершение диалога администратором',
            text='🔴 Администратор завершил диалог. Спасибо за общение!\n\n'
                 'Если появятся вопросы — воспользуйтесь меню.',
            reply_markup=get_main_keyboard()
        )

        return

    if group_id is not None and topic_id is not None:
        topic_text = {
            'client': '🔴 Клиент вышел из диалога. Сообщения в этой теме больше не пересылаются клиенту.',
            'blocked': '🔴 Клиент заблокировал бота — сообщение не доставлено, диалог завершён.',
            'timeout': '🔴 Диалог завершён автоматически — долго не было сообщений. '
                       'Сообщения в этой теме больше не пересылаются клиенту.',
        }[ended_by]
        context = f'уведомление о завершении диалога ({ended_by}) user_id={client_id}'

        if ended_by == 'timeout':
            await _send_timeout_topic_notice(
                bot, group_id, client_id, topic_id, topic_text, context=context,
                event='Диалог с клиентом {client} завершён автоматически по неактивности'
            )
        else:
            await safe_send_message(bot, group_id, context=context, text=topic_text, message_thread_id=topic_id)

    if ended_by == 'client':
        await _notify_client(
            bot, client_id,
            context='выход клиента из диалога',
            text='Вы вышли из диалога с администратором.',
            reply_markup=get_main_keyboard()
        )
    elif ended_by == 'timeout':
        await _notify_client(
            bot, client_id,
            context='автозавершение диалога',
            text='🔴 Диалог с администратором завершён — долго не было сообщений. Спасибо за общение!\n\n'
                 'Если появятся вопросы — воспользуйтесь меню.',
            reply_markup=get_main_keyboard()
        )


async def _send_timeout_topic_notice(bot: Bot, group_id: int, client_id: int, topic_id: int, text: str, *,
                                     context: str, event: str) -> None:
    """
    Уведомление в тему об автозакрытии. Если темы в Telegram больше нет, самолечение как в
    _deliver_client_submission: привязка сбрасывается, админ узнаёт о проблеме в личке, а не
    только из лога. event — что случилось, с плейсхолдером {client}.
    """
    sent, error = await send_message_capturing_error(
        bot, group_id, context=context, text=text, message_thread_id=topic_id
    )

    if sent is None and error is not None and is_dead_topic_error(error):
        client = f'<b>{html.escape(await get_topic_name(client_id) or "")}</b> (id <code>{client_id}</code>)'
        await _heal_dead_topic(
            bot, client_id, topic_id, DEAD_TOPIC_TIMEOUT_ADMIN_NOTICE.format(event=event.format(client=client))
        )


async def close_idle_dialogs(bot: Bot, storage) -> int:
    """
    Один проход автозакрытия (его крутит background.close_idle_dialogs_periodically):
    Dialog.waiting без активности дольше DIALOG_WAITING_TIMEOUT и Dialog.active — дольше
    DIALOG_ACTIVE_TIMEOUT закрываются с уведомлением обеих сторон. Возвращает число закрытых.

    Выборка кандидатов — без блокировок; решает transition_state (здесь же, а не в
    _close_dialog_request/_finish_active_dialog) с match по dialog_id и activity_id, которые
    кандидат имел при выборке. Если за это время диалог подтвердили,
    отклонили, отменили, завершили или в нём появилось сообщение (_touch_dialog сменил
    activity_id), переход не совпадёт и кандидат молча пропускается. Записи без activity_id
    (созданные до автозакрытия) сверяются только по dialog_id.

    Переход делается до отправок (at-most-once, как рассылка и статус заказа): неудачное
    уведомление не откатывает закрытие и не повторяется на следующем проходе, а закрытие всё
    равно учитывается в возвращаемом числе. Ошибка по одному кандидату логируется и не мешает
    остальным.
    """
    closed = 0

    for state, timeout, announce in (
        (Dialog.waiting, DIALOG_WAITING_TIMEOUT, partial(_announce_request_closed, closed_by='timeout')),
        (Dialog.active, DIALOG_ACTIVE_TIMEOUT, partial(_announce_dialog_finished, ended_by='timeout')),
    ):
        for client_id, data in await get_idle_fsm_rows(bot.id, state.state, timeout):
            dialog_id = data.get('dialog_id')

            if dialog_id is None:
                continue

            match = {'dialog_id': dialog_id}

            if 'activity_id' in data:
                match['activity_id'] = data['activity_id']

            try:
                old_data = await storage.transition_state(
                    _client_key(bot, client_id), from_state=state, match=match, to_state=None, to_data={}
                )
            except Exception:
                logger.exception('Автозакрытие диалога user_id=%s (%s): переход не удался', client_id, state.state)
                continue

            if old_data is None:
                continue

            # Считаем по переходу: диалог закрыт, даже если какое-то уведомление ниже не дойдёт.
            closed += 1

            try:
                await announce(bot, client_id, old_data)
            except Exception:
                logger.exception('Автозакрытие диалога user_id=%s (%s): уведомления не отправлены', client_id, state.state)

    return closed


async def _leave_dialog_by_client(message: types.Message, state: FSMContext, bot: Bot) -> bool:
    """
    Выход клиента из ожидания/диалога. Возвращает False только в одном случае: клиент
    отменял запрос, но админ успел его подтвердить — диалог активен, и клиенту об этом
    явно сказано (иначе он считал бы, что отменил запрос). Вызывающий код в этом случае
    не должен показывать обычное меню.
    """
    client_id = message.from_user.id
    current_state = await state.get_state()
    dialog_id = (await state.get_data()).get('dialog_id')

    if dialog_id is None:
        return True

    if current_state == Dialog.active.state:
        # Проигрыш перехода здесь означает, что диалог уже завершил админ или автозакрытие — победитель
        # сам уведомил клиента («Администратор завершил» / «завершён — долго не было сообщений»), а
        # «Вы вышли» клиент не получит, чтобы не решить, что это он завершил диалог.
        await _finish_active_dialog(bot, state.storage, client_id, dialog_id, ended_by='client')
        return True

    if current_state != Dialog.waiting.state:
        return True

    if await _close_dialog_request(bot, state.storage, client_id, dialog_id, closed_by='client'):
        return True

    # Отмена проиграла переход. Из waiting выходят только в None (отклонение или автозакрытие —
    # клиент уже получил ответ) или в active (подтверждение); назад в waiting состояние не возвращается,
    # поэтому перечитать его после проигрыша безопасно.
    if (await state.get_state() != Dialog.active.state
            or (await state.get_data()).get('dialog_id') != dialog_id):
        return True

    await safe_answer(
        message,
        context=f'отмена запроса опоздала — диалог начат user_id={client_id}',
        text='🟢 Администратор уже подтвердил ваш запрос — диалог начат, отменить его не получилось.\n\n'
             'Пишите сюда — сообщения уходят администратору напрямую. '
             'Чтобы закончить, нажмите «Выйти из диалога».',
        reply_markup=get_dialog_active_keyboard()
    )

    return False


@router.message(F.text == 'Запросить диалог с админом',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(None))
async def request_dialog(message: types.Message, state: FSMContext, bot: Bot):
    user = message.from_user

    # До ранних выходов ниже: при успехе set_data всё равно заменит данные, а при отказе клиент
    # остаётся в главном меню — и окно ответа уже закрыто его явным действием.
    await _close_reply_window(state)

    group_id = await get_group_id()

    if group_id is None:
        await safe_answer(
            message,
            context=f'группа не привязана (диалог) user_id={user.id}',
            text='Бот временно не работает, попробуйте позже.'
        )
        return

    try:
        topic_id = await resolve_client_topic(bot, user, group_id)
    except TopicResolutionError as error:
        if error.reason == 'not_registered':
            text = 'Вы ещё не зарегистрированы в боте. Пожалуйста, напишите /start, чтобы начать.'
        else:
            text = 'К сожалению, сейчас не получилось отправить запрос. Пожалуйста, попробуйте ещё раз чуть позже.'

        await safe_answer(message, context=f'невозможность создать тему (диалог) user_id={user.id}', text=text)
        return

    dialog_id = secrets.token_hex(4)

    # Состояние ставится ДО отправки кнопок в группу: иначе админ мог бы нажать
    # «Подтвердить» раньше, чем появится запись, и получить «запрос неактуален».
    # activity_id — с самого начала: автозакрытие сверяет его в transition_state, а `@>` не заметил
    # бы, что касание лишь добавило ключ, которого при выборке кандидата ещё не было.
    await state.set_state(Dialog.waiting)
    await state.set_data({'dialog_id': dialog_id, 'activity_id': secrets.token_hex(4)})

    request_message = await safe_send_message(
        bot, group_id,
        context=f'запрос на диалог в группу user_id={user.id} topic_id={topic_id}',
        text=DIALOG_REQUEST_TEXT, message_thread_id=topic_id,
        reply_markup=get_dialog_request_markup(user.id, dialog_id)
    )

    if not request_message:
        await state.clear()
        await safe_answer(
            message,
            context=f'запрос на диалог не доставлен user_id={user.id}',
            text='Запрос не был доставлен. Попробуйте отправить его ещё раз чуть позже.',
            reply_markup=get_main_keyboard()
        )
        return

    await state.storage.update_data_if(
        _client_key(bot, user.id),
        match={'dialog_id': dialog_id}, patch={'request_message_id': request_message.message_id}
    )

    await safe_answer(
        message,
        context=f'подтверждение запроса на диалог user_id={user.id}',
        text='📨 <b>Запрос отправлен</b>\n\n'
             'Как только администратор подключится, вы получите уведомление и сможете переписываться напрямую.\n\n'
             'Пока ждёте, можете написать суть вопроса — сообщение передадим администратору. '
             'Если передумали — нажмите «Отменить запрос», и тогда можно будет оставить заявку или задать вопрос.',
        reply_markup=get_dialog_waiting_keyboard()
    )


@router.message(CommandStart(),
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(Dialog.waiting, Dialog.active))
async def cmd_start_in_dialog(message: types.Message, state: FSMContext, bot: Bot):
    if await _leave_dialog_by_client(message, state, bot):
        await cmd_start(message, state)


@router.message(F.text.in_({'Отменить запрос', 'Выйти из диалога'}),
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(Dialog.waiting, Dialog.active))
async def leave_dialog(message: types.Message, state: FSMContext, bot: Bot):
    # «Отменить запрос» в Dialog.active (и наоборот) — нажатие по устаревшей клавиатуре,
    # поэтому выход определяется по состоянию, а не по тексту кнопки.
    await _leave_dialog_by_client(message, state, bot)


@router.message(Dialog.waiting,
                F.text.in_(CLIENT_MENU_TEXTS),
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def dialog_waiting_menu_hint(message: types.Message):
    await safe_answer(
        message,
        context=f'подсказка ожидания диалога user_id={message.from_user.id}',
        text=DIALOG_WAITING_MENU_HINT_TEXT,
        reply_markup=get_dialog_waiting_keyboard()
    )


async def _copy_client_message_to_topic(message: types.Message, group_id: int, topic_id: int) -> bool:
    """copy_to сообщения клиента в его тему; при сбое — ответ клиенту о недоставке."""
    try:
        await message.copy_to(chat_id=group_id, message_thread_id=topic_id)
        return True
    except TelegramAPIError as error:
        logger.warning("Пересылка сообщения клиента user_id=%s в тему topic_id=%s не удалась: %s",
                       message.from_user.id, topic_id, error)
        await safe_answer(
            message,
            context=f'сообщение диалога не доставлено user_id={message.from_user.id}',
            text='Сообщение не доставлено администратору. Попробуйте отправить его ещё раз чуть позже.'
        )
        return False


async def _client_dialog_topic(message: types.Message) -> tuple[int, int] | None:
    """(group_id, topic_id) для пересылки в диалоге; без группы/темы — ответ клиенту и None."""
    user = message.from_user
    group_id = await get_group_id()
    topic_id = await get_user_thread_id(user.id)

    if group_id is None or topic_id is None:
        logger.error("Диалог без группы/темы: user_id=%s group_id=%s topic_id=%s", user.id, group_id, topic_id)
        await safe_answer(
            message,
            context=f'диалог без темы user_id={user.id}',
            text='Сообщение не доставлено: бот временно не работает. Попробуйте позже.'
        )
        return None

    return group_id, topic_id


@router.message(Dialog.waiting,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def forward_client_waiting_message(message: types.Message, state: FSMContext, bot: Bot):
    # Раньше здесь была только подсказка «ожидает решения», и текст клиента пропадал.
    user = message.from_user
    dialog_id = (await state.get_data()).get('dialog_id')
    target = await _client_dialog_topic(message)

    if target is None:
        return

    group_id, topic_id = target

    # Касание — до пересылки: либо автозакрытие уже прошло (не пересылаем, говорим клиенту),
    # либо после касания оно не пройдёт, пока сообщение уходит в тему.
    if dialog_id is None or not await _touch_dialog(bot, state.storage, user.id, dialog_id):
        await safe_answer(
            message,
            context=f'сообщение после закрытия запроса на диалог user_id={user.id}',
            text=DIALOG_REQUEST_ALREADY_CLOSED_TEXT, reply_markup=get_main_keyboard()
        )
        return

    await safe_send_message(
        bot, group_id,
        context=f'пометка сообщения в ожидании диалога user_id={user.id}',
        text=DIALOG_WAITING_NOTE_TEXT, message_thread_id=topic_id
    )

    if not await _copy_client_message_to_topic(message, group_id, topic_id):
        return

    # Без reply_markup: если админ как раз подтвердил диалог, не затираем клавиатуру «Выйти из диалога».
    await safe_answer(
        message,
        context=f'сообщение в ожидании диалога передано user_id={user.id}',
        text=DIALOG_WAITING_FORWARDED_TEXT
    )


@router.message(Dialog.active,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def forward_client_dialog_message(message: types.Message, state: FSMContext, bot: Bot):
    user = message.from_user
    dialog_id = (await state.get_data()).get('dialog_id')
    target = await _client_dialog_topic(message)

    if target is None:
        return

    group_id, topic_id = target

    # Касание — до пересылки (см. forward_client_waiting_message): сообщение не уйдёт в тему
    # после уведомления об автозавершении.
    if dialog_id is None or not await _touch_dialog(bot, state.storage, user.id, dialog_id):
        await safe_answer(
            message,
            context=f'сообщение после завершения диалога user_id={user.id}',
            text=DIALOG_ALREADY_FINISHED_TEXT, reply_markup=get_main_keyboard()
        )
        return

    await _copy_client_message_to_topic(message, group_id, topic_id)


@router.callback_query(DialogCallback.filter(), F.from_user.id != Config.ADMIN_ID)
async def dialog_callback_not_admin(callback: types.CallbackQuery):
    await safe_answer_callback(
        callback,
        context=f'кнопка диалога не от админа user_id={callback.from_user.id}',
        text='Эта кнопка доступна только администратору.', show_alert=True
    )


@router.callback_query(DialogCallback.filter(F.action == 'confirm'), F.from_user.id == Config.ADMIN_ID)
async def dialog_confirm(callback: types.CallbackQuery, callback_data: DialogCallback, bot: Bot, fsm_storage):
    client_id, dialog_id = callback_data.client_id, callback_data.dialog_id
    key = _client_key(bot, client_id)

    async def reject_stale():
        await _drop_stale_buttons(bot, callback)
        await safe_answer_callback(callback, context=f'неактуальный запрос на диалог user_id={client_id}',
                                   text='Запрос уже неактуален.')

    # Дешёвая предпроверка отсекает типичный двойной клик (апдейты одного админа в группе
    # и так сериализованы) до отправки статус-сообщения. От настоящей гонки с клиентом
    # защищает только transition_state ниже.
    if (await fsm_storage.get_state(key) != Dialog.waiting.state
            or (await fsm_storage.get_data(key)).get('dialog_id') != dialog_id):
        await reject_stale()
        return

    group_id = await get_group_id()
    topic_id = await get_user_thread_id(client_id)

    status_message = None

    # Статус-сообщение отправляется ДО перехода waiting→active: если отправка не удалась,
    # состояние клиента не трогали вовсе. Раньше переход делался первым и при сбое
    # откатывался active→waiting — в этом окне «Отменить запрос» клиента проигрывал
    # переход молча, а после отката запрос оставался висеть.
    if group_id is not None and topic_id is not None:
        status_message = await safe_send_message(
            bot, group_id,
            context=f'статус активного диалога user_id={client_id}',
            text=DIALOG_ACTIVE_TEXT, message_thread_id=topic_id,
            reply_markup=get_dialog_status_markup(client_id, dialog_id)
        )

    if not status_message:
        await safe_answer_callback(callback, context=f'статус диалога не отправлен user_id={client_id}',
                                   text='Не удалось начать диалог. Попробуйте ещё раз.', show_alert=True)
        return

    old_data = await fsm_storage.transition_state(
        key, from_state=Dialog.waiting, match={'dialog_id': dialog_id}, to_state=Dialog.active
    )

    if old_data is None:
        # Запрос закрыли между предпроверкой и переходом: клиент отменил его (или /start) либо
        # сработало автозакрытие. Победитель уже уведомил клиента и тему — причина видна там.
        await safe_edit_message_text(
            bot, group_id, status_message.message_id,
            context=f'статус неначатого диалога user_id={client_id}',
            text='⚪️ <b>Диалог не начат</b>: запрос уже закрыт'
        )
        await reject_stale()
        return

    linked = await fsm_storage.update_data_if(
        key, match={'dialog_id': dialog_id}, patch={'status_message_id': status_message.message_id}
    )

    await safe_edit_message_text(
        bot, callback.message.chat.id, callback.message.message_id,
        context=f'подтверждение запроса на диалог user_id={client_id}',
        text=f'{DIALOG_REQUEST_TEXT}\n\n✅ Запрос подтверждён.'
    )

    if not linked:
        # Клиент успел выйти (например, /start) между переходом и записью status_message_id —
        # его хендлер уже уведомил тему, но снять кнопку со статуса было не по чему.
        await safe_edit_message_text(
            bot, group_id, status_message.message_id,
            context=f'статус диалога, завершённого до старта user_id={client_id}',
            text='🔴 <b>Диалог завершён клиентом</b>'
        )
        await safe_answer_callback(callback, context=f'диалог завершён до старта user_id={client_id}',
                                   text='Клиент уже вышел из диалога.')
        return

    try:
        await bot.send_message(
            chat_id=client_id,
            text='🟢 <b>Администратор на связи!</b>\n\n'
                 'Пишите сюда — сообщения уходят администратору напрямую. '
                 'Чтобы закончить, нажмите «Выйти из диалога».',
            reply_markup=get_dialog_active_keyboard()
        )
    except TelegramForbiddenError as error:
        logger.warning("Начало диалога: клиент user_id=%s заблокировал бота: %s", client_id, error)
        await deactivated_user(client_id)
        await _finish_active_dialog(bot, fsm_storage, client_id, dialog_id, ended_by='blocked')
    except TelegramAPIError as error:
        logger.warning("Начало диалога: не удалось уведомить клиента user_id=%s: %s", client_id, error)
        await safe_send_message(
            bot, group_id,
            context=f'уведомление о недоставке старта диалога user_id={client_id}',
            text='⚠️ Не удалось уведомить клиента о начале диалога. Ваши сообщения всё равно будут ему пересылаться.',
            message_thread_id=topic_id
        )

    await safe_answer_callback(callback, context=f'диалог начат user_id={client_id}', text='Диалог начат.')


@router.callback_query(DialogCallback.filter(F.action == 'reject'), F.from_user.id == Config.ADMIN_ID)
async def dialog_reject(callback: types.CallbackQuery, callback_data: DialogCallback, bot: Bot, fsm_storage):
    closed = await _close_dialog_request(
        bot, fsm_storage, callback_data.client_id, callback_data.dialog_id, closed_by='admin'
    )

    if not closed:
        await _drop_stale_buttons(bot, callback)
        await safe_answer_callback(callback, context=f'неактуальный запрос на диалог user_id={callback_data.client_id}',
                                   text='Запрос уже неактуален.')
        return

    await safe_answer_callback(callback, context=f'запрос на диалог отклонён user_id={callback_data.client_id}',
                               text='Запрос отклонён.')


@router.callback_query(DialogCallback.filter(F.action == 'end'), F.from_user.id == Config.ADMIN_ID)
async def dialog_end(callback: types.CallbackQuery, callback_data: DialogCallback, bot: Bot, fsm_storage):
    finished = await _finish_active_dialog(
        bot, fsm_storage, callback_data.client_id, callback_data.dialog_id, ended_by='admin'
    )

    if not finished:
        await _drop_stale_buttons(bot, callback)
        await safe_answer_callback(callback, context=f'диалог уже завершён user_id={callback_data.client_id}',
                                   text='Диалог уже завершён.')
        return

    await safe_answer_callback(callback, context=f'диалог завершён user_id={callback_data.client_id}',
                               text='Диалог завершён.')


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


def _history_message_texts(history: list) -> list[str]:
    """
    Собирает историю заказов в одно или несколько сообщений (не более
    TELEGRAM_MESSAGE_LIMIT символов каждое), не разрывая ни одну запись
    посередине. Записей мало, а превью коротко (HISTORY_PREVIEW_MAX_LENGTH),
    поэтому одна запись сама по себе никогда не превышает лимит.
    """
    header = '🗂 <b>История заказов клиента</b>'
    entries = []

    for created_at, text_value, status in history:
        preview = text_value if len(text_value) <= HISTORY_PREVIEW_MAX_LENGTH \
            else text_value[:HISTORY_PREVIEW_MAX_LENGTH] + '…'
        status_label = ORDER_STATUS_LABELS.get(status, status)
        entries.append(f'📅 {created_at.strftime("%d.%m.%Y %H:%M")} · {status_label}\n{html.escape(preview)}')

    messages = []
    current = header

    for entry in entries:
        candidate = f'{current}\n\n{entry}'

        if exceeds_telegram_limit(candidate):
            messages.append(current)
            current = entry
        else:
            current = candidate

    messages.append(current)

    return messages


@router.message(Command('history'), F.chat.type.in_({'group', 'supergroup'}))
async def client_history(message: types.Message):
    """
    /history — только для админа, только внутри темы конкретного клиента
    (Users.topic_id == message.message_thread_id, тот же способ резолва, что и
    в reply_to_message). Не-админ получает полную тишину — без утечки самого
    факта существования этой команды или данных клиента (проверено e2e).
    Админ вне темы клиента (общая тема группы либо тема без привязанного
    клиента) получает короткое пояснение — это не утечка данных (клиент ещё
    не определён) и избавляет админа от гадания, почему команда промолчала.
    """
    if message.from_user is None or message.from_user.id != Config.ADMIN_ID:
        return

    group_id = await get_group_id()

    if group_id is None or message.chat.id != group_id or message.message_thread_id is None:
        await safe_answer(
            message,
            context=f'/history вне темы клиента admin_id={message.from_user.id}',
            text='Эта команда работает только внутри темы конкретного клиента в рабочей группе.'
        )

        return

    history = await get_bid_history_by_thread_id(message.message_thread_id)

    if history is None:
        await safe_answer(
            message,
            context=f'/history в непривязанной теме thread_id={message.message_thread_id}',
            text='Клиент, привязанный к этой теме, не найден в базе бота.'
        )

        return

    if not history:
        await safe_answer(
            message,
            context=f'/history без заказов thread_id={message.message_thread_id}',
            text='У этого клиента пока нет ни одной заявки.'
        )

        return

    for chunk in _history_message_texts(history):
        await safe_answer(
            message,
            context=f'/history сообщение thread_id={message.message_thread_id}',
            text=chunk
        )


# ------------------------------------------------------------ Статус заказа
#
# Кнопки под карточкой заявки (Bid) в теме клиента: «Принято в работу» (new → in_progress) и
# «Готово» (in_progress → done). Статус — поле Requests.status, не FSM: переход — атомарный условный
# UPDATE на requests (transition_request_status). Заявка ищется по самой карточке
# (callback.message.message_id == Requests.group_message_id). Уведомляет клиента и правит карточку
# только выигравший переход; проигравший (повтор, позднее или гоночное нажатие) лишь приводит кнопки
# к актуальному статусу и получает answer.

ORDER_STALE_TEXT = 'Статус этой заявки уже изменён.'
ORDER_CLIENT_TEXTS = {
    'in_progress': (
        '🛠 <b>Ваша заявка в работе!</b>\n\n'
        'Мы начали готовить разбор по заявке от {date}. Как только он будет готов — сразу напишем вам здесь.'
    ),
    'done': (
        '✨ <b>Ваш разбор готов!</b>\n\n'
        'Работа по заявке от {date} завершена. Если появятся вопросы — просто воспользуйтесь меню.'
    ),
}


async def _change_order_status(callback: types.CallbackQuery, bot: Bot, *, from_status: str, to_status: str) -> None:
    card = callback.message
    group_id = await get_group_id()

    if card is None or group_id is None or card.chat.id != group_id:
        await _drop_stale_buttons(bot, callback)
        await safe_answer_callback(callback, context=f'кнопка статуса вне рабочей группы admin_id={callback.from_user.id}',
                                   text='Заявка не найдена.')
        return

    changed = await transition_request_status(card.message_id, from_status, to_status)

    if changed is None:
        current = await get_bid_card_by_group_message_id(card.message_id)

        if current is None:
            await _drop_stale_buttons(bot, callback)
            await safe_answer_callback(callback, context=f'кнопка статуса без заявки message_id={card.message_id}',
                                       text='Заявка не найдена.')
            return

        await safe_edit_message_text(
            bot, card.chat.id, card.message_id,
            context=f'актуализация карточки заявки message_id={card.message_id}',
            text=_bid_card_text(current.name, current.birthday, current.text, current.status),
            reply_markup=get_order_status_markup(current.status)
        )
        await safe_answer_callback(callback, context=f'неактуальная кнопка статуса message_id={card.message_id}',
                                   text=ORDER_STALE_TEXT)
        return

    client_id = changed.telegram_id

    await safe_edit_message_text(
        bot, card.chat.id, card.message_id,
        context=f'карточка заявки после смены статуса на {to_status} user_id={client_id}',
        text=_bid_card_text(changed.name, changed.birthday, changed.text, to_status),
        reply_markup=get_order_status_markup(to_status)
    )

    notified = await _notify_client(
        bot, client_id,
        context=f'уведомление о статусе заказа {to_status} user_id={client_id}',
        text=ORDER_CLIENT_TEXTS[to_status].format(date=format_client_date(changed.created_at))
    )

    if notified:
        await safe_answer_callback(callback, context=f'статус заказа {to_status} user_id={client_id}',
                                   text='Статус обновлён, клиент уведомлён.')
    else:
        await safe_answer_callback(
            callback, context=f'статус заказа {to_status} без уведомления user_id={client_id}',
            text='Статус обновлён, но клиент не получил уведомление (возможно, заблокировал бота).',
            show_alert=True
        )


@router.callback_query(OrderStatusCallback.filter(), F.from_user.id != Config.ADMIN_ID)
async def order_callback_not_admin(callback: types.CallbackQuery):
    await safe_answer_callback(
        callback,
        context=f'кнопка статуса заказа не от админа user_id={callback.from_user.id}',
        text='Эта кнопка доступна только администратору.', show_alert=True
    )


@router.callback_query(OrderStatusCallback.filter(F.action == 'accept'), F.from_user.id == Config.ADMIN_ID)
async def order_accept(callback: types.CallbackQuery, bot: Bot):
    await _change_order_status(callback, bot, from_status='new', to_status='in_progress')


@router.callback_query(OrderStatusCallback.filter(F.action == 'done'), F.from_user.id == Config.ADMIN_ID)
async def order_done(callback: types.CallbackQuery, bot: Bot):
    await _change_order_status(callback, bot, from_status='in_progress', to_status='done')


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


GROUP_REMOVAL_ADMIN_NOTICE = (
    "🚨 <b>Бот удалён из рабочей группы</b>\n\n"
    "Бот больше не состоит в группе, куда приходят заявки и вопросы, поэтому доставить новое "
    "обращение клиента сейчас невозможно — клиенты будут получать ошибку.\n\n"
    "Чтобы всё заработало снова: добавьте бота обратно в <b>ту же самую</b> группу и выдайте ему права "
    "администратора с разрешением «Управление темами».\n\n"
    "<i>Привязка группы сохранена вместе с темами клиентов — повторный /bind не нужен.</i>"
)


@router.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER),
                    F.chat.type != 'private')
async def bot_removed_from_group(event: types.ChatMemberUpdated, bot: Bot):
    """
    Бота выгнали (или он вышел) из группы. Пока это рабочая группа, молчать нельзя:
    все дальнейшие обращения клиентов будут падать, а админ узнал бы об этом только
    из жалоб. Привязку (Settings.group_id) намеренно НЕ сбрасываем: она же держит
    связь тем клиентов с группой, а /bind требует, чтобы бот уже был в группе —
    сброс лишь потребовал бы привязывать заново и осиротил бы Users.topic_id. При
    возврате бота в ту же группу bot_added_to_chat увидит совпадение с group_id и
    ничего лишнего не сделает.
    """
    group_id = await get_group_id()

    if group_id is None or event.chat.id != group_id:
        # Чужой чат: bot_added_to_chat сразу выходит из любой группы, кроме рабочей,
        # так что штатно сюда попадает разве что этот собственный выход — он ничего
        # не ломает и админу не интересен.
        logger.info(
            "Бот удалён из постороннего чата chat_id=%s (рабочая группа group_id=%s) — реакции не требуется",
            event.chat.id, group_id
        )
        return

    logger.error(
        "Бот удалён из рабочей группы group_id=%s (инициатор user_id=%s) — доставка обращений клиентов "
        "остановлена до возврата бота в группу; привязка group_id сохранена",
        event.chat.id, event.from_user.id if event.from_user else None
    )

    await safe_send_message(
        bot, Config.ADMIN_ID,
        context=f'уведомление админу об удалении бота из рабочей группы group_id={event.chat.id}',
        text=GROUP_REMOVAL_ADMIN_NOTICE
    )


@router.message(F.text == 'Оставить заявку',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(None))
async def set_name(message: types.Message, state: FSMContext):
    await _close_reply_window(state)
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
async def about_us(message: types.Message, state: FSMContext):
    user = message.from_user

    if user is None:
        return

    await _close_reply_window(state)

    about_us_text = await get_about_us()

    await safe_answer(
        message,
        context=(f'отсутствие описания "О нас" user_id={user.id}' if about_us_text is None
                 else f'текст "О нас" user_id={user.id}'),
        text=render_about_us_text(about_us_text)
    )


# Как и остальные кнопки главного меню клиента — StateFilter(None) и регистрация выше
# free_text_hint. Внутри форм/диалога главного меню на экране нет (там своя клавиатура),
# а набранный вручную текст кнопки в форме — данные формы, как и у «О нас».
@router.message(F.text == 'Показать прайс',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(None))
async def show_price(message: types.Message, state: FSMContext):
    await _close_reply_window(state)
    await send_welcome_menu(message, 'показ прайса')


@router.message(F.text == 'Задать вопрос',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(None))
async def question_text(message: types.Message, state: FSMContext):
    await _close_reply_window(state)
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
    await _start_settings_edit(message, state, 'about')


@router.message(F.text == 'Изменить прайс',
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID,
                StateFilter(None))
async def change_price(message: types.Message, state: FSMContext):
    await _start_settings_edit(message, state, 'price')


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
        '• Обычное сообщение в теме клиенту автоматически не отправляется '
        '(кроме активного диалога — см. ниже).\n'
        '• Команда /history прямо в теме клиента покажет список всех его заявок '
        '(без вопросов) с датами.\n\n'

        '<b>Диалог с клиентом</b>\n'
        '• Клиент может нажать «Запросить диалог с админом» — в его теме появится запрос '
        'с кнопками «Подтвердить» / «Отклонить».\n'
        '• После подтверждения в теме появится «🟢 Диалог активен»: пока он активен, любое ваше '
        'сообщение в теме уходит клиенту напрямую, без Reply, а сообщения клиента приходят в тему.\n'
        '• Завершить диалог можно кнопкой «Завершить» под статусом; клиент тоже может выйти сам — '
        'вы получите уведомление в теме.\n\n'

        '<b>3. Прайс и раздел «О нас»</b>\n'
        '• Кнопка «Изменить прайс» меняет прайс, который видят пользователи.\n'
        '• Кнопка Изменить «О нас» меняет описание сервиса.\n'
        '• Бот сначала пришлёт текущий текст, а после ввода нового покажет, как его увидят клиенты. '
        'Сохраняется только кнопкой «Сохранить» под превью.\n'
        '• Передумали — нажмите «Меню» или «Отменить»: сохранённый текст не изменится.\n'
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


async def active_dialog_in_topic(message: types.Message, bot: Bot, fsm_storage) -> dict | bool:
    """
    Фильтр: сообщение пришло в тему клиента в привязанной группе, и у этого клиента сейчас
    Dialog.active. Передаёт в хендлер client_id и dialog_id. Вне активного диалога не
    срабатывает — сообщения в теме идут по старому пути (reply_to_message).
    """
    group_id = await get_group_id()

    if group_id is None or message.chat.id != group_id:
        return False

    client_id = await get_user_id(message.message_thread_id)

    if client_id is None:
        return False

    client_key = _client_key(bot, client_id)

    if await fsm_storage.get_state(client_key) != Dialog.active.state:
        return False

    dialog_id = (await fsm_storage.get_data(client_key)).get('dialog_id')

    if dialog_id is None:
        return False

    return {'client_id': client_id, 'dialog_id': dialog_id}


@router.message(F.chat.type.in_({'group', 'supergroup'}),
                F.from_user.id == Config.ADMIN_ID,
                F.message_thread_id.is_not(None),
                active_dialog_in_topic)
async def forward_admin_dialog_message(message: types.Message, bot: Bot, fsm_storage,
                                       client_id: int, dialog_id: str):
    # Касание — до пересылки, как у клиента: заметка админа не уйдёт клиенту после «диалог завершён».
    if not await _touch_dialog(bot, fsm_storage, client_id, dialog_id):
        await safe_answer(
            message,
            context=f'сообщение админа после завершения диалога user_id={client_id}',
            text=DIALOG_ALREADY_FINISHED_ADMIN_TEXT
        )
        return

    try:
        await message.copy_to(chat_id=client_id)
    except TelegramForbiddenError as error:
        logger.warning("Пересылка в диалоге: клиент user_id=%s заблокировал бота: %s", client_id, error)
        await deactivated_user(client_id)
        await _finish_active_dialog(bot, fsm_storage, client_id, dialog_id, ended_by='blocked')
    except TelegramAPIError as error:
        logger.warning("Пересылка в диалоге клиенту user_id=%s не удалась: %s", client_id, error)
        await safe_answer(
            message,
            context=f'сообщение диалога не доставлено клиенту user_id={client_id}',
            text='Не удалось доставить сообщение клиенту. Попробуйте отправить его ещё раз чуть позже.'
        )


REPLY_NOT_CLIENT_CARD_TEXT = (
    'Ответ доходит клиенту, только если сделать Reply на карточку его заявки или вопроса. '
    'Это служебное сообщение бота — клиенту ничего не отправлено.'
)
REPLY_FOREIGN_CARD_TEXT = (
    'Это сообщение относится к другому клиенту, поэтому ответ не отправлен. '
    'Сделайте Reply на карточку заявки или вопроса в теме нужного клиента.'
)


@router.message(F.reply_to_message,
                F.from_user.id == Config.ADMIN_ID,
                F.message_thread_id.is_not(None))
async def reply_to_message(message: types.Message, bot: Bot, fsm_storage):
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

    # Страховка: по Bot API reply_to_message заполняется только для ответа в той же ветке
    # (ответ на сообщение из другой темы приходит в external_reply), но клиента мы берём
    # по теме, где пишет админ, а цитату — по процитированному сообщению, и они обязаны совпадать.
    # Если Telegram не указал тему цитаты, основной защитой остаётся сверка владельца карточки ниже.
    if (original_message.message_thread_id is not None
            and original_message.message_thread_id != message.message_thread_id):
        logger.warning(
            "Reply из темы %s на сообщение %s из темы %s — не доставляется",
            message.message_thread_id, original_message.message_id, original_message.message_thread_id
        )
        await safe_answer(
            message,
            context=f'reply на сообщение из другой темы admin_id={message.from_user.id}',
            text=REPLY_FOREIGN_CARD_TEXT
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

    # Обращение клиента — это ровно те сообщения бота, что записаны в Requests.group_message_id
    # (карточки заявок и вопросов). Всё остальное от бота в теме (статусы диалога, уведомления,
    # корень темы) — служебное, его текст клиенту не пересказываем.
    reply_target = await get_reply_target_by_group_message_id(original_message.message_id)

    if reply_target is None:
        await safe_answer(
            message,
            context=f'reply на служебное сообщение бота admin_id={message.from_user.id}',
            text=REPLY_NOT_CLIENT_CARD_TEXT
        )
        return

    if reply_target.telegram_id != user_id:
        logger.warning(
            "Reply в теме %s (клиент user_id=%s) на карточку клиента user_id=%s — не доставляется",
            message.message_thread_id, user_id, reply_target.telegram_id
        )
        await safe_answer(
            message,
            context=f'reply на карточку чужого клиента admin_id={message.from_user.id}',
            text=REPLY_FOREIGN_CARD_TEXT
        )
        return

    quoted_text = reply_target.text

    if len(quoted_text) > CLIENT_QUOTE_MAX_LENGTH:
        quoted_text = quoted_text[:CLIENT_QUOTE_MAX_LENGTH] + '…'

    context_text = f"📩 Ответ на ваше обращение «{html.escape(quoted_text)}»:"

    try:
        await bot.send_message(chat_id=user_id, text=context_text)
    except TelegramForbiddenError as error:
        logger.warning("Доставка ответа админа пользователю user_id=%s не удалась (пользователь заблокировал бота): %s", user_id, error)
        await safe_answer(message, context=f'уведомление о блокировке admin_id={message.from_user.id}',
                           text='Пользователь заблокировал бота, поэтому ваше сообщение не доставлено.')
        await deactivated_user(user_id)
        return
    except TelegramAPIError as error:
        logger.warning("Доставка ответа админа пользователю user_id=%s не удалась: %s", user_id, error)
        await safe_answer(message, context=f'уведомление о недоставке admin_id={message.from_user.id}',
                           text='Не удалось доставить сообщение клиенту. Попробуйте отправить его ещё раз чуть позже.')
        return

    copy_kwargs = {}
    followup_caption = None

    if not message.text and not message.caption:
        fallback_caption = (
            DOCUMENT_FALLBACK_CAPTION if message.content_type == ContentType.DOCUMENT
            else NEUTRAL_FALLBACK_CAPTION
        )

        if message.content_type in CAPTION_CAPABLE_CONTENT_TYPES:
            copy_kwargs['caption'] = fallback_caption
        else:
            # Telegram не поддерживает caption для этого типа контента (стикеры,
            # video_note и т.п.) ни в copyMessage, ни в исходном методе отправки —
            # подставить его некуда, поэтому шлём тем же нейтральным текстом отдельным
            # сообщением следом за успешной копией.
            followup_caption = fallback_caption

    try:
        await message.copy_to(chat_id=user_id, **copy_kwargs)
    except TelegramForbiddenError as error:
        logger.warning("Доставка ответа админа пользователю user_id=%s не удалась (пользователь заблокировал бота): %s", user_id, error)
        await safe_answer(message, context=f'уведомление о блокировке (содержимое) admin_id={message.from_user.id}',
                           text='Пользователь заблокировал бота, поэтому ваше сообщение не доставлено.')
        await deactivated_user(user_id)
        return
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
        return

    if followup_caption is not None:
        await safe_send_message(
            bot, user_id,
            context=f'подпись к содержимому без caption user_id={user_id}',
            text=followup_caption
        )

    await _open_reply_window(bot, fsm_storage, user_id)


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

    if not message.text.strip():
        await safe_answer(
            message,
            context=f'пустое имя user_id={message.from_user.id}',
            text="⚠️ <i>Имя не может быть пустым. Пожалуйста, введите ваше имя:</i>"
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

    if not message.text.strip():
        await safe_answer(
            message,
            context=f'пустой текст заявки user_id={message.from_user.id}',
            text="⚠️ <i>Текст обращения не может быть пустым. Пожалуйста, опишите вашу заявку:</i>"
        )
        return

    await state.update_data(text=message.text)
    data = await state.get_data()

    final_text = _bid_card_text(data['name'], data['birthday'], data['text'])

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
        reply_markup=get_order_status_markup('new'),
        telegram_error_text='К сожалению, сейчас не получилось отправить заявку. Пожалуйста, попробуйте ещё раз чуть позже.',
        db_error_text='Не получилось обработать вашу заявку. Пожалуйста, отправьте её ещё раз через несколько минут.',
        not_registered_text='Вы ещё не зарегистрированы в боте. Пожалуйста, напишите /start, чтобы начать.',
        undelivered_text='Ваша заявка не была доставлена. Попробуйте отправить её ещё раз чуть позже.',
        success_text=(
            "✅ <b>Заявка отправлена!</b>\n\n"
            "Мы её получили. Администратор напишет вам в этот чат, чтобы уточнить детали "
            "и прислать реквизиты для оплаты. Сам разбор обычно готов в течение нескольких часов.\n\n"
            "<i>Чтобы отправить ещё одну заявку или задать вопрос, воспользуйтесь меню.</i>"
        ),
    )


@router.message(Question.question,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def save_question(message: types.Message, state: FSMContext, bot: Bot):
    attachment = None

    if message.photo or message.document:
        # Вопрос-вложение (фото чека, скриншот, файл): в Requests.text — пометка вида вложения и подпись.
        attachment = message
        kind = '[фото]' if message.photo else '[документ]'
        caption = (message.caption or '').strip()
        question = f'{kind} {caption}' if caption else kind
    elif message.text:
        question = message.text
    else:
        await safe_answer(
            message,
            context=f'нераспознанный текст вопроса user_id={message.from_user.id}',
            text='Пожалуйста, отправьте вопрос текстом, фото или документом.'
        )
        return

    if not question.strip():
        await safe_answer(
            message,
            context=f'пустой текст вопроса user_id={message.from_user.id}',
            text='⚠️ Вопрос не может быть пустым. Пожалуйста, напишите ваш вопрос:'
        )
        return

    await state.update_data(question=question)
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
        appeal_type='Question', save_text=question, attachment=attachment,
        telegram_error_text='К сожалению, сейчас не получилось отправить вопрос. Пожалуйста, попробуйте ещё раз чуть позже.',
        db_error_text='Не получилось обработать ваш вопрос. Пожалуйста, отправьте его ещё раз через несколько минут.',
        not_registered_text='Вы ещё не зарегистрированы в боте. Пожалуйста, напишите /start, чтобы начать.',
        undelivered_text='Ваш вопрос не был доставлен. Попробуйте отправить его ещё раз чуть позже.',
        success_text=(
            "✅ <b>Вопрос отправлен!</b>\n\n"
            "Администратор ответит вам прямо здесь, как только сможет.\n\n"
            "<i>Чтобы отправить ещё одно сообщение, воспользуйтесь меню.</i>"
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

    if not message.text.strip():
        await safe_answer(
            message,
            context=f'пустой текст рассылки admin_id={message.from_user.id}',
            text='⚠️ Текст рассылки не может быть пустым. Пожалуйста, отправьте текст рассылки.'
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

    draft_id = secrets.token_hex(4)

    await state.update_data(newsletter=message.text, draft_id=draft_id)
    await state.set_state(Newsletter.sure)

    await safe_answer(
        message,
        context=f'предпросмотр и запрос подтверждения рассылки admin_id={message.from_user.id}',
        text=_newsletter_preview_text(message.text),
        reply_markup=get_newsletter_confirm_markup(draft_id)
    )


@router.message(Newsletter.sure,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def accept_newsletter(message: types.Message):
    await safe_answer(
        message,
        context=f'некорректный вариант подтверждения рассылки admin_id={message.from_user.id}',
        text='Пожалуйста, воспользуйтесь кнопками «Подтвердить», «Изменить» или «Отменить».'
    )


# ------------------------------------------------------------ Подтверждение рассылки
#
# Превью и запрос подтверждения — одно сообщение с inline-кнопками NewsletterCallback.
# Решение по кнопке принимается только через атомарный PostgresStorage.transition_state
# (Newsletter.sure + текущий draft_id → новое состояние, data стирается): из любых
# повторных/гоночных нажатий — одной и той же или разных кнопок — эффект даёт ровно одно,
# остальные получают answer и снимают устаревшие кнопки. Переход делается ДО рассылки:
# крах посреди отправки не приведёт к повторной рассылке (at-most-once).

NEWSLETTER_PREVIEW_HEADER = (
    'Подтвердите отправку рассылки.\n\n'
    'Сообщение будет отправлено всем активным клиентам бота. Вот как оно выглядит:\n\n'
)
NEWSLETTER_PREVIEW_TRUNCATED = '\n\n<i>✂️ Предпросмотр обрезан — клиентам уйдёт полный текст.</i>'
NEWSLETTER_STALE_TEXT = 'Эта рассылка уже отправлена, изменена или отменена.'


def _newsletter_preview_text(newsletter: str, footer: str = '') -> str:
    """
    Превью рассылки для админа. Сам текст рассылки ограничен лимитом Telegram ещё на вводе,
    но вместе с заголовком и footer превью может его превысить — тогда обрезается только
    показ (по исходным символам, чтобы не разорвать HTML-сущность вроде &amp;), а клиентам
    уходит полный текст из FSM.
    """
    footer_part = f'\n\n{footer}' if footer else ''
    escaped = html.escape(newsletter)

    if telegram_text_length(f'{NEWSLETTER_PREVIEW_HEADER}{escaped}{footer_part}') <= TELEGRAM_MESSAGE_LIMIT:
        return f'{NEWSLETTER_PREVIEW_HEADER}{escaped}{footer_part}'

    budget = TELEGRAM_MESSAGE_LIMIT - telegram_text_length(
        f'{NEWSLETTER_PREVIEW_HEADER}{NEWSLETTER_PREVIEW_TRUNCATED}{footer_part}'
    )
    pieces = []
    used = 0

    for char in newsletter:
        escaped_char = html.escape(char)

        if used + telegram_text_length(escaped_char) > budget:
            break

        pieces.append(escaped_char)
        used += telegram_text_length(escaped_char)

    return f'{NEWSLETTER_PREVIEW_HEADER}{"".join(pieces)}{NEWSLETTER_PREVIEW_TRUNCATED}{footer_part}'


async def _decide_newsletter(callback: types.CallbackQuery, callback_data: NewsletterCallback,
                             state: FSMContext, bot: Bot, *, to_state, footer: str) -> dict | None:
    """
    Newsletter.sure (с этим draft_id) → to_state. Победитель получает прежнюю data, а превью
    теряет кнопки и получает footer с решением. Проигравший (повтор, гонка, старое превью) —
    None, кнопки снимаются, на callback отвечено.
    """
    old_data = await state.storage.transition_state(
        state.key, from_state=Newsletter.sure, match={'draft_id': callback_data.draft_id},
        to_state=to_state, to_data={}
    )

    if old_data is None:
        await _drop_stale_buttons(bot, callback)
        await safe_answer_callback(callback, context=f'неактуальное превью рассылки admin_id={callback.from_user.id}',
                                   text=NEWSLETTER_STALE_TEXT)
        return None

    if callback.message is not None:
        await safe_edit_message_text(
            bot, callback.message.chat.id, callback.message.message_id,
            context=f'итог превью рассылки admin_id={callback.from_user.id}',
            text=_newsletter_preview_text(old_data['newsletter'], footer)
        )

    return old_data


@router.callback_query(NewsletterCallback.filter(), F.from_user.id != Config.ADMIN_ID)
async def newsletter_callback_not_admin(callback: types.CallbackQuery):
    await safe_answer_callback(
        callback,
        context=f'кнопка рассылки не от админа user_id={callback.from_user.id}',
        text='Эта кнопка доступна только администратору.', show_alert=True
    )


@router.callback_query(NewsletterCallback.filter(F.action == 'confirm'), F.from_user.id == Config.ADMIN_ID)
async def newsletter_confirm(callback: types.CallbackQuery, callback_data: NewsletterCallback,
                             state: FSMContext, bot: Bot):
    admin_id = callback.from_user.id

    data = await _decide_newsletter(callback, callback_data, state, bot, to_state=None,
                                    footer='✅ Рассылка подтверждена — отправляем…')

    if data is None:
        return

    # Отвечаем сразу: рассылка по большой базе может идти дольше, чем живёт callback_query.
    await safe_answer_callback(callback, context=f'рассылка запущена admin_id={admin_id}', text='Рассылка запущена.')

    users = await get_users()

    if users is None:
        await safe_send_message(
            bot, admin_id,
            context=f'отсутствие пользователей для рассылки admin_id={admin_id}',
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

    await safe_send_message(
        bot, admin_id,
        context=f'итоги рассылки admin_id={admin_id}',
        text=f'Рассылка завершена\n\n'
             f'Отправлено: {sent}\n'
             f'Не доставлено (не отправлено): {not_sent}\n\n'
             f'Всего пользователей в базе: {len(users)}\n'
             f'Из них активных: {sent + not_sent}',
        reply_markup=get_admin_keyboard()
    )


@router.callback_query(NewsletterCallback.filter(F.action == 'edit'), F.from_user.id == Config.ADMIN_ID)
async def newsletter_edit(callback: types.CallbackQuery, callback_data: NewsletterCallback,
                          state: FSMContext, bot: Bot):
    admin_id = callback.from_user.id

    data = await _decide_newsletter(callback, callback_data, state, bot, to_state=Newsletter.text,
                                    footer='✏️ Текст рассылки меняется.')

    if data is None:
        return

    await safe_answer_callback(callback, context=f'изменение рассылки admin_id={admin_id}')

    await safe_send_message(
        bot, admin_id,
        context=f'повторный запрос текста рассылки admin_id={admin_id}',
        text='Введите текст рассылки:',
        reply_markup=get_cancel_keyboard()
    )


@router.callback_query(NewsletterCallback.filter(F.action == 'cancel'), F.from_user.id == Config.ADMIN_ID)
async def newsletter_cancel(callback: types.CallbackQuery, callback_data: NewsletterCallback,
                            state: FSMContext, bot: Bot):
    admin_id = callback.from_user.id

    data = await _decide_newsletter(callback, callback_data, state, bot, to_state=None,
                                    footer='❌ Рассылка отменена.')

    if data is None:
        return

    await safe_answer_callback(callback, context=f'отмена рассылки admin_id={admin_id}', text='Рассылка отменена.')

    await safe_send_message(
        bot, admin_id,
        context=f'меню администратора после отмены рассылки admin_id={admin_id}',
        text='Главное меню администратора.',
        reply_markup=get_admin_keyboard()
    )


# ------------------------------------------------------------ Правка прайса и «О нас»
#
# Вход: текущее значение отдельным сообщением (как есть, экранированным — удобно скопировать),
# затем приглашение и клавиатура «Меню». В состоянии ввода любой текст — новый вариант (тексты
# админских кнопок не перехватываются), но в Settings он попадает только после превью «как увидит
# клиент» и кнопки «Сохранить». Черновик и draft_id лежат в FSM-данных админа (PostgresStorage).
# Решение по кнопке — атомарный transition_state из <цель>.confirm с этим draft_id, как у рассылки:
# из повторных/гоночных нажатий любых кнопок эффект даёт ровно одно, кнопки старых превью и превью
# другой цели не проходят проверку. «Меню» и /start в этих состояниях перехватывают menu/cmd_start
# (зарегистрированы выше): state.clear() стирает черновик, поздние нажатия его превью — no-op.

@dataclass(frozen=True)
class _SettingsTarget:
    input_state: State
    confirm_state: State
    get_current: Callable[[], Awaitable[str | None]]
    render: Callable[[str], str]
    too_long: Callable[[str], bool]
    save: Callable[[str], Awaitable[bool]]
    log_name: str
    not_set_text: str
    prompt_text: str
    prompt_again_text: str
    not_text_text: str
    empty_text: str
    too_long_text: str
    preview_header: str
    saved_text: str
    cancelled_text: str
    stale_on_save_text: str
    save_failed_text: str


# Функции берутся из модуля в момент вызова (lambda), а не при импорте: рендер всегда видит
# актуальный шаблон, и повторная проверка при «Сохранить» считает тем же кодом, что и отправка.
_SETTINGS_TARGETS = {
    'price': _SettingsTarget(
        input_state=ChangePrice.price,
        confirm_state=ChangePrice.confirm,
        get_current=lambda: get_price(),
        render=lambda text: render_welcome_text(text),
        too_long=lambda text: exceeds_welcome_limit(text),
        save=lambda text: set_price(text),
        log_name='прайс',
        not_set_text=(
            'Прайс ещё не задан — сейчас клиенты видят стандартный текст:\n\n'
            f'«{html.escape(DEFAULT_PRICE_FALLBACK)}»'
        ),
        prompt_text=(
            '✏️ Пришлите новый прайс целиком, одним сообщением. Перед сохранением я покажу, '
            'как его увидят клиенты.\n\n'
            'Чтобы выйти без изменений, нажмите «Меню».'
        ),
        prompt_again_text=(
            '✏️ Выше — прежний вариант, его можно скопировать и поправить. Пришлите новый прайс '
            'одним сообщением.\n\n'
            'Чтобы выйти без изменений, нажмите «Меню».'
        ),
        not_text_text='⚠️ <i>Пожалуйста, отправьте прайс текстовым сообщением:</i>',
        empty_text='⚠️ <i>Прайс не может быть пустым. Пожалуйста, отправьте текст прайса:</i>',
        too_long_text=PRICE_TOO_LONG_TEXT,
        preview_header='👇 Так клиенты увидят приветствие с новым прайсом. Сохранить?',
        saved_text='✅ Прайс сохранён — клиенты уже видят новый.',
        cancelled_text='Изменение прайса отменено, прайс остался прежним.',
        stale_on_save_text=(
            '⚠️ Прайс не сохранён: приветствие с ним больше не помещается в лимит Telegram. '
            'Сократите текст и отправьте заново — прежний вариант ниже.'
        ),
        save_failed_text='⚠️ Прайс не удалось сохранить. Пожалуйста, попробуйте ещё раз — прежний вариант ниже.',
    ),
    'about': _SettingsTarget(
        input_state=ChangeAboutUs.about_us_text,
        confirm_state=ChangeAboutUs.confirm,
        get_current=lambda: get_about_us(),
        render=lambda text: render_about_us_text(text),
        too_long=lambda text: exceeds_about_us_limit(text),
        save=lambda text: set_about_us_text(text),
        log_name='"О нас"',
        not_set_text=(
            'Раздел «О нас» ещё не заполнен — сейчас клиенты видят стандартный текст:\n\n'
            f'«{html.escape(ABOUT_US_FALLBACK)}»'
        ),
        prompt_text=(
            '✏️ Пришлите новый текст раздела «О нас» — описание вашего сервиса и услуг — целиком, '
            'одним сообщением. Перед сохранением я покажу, как его увидят клиенты.\n\n'
            'Чтобы выйти без изменений, нажмите «Меню».'
        ),
        prompt_again_text=(
            '✏️ Выше — прежний вариант, его можно скопировать и поправить. Пришлите новый текст '
            'раздела «О нас» одним сообщением.\n\n'
            'Чтобы выйти без изменений, нажмите «Меню».'
        ),
        not_text_text='⚠️ <i>Пожалуйста, отправьте описание текстовым сообщением:</i>',
        empty_text='⚠️ <i>Описание не может быть пустым. Пожалуйста, отправьте текст «О нас»:</i>',
        too_long_text=ABOUT_US_TOO_LONG_TEXT,
        preview_header='👇 Так клиенты увидят раздел «О нас». Сохранить?',
        saved_text='✅ Текст «О нас» сохранён — клиенты уже видят новый.',
        cancelled_text='Изменение «О нас» отменено, текст остался прежним.',
        stale_on_save_text=(
            '⚠️ Текст не сохранён: раздел «О нас» с ним больше не помещается в лимит Telegram. '
            'Сократите текст и отправьте заново — прежний вариант ниже.'
        ),
        save_failed_text='⚠️ Текст не удалось сохранить. Пожалуйста, попробуйте ещё раз — прежний вариант ниже.',
    ),
}

SETTINGS_STALE_TEXT = 'Это превью уже сохранено, изменено или отменено.'
SETTINGS_CONFIRM_HINT = (
    'Пожалуйста, воспользуйтесь кнопками «Сохранить», «Изменить» или «Отменить» под превью — '
    'или нажмите «Меню», чтобы выйти без изменений.'
)
SETTINGS_CURRENT_UNAVAILABLE_TEXT = (
    '⚠️ Текущий текст показать не удалось (он слишком длинный или Telegram вернул ошибку). '
    'Можно просто прислать новый.'
)
SETTINGS_SAVED_FOOTER = '✅ Сохранено.'
SETTINGS_NOT_SAVED_FOOTER = '⚠️ Не сохранено.'
SETTINGS_EDIT_FOOTER = '✏️ Текст меняется.'
SETTINGS_CANCEL_FOOTER = '❌ Изменение отменено.'


async def _start_settings_edit(message: types.Message, state: FSMContext, target: str) -> None:
    settings_target = _SETTINGS_TARGETS[target]
    admin_id = message.from_user.id

    # Состояние ввода — до показа текущего значения: что бы ни случилось с этим показом, админ
    # остаётся в сценарии и получает приглашение ниже.
    await state.set_state(settings_target.input_state)

    # Сломанное значение (например, сохранённое до проверки длины) не должно запирать правку:
    # именно тогда её и нужно сделать. Исключение не уходит в dp.errors(), чтобы админ не получил
    # поверх приглашения общее «попробуйте ещё раз».
    shown = False

    try:
        current = await settings_target.get_current()

        if current is None:
            shown = await safe_answer(message, context=f'текущий {settings_target.log_name} admin_id={admin_id}',
                                      text=settings_target.not_set_text)
        elif exceeds_telegram_limit(html.escape(current)):
            logger.warning("Текущий %s не помещается в сообщение Telegram, не показан admin_id=%s",
                           settings_target.log_name, admin_id)
        else:
            shown = await safe_answer(message, context=f'текущий {settings_target.log_name} admin_id={admin_id}',
                                      text=html.escape(current))
    except Exception:
        logger.exception("Не удалось показать текущий %s admin_id=%s", settings_target.log_name, admin_id)

    if not shown:
        await safe_answer(message, context=f'текущий {settings_target.log_name} не показан admin_id={admin_id}',
                          text=SETTINGS_CURRENT_UNAVAILABLE_TEXT)

    await safe_answer(
        message,
        context=f'запрос нового текста {settings_target.log_name} admin_id={admin_id}',
        text=settings_target.prompt_text,
        reply_markup=get_cancel_keyboard()
    )


async def _send_draft_for_rework(bot: Bot, admin_id: int, settings_target: _SettingsTarget, draft: str) -> None:
    """Прежний черновик отдельным сообщением (чтобы скопировать и поправить), затем приглашение и «Меню»."""
    await safe_send_message(
        bot, admin_id,
        context=f'прежний черновик {settings_target.log_name} admin_id={admin_id}',
        text=html.escape(draft)
    )
    await safe_send_message(
        bot, admin_id,
        context=f'повторный запрос текста {settings_target.log_name} admin_id={admin_id}',
        text=settings_target.prompt_again_text,
        reply_markup=get_cancel_keyboard()
    )


async def _accept_settings_draft(message: types.Message, state: FSMContext, target: str) -> None:
    """Шаг ввода: проверки и превью. В Settings здесь не пишется ничего."""
    settings_target = _SETTINGS_TARGETS[target]
    admin_id = message.from_user.id

    if not message.text:
        await safe_answer(message, context=f'нераспознанный текст {settings_target.log_name} admin_id={admin_id}',
                          text=settings_target.not_text_text)
        return

    if not message.text.strip():
        await safe_answer(message, context=f'пустой текст {settings_target.log_name} admin_id={admin_id}',
                          text=settings_target.empty_text)
        return

    # Клиенту текст уходит одним сообщением: если оно не влезет в лимит, его не получит никто.
    # Проверяем тем же рендером, что и реальная отправка; состояние ввода не трогаем — админ
    # сразу присылает новый вариант.
    if settings_target.too_long(message.text):
        await safe_answer(message, context=f'слишком длинный {settings_target.log_name} admin_id={admin_id}',
                          text=settings_target.too_long_text)
        return

    draft_id = secrets.token_hex(4)

    await state.update_data(draft=message.text, draft_id=draft_id)
    await state.set_state(settings_target.confirm_state)

    # Заголовок — отдельным сообщением: превью байт-в-байт совпадает с тем, что получит клиент,
    # и текст ровно на пределе лимита тоже можно показать.
    await safe_answer(message, context=f'заголовок превью {settings_target.log_name} admin_id={admin_id}',
                      text=settings_target.preview_header)
    await safe_answer(
        message,
        context=f'превью {settings_target.log_name} admin_id={admin_id}',
        text=settings_target.render(message.text),
        reply_markup=get_settings_confirm_markup(target, draft_id)
    )


async def _take_settings_draft(callback: types.CallbackQuery, callback_data: SettingsCallback,
                               state: FSMContext, bot: Bot, *, to_input: bool) -> dict | None:
    """
    <цель>.confirm (с этим draft_id) → состояние ввода (to_input) или None, data стирается.
    Победитель получает прежнюю data — черновик берётся только из неё. Проигравший (повтор, гонка,
    старое превью, превью другой цели, неизвестная цель) — None, кнопки снимаются, на callback отвечено.
    """
    settings_target = _SETTINGS_TARGETS.get(callback_data.target)
    old_data = None

    if settings_target is not None:
        old_data = await state.storage.transition_state(
            state.key, from_state=settings_target.confirm_state, match={'draft_id': callback_data.draft_id},
            to_state=settings_target.input_state if to_input else None, to_data={}
        )

    if old_data is None:
        await _drop_stale_buttons(bot, callback)
        await safe_answer_callback(callback, context=f'неактуальное превью настроек admin_id={callback.from_user.id}',
                                   text=SETTINGS_STALE_TEXT)
        return None

    return old_data


async def _close_settings_preview(bot: Bot, callback: types.CallbackQuery, settings_target: _SettingsTarget,
                                  draft: str, footer: str) -> None:
    """Снимает кнопки с превью и дописывает итог; если с итогом превью не влезет в лимит — только снимает кнопки."""
    if callback.message is None:
        return

    text = f'{settings_target.render(draft)}\n\n{footer}'

    if telegram_text_length(text) > TELEGRAM_MESSAGE_LIMIT:
        await _drop_stale_buttons(bot, callback)
        return

    await safe_edit_message_text(
        bot, callback.message.chat.id, callback.message.message_id,
        context=f'итог превью {settings_target.log_name} admin_id={callback.from_user.id}',
        text=text
    )


@router.message(ChangeAboutUs.about_us_text,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def set_about_us(message: types.Message, state: FSMContext):
    await _accept_settings_draft(message, state, 'about')


@router.message(ChangePrice.price,
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def set_price_text(message: types.Message, state: FSMContext):
    await _accept_settings_draft(message, state, 'price')


@router.message(StateFilter(ChangePrice.confirm, ChangeAboutUs.confirm),
                F.chat.type == 'private',
                F.from_user.id == Config.ADMIN_ID)
async def settings_confirm_hint(message: types.Message):
    await safe_answer(
        message,
        context=f'текст вместо кнопок превью настроек admin_id={message.from_user.id}',
        text=SETTINGS_CONFIRM_HINT
    )


@router.callback_query(SettingsCallback.filter(), F.from_user.id != Config.ADMIN_ID)
async def settings_callback_not_admin(callback: types.CallbackQuery):
    await safe_answer_callback(
        callback,
        context=f'кнопка превью настроек не от админа user_id={callback.from_user.id}',
        text='Эта кнопка доступна только администратору.', show_alert=True
    )


@router.callback_query(SettingsCallback.filter(F.action == 'save'), F.from_user.id == Config.ADMIN_ID)
async def settings_save(callback: types.CallbackQuery, callback_data: SettingsCallback, state: FSMContext, bot: Bot):
    admin_id = callback.from_user.id

    data = await _take_settings_draft(callback, callback_data, state, bot, to_input=False)

    if data is None:
        return

    settings_target = _SETTINGS_TARGETS[callback_data.target]
    draft = data['draft']
    saved = False

    # Между превью и нажатием могло измениться то, во что текст вставляется (шаблон приветствия),
    # поэтому лимит проверяется ещё раз тем же рендером.
    if settings_target.too_long(draft):
        reason = settings_target.stale_on_save_text
    else:
        reason = settings_target.save_failed_text

        # Исключение ловим здесь, а не в dp.errors(): состояние уже снято переходом, и без
        # восстановления ниже черновик пропал бы, а админ получил бы только общее «попробуйте ещё раз».
        try:
            saved = await settings_target.save(draft)
        except Exception:
            logger.exception("Не удалось сохранить %s admin_id=%s", settings_target.log_name, admin_id)

    if saved:
        await _close_settings_preview(bot, callback, settings_target, draft, SETTINGS_SAVED_FOOTER)
        await safe_answer_callback(callback, context=f'сохранён {settings_target.log_name} admin_id={admin_id}',
                                   text='Сохранено.')
        await safe_send_message(
            bot, admin_id,
            context=f'успешное изменение {settings_target.log_name} admin_id={admin_id}',
            text=settings_target.saved_text,
            reply_markup=get_admin_keyboard()
        )
        return

    # Не сохранено: возвращаем админа к вводу с тем же черновиком. Между переходом выше и этими
    # строками состояние на мгновение пустое — в это окно ни одна кнопка превью уже не сработает
    # (они ждут <цель>.confirm), а текст админа обработается как вне сценария.
    await state.set_state(settings_target.input_state)
    await state.update_data(draft=draft, draft_id=data['draft_id'])

    await _close_settings_preview(bot, callback, settings_target, draft, SETTINGS_NOT_SAVED_FOOTER)
    await safe_answer_callback(callback, context=f'не сохранён {settings_target.log_name} admin_id={admin_id}',
                               text='Не сохранено.')
    await safe_send_message(
        bot, admin_id,
        context=f'ошибка сохранения {settings_target.log_name} admin_id={admin_id}',
        text=reason
    )
    await _send_draft_for_rework(bot, admin_id, settings_target, draft)


@router.callback_query(SettingsCallback.filter(F.action == 'edit'), F.from_user.id == Config.ADMIN_ID)
async def settings_edit(callback: types.CallbackQuery, callback_data: SettingsCallback, state: FSMContext, bot: Bot):
    admin_id = callback.from_user.id

    data = await _take_settings_draft(callback, callback_data, state, bot, to_input=True)

    if data is None:
        return

    settings_target = _SETTINGS_TARGETS[callback_data.target]

    await _close_settings_preview(bot, callback, settings_target, data['draft'], SETTINGS_EDIT_FOOTER)
    await safe_answer_callback(callback, context=f'изменение черновика {settings_target.log_name} admin_id={admin_id}')
    await _send_draft_for_rework(bot, admin_id, settings_target, data['draft'])


@router.callback_query(SettingsCallback.filter(F.action == 'cancel'), F.from_user.id == Config.ADMIN_ID)
async def settings_cancel(callback: types.CallbackQuery, callback_data: SettingsCallback, state: FSMContext, bot: Bot):
    admin_id = callback.from_user.id

    data = await _take_settings_draft(callback, callback_data, state, bot, to_input=False)

    if data is None:
        return

    settings_target = _SETTINGS_TARGETS[callback_data.target]

    await _close_settings_preview(bot, callback, settings_target, data['draft'], SETTINGS_CANCEL_FOOTER)
    await safe_answer_callback(callback, context=f'отмена изменения {settings_target.log_name} admin_id={admin_id}',
                               text='Изменение отменено.')
    await safe_send_message(
        bot, admin_id,
        context=f'меню администратора после отмены изменения {settings_target.log_name} admin_id={admin_id}',
        text=settings_target.cancelled_text,
        reply_markup=get_admin_keyboard()
    )


# ------------------------------------------------------------ Окно ответа клиента
#
# После успешного Reply админа на карточку (reply_to_message) клиент может просто ответить
# сообщением — оно уйдёт в его тему, а не упрётся в free_text_hint. Нового состояния FSM и фоновой
# задачи нет: в data клиента (только при state None) лежат ISO-метки UTC, сравниваемые с now()
# в момент прихода сообщения:
#   awaiting_reply_since — последний ответ админа; ждёт первого сообщения клиента до REPLY_AWAITING_TTL;
#   reply_window_until   — после пересылки: ещё REPLY_WINDOW на дополнения, каждая пересылка
#                          пересчитывает его от текущего момента.
# Окно открыто, если действует хотя бы одна метка: так повторный ответ админа при ещё активном окне
# не теряется, когда окно истечёт. Протухшие метки не удаляются, просто не срабатывают; явное
# действие клиента (кнопка главного меню, «Меню», /start) их стирает.
#
# Хендлер стоит прямо перед free_text_hint: формы, диалог, кнопки и команды обрабатываются раньше
# и в приоритете (плюс StateFilter(None)).

REPLY_AWAITING_TTL = timedelta(days=7)
REPLY_WINDOW = timedelta(minutes=10)
REPLY_WINDOW_KEYS = ('awaiting_reply_since', 'reply_window_until')

CLIENT_REPLY_NOTE_TEXT = '↩️ Ответ клиента:'
CLIENT_REPLY_FORWARDED_TEXT = (
    '✅ Передали администратору. Если нужно добавить что-то ещё — можно написать в течение ближайших 10 минут.'
)
CLIENT_REPLY_FORWARDED_AGAIN_TEXT = '✅ Передали и это. Дописать можно ещё в течение 10 минут.'


def _parse_reply_mark(value) -> datetime | None:
    """ISO-метка окна ответа; битое, нестроковое или без часового пояса значение — как отсутствующее."""
    if not isinstance(value, str):
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    return parsed if parsed.tzinfo is not None else None


def _reply_window_status(data: dict, now: datetime) -> str | None:
    """'window' — идёт окно дополнений, 'awaiting' — ждём первого ответа клиента, None — окно закрыто."""
    until = _parse_reply_mark(data.get('reply_window_until'))

    if until is not None and now < until:
        return 'window'

    since = _parse_reply_mark(data.get('awaiting_reply_since'))

    if since is not None and now - since < REPLY_AWAITING_TTL:
        return 'awaiting'

    return None


async def _open_reply_window(bot: Bot, storage, client_id: int) -> None:
    """
    Отметка «клиенту есть на что ответить» после доставленного ответа админа. В форме/диалоге не
    ставится (update_data_if_no_state): там сообщения клиента обрабатывает своё состояние, а его выход
    всё равно стёр бы данные. Сбой БД только логируется — ответ уже у клиента, повтор был бы дублем.
    """
    try:
        await storage.update_data_if_no_state(
            _client_key(bot, client_id),
            {'awaiting_reply_since': datetime.now(timezone.utc).isoformat()}
        )
    except SQLAlchemyError as error:
        logger.error("Не удалось открыть окно ответа user_id=%s (ошибка БД): %s", client_id, error)


async def _close_reply_window(state: FSMContext) -> None:
    """Явное действие клиента в главном меню закрывает окно ответа досрочно."""
    await state.storage.patch_data(state.key, remove=REPLY_WINDOW_KEYS)


async def reply_window_open(message: types.Message, state: FSMContext) -> dict | bool:
    """Фильтр: у клиента открыто окно ответа. Передаёт в хендлер reply_continues (идёт окно дополнений)."""
    status = _reply_window_status(await state.get_data(), datetime.now(timezone.utc))

    if status is None:
        return False

    return {'reply_continues': status == 'window'}


@router.message(StateFilter(None),
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                reply_window_open)
async def forward_client_reply(message: types.Message, state: FSMContext, bot: Bot, reply_continues: bool):
    user = message.from_user
    target = await _client_dialog_topic(message)

    if target is None:
        return

    group_id, topic_id = target

    await safe_send_message(
        bot, group_id,
        context=f'пометка ответа клиента user_id={user.id}',
        text=CLIENT_REPLY_NOTE_TEXT, message_thread_id=topic_id
    )

    if not await _copy_client_message_to_topic(message, group_id, topic_id):
        return

    # Пересчёт от текущего момента, а не сумма: серия быстрых сообщений не обрывается посередине.
    # Сбой записи не откатывает пересылку: остаётся прежняя метка, и окно живёт по ней.
    try:
        await state.storage.patch_data(
            state.key, remove=['awaiting_reply_since'],
            merge={'reply_window_until': (datetime.now(timezone.utc) + REPLY_WINDOW).isoformat()}
        )
    except SQLAlchemyError as error:
        logger.error("Ответ клиента user_id=%s переслан, но окно ответа не обновлено (ошибка БД): %s", user.id, error)

    await safe_answer(
        message,
        context=f'ответ клиента передан user_id={user.id}',
        text=CLIENT_REPLY_FORWARDED_AGAIN_TEXT if reply_continues else CLIENT_REPLY_FORWARDED_TEXT
    )


# Регистрируется последним: срабатывает, только если сообщение клиента вне формы/диалога не
# подошло ни одной кнопке или команде выше и окно ответа закрыто. Ничего не сохраняет и никуда не пересылает.
FREE_TEXT_HINT = (
    '🙂 Я понимаю только кнопки меню.\n\n'
    'Чтобы оставить заявку, задать вопрос или написать администратору — выберите нужный пункт ниже 👇'
)


@router.message(StateFilter(None),
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def free_text_hint(message: types.Message):
    await safe_answer(
        message,
        context=f'подсказка на сообщение вне меню user_id={message.from_user.id}',
        text=FREE_TEXT_HINT,
        reply_markup=get_main_keyboard()
    )
