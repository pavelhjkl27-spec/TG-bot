from aiogram import types


def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(keyboard=[
        [
            types.KeyboardButton(text='Задать вопрос'),
            types.KeyboardButton(text='Оставить заявку'),
        ],
        [
            types.KeyboardButton(text='О нас')
        ]
    ], resize_keyboard=True)

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
                types.KeyboardButton(text='Изменить "О нас"')
            ],
        ]
    )

    return keyboard
