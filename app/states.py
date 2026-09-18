from aiogram.fsm.state import State, StatesGroup


class Form(StatesGroup):
    name = State()
    birthday = State()
    text = State()


class Question(StatesGroup):
    question = State()


class Newsletter(StatesGroup):
    text = State()
    sure = State()


class ChangeAboutUs(StatesGroup):
    about_us_text = State()
    confirm = State()


class ChangePrice(StatesGroup):
    price = State()
    confirm = State()


class Dialog(StatesGroup):
    waiting = State()
    active = State()

