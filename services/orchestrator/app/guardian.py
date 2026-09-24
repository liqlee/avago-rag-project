import logging

import httpx

from .config import settings

logger = logging.getLogger(__name__)

GROUNDEDNESS_PROMPT = (
    "You are a safety verification agent. Determine whether the AI response "
    "below is fully grounded in the provided context.\n\n"
    "CONTEXT (from maintenance manuals):\n{context}\n\n"
    "AI RESPONSE TO VERIFY:\n{answer}\n\n"
    "Instructions:\n"
    "1. Check each factual claim against the context.\n"
    "2. Numerical specs (torque, pressure, temperature, clearance) must match "
    "the context exactly.\n"
    "3. Respond with exactly one of:\n"
    '   - "GROUNDED" if all claims are supported by the context\n'
    '   - "NOT GROUNDED: [list the specific ungrounded claims]" if any claim '
    "lacks support"
)

_DISCLAIMER_UNGROUNDED = (
    "\n\n---\n"
    "*Note: Some information in this response could not be fully verified "
    "against the available manuals. Please consult the original documentation "
    "before proceeding with safety-critical work.*"
)

_DISCLAIMER_UNAVAILABLE = (
    "\n\n---\n"
    "*Note: Safety verification was unavailable for this response. "
    "Please verify all information against the original documentation.*"
)


def check_groundedness(answer: str, context: str) -> tuple[bool, str]:
    """Verify the answer against retrieved context via Granite Guardian.

    Returns (is_grounded, possibly_modified_answer).
    """
    if not settings.GUARDIAN_ENABLED or not context:
        return True, answer

    try:
        response = httpx.post(
            f"{settings.GUARDIAN_URL}/v1/chat/completions",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": GROUNDEDNESS_PROMPT.format(
                            context=context[:4000],
                            answer=answer,
                        ),
                    },
                ],
                "max_tokens": 200,
                "temperature": 0.0,
            },
            timeout=60.0,
        )
        response.raise_for_status()
        verdict = response.json()["choices"][0]["message"]["content"].strip()
        logger.info("Guardian verdict: %s", verdict[:200])

        if verdict.upper().startswith("GROUNDED"):
            return True, answer

        return False, answer + _DISCLAIMER_UNGROUNDED

    except Exception:
        logger.warning("Guardian check failed", exc_info=True)
        return False, answer + _DISCLAIMER_UNAVAILABLE
