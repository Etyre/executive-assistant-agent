from __future__ import annotations

from .. import config
from .anthropic_provider import AnthropicProvider
from .base import Provider
from .openai_compat_provider import OpenAICompatProvider


def make_provider(settings: config.Settings | None = None) -> Provider:
    s = settings or config.load()
    llm = s.llm
    if llm.provider == "anthropic":
        return AnthropicProvider(model=llm.model, effort=llm.effort, fallbacks=llm.fallbacks)
    if llm.provider == "openai_compat":
        return OpenAICompatProvider(base_url=llm.openai_base_url, model=llm.model,
                                    api_key=config.secret(llm.openai_api_key_env))
    raise ValueError(f"unknown llm.provider {llm.provider!r} (anthropic | openai_compat)")
