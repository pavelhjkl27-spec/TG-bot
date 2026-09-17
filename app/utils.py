import logging

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Message

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096

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


def exceeds_telegram_limit(text: str) -> int:
    """Возвращает, на сколько символов text превышает лимит Telegram (0, если укладывается)."""
    return max(0, len(text) - TELEGRAM_MESSAGE_LIMIT)


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
