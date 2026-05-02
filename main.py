"""Bot entrypoint.

Wires every command handler into the Telegram application and starts polling.
The MessageHandler in group=1 is the recorder fallback that captures non-command
messages while a recording session is active.
"""

import logging

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from config import BOT_TOKEN
from handlers.getids import getids
from handlers.initiative import initiative, load_all_encounters
from handlers.record import force_summary, record_message, start_recording, stop_recording
from handlers.roll import roll
from handlers.start import start
from handlers.timer import start_timer

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs every Telegram API call at INFO; keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    # concurrent_updates(True) lets long-running handlers (e.g. /timer, which
    # sleeps for minutes) coexist with other commands instead of blocking the
    # update queue.
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # group=0 handlers run before group=1, so command handlers always win
    # over the recorder's catch-all MessageHandler.
    app.add_handler(CommandHandler("start", start), group=0)
    app.add_handler(CommandHandler("getids", getids), group=0)
    app.add_handler(CommandHandler("SummStart", start_recording), group=0)
    app.add_handler(CommandHandler("SummEnd", stop_recording), group=0)
    app.add_handler(CommandHandler("forcesumm", force_summary), group=0)
    app.add_handler(CommandHandler("timer", start_timer), group=0)
    app.add_handler(CommandHandler("roll", roll), group=0)
    app.add_handler(CommandHandler("init", initiative), group=0)
    # Catch-all: any text/captioned message goes to the recorder; it no-ops
    # if no session is active in the current chat/thread.
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, record_message), group=1)

    # Restore persisted initiative state from disk before serving updates.
    load_all_encounters()
    app.run_polling()


if __name__ == "__main__":
    main()
