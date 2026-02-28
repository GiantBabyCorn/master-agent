"""Minimal environment setup so pydantic-settings doesn't blow up on import."""
import os

# Provide required fields so Settings() can be instantiated even without a real DB.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
os.environ.setdefault("TELEGRAM_MODE", "disabled")
