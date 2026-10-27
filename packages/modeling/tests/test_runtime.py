import hashlib
import json
from pathlib import Path

import pytest

from fra_core.normalization import NormalizationSettings, load_normalization_settings
from fra_core.profile import Profile
from fra_core.schemas import StatementType
from fra_model.runtime import LlamaServerRuntime, ModelRuntimeError


def settings(tmp_path: Path) -> NormalizationSettings:
    path = tmp_path / "model.gguf"
    path.write_bytes(b"weights")
    return NormalizationSettings(
        enabled=True,
        url="http://127.0.0.1:8080",
        model_path=path,
        sha256=hashlib.sha256(b"weights").hexdigest(),
        size_bytes=7,
        timeout_seconds=2,
    )


class Transport:
    def __init__(self, path: Path, content: str = '{"canonical_id":null}') -> None:
        self.path = path
        self.content = content
        self.calls: list[tuple[str, str, object, float]] = []
        self.model: str | None = None
        self.finish = "stop"

    def __call__(self, method: str, url: str, body: bytes | None, timeout: float) -> bytes:
        self.calls.append((method, url, json.loads(body) if body else None, timeout))
        if url.endswith("/props"):
            return json.dumps({"model_path": str(self.path)}).encode()
        return json.dumps(
            {
                "model": self.model or str(self.path),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": self.finish,
                        "message": {"role": "assistant", "content": self.content},
                    }
                ],
            }
        ).encode()


def test_request_and_lazy_identity(tmp_path: Path) -> None:
    config = settings(tmp_path)
    transport = Transport(config.model_path)
    runtime = LlamaServerRuntime(config, transport=transport)
    assert not transport.calls
    assert runtime.normalize("  التزامات  ", StatementType.BALANCE) == '{"canonical_id":null}'
    assert len(transport.calls) == 2
    body = transport.calls[1][2]
    assert isinstance(body, dict)
    assert body["temperature"] == 0 and body["cache_prompt"] is False
    assert body["max_tokens"] <= 64
    assert json.loads(body["messages"][1]["content"]) == {
        "label": "  التزامات  ",
        "statement": "balance",
    }
    assert all(call[3] == 2 for call in transport.calls)


@pytest.mark.parametrize(
    "reply",
    [
        "null",
        "{}",
        '{"canonical_id":1}',
        '{"canonical_id":"unknown"}',
        '{"canonical_id":"revenue"}',
        '{"canonical_id":null,"canonical_id":null}',
        "```json\n{}\n```",
    ],
)
def test_invalid_reply_is_returned_for_proposal_assessment(tmp_path: Path, reply: str) -> None:
    config = settings(tmp_path)
    assert (
        LlamaServerRuntime(config, transport=Transport(config.model_path, reply)).normalize(
            "x", StatementType.BALANCE
        )
        == reply
    )


@pytest.mark.parametrize("defect", ["path", "hash", "size", "reply_model", "truncated"])
def test_wrong_identity_and_truncation_fail(tmp_path: Path, defect: str) -> None:
    config = settings(tmp_path)
    transport = Transport(config.model_path)
    if defect == "path":
        transport.path = tmp_path / "other.gguf"
    elif defect == "hash":
        config = config.model_copy(update={"sha256": "0" * 64})
    elif defect == "size":
        config = config.model_copy(update={"size_bytes": 9})
    elif defect == "reply_model":
        transport.model = "other"
    else:
        transport.finish = "length"
    with pytest.raises(ModelRuntimeError):
        LlamaServerRuntime(config, transport=transport).normalize("x", StatementType.BALANCE)


def test_network_failure_is_named(tmp_path: Path) -> None:
    def fail(method: str, url: str, body: bytes | None, timeout: float) -> bytes:
        raise TimeoutError("timed out")

    with pytest.raises(ModelRuntimeError, match=r"normalization.*timed out"):
        LlamaServerRuntime(settings(tmp_path), transport=fail).normalize("x", StatementType.BALANCE)


def test_off_needs_no_port_and_profile_hosts(tmp_path: Path) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "normalization.toml").write_text(
        (Path(__file__).resolve().parents[3] / "configs" / "normalization.toml").read_text()
    )
    assert not load_normalization_settings({}, tmp_path).enabled
    for profile, host in [(Profile.NATIVE, "127.0.0.1"), (Profile.DOCKER, "llm")]:
        config = load_normalization_settings(
            {
                "FRA_NORMALIZATION_ENABLED": "1",
                "FRA_PROFILE": profile.value,
                "FRA_LLM_PORT": "8123",
            },
            tmp_path,
        )
        assert config.url == f"http://{host}:8123"
    with pytest.raises(ValueError, match="FRA_NORMALIZATION_ENABLED"):
        load_normalization_settings({"FRA_NORMALIZATION_ENABLED": "yes"}, tmp_path)


@pytest.mark.parametrize(
    "envelope",
    [b"null", b"[]", b"{", b'{"model":"x","model":"x"}', b'{"choices":[]}', b'{"model":null}'],
)
def test_invalid_http_envelope_fails(tmp_path: Path, envelope: bytes) -> None:
    config = settings(tmp_path)

    def malformed(method: str, url: str, body: bytes | None, timeout: float) -> bytes:
        if url.endswith("/props"):
            return json.dumps({"model_path": str(config.model_path)}).encode()
        return envelope

    with pytest.raises(ModelRuntimeError):
        LlamaServerRuntime(config, transport=malformed).normalize("x", StatementType.BALANCE)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com:443",
        "http://127.0.0.1:8080/proxy",
        "http://user@127.0.0.1:8080",
        "http://127.0.0.1:8080?token=x",
    ],
)
def test_remote_or_credentialed_endpoints_refused_before_io(tmp_path: Path, url: str) -> None:
    def unexpected(method: str, url: str, body: bytes | None, timeout: float) -> bytes:
        raise AssertionError("network must not be attempted")

    config = settings(tmp_path).model_copy(update={"url": url})
    with pytest.raises(ModelRuntimeError, match="local"):
        LlamaServerRuntime(config, transport=unexpected).normalize("x", StatementType.BALANCE)
