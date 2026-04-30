import logging

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from config import BOT_TOKEN
from handlers.record import record_message, start_recording, stop_recording
from handlers.timer import start_timer

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("SummStart", start_recording), group=0)
    app.add_handler(CommandHandler("SummEnd", stop_recording), group=0)
    app.add_handler(CommandHandler("timer", start_timer), group=0)
    app.add_handler(MessageHandler(filters.TEXT, record_message), group=1)

    app.run_polling()


if __name__ == "__main__":
    main()
