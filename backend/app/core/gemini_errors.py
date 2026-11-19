import logging
import re

from google.genai.errors import APIError

from app.core.config import GEMINI_MODEL

logger = logging.getLogger("uvicorn.error")


def model_label() -> str:
    return GEMINI_MODEL if re.fullmatch(r"gemini-[a-z0-9.-]{1,80}", GEMINI_MODEL) else "(invalid model format)"


def gemini_failure(error: Exception) -> tuple[int, str] | None:
    cause = error.__cause__
    if not isinstance(cause, APIError):
        if cause is None or str(error) != "Gemini API request failed.":
            return None
        logger.warning("Gemini request failed using %s (%s)", model_label(), type(cause).__name__)
        return 502, "Gemini API request failed."

    code = cause.code
    logger.warning("Gemini returned HTTP %s for %s", code, model_label())
    if code in {500, 502, 503, 504}:
        return 503, f"Gemini is temporarily unavailable ({model_label()}, upstream HTTP {code}). Retry shortly."
    if code == 429:
        return 429, f"Gemini rate or quota limit reached for {model_label()}. Check this project's limits in Google AI Studio before retrying."
    if code in {401, 403}:
        return 502, f"Gemini denied access to {model_label()} (upstream HTTP {code}). Check the backend API key and project permissions."
    if code == 404:
        return 502, f"Gemini did not find {model_label()} for this API project. Check GEMINI_MODEL."
    return 502, f"Gemini rejected the request for {model_label()} (upstream HTTP {code}). Check the model settings and API key."
