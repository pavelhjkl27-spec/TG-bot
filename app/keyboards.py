from aiogram import types


def get_reply_keyboard():
    keyboard = types.ReplyKeyboardMarkup(keyboard=[
        [
            types.KeyboardButton(text='Задать вопрос'),
            types.KeyboardButton(text='Оставить заявку'),
        ],
        [
            types.KeyboardButton(text='О нас')
        ]
    ], resize_keyboard=True, one_time_keyboard=True)

    return keyboard