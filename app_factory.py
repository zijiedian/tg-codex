from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bridge import Bridge
from settings import Settings


def build_app(settings: Settings) -> tuple[FastAPI, Application]:
    telegram_app = ApplicationBuilder().token(settings.bot_token).build()
    bridge = Bridge(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await telegram_app.initialize()
        await telegram_app.start()
        if settings.webhook_url:
            await telegram_app.bot.set_webhook(
                url=settings.webhook_url,
                secret_token=settings.webhook_secret or None,
            )
        elif telegram_app.updater:
            await telegram_app.updater.start_polling(drop_pending_updates=True)

        try:
            yield
        finally:
            if telegram_app.updater:
                await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()

    fastapi_app = FastAPI(title="tg-codex", lifespan=lifespan)

    telegram_app.add_handler(CommandHandler("start", bridge.start))
    telegram_app.add_handler(CommandHandler("id", bridge.chat_id))
    telegram_app.add_handler(CommandHandler("status", bridge.status))
    telegram_app.add_handler(CommandHandler("cancel", bridge.cancel))
    telegram_app.add_handler(CommandHandler("new", bridge.new_session))
    telegram_app.add_handler(CommandHandler("cwd", bridge.cwd))
    telegram_app.add_handler(CommandHandler("auth", bridge.auth))
    telegram_app.add_handler(CommandHandler("cmd", bridge.cmd))
    telegram_app.add_handler(CommandHandler("run", bridge.run))
    telegram_app.add_handler(CallbackQueryHandler(bridge.paginate, pattern=r"^page:"))
    telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, bridge.run_image))
    if settings.allow_plain_text:
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.run_text))

    @fastapi_app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @fastapi_app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
    ) -> dict:
        if not settings.webhook_secret:
            raise HTTPException(status_code=403, detail="webhook disabled")
        if x_telegram_bot_api_secret_token != settings.webhook_secret:
            raise HTTPException(status_code=403, detail="invalid webhook secret")

        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}

    return fastapi_app, telegram_app
