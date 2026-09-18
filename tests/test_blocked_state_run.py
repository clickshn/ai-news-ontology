"""이전 완료 판정 — 진입점에 벤더 호출 지점이 **0개**인지 (ADR-018).

## 이 파일의 계약은 한 번 뒤집혔다

session-02 에서 이 파일이 고정한 것은 **"승인 없이 돌리면 진입점이 벤더 호출 전에
멈춘다"** 였다 (ADR-017, MARA session-11 §5.2-5). 그때는 그것이 맞았다 — 세 단계가
전부 벤더를 가리키고 있었고, 멈추는 것이 이식 성공의 증거였다.

**이전이 끝난 지금 그 계약은 참이 아니다.** 멈출 벤더 호출이 없다. 되돌리지 않고
**새 계약으로 갱신한다** (`docs/governance.md` "테스트": 동작을 의도적으로 바꿔
기존 테스트가 깨졌다면 테스트를 새 계약에 맞게 갱신한다).

여기서 고정하는 것은 셋이다.

1. **모든 진입점이 벤더 클라이언트를 만들지 않고 끝까지 돈다.** 승인 파일이 없는
   상태에서 `ExternalVendorCallError` 가 나오면 **어딘가 벤더 지점이 남은 것**이다.
2. **만들어지는 클라이언트가 내부 엔드포인트를 가리킨다.** "벤더를 안 부른다"와
   "내부를 부른다"는 다른 명제이고, 둘 다 확인해야 이전이 끝난 것이다.
3. **되돌리면 게이트가 여전히 잡는다.** `llm.provider` 한 줄로 되돌아가므로
   (ADR-018 Risks) ADR-017 의 보장이 살아 있는지를 같은 파일에서 고정한다.

⚠️ 네트워크는 여전히 **0건**이다. 피드 수집은 픽스처로, vLLM 호출은 전송 계층
스텁으로 대체한다. 소켓 트립와이어는 그대로 둔다 — 이 검사에서 뭔가 나가면 그
자체가 실패다.
"""

from __future__ import annotations

import json
import socket
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import eval.runner as eval_mod
import export.runner as export_mod
import extraction.extractor as extractor_mod
from collectors.base import RawItem
from export.store import ExtractionStore
from extraction import egress
from extraction.egress import ExternalVendorCallError
from extraction.vllm import VLLMClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 테스트 전용 내부 엔드포인트. 실제 `.env` 의 `VLLM_BASE` 를 읽지 않는다 —
#: 읽으면 이 검사가 그 파일의 존재에 의존하게 되고, 값이 로그에 실릴 수 있다.
FAKE_VLLM_BASE = "https://vllm.internal.invalid/v1"

FIXTURE = RawItem(
    url="https://example.invalid/blocked-state-probe",
    title="프로브용 항목 — 네트워크를 타지 않는다",
    body="게이트와 추출 경로를 밟기 위한 합성 본문이다. 실제 피드에서 오지 않았다." * 3,
    source_name="GeekNews",
    published_at=date(2026, 9, 18),
    collected_at=date(2026, 9, 18),
    tags=("ko", "aggregator"),
)


class NetworkAttempt(RuntimeError):
    """아웃바운드 연결 시도. 이 검사에서는 한 건도 없어야 한다."""


# ---------------------------------------------------------------------------
# 전송 계층 스텁 — 스키마 이름을 보고 맞는 응답을 만든다
# ---------------------------------------------------------------------------
_CANNED: dict[str, dict[str, Any]] = {
    "RelevanceGate": {
        "관련있음": True,
        "근거": "AI 엔지니어링과 직접 관련된 합성 항목이라 통과시킨다.",
    },
    "NewsOntology": {
        "요약": "프로브용 온톨로지다. 경로를 밟기 위한 값이며 판정에 쓰지 않는다.",
        "기술영역": ["Agent"],
        "발표유형": "Community/Discussion",
        "관련기업": [],
        "관련기존기술": [],
        "영향도": {"점수": 1, "근거": "합성 입력이라 실제 영향도를 판정할 수 없다."},
    },
    "SummaryJudgement": {
        "faithfulness": {"rationale": "합성 응답이다. 채점 경로만 밟는다.", "score": 3},
        "completeness": {"rationale": "합성 응답이다. 채점 경로만 밟는다.", "score": 3},
        "concision": {"rationale": "합성 응답이다. 채점 경로만 밟는다.", "score": 3},
        "unsupported_claims": [],
    },
}


def fake_post(self: VLLMClient, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """`VLLMClient._post` 대역. **요청한 스키마 이름**으로 응답을 고른다.

    스키마 이름을 보는 이유: 진입점마다 다른 출력 모델을 요구하는데, 하나로
    고정하면 검증이 실패해 "이전이 안 됐다"와 구분되지 않는다.
    """
    name = (payload.get("response_format") or {}).get("json_schema", {}).get("name")
    body = _CANNED.get(name)
    assert body is not None, f"스텁에 없는 출력 모델입니다: {name!r}"
    return {
        "model": payload.get("model"),
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(body, ensure_ascii=False)}}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


@pytest.fixture
def internal_only(monkeypatch):
    """승인 파일 없음 + 피드 스텁 + vLLM 전송 스텁 + 소켓 트립와이어."""
    assert not egress.is_approved(), (
        "승인 파일이 레포에 있습니다. 1회용 승인 파일이 남아 있다는 뜻입니다."
    )

    attempts: list[str] = []

    def trip(address, *_a, **_k):
        attempts.append(repr(address))
        raise NetworkAttempt(f"outbound connection attempted: {address!r}")

    monkeypatch.setenv("VLLM_BASE", FAKE_VLLM_BASE)
    monkeypatch.setattr(socket.socket, "connect", lambda self, address, *a, **k: trip(address))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address, *a, **k: trip(address))
    monkeypatch.setattr(socket, "create_connection", trip)
    monkeypatch.setattr(extractor_mod, "collect", lambda *a, **k: iter([FIXTURE]))
    monkeypatch.setattr(export_mod, "collect_feeds", lambda *a, **k: iter([FIXTURE]))
    monkeypatch.setattr(VLLMClient, "_post", fake_post)
    return attempts


def _predictions(tmp_path: Path) -> Path:
    """골든셋 기대값을 예측 형식으로 옮긴다. eval 경로를 밟기 위한 입력일 뿐이다."""
    rows = []
    for path in sorted((PROJECT_ROOT / "eval" / "golden_set").glob("*.json")):
        if path.name == "TEMPLATE.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") != "confirmed":
            continue
        expected = dict(data["expected"])
        expected["요약"] = "프로브용 요약이다. 채점 경로를 밟기 위한 문장이며 판정에 쓰지 않는다."
        # 골든셋의 관련기업은 문자열 목록이고 NewsOntology 는 CompanyRef 를 받는다.
        expected["관련기업"] = [
            {"원문표기": c} if isinstance(c, str) else c for c in expected.get("관련기업", [])
        ]
        rows.append({"item_id": data["id"], "ontology": expected})
    out = tmp_path / "predictions.jsonl"
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return out


def _entry_points(tmp_path: Path):
    """실제 진입점들. 새 진입점이 생기면 여기 추가한다."""
    return {
        "extraction.extractor --gate-only": lambda: extractor_mod.main(
            ["--index", "0", "--gate-only"]
        ),
        "extraction.extractor (full)": lambda: extractor_mod.main(["--index", "0"]),
        "export.runner run_collect": lambda: export_mod.run_collect(
            eval_mod.load_config(),
            take={"GeekNews": 1},
            urls=[],
            store=ExtractionStore(tmp_path / "store"),
            max_extractions=1,
            skip_stored=False,
        ),
        "eval.runner judge_client_from_config": lambda: eval_mod.judge_client_from_config(
            eval_mod.load_config()
        ),
        "eval.runner --judge": lambda: eval_mod.main(
            [
                "--predictions",
                str(_predictions(tmp_path)),
                "--judge",
                "--scores-dir",
                str(tmp_path / "scores"),
            ]
        ),
    }


@pytest.mark.parametrize("name", list(_entry_points(Path("."))))
def test_entry_point_never_reaches_a_vendor(name, internal_only, tmp_path, capsys):
    """승인 파일 없이 끝까지 돈다. **여기서 멈추면 벤더 지점이 남은 것이다.**"""
    entry = _entry_points(tmp_path)[name]
    try:
        entry()
    except ExternalVendorCallError as exc:  # pragma: no cover - 실패 경로
        pytest.fail(
            f"{name} 이 아직 벤더 클라이언트를 만듭니다: {exc}\n"
            "이전이 끝나지 않았습니다 (ADR-018). 생성 지점을 client_from_config 로 옮기세요."
        )
    capsys.readouterr()
    assert internal_only == [], f"아웃바운드 연결 시도가 있었다: {internal_only}"


def test_every_stage_client_points_at_the_internal_endpoint(internal_only):
    """"벤더를 안 부른다"와 "내부를 부른다"는 다른 명제다. 둘 다 고정한다."""
    from extraction.llm import client_from_config

    config = eval_mod.load_config()
    for stage in ("relevance_gate", "extraction"):
        client = client_from_config(config, stage=stage)
        assert isinstance(client, VLLMClient), f"{stage} 가 vLLM 클라이언트가 아닙니다"
        assert client.base_url == FAKE_VLLM_BASE, f"{stage} 의 목적지: {client.base_url}"
        assert client.stage == stage

    judge = eval_mod.judge_client_from_config(config)
    assert isinstance(judge, VLLMClient)
    assert judge.base_url == FAKE_VLLM_BASE
    assert judge.stage == "eval_judge"


def test_config_does_not_name_a_vendor_model(internal_only):
    """설정이 벤더 모델명을 들고 있으면 되돌아간 것이다.

    모델명으로 목적지를 판정하지 않는다는 규칙(governance 승인 양식)과 어긋나 보이지만,
    여기서 보는 것은 목적지가 아니라 **설정이 어느 상태인지**다.
    """
    config = eval_mod.load_config()
    llm = config.get("llm") or {}
    assert llm.get("provider") == "vllm"
    models = [
        (llm.get(stage) or {}).get("model") for stage in ("relevance_gate", "extraction")
    ] + [(config.get("eval") or {}).get("judge_model")]
    for model in models:
        assert model and not str(model).startswith("claude-"), f"벤더 모델이 남아 있습니다: {model}"


def test_the_gate_still_stops_a_rollback(internal_only, tmp_path):
    """되돌리면 게이트가 여전히 잡는다 — ADR-017 의 보장은 살아 있다.

    `llm.provider` 한 줄로 되돌아가므로(ADR-018 Risks) 이 보장이 사라지면 이전이
    **되돌릴 수 있는 것에서 되돌리면 조용히 위반이 되는 것**으로 바뀐다.
    """
    from extraction.llm import client_from_config

    config = eval_mod.load_config()
    config = json.loads(json.dumps(config))
    config["llm"]["provider"] = "anthropic"

    seen: set[str] = set()
    for stage in ("relevance_gate", "extraction"):
        with pytest.raises(ExternalVendorCallError) as exc:
            client_from_config(config, stage=stage)
        assert stage in str(exc.value)
        seen.add(stage)

    with pytest.raises(ExternalVendorCallError) as exc:
        eval_mod.judge_client_from_config(config)
    assert "eval_judge" in str(exc.value)
    seen.add("eval_judge")

    assert seen == {"relevance_gate", "extraction", "eval_judge"}
