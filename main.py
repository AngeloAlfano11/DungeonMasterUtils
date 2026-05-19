"""Bot entrypoint.

Wires every command handler into the Telegram application and starts polling.
The MessageHandler in group=1 is the recorder fallback that captures non-command
messages while a recording session is active.
"""

import logging

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from config import BOT_TOKEN
from handlers.getids import getids
from handlers.initiative import (
    initclear,
    initdel,
    inithp,
    initiative,
    initkill,
    initnext,
    initprev,
    initrack,
    initrevive,
    inittrack,
    load_all_encounters,
)
from handlers.macro import macro, macro_del, macro_reset
from handlers.record import (
    force_summary,
    record_message,
    restart_recording,
    start_recording,
    stop_recording,
)
from handlers.remind import load_all_reminders, remind
from handlers.roll import roll
from handlers.start import start
from handlers.timer import start_timer, stop_timer

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
    app.add_handler(CommandHandler("SummRestart", restart_recording), group=0)
    app.add_handler(CommandHandler("SummForce", force_summary), group=0)
    app.add_handler(CommandHandler("timer", start_timer), group=0)
    app.add_handler(CommandHandler("timerstop", stop_timer), group=0)
    app.add_handler(CommandHandler(["roll", "r"], roll), group=0)
    app.add_handler(CommandHandler("init", initiative), group=0)
    app.add_handler(CommandHandler("initrack", initrack), group=0)
    app.add_handler(CommandHandler(["initnext", "initn"], initnext), group=0)
    app.add_handler(CommandHandler(["initprev", "initp"], initprev), group=0)
    app.add_handler(CommandHandler("inithp", inithp), group=0)
    app.add_handler(CommandHandler("initkill", initkill), group=0)
    app.add_handler(CommandHandler("initrevive", initrevive), group=0)
    app.add_handler(CommandHandler("initdel", initdel), group=0)
    app.add_handler(CommandHandler("inittrack", inittrack), group=0)
    app.add_handler(CommandHandler("initclear", initclear), group=0)
    app.add_handler(CommandHandler("remind", remind), group=0)
    app.add_handler(CommandHandler("macro", macro), group=0)
    app.add_handler(CommandHandler("MacroDel", macro_del), group=0)
    app.add_handler(CommandHandler("MacroReset", macro_reset), group=0)
    # Catch-all: any text/captioned message goes to the recorder; it no-ops
    # if no session is active in the current chat/thread.
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, record_message), group=1)

    # Restore persisted state from disk before serving updates. Reminders need
    # the job_queue (which is created with the Application) to re-register
    # their run_daily jobs.
    load_all_encounters()
    load_all_reminders(app.job_queue)
    app.run_polling()


if __name__ == "__main__":
    main()
