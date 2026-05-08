"""LLM helper — give SQ1 cloud agents a brain.

The pattern for every smart agent is:
1. Pull data deterministically (existing code)
2. Build a structured payload dict
3. Call ``LLM(secrets()).annotate(system=..., payload=...)``
4. Splice the returned annotation into the agent's output (email, HTML, etc.)

Anthropic API key is read from sq1-platform-kv as ``anthropic-api-key`` via
the same ``Secrets`` helper the rest of the platform uses, so the cloud
agent's Managed Identity is the only auth surface to manage.

Prompt caching (5-min TTL) is enabled for the system prompt, so a
high-frequency caller (e.g. clock-in alerts that fire 5x/day) effectively
pays for the system prompt once per cache window. See:
https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .secrets import Secrets

log = logging.getLogger("sq1_platform.llm")

# Default model — Sonnet 4.5 is the current sweet spot for these annotation
# workloads (smart enough for context-rich summaries, cheap enough at scale).
# Override per-call via ``model=`` kwarg if a specific agent needs Opus.
DEFAULT_MODEL = "claude-sonnet-4-5"


class LLM:
    """Thin Anthropic SDK wrapper.

    One instance can be reused across many ``annotate`` calls within the
    same Function App invocation. The SDK client itself is lazy — it's
    not constructed until the first call, so importing this module is
    free even on agents that don't always need the LLM.
    """

    def __init__(
        self,
        secrets: Optional[Secrets] = None,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
    ):
        self._secrets = secrets or Secrets()
        self._api_key = api_key  # if provided, skip KV lookup
        self._model = model
        self._client = None  # lazy

    @property
    def client(self):
        if self._client is None:
            # Import inside the property so agents that never call .annotate()
            # don't pay the import cost (anthropic SDK is ~50MB of deps).
            from anthropic import Anthropic

            key = self._api_key or self._secrets.get("anthropic-api-key")
            self._client = Anthropic(api_key=key)
        return self._client

    def annotate(
        self,
        *,
        system: str,
        payload: Dict[str, Any],
        max_tokens: int = 1024,
        model: Optional[str] = None,
        cache_system: bool = True,
    ) -> str:
        """Get a single text annotation for a structured payload.

        Args:
            system: Persona / instructions for the model (the agent's
                "personality"). Cached for cheap repeats.
            payload: Structured dict — gets serialized to JSON and sent
                as the user message. Keep it small (<5KB ideally).
            max_tokens: Cap on response length. 1024 is plenty for a
                paragraph-style annotation; bump for longer outputs.
            model: Override the default model (e.g. for Opus on a
                high-stakes summary).
            cache_system: Toggle prompt caching for the system block.
                Set False if the system prompt changes per-call.

        Returns:
            The text content of the model's response. Empty string if
            something went sideways (caller should treat as "no
            annotation available" rather than fail the run).
        """
        try:
            user_content = (
                "Here is the structured event data to annotate:\n\n"
                f"<data>\n{json.dumps(payload, indent=2, default=str)}\n</data>"
            )
            system_block: Any
            if cache_system:
                system_block = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                system_block = system

            resp = self.client.messages.create(
                model=model or self._model,
                max_tokens=max_tokens,
                system=system_block,
                messages=[{"role": "user", "content": user_content}],
            )
            # Token usage logging is useful for cost visibility in App Insights
            usage = getattr(resp, "usage", None)
            if usage is not None:
                log.info(
                    "LLM.annotate model=%s in=%s out=%s cache_read=%s cache_create=%s",
                    model or self._model,
                    getattr(usage, "input_tokens", 0),
                    getattr(usage, "output_tokens", 0),
                    getattr(usage, "cache_read_input_tokens", 0),
                    getattr(usage, "cache_creation_input_tokens", 0),
                )
            # The .content list has TextBlock entries; concatenate all text
            parts = []
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    parts.append(block.text)
            return "".join(parts).strip()
        except Exception as e:
            # Annotation failures must NOT fail the agent run — every caller
            # should keep working with the raw deterministic output if the
            # LLM is unavailable, rate-limited, or having a bad day.
            log.exception("LLM.annotate failed: %s", e)
            return ""
