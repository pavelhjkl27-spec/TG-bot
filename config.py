from dotenv import load_dotenv
import os

load_dotenv()

class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    ADMIN_ID = int(os.environ['ADMIN_ID'])


