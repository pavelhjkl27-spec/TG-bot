import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Message, Update

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096

# Часовой пояс клиента Telegram боту не сообщает, поэтому все даты для клиента —
# по Москве, с явной пометкой.
CLIENT_TIMEZONE = ZoneInfo('Europe/Moscow')
CLIENT_TIMEZONE_LABEL = 'МСК'

UPDATE_ERROR_TEXT = '⚠️ Что-то пошло не так, попробуйте, пожалуйста, ещё раз.'
UPDATE_ERROR_CALLBACK_TEXT = 'Произошла ошибка, попробуйте ещё раз'

# Фрагменты текста ошибки Telegram, означающие ровно одно: темы, в которую бот
# пытался написать, в группе больше нет (её удалили вручную). Это НЕ временный
# сбой — повтор в ту же тему будет падать всегда, поэтому вызывающий код
# сбрасывает Users.topic_id и пересоздаёт тему с нуля. Сверено с текстами Bot API:
# «Bad Request: message thread not found» и вариант «Bad Request: TOPIC_DELETED».
# Сравнение идёт по приведённому к нижнему регистру тексту исключения.
DEAD_TOPIC_ERROR_MARKERS = (
    'thread not found',
    'topic_deleted',
    'topic deleted',
)


def is_dead_topic_error(error: Exception) -> bool:
    """
    True, если ошибка отправки означает «темы больше не существует».

    Намеренно узко: только TelegramBadRequest с конкретным текстом. Всё
    остальное (Forbidden — бота выгнали, NetworkError — таймаут, ServerError)
    временное или относится к группе целиком, и сбрасывать из-за него
    привязку темы нельзя — иначе на каждом сетевом сбое бот плодил бы клиенту
    новые темы.
    """
    if not isinstance(error, TelegramBadRequest):
        return False

    text = str(error).lower()

    return any(marker in text for marker in DEAD_TOPIC_ERROR_MARKERS)


def telegram_text_length(text: str) -> int:
    """
    Длина text так, как её считает Telegram, — в единицах UTF-16. len() считает кодовые
    точки, и любой символ вне BMP (эмодзи 😀 и т.п.) для Telegram вдвое длиннее.
    """
    return len(text.encode('utf-16-le')) // 2


def exceeds_telegram_limit(text: str) -> int:
    """Возвращает, на сколько символов (UTF-16) text превышает лимит Telegram (0, если укладывается)."""
    return max(0, telegram_text_length(text) - TELEGRAM_MESSAGE_LIMIT)


def format_client_date(value: datetime) -> str:
    """Дата для сообщений клиенту: created_at (UTC) в московском времени с пометкой «(МСК)»."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return f"{value.astimezone(CLIENT_TIMEZONE).strftime('%d.%m.%Y')} ({CLIENT_TIMEZONE_LABEL})"


def _log_send_failure(context: str, error: Exception) -> None:
    if isinstance(error, TelegramForbiddenError):
        logger.warning(
            "Не удалось отправить [%s] — доступ к получателю запрещён (бот заблокирован/удалён): %s",
            context, error
        )
    elif isinstance(error, TelegramRetryAfter):
        logger.error(
            "Не удалось отправить [%s] — исчерпаны попытки после превышения лимита запросов Telegram: %s",
            context, error
        )
    elif isinstance(error, TelegramBadRequest):
        logger.warning("Не удалось отправить [%s]: %s", context, error)
    else:
        logger.error("Не удалось отправить [%s] — непредвиденная ошибка Telegram API: %s", context, error)


async def safe_answer(message, *, context: str, **answer_kwargs) -> bool:
    """
    Отправляет message.answer(**answer_kwargs), логируя и гася любую ошибку
    доставки Telegram вместо того, чтобы уронить хендлер.

    `context` — короткое человекочитаемое описание для лога (например
    "приветствие user_id=123"), чтобы по логам можно было понять, какая именно
    отправка не удалась, без похода в БД.
    """
    try:
        await message.answer(**answer_kwargs)
        return True
    except TelegramAPIError as error:
        _log_send_failure(context, error)
        return False


async def send_message_capturing_error(
    bot, chat_id, *, context: str, **send_kwargs
) -> tuple[Message | None, TelegramAPIError | None]:
    """
    То же, что safe_send_message, но дополнительно ОТДАЁТ перехваченную ошибку —
    для вызывающего кода, которому мало факта «не доставлено» и нужно разобрать
    причину (например, отличить мёртвую тему от временного сбоя, см.
    is_dead_topic_error). Логирование то же самое, дублировать его не нужно.
    """
    try:
        return await bot.send_message(chat_id=chat_id, **send_kwargs), None
    except TelegramAPIError as error:
        _log_send_failure(context, error)
        return None, error


async def safe_send_message(bot, chat_id, *, context: str, **send_kwargs) -> Message | None:
    """
    То же самое, что safe_answer, но через bot.send_message(chat_id=...).
    Возвращает отправленный Message при успехе (используется, например, чтобы
    сохранить message_id отправленной в группу карточки) и None при неудаче.
    """
    message, _ = await send_message_capturing_error(bot, chat_id, context=context, **send_kwargs)

    return message


async def safe_edit_message_text(bot, chat_id, message_id, *, context: str, **edit_kwargs) -> bool:
    """
    bot.edit_message_text с тем же log-and-swallow, что и у safe_send_message: правка
    служебного сообщения (снять неактуальные inline-кнопки и т.п.) может не пройти —
    сообщение удалено, слишком старое, "message is not modified" — и это не должно
    обрывать хендлер посреди уведомлений.
    """
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, **edit_kwargs)
        return True
    except TelegramAPIError as error:
        _log_send_failure(context, error)
        return False


async def safe_answer_callback(callback, *, context: str, **answer_kwargs) -> bool:
    """
    callback.answer(**answer_kwargs) с log-and-swallow. Отвечать нужно на КАЖДЫЙ
    callback_query (иначе кнопка у пользователя "крутится"), поэтому ошибка ответа —
    например, query уже протух — не должна мешать остальной обработке.
    """
    try:
        await callback.answer(**answer_kwargs)
        return True
    except TelegramAPIError as error:
        _log_send_failure(context, error)
        return False


async def notify_update_error(bot, update: Update) -> None:
    """
    Общий ответ инициатору апдейта, хендлер которого упал необработанным исключением
    (вызывается из dp.errors()), — чтобы вместо тишины пользователь понял, что нужно
    повторить. На callback сначала отвечаем answerCallbackQuery, иначе кнопка «крутится»;
    если хендлер успел ответить сам, повтор отклонит Telegram — safe_answer_callback это
    проглотит, а сообщение в чат всё равно уйдёт. Сам никогда не бросает исключений.
    """
    try:
        if update.message is not None or update.edited_message is not None:
            message = update.message or update.edited_message
            chat_id, thread_id = message.chat.id, message.message_thread_id
        elif update.callback_query is not None:
            callback = update.callback_query

            await safe_answer_callback(
                callback, context=f'ответ об ошибке на callback user_id={callback.from_user.id}',
                text=UPDATE_ERROR_CALLBACK_TEXT, show_alert=True
            )

            if callback.message is not None:
                chat_id = callback.message.chat.id
                thread_id = getattr(callback.message, 'message_thread_id', None)
            else:
                chat_id, thread_id = callback.from_user.id, None
        else:
            return

        await safe_send_message(
            bot, chat_id,
            context=f'общий ответ об ошибке апдейта {update.update_id} chat_id={chat_id}',
            text=UPDATE_ERROR_TEXT,
            message_thread_id=thread_id
        )
    except Exception:
        logger.exception('Не удалось сообщить об ошибке инициатору апдейта %s', update.update_id)
