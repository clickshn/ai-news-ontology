"""`llm.provider` 롤백 경로가 실제로 동작하는지 고정한다 (F1, ADR-018 Risks).

ADR-018 은 `anthropic` SDK 를 **롤백 경로로 남긴다**고 적었고, Risks 에는
"되돌리기가 너무 쉽다 — `llm.provider` 한 줄이다"라고 적었다. 그런데 그 한 줄을
바꾸면 세 단계 전부 벤더가 거부하는 요청을 만들고 있었다.

- `temperature: 0` 이 그대로 실려 Opus 5 가 400 을 낸다 (D-033).
- 이전이 세 단계의 `model` 을 gemma 로 **제자리에 덮어써서**, 벤더에 없는 모델을
  부른다. 벤더 모델명은 `# 이전: ...` 주석으로만 남아 있었다.

둘 다 **런타임에만 드러나는 파손**이라 테스트가 없으면 다음 롤백 때 똑같이 겪는다.
안전장치라고 적어 둔 것이 동작하는지는 적어 둔 곳이 아니라 여기서 확인한다.

실제 호출은 하지 않는다 — `client=object()` 로 목을 주입해 요청 **형태**만 본다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from eval.runner import judge_client_from_config
from extraction.llm import AnthropicClient, client_from_config, unsupported_params

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 벤더 모델명은 `claude-` 로 시작한다. 롤백이 성립하려면 세 단계 전부 그렇다.
VENDOR_PREFIX = "claude-"


@pytest.fixture
def rolled_back() -> dict[str, Any]:
    """레포의 실제 `config.yaml` 을 벤더로 되돌린 사본.

    픽스처를 손으로 만들지 않고 정본을 읽는다 — 이 테스트가 지키려는 것은
    "우리 config 로 롤백이 되는가"이고, 흉내 낸 설정으로는 그걸 못 본다.
    """
    config = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    config["llm"]["provider"] = "anthropic"
    return config


def kwargs_for(config: dict[str, Any], stage: str) -> dict[str, Any]:
    if stage == "eval_judge":
        client = judge_client_from_config(config, client=object())
    else:
        client = client_from_config(config, stage=stage, client=object())
    assert isinstance(client, AnthropicClient)
    return client._request_kwargs()


# ---------------------------------------------------------------------------
# 모델명 — 주석이 아니라 키로 남아 있는가
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stage", ["relevance_gate", "extraction", "eval_judge"])
def test_rollback_resolves_a_vendor_model(rolled_back: dict[str, Any], stage: str) -> None:
    """롤백하면 세 단계 전부 **벤더에 존재하는** 모델을 가리킨다."""
    model = kwargs_for(rolled_back, stage)["model"]
    assert model.startswith(VENDOR_PREFIX), f"{stage} 가 벤더에 없는 모델을 부른다: {model}"


@pytest.mark.parametrize("stage", ["relevance_gate", "extraction", "eval_judge"])
def test_default_provider_keeps_the_internal_model(stage: str) -> None:
    """반대 방향도 고정한다 — `vendor_model` 추가가 기본 경로를 건드리지 않는다."""
    config = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    if stage == "eval_judge":
        client = judge_client_from_config(config)
    else:
        client = client_from_config(config, stage=stage)
    assert not client.model.startswith(VENDOR_PREFIX)


# ---------------------------------------------------------------------------
# 파라미터 — 모델이 거부하는 것이 실리지 않는가
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stage", ["relevance_gate", "extraction", "eval_judge"])
def test_rollback_sends_no_rejected_parameter(rolled_back: dict[str, Any], stage: str) -> None:
    """실린 요청에 그 모델이 400 으로 거부하는 파라미터가 없다."""
    kwargs = kwargs_for(rolled_back, stage)
    rejected = unsupported_params(kwargs["model"])

    if "temperature" in rejected:
        assert "temperature" not in (kwargs.get("extra_body") or {})
    if "effort" in rejected:
        assert "effort" not in (kwargs.get("output_config") or {})
    if "thinking" in rejected:
        assert "thinking" not in kwargs


def test_opus_drops_temperature_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    """뺀 것을 **조용히** 빼지 않는다 — 이름을 남기고 stderr 로 알린다."""
    client = AnthropicClient(model="claude-opus-5", temperature=0, client=object())
    assert client.temperature is None
    assert client.dropped_params == ("temperature",)
    assert "temperature" in capsys.readouterr().err


def test_haiku_keeps_temperature_but_drops_effort() -> None:
    """모델마다 거부하는 것이 다르다. 프로바이더 단위로 가르면 표현할 수 없는 차이다."""
    client = AnthropicClient(
        model="claude-haiku-4-5-20251001", temperature=0, effort="high", client=object()
    )
    assert client.temperature == 0
    assert client.effort is None
    assert client.dropped_params == ("effort",)


def test_unknown_model_keeps_everything() -> None:
    """표에 없는 모델에는 능력을 가정하지 않는다 — 아무것도 빼지 않는다."""
    client = AnthropicClient(
        model="gemma-4-31B-it", temperature=0, effort="high", client=object()
    )
    assert client.dropped_params == ()
    assert client._request_kwargs()["extra_body"] == {"temperature": 0}
