from telegram import Update
from telegram.ext import ContextTypes

_MESSAGE = """<b><a href="https://t.me/DM_Toolbox_bot">@DM_Toolbox_bot</a></b> is a Telegram bot built to assist Dungeon Masters at the table. It can automatically record session messages and generate a concise AI-powered summary at the end, and run visual countdown timers directly in chat for managing encounter time.

<b>Commands</b>
<code>/SummStart</code> - Start recording messages in the current thread
<code>/SummEnd</code> - Stop recording and generate an AI summary of the session
<code>/timer &lt;minutes&gt;</code> - Start a visual countdown timer (max 60 min)

⚠️ The bot must be added to a group and granted <b>administrator permissions</b>, otherwise it will not be able to delete messages or pin the timer.

📖 Setup guide: https://github.com/AngeloAlfano11/DungeonMasterUtils"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_MESSAGE, parse_mode="HTML")
