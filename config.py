from dotenv import load_dotenv
import os

load_dotenv()

class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    ADMIN_ID = int(os.environ['ADMIN_ID'])

    # Мониторинг — оба необязательны: пустое/отсутствующее значение отключает фичу.
    SENTRY_DSN = os.getenv('SENTRY_DSN') or None
    HEARTBEAT_URL = os.getenv('HEARTBEAT_URL') or None
    HEARTBEAT_INTERVAL_MINUTES = int(os.getenv('HEARTBEAT_INTERVAL_MINUTES') or 5)


