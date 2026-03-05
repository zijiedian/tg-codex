from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeDefault, Update
from telegram.error import TelegramError
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

BOT_MENU_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand("start", "Show help and available commands"),
    BotCommand("run", "Run a Codex prompt"),
    BotCommand("new", "Start a fresh Codex session"),
    BotCommand("cwd", "Show or change working directory"),
    BotCommand("skill", "List installed Codex skills"),
    BotCommand("status", "Show current task status"),
    BotCommand("cancel", "Stop current task"),
    BotCommand("id", "Show current chat/user id"),
    BotCommand("auth", "Unlock execution"),
    BotCommand("cmd", "Show or update command prefix"),
    BotCommand("setting", "Show or update bridge settings"),
)


async def _sync_bot_menu_commands(telegram_app: Application) -> None:
    try:
        commands = list(BOT_MENU_COMMANDS)
        await telegram_app.bot.set_my_commands(commands=commands, scope=BotCommandScopeDefault())
        await telegram_app.bot.set_my_commands(commands=commands, scope=BotCommandScopeAllPrivateChats())
    except TelegramError:
        # Keep service availability even if Telegram menu sync fails transiently.
        return


def build_app(settings: Settings) -> tuple[FastAPI, Optional[Application]]:
    bridge = Bridge(settings)
    app_state: dict[str, Optional[Application]] = {"telegram_app": None}

    def _create_telegram_app() -> Application:
        telegram_app = (
            ApplicationBuilder()
            .token(settings.bot_token)
            .connect_timeout(30)
            .read_timeout(30)
            .write_timeout(30)
            .pool_timeout(5)
            .get_updates_connect_timeout(30)
            .get_updates_read_timeout(30)
            .get_updates_write_timeout(30)
            .get_updates_pool_timeout(5)
            .build()
        )
        telegram_app.add_handler(CommandHandler("start", bridge.start))
        telegram_app.add_handler(CommandHandler("id", bridge.chat_id))
        telegram_app.add_handler(CommandHandler("status", bridge.status))
        telegram_app.add_handler(CommandHandler("cancel", bridge.cancel))
        telegram_app.add_handler(CommandHandler("new", bridge.new_session))
        telegram_app.add_handler(CommandHandler("cwd", bridge.cwd))
        telegram_app.add_handler(CommandHandler("skill", bridge.skill))
        telegram_app.add_handler(CommandHandler("auth", bridge.auth))
        telegram_app.add_handler(CommandHandler("cmd", bridge.cmd))
        telegram_app.add_handler(CommandHandler("setting", bridge.setting))
        telegram_app.add_handler(CommandHandler("run", bridge.run))
        telegram_app.add_handler(CallbackQueryHandler(bridge.paginate, pattern=r"^page:"))
        telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, bridge.run_image))
        if settings.allow_plain_text:
            telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.run_text))
        return telegram_app

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        telegram_app = _create_telegram_app()
        app_state["telegram_app"] = telegram_app
        await telegram_app.initialize()
        await telegram_app.start()
        await _sync_bot_menu_commands(telegram_app)
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
            if telegram_app.updater and telegram_app.updater.running:
                await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
            app_state["telegram_app"] = None

    fastapi_app = FastAPI(title="tg-codex", lifespan=lifespan)

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
        telegram_app = app_state["telegram_app"]
        if telegram_app is None:
            raise HTTPException(status_code=503, detail="telegram app not ready")

        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}

    return fastapi_app, None
