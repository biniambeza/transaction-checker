"""Starts the Telegram bot (long polling) and the dashboard web server together."""
import asyncio
import logging

from telegram import Update

import config
import db
from bot import build_application, setup_bot
import tunnel
from web import start_web

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")


async def main() -> None:
    db.init()
    tunnel_proc = None
    runner = None
    app = build_application()
    async with app:
        runner = await start_web(app.bot)
        if not config.WEBAPP_URL:
            tunnel_proc, t_url = await tunnel.start_tunnel(config.WEB_PORT)
            if t_url:
                config.WEBAPP_URL = t_url
                log.info("Dashboard URL: %s", config.WEBAPP_URL)

        # Retry bot setup up to 3 times in case of startup network hiccups
        for attempt in range(1, 4):
            try:
                await setup_bot(app)
                break
            except NetworkError as e:
                if attempt == 3:
                    raise
                log.warning("Connecting to Telegram timed out or failed (attempt %d/3: %s). Retrying in 5 seconds...", attempt, e)
                await asyncio.sleep(5)

        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        me = await app.bot.get_me()
        log.info("Bot @%s is running. Owners: %s. Press Ctrl+C to stop.", me.username, sorted(config.OWNER_IDS))
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            if runner:
                await runner.cleanup()
            if tunnel_proc:
                await tunnel.close_tunnel(tunnel_proc)
            import receipt_browser
            if receipt_browser.AVAILABLE:
                await receipt_browser.close()


if __name__ == "__main__":
    from telegram.error import InvalidToken, NetworkError

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except InvalidToken:
        raise SystemExit("Telegram rejected BOT_TOKEN. Copy the token again from @BotFather into .env.")
    except NetworkError as e:
        raise SystemExit(f"Can't reach Telegram ({e}). Check your internet connection.")
    except OSError as e:
        if getattr(e, "winerror", None) == 10048 or e.errno in (98, 10048):
            raise SystemExit(f"Port {config.WEB_PORT} is in use — is the bot already running in another window? "
                             "Close it, or change WEB_PORT in .env.")
        raise
