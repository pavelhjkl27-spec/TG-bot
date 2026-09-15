import logging

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096


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


async def safe_send_message(bot, chat_id, *, context: str, **send_kwargs) -> bool:
    """То же самое, что safe_answer, но через bot.send_message(chat_id=...)."""
    try:
        await bot.send_message(chat_id=chat_id, **send_kwargs)
        return True
    except TelegramAPIError as error:
        _log_send_failure(context, error)
        return False
