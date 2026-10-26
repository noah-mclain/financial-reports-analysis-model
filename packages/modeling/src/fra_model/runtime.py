"""Local llama.cpp normalization, with strict identity and response boundaries."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from fra_core.normalization import NormalizationSettings
from fra_core.normalization_prompt import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_SHA256,
    normalization_messages,
)
from fra_core.schemas import StatementType


class ModelRuntimeError(RuntimeError):
    """An enabled normalization stage cannot proceed; no deterministic fallback."""


class ModelRuntime(Protocol):
    def validate(self) -> None: ...

    def normalize(self, label: str, statement: StatementType) -> str: ...


Transport = Callable[[str, str, bytes | None, float], bytes]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        raise ModelRuntimeError("normalization server redirect refused")


def _http(method: str, url: str, body: bytes | None, timeout: float) -> bytes:
    # Ignore proxy environment and never follow a redirect to another inference service.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        url, data=body, method=method, headers={"Content-Type": "application/json"}
    )
    with opener.open(request, timeout=timeout) as response:
        data = response.read(1_048_577)
        if not isinstance(data, bytes):
            raise ModelRuntimeError("normalization response must be bytes")
        if len(data) > 1_048_576:
            raise ModelRuntimeError("normalization response exceeds size limit")
        return data


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate response key {key}")
        result[key] = value
    return result


class LlamaServerRuntime:
    """Both profiles use this runtime. Construction does no I/O; mapping validates lazily."""

    def __init__(self, settings: NormalizationSettings, *, transport: Transport = _http) -> None:
        self.settings = settings
        self._transport = transport
        self._validated = False

    def _request(self, path: str, payload: object | None = None) -> dict[str, object]:
        try:
            raw = self._transport(
                "GET" if payload is None else "POST",
                self.settings.url + path,
                None if payload is None else json.dumps(payload, ensure_ascii=False).encode(),
                self.settings.timeout_seconds,
            )
            parsed: object = json.loads(raw, object_pairs_hook=_object)
            if not isinstance(parsed, dict):
                raise ValueError("response envelope must be an object")
            return parsed
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ModelRuntimeError(f"normalization {path}: {exc}") from exc

    def validate(self) -> None:
        if self._validated:
            return
        try:
            url = urlsplit(self.settings.url)
            if (
                url.scheme != "http"
                or url.hostname not in ("127.0.0.1", "llm")
                or url.port is None
                or url.username is not None
                or url.password is not None
                or url.path
                or url.query
                or url.fragment
            ):
                raise ValueError("only a local normalization server is permitted")
            if hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest() != SYSTEM_PROMPT_SHA256:
                raise ValueError("normalization prompt hash differs from trained pin")
            path = self.settings.model_path
            if not path.is_absolute():
                raise ValueError("normalization model path must be absolute")
            if path.stat().st_size != self.settings.size_bytes:
                raise ValueError(f"normalization model size mismatch: {path}")
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != self.settings.sha256:
                    raise ValueError(f"normalization model SHA256 mismatch: {path}")
            props = self._request("/props")
            # Exact absolute path shared with server, never basename-only identity.
            if props.get("model_path") != str(path):
                raise ValueError("normalization /props model_path differs from pinned path")
        except (OSError, ValueError) as exc:
            raise ModelRuntimeError(f"normalization identity: {exc}") from exc
        self._validated = True

    def normalize(self, label: str, statement: StatementType) -> str:
        self.validate()
        result = self._request(
            "/v1/chat/completions",
            {
                "model": str(self.settings.model_path),
                "messages": normalization_messages(label, statement.value),
                "temperature": 0,
                "max_tokens": 64,
                "cache_prompt": False,
            },
        )
        try:
            if result.get("model") != str(self.settings.model_path):
                raise ValueError("response model differs from pinned path")
            choices = result.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("response needs exactly one choice")
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
                raise ValueError("response was truncated or did not stop")
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("response message must be assistant content")
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError("response content must be a JSON string")
            return content
        except ValueError as exc:
            raise ModelRuntimeError(f"normalization response: {exc}") from exc
