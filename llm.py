"""Thin streaming wrappers around the Anthropic and OpenAI SDKs.

Each function streams a completion token-by-token, invoking ``on_token`` for
every chunk of text as it arrives, and returns the full accumulated text once
the model is done. Keeping this isolated means the debate engine never has to
care which vendor it is talking to.
"""

from typing import Callable, Dict, List, Optional


class LLMError(Exception):
    """Raised when a provider call fails (bad key, bad model, network, ...)."""


def stream_anthropic(
    api_key: str,
    model: str,
    system: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    on_token: Callable[[str], None],
) -> str:
    """Stream a message from Claude. ``messages`` use roles user/assistant."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise LLMError("The 'anthropic' package is not installed.") from exc

    client = anthropic.Anthropic(api_key=api_key)
    collected = []
    try:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        ) as stream:
            for chunk in stream.text_stream:
                if chunk:
                    collected.append(chunk)
                    on_token(chunk)
    except Exception as exc:  # noqa: BLE001 - surface a clean message upward
        raise LLMError("Claude error: {}".format(_describe(exc))) from exc
    return "".join(collected)


def stream_openai(
    api_key: str,
    model: str,
    system: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    on_token: Callable[[str], None],
) -> str:
    """Stream a chat completion from an OpenAI model (e.g. ChatGPT)."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise LLMError("The 'openai' package is not installed.") from exc

    client = OpenAI(api_key=api_key)
    full_messages = [{"role": "system", "content": system}] + messages
    collected = []
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=full_messages,
            max_tokens=max_tokens,
            stream=True,
        )
        for event in stream:
            if not event.choices:
                continue
            delta = event.choices[0].delta
            text = getattr(delta, "content", None)
            if text:
                collected.append(text)
                on_token(text)
    except Exception as exc:  # noqa: BLE001
        raise LLMError("ChatGPT error: {}".format(_describe(exc))) from exc
    return "".join(collected)


# Maps a provider name to its streaming function. The debate engine looks each
# participant up here so adding a third vendor is a one-line change.
PROVIDERS = {
    "anthropic": stream_anthropic,
    "openai": stream_openai,
}


def stream(provider: str, **kwargs) -> str:
    fn = PROVIDERS.get(provider)
    if fn is None:
        raise LLMError("Unknown provider: {}".format(provider))
    return fn(**kwargs)


# OpenAI's /models endpoint returns everything (embeddings, audio, image, ...).
# We keep only chat-capable families and drop the rest by substring.
_OPENAI_KEEP_PREFIXES = ("gpt", "o1", "o3", "o4", "chatgpt")
_OPENAI_DROP_SUBSTRINGS = (
    "embedding", "whisper", "tts", "audio", "realtime", "moderation",
    "transcribe", "image", "dall-e", "search", "instruct", "babbage",
    "davinci", "computer-use",
)


def list_models(provider: str, api_key: str) -> List[Dict[str, str]]:
    """Return the available chat models for a provider as ``[{id, label}]``.

    Pulled live from the vendor's own API so nothing is hard-coded.
    """
    if provider == "anthropic":
        return _list_anthropic_models(api_key)
    if provider == "openai":
        return _list_openai_models(api_key)
    raise LLMError("Unknown provider: {}".format(provider))


def _list_anthropic_models(api_key: str) -> List[Dict[str, str]]:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise LLMError("The 'anthropic' package is not installed.") from exc
    client = anthropic.Anthropic(api_key=api_key)
    try:
        page = client.models.list(limit=100)
    except Exception as exc:  # noqa: BLE001
        raise LLMError("Could not list Claude models: {}".format(_describe(exc))) from exc
    out = []
    for m in page.data:
        out.append({"id": m.id, "label": getattr(m, "display_name", None) or m.id})
    return out  # Anthropic returns newest first


def _list_openai_models(api_key: str) -> List[Dict[str, str]]:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMError("The 'openai' package is not installed.") from exc
    client = OpenAI(api_key=api_key)
    try:
        page = client.models.list()
    except Exception as exc:  # noqa: BLE001
        raise LLMError("Could not list ChatGPT models: {}".format(_describe(exc))) from exc

    items = []
    for m in page.data:
        mid = m.id
        low = mid.lower()
        if any(bad in low for bad in _OPENAI_DROP_SUBSTRINGS):
            continue
        if not low.startswith(_OPENAI_KEEP_PREFIXES):
            continue
        items.append({"id": mid, "label": mid, "_created": getattr(m, "created", 0) or 0})
    items.sort(key=lambda x: x["_created"], reverse=True)  # newest first
    for it in items:
        it.pop("_created", None)
    return items


def _describe(exc: Exception) -> str:
    """Pull a short, human-friendly reason out of an SDK exception."""
    message = getattr(exc, "message", None)
    if message:
        return str(message)
    text = str(exc).strip()
    return text or exc.__class__.__name__


def validate_key(provider: str, api_key: str, model: str) -> Optional[str]:
    """Best-effort, cheap check that a key/model pair works.

    Returns ``None`` on success or a short error string on failure.
    """
    sink = lambda _chunk: None  # noqa: E731 - tiny throwaway callback
    probe = [{"role": "user", "content": "Reply with the single word: ok"}]
    try:
        stream(
            provider,
            api_key=api_key,
            model=model,
            system="You are a connectivity probe. Reply with exactly: ok",
            messages=probe,
            max_tokens=8,
            on_token=sink,
        )
    except LLMError as exc:
        return str(exc)
    return None
