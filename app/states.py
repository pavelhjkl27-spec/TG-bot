from aiogram.fsm.state import State, StatesGroup


class Form(StatesGroup):
    name = State()
    birthday = State()
    text = State()

class Question(StatesGroup):
    question = State()

