"""Structured-output LLM access.

``StructuredLLM`` is the only interface the graph uses. The production
implementation, ``OpenAIStructuredLLM``, calls the official OpenAI SDK
(Responses API, ``responses.parse``) and validates the output into a
Pydantic model. Tests use a scripted fake with the same interface.

Error policy:

* Transient errors (timeouts, network, 429, 5xx, 408/409) are retried here
  with exponential backoff and jitter, up to ``openai_max_attempts``. The
  SDK's own retries are disabled, so there is one retry policy, logged in
  one place.
* After the last attempt: ``LLMUnavailableError``. The graph routes to the
  deterministic fallback.
* Refusals, missing parsed output or schema violations raise
  ``LLMOutputError``. The graph may ask for a correction.
* Authentication and bad-request errors are not retried
  (``LLMUnavailableError`` immediately, with ``retryable=False``).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import openai
from pydantic import BaseModel, ValidationError

from app.config import Settings

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class LLMError(Exception):
    pass


class LLMUnavailableError(LLMError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMOutputError(LLMError):
    """The model answered, but not with a valid instance of the schema."""


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    parsed: T
    model: str  # model id reported by the API (exact snapshot when available)
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    attempts: int
    duration_ms: float
    meta: dict[str, Any] = field(default_factory=dict)

    def usage(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "response_id": self.response_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "attempts": self.attempts,
            "duration_ms": round(self.duration_ms, 1),
        }


class StructuredLLM(Protocol):
    @property
    def configured_model(self) -> str: ...

    async def generate[T: BaseModel](
        self, schema: type[T], *, system: str, user: str, name: str
    ) -> LLMResult[T]: ...


_TRANSIENT = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, _TRANSIENT):
        return True
    return isinstance(exc, openai.APIStatusError) and exc.status_code in (408, 409)


class OpenAIStructuredLLM:
    def __init__(
        self,
        settings: Settings,
        *,
        client: openai.AsyncOpenAI | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if client is None:
            if settings.openai_api_key is None:
                raise LLMUnavailableError("OPENAI_API_KEY is not set", retryable=False)
            client = openai.AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=settings.openai_base_url,
                timeout=settings.openai_timeout_seconds,
                max_retries=0,  # retries are handled (and logged) below
            )
        self._client = client
        self._model = settings.openai_model
        self._max_attempts = settings.openai_max_attempts
        self._backoff_base = settings.openai_backoff_base_seconds
        self._backoff_max = settings.openai_backoff_max_seconds
        self._sleep = sleep

    @property
    def configured_model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._client.close()

    async def generate[T: BaseModel](
        self, schema: type[T], *, system: str, user: str, name: str
    ) -> LLMResult[T]:
        started = time.perf_counter()
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.responses.parse(
                    model=self._model,
                    input=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    text_format=schema,
                )
                break
            except ValidationError as exc:  # SDK validates into the Pydantic model
                logger.warning(
                    "llm_output_invalid", extra={"node": name, "errors": exc.errors()[:5]}
                )
                raise LLMOutputError(f"{name}: output failed schema validation: {exc}") from exc
            except openai.APIError as exc:
                transient = _is_transient(exc)
                status = getattr(exc, "status_code", None)
                if transient and attempt < self._max_attempts:
                    delay = random.uniform(  # noqa: S311 - jitter
                        0, min(self._backoff_max, self._backoff_base * 2 ** (attempt - 1))
                    )
                    logger.warning(
                        "llm_request_retry",
                        extra={
                            "node": name,
                            "attempt": attempt,
                            "status_code": status,
                            "error": type(exc).__name__,
                            "retry_in_s": round(delay, 2),
                        },
                    )
                    await self._sleep(delay)
                    continue
                logger.error(
                    "llm_request_failed",
                    extra={
                        "node": name,
                        "attempts": attempt,
                        "status_code": status,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:300],
                    },
                )
                raise LLMUnavailableError(
                    f"{name}: OpenAI request failed after {attempt} attempt(s): "
                    f"{type(exc).__name__}: {str(exc)[:300]}",
                    retryable=transient,
                ) from exc

        duration_ms = (time.perf_counter() - started) * 1000
        parsed = response.output_parsed
        if parsed is None:
            refusal = _refusal_text(response)
            raise LLMOutputError(
                f"{name}: no structured output" + (f" (refusal: {refusal})" if refusal else "")
            )
        usage = getattr(response, "usage", None)
        result = LLMResult(
            parsed=parsed,
            model=getattr(response, "model", None) or self._model,
            response_id=getattr(response, "id", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            attempts=attempt,
            duration_ms=duration_ms,
        )
        logger.info("llm_request", extra={"node": name, **result.usage()})
        return result


def _refusal_text(response: Any) -> str | None:
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                return str(getattr(content, "refusal", ""))[:300]
    return None
