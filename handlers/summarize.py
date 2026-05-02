"""AI summarization via Google Gemini.

The transcript is uploaded as a file (Files API) so we don't pay the inline
token tax on long sessions. The job runs through PTB's job queue with a
fixed-delay retry on 503 (model unavailable) and posts the final summary by
editing the placeholder message left by /SummEnd.
"""

import logging
from pathlib import Path

from google import genai
from google.genai import types
from telegram.ext import ContextTypes

from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

# Single client instance, reused across all summary jobs.
_client = genai.Client(api_key=GEMINI_API_KEY)

MODEL = "gemini-2.5-flash"
# 5 attempts × 60s ≈ 5 minutes of total tolerance for transient 503s before
# giving up and reporting failure to the GM.
MAX_RETRIES = 5
RETRY_DELAY = 60  # seconds

_SYSTEM_INSTRUCTION = """You are a scribe helping a Dungeon Master recap a tabletop RPG session.

You will receive a JSONL transcript where each line is a JSON object with two fields:
- "sender": the name of the character or person who sent the message
- "text": what they wrote

The text field uses two conventions you must understand:
- Text wrapped in square brackets, e.g. ["the dragon descends from the sky"], represents a narrative action or global event narrated by the Dungeon Master. Treat these as scene descriptions or DM narration.
- Text wrapped in asterisks and quotes, e.g. *"I draw my sword"*, or simply in bold quotes, represents a direct action or spoken line performed by that specific character.

Your task: produce a concise bullet-point recap of the session events in the SAME LANGUAGE as the transcript.

Rules:
- One bullet per meaningful event. Group minor back-and-forth into a single bullet if they form one coherent action.
- Always name the actor at the start of the bullet so the reader knows at a glance who did what, without having to read other bullets (e.g. "- Kael strikes the orc for 14 damage" or "- The DM reveals a secret passage behind the altar").
- Preserve specific numbers: damage dealt or taken, distances, HP, spell slots, resource counts. Do not round or omit them.
- Do not invent or infer events that are not explicitly present in the transcript.
- No emojis.
- Output only the bullet list. No title, no introduction, no closing sentence. Do not write things like "Here is the summary" or "Sure, here it is".
- Keep the total response under 3800 characters."""

# Telegram's hard limit per message is 4096 chars. The system instruction asks
# Gemini to stay under 3800 to leave headroom for any wrapping.
_USER_PROMPT = (
    "Summarize the attached RPG session transcript following the system instructions. Use the SAME LANGUAGE as the transcript."
)


async def generate_summary(file_path: Path) -> str:
    """Upload the JSONL, request a summary, then delete the upload.

    The upload + delete happens for every call: Gemini's free-tier file
    storage is limited and we have no need to keep transcripts on their side.
    """
    uploaded = await _client.aio.files.upload(
        file=file_path,
        config=types.UploadFileConfig(
            display_name=file_path.name,
            mime_type="text/plain",
        ),
    )
    logger.info("File uploaded to Gemini: %s", uploaded.name)
    try:
        response = await _client.aio.models.generate_content(
            model=MODEL,
            contents=[uploaded, _USER_PROMPT],
            config=types.GenerateContentConfig(system_instruction=_SYSTEM_INSTRUCTION),
        )
        return response.text
    finally:
        # Delete the uploaded file even if generation failed.
        await _client.aio.files.delete(name=uploaded.name)
        logger.info("File deleted from Gemini: %s", uploaded.name)


async def summarize_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB job that runs generate_summary, with retry on transient 503s.

    Re-schedules itself via context.job_queue when retries remain — this is
    why MAX_RETRIES / RETRY_DELAY work without explicit asyncio.sleep loops
    (and don't block the bot's update loop).
    """
    data = context.job.data
    file_path: Path = data["file_path"]
    chat_id: int = data["chat_id"]
    message_id: int = data["message_id"]
    attempt: int = data["attempt"]

    try:
        summary = await generate_summary(file_path)
        # Replace the "Generating summary..." placeholder with the final text.
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=summary,
        )
        logger.info("Summary delivered after %d attempt(s)", attempt)
    except Exception as e:
        # 503 = model temporarily unavailable; everything else is treated as fatal.
        is_503 = "503" in str(e)
        if is_503 and attempt < MAX_RETRIES:
            # Update the placeholder so the GM knows we're still trying.
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"Model unavailable, {attempt}/{MAX_RETRIES} retry...",
            )
            context.job_queue.run_once(
                summarize_job,
                when=RETRY_DELAY,
                data={**data, "attempt": attempt + 1},
            )
            logger.warning("503 on attempt %d/%d, retrying in %ds", attempt, MAX_RETRIES, RETRY_DELAY)
        else:
            # Either a non-503 error or we've exhausted retries — surface failure.
            # The JSONL on disk is preserved so /forcesumm can retry later.
            logger.error("Summary generation failed after %d attempt(s): %s", attempt, e)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="Summary generation failed. The session file has been saved.",
            )
