from app_factory import build_app
from settings import load_settings

settings = load_settings()
app, _telegram_app = build_app(settings)
