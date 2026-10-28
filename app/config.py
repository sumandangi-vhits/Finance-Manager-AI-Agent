import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains run.py
BASE_DIR = Path(__file__).resolve().parent.parent

# Load variables from .env into the environment
load_dotenv(BASE_DIR / ".env")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY")

    DATABASE_PATH = BASE_DIR / os.environ.get("DATABASE_PATH", "finance.db")
    UPLOAD_FOLDER = BASE_DIR / os.environ.get("UPLOAD_FOLDER", "uploads")

    # Reject uploads larger than 10 MB
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024

    LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

    # Full path to tesseract.exe if it is not on PATH (optional)
    TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "")
