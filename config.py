# config.py
import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
DEFAULT_LIMIT = int(os.environ.get("DEFAULT_LIMIT", "10000"))
ADMIN_LIMIT = int(os.environ.get("ADMIN_LIMIT", "50000"))

BOT_SESSION_NAME = os.environ.get("BOT_SESSION_NAME", "bot_session")

# Session pool — comma-separated names. First one runs on startup;
# the rest are lazily started and rotated for scrapes.
SESSION_POOL_NAMES = [
    x.strip() for x in os.environ.get("SESSION_POOL_NAMES", "user_session_1").split(",") if x.strip()
]