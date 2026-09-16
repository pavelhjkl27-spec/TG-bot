from aiogram import types

from app.callbacks import DialogCallback


def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(keyboard=[
        [
            types.KeyboardButton(text='Задать вопрос'),
            types.KeyboardButton(text='Оставить заявку'),
        ],
        [
            types.KeyboardButton(text='О нас')
        ],
        [
            types.KeyboardButton(text='Запросить диалог с админом')
        ]
    ], resize_keyboard=True)

    return keyboard


def get_dialog_waiting_keyboard():
    keyboard = types.ReplyKeyboardMarkup(keyboard=[
        [
            types.KeyboardButton(text='Отменить запрос')
        ]
    ], resize_keyboard=True)

    return keyboard


def get_dialog_active_keyboard():
    keyboard = types.ReplyKeyboardMarkup(keyboard=[
        [
            types.KeyboardButton(text='Выйти из диалога')
        ]
    ], resize_keyboard=True)

    return keyboard


def get_dialog_request_markup(client_id: int, dialog_id: str):
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text='Подтвердить',
                callback_data=DialogCallback(action='confirm', client_id=client_id, dialog_id=dialog_id).pack()
            ),
            types.InlineKeyboardButton(
                text='Отклонить',
                callback_data=DialogCallback(action='reject', client_id=client_id, dialog_id=dialog_id).pack()
            ),
        ]
    ])

    return keyboard


def get_dialog_status_markup(client_id: int, dialog_id: str):
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text='Завершить',
                callback_data=DialogCallback(action='end', client_id=client_id, dialog_id=dialog_id).pack()
            ),
        ]
    ])

    return keyboard


def get_cancel_keyboard():
    keyboard = types.ReplyKeyboardMarkup(keyboard=[
        [
            types.KeyboardButton(text='Меню')
        ]
    ], resize_keyboard=True)

    return keyboard


def get_back_cancel_keyboard():
    keyboard = types.ReplyKeyboardMarkup(keyboard=[
        [
            types.KeyboardButton(text='Назад')
        ],
        [
            types.KeyboardButton(text='Меню')
        ]
    ], resize_keyboard=True)

    return keyboard


def get_admin_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(text='Сделать рассылку'),
                types.KeyboardButton(text='Как пользоваться ботом?')
            ],
            [
                types.KeyboardButton(text='Изменить «О нас»'),
                types.KeyboardButton(text='Изменить прайс')
            ],
        ], resize_keyboard=True
    )

    return keyboard


def get_sure_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text='Подтвердить'), types.KeyboardButton(text='Изменить')]
        ], resize_keyboard=True, one_time_keyboard=True
    )

    return keyboard
