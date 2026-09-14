TELEGRAM_MESSAGE_LIMIT = 4096


def exceeds_telegram_limit(text: str) -> int:
    """Возвращает, на сколько символов text превышает лимит Telegram (0, если укладывается)."""
    return max(0, len(text) - TELEGRAM_MESSAGE_LIMIT)
