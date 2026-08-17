import os
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

API_KEY = os.getenv("API_KEY", "default_secret_api_key")
DATABASE_PATH = os.getenv("DATABASE_PATH", "linkplease.db")
PSEUDOGRAM_BASE_URL = os.getenv("PSEUDOGRAM_BASE_URL", "https://pseudogram-api.onrender.com").rstrip("/")
PORT = int(os.getenv("PORT", "8000"))
