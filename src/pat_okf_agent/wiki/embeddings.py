"""HTTP embedding adapters (Gemma/OpenAI-compatible endpoints).

The core package stays dependency-free.  ``HttpEmbedder`` talks to a local or
hosted inference server over HTTP and can be paired with any search backend
that accepts ``embedder(text) -> list[float]``.  ``GemmaEmbedder`` is a thin
Gemini-style preset for a Gemma Embedding endpoint.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable

from .search import BackendUnavailable

__all__ = ["GemmaEmbedder", "HttpEmbedder"]

Transport = Callable[[urllib.request.Request, float], Any]


class HttpEmbedder:
    """Call an OpenAI-compatible or Gemini-style embedding endpoint."""

    def __init__(self, url: str, *, model: str | None = None,
                 api_key: str | None = None, style: str = "openai",
                 timeout: float = 60.0, transport: Transport | None = None,
                 user_agent: str = "okf-embeddings/0.1"):
        if not url or not url.startswith(("http://", "https://")):
            raise ValueError("embedding url must be http(s)")
        if style not in ("openai", "gemini"):
            raise ValueError("style must be 'openai' or 'gemini'")
        self.url = url
        self.model = model
        self.api_key = api_key
        self.style = style
        self.timeout = float(timeout)
        self.transport = transport or _default_transport
        self.user_agent = user_agent

    def __call__(self, text: str) -> list[float]:
        url = self.url
        headers = {"Content-Type": "application/json",
                   "User-Agent": self.user_agent}
        if self.style == "gemini":
            model_placeholder = "{model}" in url
            if self.model and model_placeholder:
                url = url.replace(
                    "{model}", urllib.parse.quote(self.model, safe=""))
            payload: dict[str, Any] = {
                "content": {"parts": [{"text": text}]},
            }
            if self.model and not model_placeholder:
                payload["model"] = f"models/{self.model}"
            if self.api_key:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}key=" + urllib.parse.quote(
                    self.api_key, safe="")
        else:
            payload = {"input": text}
            if self.model:
                payload["model"] = self.model
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers=headers, method="POST")
        try:
            response = self.transport(request, self.timeout)
            try:
                raw = response.read() if hasattr(response, "read") else response
                status = getattr(response, "status", 200)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if isinstance(status, int) and status >= 400:
                raise BackendUnavailable(
                    f"embedding endpoint HTTP {status}")
            if isinstance(raw, (dict, list)):
                data = raw
            else:
                data = json.loads(raw.decode("utf-8")
                                  if isinstance(raw, bytes) else str(raw))
        except BackendUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - optional adapter boundary
            close = getattr(exc, "close", None)
            try:
                raise BackendUnavailable(
                    f"embedding request failed: "
                    f"{type(exc).__name__}: {exc}") from exc
            finally:
                if callable(close):
                    close()
        values = _extract_values(data)
        if not values:
            raise BackendUnavailable("embedding endpoint returned no vector")
        return [float(value) for value in values]


class GemmaEmbedder(HttpEmbedder):
    """Gemini-style preset for a Gemma Embedding 2 endpoint."""

    def __init__(self, url: str, *, model: str = "gemma-embedding-2",
                 api_key: str | None = None, **kwargs: Any):
        super().__init__(url, model=model, api_key=api_key,
                         style="gemini", **kwargs)


def _extract_values(data: Any) -> list[Any]:
    if isinstance(data, dict):
        if isinstance(data.get("embedding"), dict):
            return list(data["embedding"].get("values") or [])
        if isinstance(data.get("embedding"), list):
            return list(data["embedding"])
        if isinstance(data.get("embeddings"), list):
            first = data["embeddings"][0] if data["embeddings"] else None
            if isinstance(first, dict):
                return list(first.get("embedding")
                            or first.get("values") or [])
            if isinstance(first, list):
                return list(first)
        if isinstance(data.get("data"), list) and data["data"]:
            first = data["data"][0]
            if isinstance(first, dict):
                return list(first.get("embedding") or [])
        if isinstance(data.get("vector"), list):
            return list(data["vector"])
    return []


def _default_transport(request: urllib.request.Request,
                       timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)
