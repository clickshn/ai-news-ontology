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

import subprocess
import sys
import tomllib
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


# ---------------------------------------------------------------------------
# 롤백에 필요한 것은 설정 한 줄이 아니다 (F7)
# ---------------------------------------------------------------------------
#: SDK 가 없는 상태를 흉내 낸다. 실제로 지웠다 깔았다 할 수 없으므로
#: **import 를 막는 것**으로 같은 상황을 만든다.
_BLOCK_VENDOR_SDK = """
import sys
from importlib.abc import MetaPathFinder


class _Block(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "anthropic" or fullname.startswith("anthropic."):
            raise ModuleNotFoundError("No module named 'anthropic'", name=fullname)
        return None


sys.meta_path.insert(0, _Block())

import extraction.llm as llm
import extraction.vllm  # noqa: F401  기본 경로가 SDK 를 건드리지 않는지도 같이 본다

# 목 주입 경로는 SDK 없이도 살아 있어야 한다 — 테스트 전체가 이 경로를 쓴다.
client = llm.AnthropicClient(model="claude-opus-5", client=object())
assert client.model == "claude-opus-5"
assert "anthropic" not in sys.modules, "기본 경로가 벤더 SDK 를 끌고 들어왔다"

try:
    llm._load_vendor_sdk()
except llm.LLMError as exc:
    assert "[vendor]" in str(exc), str(exc)
else:
    raise AssertionError("SDK 가 없는데 _load_vendor_sdk 가 성공했다")

print("OK")
"""


def test_core_path_runs_without_the_vendor_sdk() -> None:
    """`anthropic` 이 없어도 기본 경로가 import 되고 동작한다.

    코어 의존성에서 뺀 것(F7)이 **런타임에서도 참인지**를 여기서 잰다. 선언만
    바꾸고 `import anthropic` 을 모듈 상단에 남겨 두면 기본 설치가 그 자리에서
    깨지는데, 이 레포의 .venv 에는 SDK 가 깔려 있어 평소에는 드러나지 않는다.
    별도 프로세스로 도는 이유가 그것이다 — 이미 import 된 모듈은 못 되돌린다.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _BLOCK_VENDOR_SDK],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "OK" in proc.stdout


def test_vendor_sdk_is_an_extra_not_a_core_dependency() -> None:
    """`pyproject.toml` 이 실제로 그렇게 적혀 있는지 잰다.

    **설치돼 있는 것만으로 경로가 생긴다** — 게이트가 막는 것은 우리 코드를
    지나는 호출뿐이고, SDK 와 키가 같은 머신에 있으면 스크립트 파일 하나로
    두 계층 밖에서 나간다 (ADR-017 Consequences). 그래서 "코어에 없다"는
    주석이 아니라 검사 대상이다.
    """
    meta = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = " ".join(meta["project"]["dependencies"])
    extras = meta["project"]["optional-dependencies"]

    assert "anthropic" not in core
    assert any("anthropic" in dep for dep in extras["vendor"])


def test_rollback_procedure_names_the_extra() -> None:
    """롤백 절차 문서가 `[vendor]` 설치를 적고 있어야 한다.

    ADR-020 은 롤백을 "`llm.provider` 한 줄"로 적었고 F1 이 그 한 줄이 실제로는
    동작하지 않는다는 결함이었다. 의존성을 extra 로 내리면서 **그 한 줄이 다시
    한 줄이 아니게 됐다** — 절차에 적히지 않으면 다음 롤백은 `LLMError` 로
    시작한다.
    """
    adr = (PROJECT_ROOT / "docs" / "adr" / "ADR-020-model-capability-resolves-vendor-parameters.md")
    assert "[vendor]" in adr.read_text(encoding="utf-8")
