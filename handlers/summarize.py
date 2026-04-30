import logging
from pathlib import Path

from google import genai
from google.genai import types
from telegram.ext import ContextTypes

from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GEMINI_API_KEY)

MODEL = "gemini-2.5-flash"
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

_USER_PROMPT = (
    "Summarize the attached RPG session transcript following the system instructions. Use the SAME LANGUAGE as the transcript."
)


async def generate_summary(file_path: Path) -> str:
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
        await _client.aio.files.delete(name=uploaded.name)
        logger.info("File deleted from Gemini: %s", uploaded.name)


async def summarize_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    file_path: Path = data["file_path"]
    chat_id: int = data["chat_id"]
    message_id: int = data["message_id"]
    attempt: int = data["attempt"]

    try:
        summary = await generate_summary(file_path)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=summary,
        )
        logger.info("Summary delivered after %d attempt(s)", attempt)
    except Exception as e:
        is_503 = "503" in str(e)
        if is_503 and attempt < MAX_RETRIES:
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
            logger.error("Summary generation failed after %d attempt(s): %s", attempt, e)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="Summary generation failed. The session file has been saved.",
            )
