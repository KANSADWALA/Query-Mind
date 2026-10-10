"""Thin wrapper around any OpenAI-compatible chat endpoint (Groq, OpenAI, Ollama, LM Studio, ...)."""
from __future__ import annotations

import json
import re
import time
from typing import Protocol


class LLMError(RuntimeError):
    pass


class ChatModel(Protocol):
    def chat(self, messages: list[dict], json_mode: bool = False, temperature: float | None = None) -> str: ...


def parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences or surrounding prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError(f"Model did not return valid JSON: {text[:200]}")


def is_reasoning_model(model: str) -> bool:
    """Models that accept a `reasoning_effort` parameter (gpt-oss, o-series, qwen3 on Groq...)."""
    m = model.lower()
    return any(k in m for k in ("gpt-oss", "/o1", "/o3", "/o4", "qwen3")) or m.startswith(("o1", "o3", "o4", "gpt-5"))


class OpenAICompatibleLLM:
    def __init__(self, api_key: str, model: str, base_url: str, temperature: float = 0.1,
                 reasoning_effort: str = "medium", timeout: float = 90):
        if not api_key:
            raise LLMError("No API key configured. Add it in the sidebar or in your .env file.")
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout)
        self.model = model
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort if is_reasoning_model(model) else ""
        self._json_supported = True

    def chat(self, messages: list[dict], json_mode: bool = False, temperature: float | None = None) -> str:
        from openai import APIConnectionError, APIStatusError, BadRequestError, RateLimitError

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if json_mode and self._json_supported:
            kwargs["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}

        last_error: Exception | None = None
        for attempt in range(4):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
                if not content.strip():
                    raise LLMError("The model returned an empty response.")
                return content
            except RateLimitError as e:
                last_error = e
                time.sleep(2 * (attempt + 1))
            except BadRequestError as e:
                last_error = e
                msg = str(e).lower()
                # Degrade gracefully on providers/models that reject optional parameters,
                # or when JSON mode validation fails (we can still parse JSON from plain text).
                if "reasoning" in msg and "extra_body" in kwargs:
                    kwargs.pop("extra_body")
                    self.reasoning_effort = ""
                    continue
                if "response_format" in kwargs:
                    kwargs.pop("response_format")
                    if "json" not in msg:
                        self._json_supported = False
                    continue
                if "temperature" in msg and "temperature" in kwargs:
                    kwargs.pop("temperature")
                    continue
                raise LLMError(f"LLM request rejected: {e}") from e
            except APIConnectionError as e:
                last_error = e
                time.sleep(1.5)
            except APIStatusError as e:
                if e.status_code in (401, 403):
                    raise LLMError("The API key was rejected. Check the key in the sidebar or .env.") from e
                if e.status_code == 404:
                    raise LLMError(f"Model '{self.model}' was not found at this provider. Pick another model in the sidebar.") from e
                last_error = e
                time.sleep(1.5)
            except LLMError:
                raise
            except Exception as e:
                raise LLMError(f"LLM request failed: {e}") from e
        if isinstance(last_error, RateLimitError):
            raise LLMError("The LLM provider is rate-limiting requests. Please wait a moment and retry.")
        raise LLMError(f"LLM request failed: {last_error}")

    def stream(self, messages: list[dict], temperature: float | None = None):
        """Yield the reply text piece by piece (plain text, no JSON mode). Falls back to one chunk on error."""
        from openai import APIError

        kwargs: dict = {"model": self.model, "messages": messages, "stream": True,
                        "temperature": self.temperature if temperature is None else temperature}
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}
        try:
            for chunk in self.client.chat.completions.create(**kwargs):
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except APIError:
            yield self.chat(messages, temperature=temperature)  # non-streaming retry path with its own fallbacks
        except Exception as e:
            raise LLMError(f"LLM request failed: {e}") from e

    def chat_json(self, messages: list[dict], temperature: float | None = None) -> dict:
        return parse_json(self.chat(messages, json_mode=True, temperature=temperature))
