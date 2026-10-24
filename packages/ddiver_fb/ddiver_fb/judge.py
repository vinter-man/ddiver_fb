"""The AI rule's judge: a wish in plain words and a post's text in, a percent and a reason out.

It knows nothing of Facebook or of the brain: a wish, a text and a line of context (the group's
name) are all it reads, so ddiver's Telegram listener can use it as it is once it moves to
ddiver-core (the owner's plan of 2026-10-10). It imports only the standard library and httpx.

- **The model** is any OpenRouter chat model with structured outputs; the owner's choice of
  2026-10-12 is DeepSeek V4.1 Flash, after a comparison of seven models on 206 real posts
  labelled by hand: every rideshare request found, no false one, none of 135 unrelated
  classifieds taken for a match, about $0.24 per thousand posts.
- **The prompt** is the one that comparison chose, in English since 2026-10-24 (the reason
  comes back in the wish's own language). It spells out what the earlier one left to the model:
  a wish for a thing wants the posts that offer it, a wish for people of a kind wants the posts
  of those people. The earlier prompt said only the first, and four of seven models read "people
  who are looking for a ride" as the drivers who offer one.
- **Answers are JSON** under a strict schema at temperature 0. An empty or unparsable answer
  (DeepSeek gave seven empty ones in 206 calls once) is asked again at once, then reported as a
  transient failure for the caller's queue to retry later.

Never logged: the wish, the text, the reason (invariant 8; the reason retells the post).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

OPENROUTER_URL: Final = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL: Final = "deepseek/deepseek-v4.1-flash"
MAX_TEXT_CHARS: Final = 4000
"""What a post may bring to the model; a classified's text is a few hundred characters."""
MAX_REASON_CHARS: Final = 300
TIMEOUT: Final = 60.0
ATTEMPTS: Final = 2
"""An empty or unparsable answer is asked once more before the call counts as failed."""

SYSTEM: Final = (
    "You filter posts from social media groups. The user has described, in their own words, "
    "which posts they want to receive. Decide whether this post fits. Posts may be in "
    "Bulgarian, Russian, Ukrainian or English. Judge by meaning: who is writing, what they "
    "offer or look for, what object, where, in which direction. If the user is looking for a "
    "thing, housing or a service, they want the posts that offer it. If they are looking for "
    "people of a certain kind (for example, people who are looking for something), they want "
    "the posts of exactly those people, not of those who offer the same thing. Keep to the "
    "place and the other conditions of the wish exactly as the user wrote them. Ads, services "
    "and posts about something else do not fit; an empty or unclear post does not fit. "
    'Answer with JSON only: {"percent": 0-100, "reason": "..."}, where percent is the '
    "probability, in percent, that the post fits the wish (0: certainly not, 100: certainly "
    "yes), and reason is why, in 5 to 15 words, in the language the wish is written in."
)

SCHEMA: Final[dict[str, Any]] = {
    "name": "verdict",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "percent": {"type": "integer", "minimum": 0, "maximum": 100},
            "reason": {"type": "string"},
        },
        "required": ["percent", "reason"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True, slots=True)
class Verdict:
    percent: int
    reason: str
    model: str
    cost: float | None = None
    """US dollars, as the provider counted them; ``None`` when it did not say."""
    seconds: float = 0.0


class JudgeError(Exception):
    """The model could not be asked or gave no usable answer. ``kind`` names the cause without
    quoting anything the model or the post said: ``http_401``, ``timeout``, ``unparsable``…"""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def prompt(wish: str, text: str, context: str = "") -> str:
    """The user message: the wish, where the post is from, and the post."""
    body = text.strip()[:MAX_TEXT_CHARS] or "(empty)"
    where = f"Group: {context}\n" if context else ""
    return f"The user's wish: «{wish}»\n\n{where}Post:\n{body}"


def parse(content: str) -> tuple[int, str]:
    """``(percent, reason)`` from the model's answer; ``ValueError`` when there is none."""
    raw = content.strip().removeprefix("```json").removesuffix("```").strip()
    data = json.loads(raw)
    # A list or a string is a malformed answer like any other: ValueError, not TypeError.
    found = data if isinstance(data, dict) else {}
    percent = found.get("percent")
    if (
        isinstance(percent, bool)
        or not isinstance(percent, int | float)
        or not math.isfinite(percent)
    ):
        msg = "no percent"
        raise ValueError(msg)
    reason = str(found.get("reason") or "").strip()[:MAX_REASON_CHARS]
    return max(0, min(100, round(percent))), reason


class OpenRouterJudge:
    """One model behind OpenRouter's chat completions API."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        url: str = OPENROUTER_URL,
        timeout: float = TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._url = url
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "ddiver"}
        self._client = client if client is not None else httpx.AsyncClient()
        self._own_client = client is None

    async def close(self) -> None:
        if self._own_client:
            await self._client.aclose()

    def body(self, wish: str, text: str, context: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt(wish, text, context)},
            ],
            "response_format": {"type": "json_schema", "json_schema": SCHEMA},
            "temperature": 0,
            "max_tokens": 1500,
            "reasoning": {"effort": "low"},
            "usage": {"include": True},
        }

    async def judge(self, wish: str, text: str, *, context: str = "") -> Verdict:
        """Raises :class:`JudgeError`; never returns a guess."""
        body = self.body(wish, text, context)
        started = time.monotonic()
        kind = "unparsable"
        for _ in range(ATTEMPTS):
            try:
                response = await self._client.post(
                    self._url, json=body, headers=self._headers, timeout=self._timeout
                )
            except httpx.TimeoutException as exc:
                raise JudgeError("timeout") from exc
            except httpx.HTTPError as exc:
                raise JudgeError(type(exc).__name__) from exc
            if response.status_code != 200:
                raise JudgeError(f"http_{response.status_code}")
            try:
                data = response.json()
                message = (data.get("choices") or [{}])[0].get("message") or {}
                percent, reason = parse(message.get("content") or "")
            except (ValueError, AttributeError, IndexError, TypeError):
                kind = "unparsable"
                continue
            usage = data.get("usage") or {}
            cost = usage.get("cost")
            return Verdict(
                percent=percent,
                reason=reason,
                model=str(data.get("model") or self.model),
                cost=float(cost) if isinstance(cost, int | float) else None,
                seconds=round(time.monotonic() - started, 2),
            )
        raise JudgeError(kind)
