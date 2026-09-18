import asyncio
from datetime import datetime
import html
import logging
import secrets

from aiogram import Router, types, F, Bot
from aiogram.enums import ContentType
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy.exc import SQLAlchemyError

from app.callbacks import DialogCallback, NewsletterCallback, OrderStatusCallback
from app.keyboards import (get_main_keyboard,
                           get_cancel_keyboard,
                           get_back_cancel_keyboard,
                           get_admin_keyboard, get_newsletter_confirm_markup,
                           get_dialog_waiting_keyboard, get_dialog_active_keyboard,
                           get_dialog_request_markup, get_dialog_status_markup,
                           get_order_status_markup)
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
                             transition_request_status, get_bid_card_by_group_message_id)
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


async def _heal_dead_topic(bot: Bot, user: types.User, topic_id: int) -> None:
    """
    Тема клиента есть в БД, но в Telegram её уже нет (удалили вручную). Без
    сброса привязки каждое следующее обращение этого клиента падало бы в ту же
    несуществующую тему — бесконечный цикл «не доставлено» без шанса
    самовосстановиться. Сбрасываем Users.topic_id (следующая попытка пойдёт
    штатным путём resolve_client_topic и создаст новую тему) и сообщаем админу,
    чтобы он не узнал о поломке от клиента.
    """
    logger.error(
        "Тема topic_id=%s клиента user_id=%s недоступна в Telegram (удалена?) — сбрасываем привязку, "
        "следующее обращение создаст новую тему",
        topic_id, user.id
    )

    try:
        cleared = await clear_user_thread_id(user.id, topic_id)
    except SQLAlchemyError as error:
        logger.error(
            "Не удалось сбросить topic_id=%s у user_id=%s после недоступной темы (ошибка БД): %s",
            topic_id, user.id, error
        )
        return

    if not cleared:
        # Привязку уже сбросила (или заменила) параллельная попытка того же клиента —
        # второе уведомление админу о том же инциденте не нужно.
        logger.info(
            "topic_id=%s у user_id=%s к моменту сброса уже был изменён — уведомление админу не дублируем",
            topic_id, user.id
        )
        return

    await safe_send_message(
        bot, Config.ADMIN_ID,
        context=f'уведомление админу о недоступной теме user_id={user.id} topic_id={topic_id}',
        text=DEAD_TOPIC_ADMIN_NOTICE.format(client=html.escape(user.full_name), user_id=user.id)
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

    delivered_message, send_error = await send_message_capturing_error(
        bot, group_id,
        context=f'обращение (type={appeal_type}) в группу user_id={user.id} topic_id={topic_id}',
        text=final_text, message_thread_id=topic_id, reply_markup=reply_markup
    )

    if not delivered_message:
        if send_error is not None and is_dead_topic_error(send_error):
            await _heal_dead_topic(bot, user, topic_id)

        await safe_answer(message, context=f'уведомление о недоставке user_id={user.id}', text=undelivered_text)
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

DIALOG_REQUEST_TEXT = (
    '💬 <b>Клиент запрашивает диалог</b>\n\n'
    'После подтверждения все ваши сообщения в этой теме будут уходить клиенту напрямую, без Reply.'
)
DIALOG_ACTIVE_TEXT = (
    '🟢 <b>Диалог активен</b>\n\n'
    'Пишите в эту тему — сообщения уходят клиенту напрямую. Сообщения клиента появятся здесь.'
)


def _client_key(bot: Bot, client_id: int) -> StorageKey:
    return StorageKey(bot_id=bot.id, chat_id=client_id, user_id=client_id)


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


async def _close_dialog_request(bot: Bot, storage, client_id: int, dialog_id: str, *, by_admin: bool) -> bool:
    """
    Dialog.waiting → None: отклонение админом (by_admin=True) или отмена клиентом.
    Возвращает False, если переход уже сделала другая сторона — тогда ничего не шлёт.
    """
    old_data = await storage.transition_state(
        _client_key(bot, client_id),
        from_state=Dialog.waiting, match={'dialog_id': dialog_id}, to_state=None, to_data={}
    )

    if old_data is None:
        return False

    group_id = await get_group_id()
    topic_id = await get_user_thread_id(client_id)
    request_message_id = old_data.get('request_message_id')

    if by_admin:
        request_result_text = '❌ Запрос отклонён администратором.'
        client_text = (
            'К сожалению, сейчас администратор не может начать диалог. '
            'Вы можете задать вопрос или оставить заявку — мы обязательно ответим.'
        )
    else:
        request_result_text = '❌ Клиент отменил запрос на диалог.'
        client_text = 'Запрос на диалог отменён.'

    if group_id is not None and request_message_id is not None:
        await safe_edit_message_text(
            bot, group_id, request_message_id,
            context=f'итог запроса на диалог user_id={client_id}',
            text=f'{DIALOG_REQUEST_TEXT}\n\n{request_result_text}'
        )

    if not by_admin and group_id is not None and topic_id is not None:
        await safe_send_message(
            bot, group_id,
            context=f'уведомление об отмене запроса на диалог user_id={client_id}',
            text=request_result_text, message_thread_id=topic_id
        )

    await _notify_client(
        bot, client_id,
        context='закрытие запроса на диалог',
        text=client_text, reply_markup=get_main_keyboard()
    )

    return True


async def _finish_active_dialog(bot: Bot, storage, client_id: int, dialog_id: str, *, ended_by: str) -> bool:
    """
    Dialog.active → None. ended_by: 'client' | 'admin' | 'blocked' (бот заблокирован
    клиентом — выяснилось при пересылке). Статус-сообщение в теме в любом случае
    теряет кнопку «Завершить»; вторая сторона получает явное уведомление. Возвращает
    False (и ничего не шлёт), если диалог уже завершила другая сторона.
    """
    old_data = await storage.transition_state(
        _client_key(bot, client_id),
        from_state=Dialog.active, match={'dialog_id': dialog_id}, to_state=None, to_data={}
    )

    if old_data is None:
        return False

    group_id = await get_group_id()
    topic_id = await get_user_thread_id(client_id)
    status_message_id = old_data.get('status_message_id')

    status_texts = {
        'client': '🔴 <b>Диалог завершён клиентом</b>',
        'admin': '🔴 <b>Диалог завершён администратором</b>',
        'blocked': '🔴 <b>Диалог завершён</b>: клиент заблокировал бота',
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

        return True

    if group_id is not None and topic_id is not None:
        topic_text = (
            '🔴 Клиент вышел из диалога. Сообщения в этой теме больше не пересылаются клиенту.'
            if ended_by == 'client' else
            '🔴 Клиент заблокировал бота — сообщение не доставлено, диалог завершён.'
        )
        await safe_send_message(
            bot, group_id,
            context=f'уведомление о завершении диалога ({ended_by}) user_id={client_id}',
            text=topic_text, message_thread_id=topic_id
        )

    if ended_by == 'client':
        await _notify_client(
            bot, client_id,
            context='выход клиента из диалога',
            text='Вы вышли из диалога с администратором.',
            reply_markup=get_main_keyboard()
        )

    return True


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
        # Проигрыш перехода здесь означает, что диалог уже завершил админ и сам уведомил клиента.
        await _finish_active_dialog(bot, state.storage, client_id, dialog_id, ended_by='client')
        return True

    if current_state != Dialog.waiting.state:
        return True

    if await _close_dialog_request(bot, state.storage, client_id, dialog_id, by_admin=False):
        return True

    # Отмена проиграла переход. Из waiting выходят только в None (отклонение — клиент уже
    # получил ответ) или в active (подтверждение); назад в waiting состояние не возвращается,
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
    await state.set_state(Dialog.waiting)
    await state.set_data({'dialog_id': dialog_id})

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
             'Как только администратор подключится, вы получите уведомление и сможете переписываться напрямую.',
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
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def dialog_waiting_hint(message: types.Message):
    await safe_answer(
        message,
        context=f'подсказка ожидания диалога user_id={message.from_user.id}',
        text='⏳ Ваш запрос на диалог ожидает решения администратора. '
             'Если передумали — нажмите «Отменить запрос».',
        reply_markup=get_dialog_waiting_keyboard()
    )


@router.message(Dialog.active,
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID)
async def forward_client_dialog_message(message: types.Message, bot: Bot):
    user = message.from_user
    group_id = await get_group_id()
    topic_id = await get_user_thread_id(user.id)

    if group_id is None or topic_id is None:
        logger.error("Активный диалог без группы/темы: user_id=%s group_id=%s topic_id=%s", user.id, group_id, topic_id)
        await safe_answer(
            message,
            context=f'диалог без темы user_id={user.id}',
            text='Сообщение не доставлено: бот временно не работает. Попробуйте позже.'
        )
        return

    try:
        await message.copy_to(chat_id=group_id, message_thread_id=topic_id)
    except TelegramAPIError as error:
        logger.warning("Пересылка сообщения клиента user_id=%s в тему topic_id=%s не удалась: %s", user.id, topic_id, error)
        await safe_answer(
            message,
            context=f'сообщение диалога не доставлено user_id={user.id}',
            text='Сообщение не доставлено администратору. Попробуйте отправить его ещё раз чуть позже.'
        )


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
        # Клиент успел отменить запрос (или /start) между предпроверкой и переходом —
        # он уже получил «Запрос отменён», а тема — уведомление об отмене.
        await safe_edit_message_text(
            bot, group_id, status_message.message_id,
            context=f'статус неначатого диалога user_id={client_id}',
            text='⚪️ <b>Диалог не начат</b>: клиент отменил запрос'
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
        bot, fsm_storage, callback_data.client_id, callback_data.dialog_id, by_admin=True
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


# Как и остальные кнопки главного меню клиента — StateFilter(None) и регистрация выше
# free_text_hint. Внутри форм/диалога главного меню на экране нет (там своя клавиатура),
# а набранный вручную текст кнопки в форме — данные формы, как и у «О нас».
@router.message(F.text == 'Показать прайс',
                F.chat.type == 'private',
                F.from_user.id != Config.ADMIN_ID,
                StateFilter(None))
async def show_price(message: types.Message):
    await send_welcome_menu(message, 'показ прайса')


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
            "Мы получили её и свяжемся с вами лично, как только разбор будет готов — "
            "это может занять некоторое время.\n\n"
            "<i>Чтобы отправить ещё одну заявку или задать вопрос, воспользуйтесь меню.</i>"
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

    # Приветствие с прайсом уходит клиентам одним сообщением: если оно не влезет в лимит,
    # его не получит никто. Проверяем тем же рендером, что и реальная отправка; состояние
    # ChangePrice.price не трогаем — админ сразу присылает новый вариант.
    if exceeds_welcome_limit(message.text):
        await safe_answer(
            message,
            context=f'слишком длинный прайс admin_id={message.from_user.id}',
            text=PRICE_TOO_LONG_TEXT
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


# Регистрируется последним: срабатывает, только если сообщение клиента вне формы/диалога не
# подошло ни одной кнопке или команде выше. Ничего не сохраняет и никуда не пересылает.
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
