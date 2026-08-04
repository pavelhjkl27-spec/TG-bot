from aiogram import types


def get_reply_keyboard():
    KeyBoard = types.ReplyKeyboardMarkup(keyboard=[
        [
            types.KeyboardButton(text='Задать вопрос'),
            types.KeyboardButton(text='Оставить заявку'),
        ],
        [
            types.KeyboardButton(text='О нас')
        ]
    ], resize_keyboard=True)

    return KeyBoard