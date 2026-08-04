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
