"""Cliente de LLM con proveedores intercambiables (Anthropic / OpenAI) e instrumentación."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, LLMError
from app.schemas import LLMResponse, TokenUsage
from app.services.metrics import MetricsService, Stopwatch

logger = logging.getLogger(__name__)

Message = dict[str, str]  # {"role": "user"|"assistant", "content": "..."}


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def complete(self, system: str, messages: list[Message]) -> tuple[str, TokenUsage]: ...


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings):
        if not settings.anthropic_api_key:
            raise ConfigurationError("Define ANTHROPIC_API_KEY en tu .env")
        from anthropic import Anthropic

        self.model = settings.anthropic_model
        self._max_tokens = settings.llm_max_tokens
        self._temperature = settings.llm_temperature
        self._client = Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,  # reintentos con backoff del propio SDK
        )

    def complete(self, system: str, messages: list[Message]) -> tuple[str, TokenUsage]:
        kwargs = dict(model=self.model, max_tokens=self._max_tokens, system=system, messages=messages)
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        try:
            resp = self._client.messages.create(**kwargs)
        except TypeError as exc:  # algunas versiones del SDK / modelos ya no aceptan 'temperature'
            if "temperature" not in kwargs:
                raise
            logger.warning("El SDK rechazó 'temperature' (%s); se reintenta sin ese parámetro", exc)
            self._temperature = None
            kwargs.pop("temperature")
            resp = self._client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return text, TokenUsage(input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise ConfigurationError("Define OPENAI_API_KEY en tu .env")
        from openai import OpenAI

        self.model = settings.openai_model
        self._max_tokens = settings.llm_max_tokens
        self._temperature = settings.llm_temperature
        self._client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )

    def complete(self, system: str, messages: list[Message]) -> tuple[str, TokenUsage]:
        kwargs = dict(
            model=self.model,
            max_completion_tokens=self._max_tokens,
            messages=[{"role": "system", "content": system}, *messages],
        )
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        resp = self._client.chat.completions.create(**kwargs)
        usage = resp.usage
        return (
            resp.choices[0].message.content or "",
            TokenUsage(input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens),
        )


def build_llm_provider(settings: Settings) -> LLMProvider:
    return AnthropicProvider(settings) if settings.llm_provider == "anthropic" else OpenAIProvider(settings)


class LLMService:
    def __init__(self, provider: LLMProvider, metrics: MetricsService):
        self.provider = provider
        self.metrics = metrics

    def generate(self, system: str, messages: list[Message]) -> LLMResponse:
        usage, text, error = TokenUsage(), "", None
        with Stopwatch() as sw:
            try:
                text, usage = self.provider.complete(system, messages)
            except Exception as exc:
                error = exc
        cost = self.metrics.cost_usd(usage.input_tokens, usage.output_tokens)
        self.metrics.record(
            operation="chat", provider=self.provider.name, model=self.provider.model,
            latency_ms=sw.ms, status="error" if error else "ok",
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            error=str(error) if error else None, meta={"n_messages": len(messages)},
        )  # fmt: skip
        if error:
            raise LLMError(f"Error del proveedor de LLM ({self.provider.name}): {error}") from error
        return LLMResponse(
            text=text, provider=self.provider.name, model=self.provider.model,
            usage=usage, latency_ms=sw.ms, cost_usd=cost,
        )  # fmt: skip
