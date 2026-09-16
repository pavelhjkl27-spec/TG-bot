from aiogram.filters.callback_data import CallbackData


class DialogCallback(CallbackData, prefix='dialog'):
    """
    Inline-кнопки диалога клиент↔админ: `dialog:<action>:<client_id>:<dialog_id>`.

    action — 'confirm' | 'reject' (сообщение-запрос в теме) или 'end' (статус-сообщение).
    dialog_id совпадает с data['dialog_id'] в FSM клиента: кнопка от уже закрытого или
    прошлого диалога не пройдёт атомарную проверку перехода и ничего не изменит.

    Новые inline-подтверждения заводят свой CallbackData-класс с собственным prefix по
    этому же образцу (лимит Telegram на callback_data — 64 байта).
    """
    action: str
    client_id: int
    dialog_id: str


class NewsletterCallback(CallbackData, prefix='newsletter'):
    """
    Inline-кнопки под превью рассылки: `newsletter:<action>:<draft_id>`.

    action — 'confirm' | 'edit' | 'cancel'. draft_id совпадает с data['draft_id'] в FSM
    админа (Newsletter.sure): любое решение атомарно стирает draft_id, поэтому повторное,
    позднее или гоночное нажатие (в том числе другой кнопки того же превью) и кнопки старых
    превью не проходят проверку перехода и ничего не делают.
    """
    action: str
    draft_id: str
