"""차단 상태 1회 실행 — 진입점이 벤더 호출 전에 멈추는지 (ADR-017).

MARA session-11 §5.2-5 가 이식 직후의 **검사 항목**으로 요구한 것이다. 게이트를
만들어 두는 것과 그것이 실제 진입점에서 도는 것은 다르다 — 승인 파일이 없는 상태로
파이프라인을 돌렸을 때 **통과해서 끝까지 돌면 이식이 실패한 것이다.**

여기서 고정하는 것은 두 가지다.

1. **모든 진입점이 벤더 클라이언트를 만들기 전에 멈춘다.** 새 진입점이 생겼는데
   게이트를 안 거치면 이 파일이 깨진다.
2. **그 과정에서 아웃바운드 연결이 0건이다.** 차단이 목적인 실행에서 뭔가 나가면
   그것 자체가 이식 실패의 증거다.

⚠️ 피드 수집은 픽스처로 대체한다. 수집기는 LLM 을 부르지 않으므로 벤더 호출 지점을
가리지 않고, 공개 RSS 라도 네트워크는 네트워크다.
"""

from __future__ import annotations

import json
import socket
from datetime import date
from pathlib import Path

import pytest

import eval.runner as eval_mod
import export.runner as export_mod
import extraction.extractor as extractor_mod
from collectors.base import RawItem
from export.store import ExtractionStore
from extraction import egress
from extraction.egress import ExternalVendorCallError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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


@pytest.fixture
def blocked_state(monkeypatch):
    """승인 파일 없음 + 피드 스텁 + 소켓 트립와이어."""
    assert not egress.is_approved(), (
        "승인 파일이 레포에 있습니다. 차단 상태가 아니므로 이 검사는 무의미합니다."
    )

    attempts: list[str] = []

    def trip(address, *_a, **_k):
        attempts.append(repr(address))
        raise NetworkAttempt(f"outbound connection attempted: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", lambda self, address, *a, **k: trip(address))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address, *a, **k: trip(address))
    monkeypatch.setattr(socket, "create_connection", trip)
    monkeypatch.setattr(extractor_mod, "collect", lambda *a, **k: iter([FIXTURE]))
    monkeypatch.setattr(export_mod, "collect_feeds", lambda *a, **k: iter([FIXTURE]))
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
def test_entry_point_stops_before_calling_a_vendor(name, blocked_state, tmp_path, capsys):
    """승인 없이 돌리면 벤더 클라이언트 생성 지점에서 멈춘다.

    **통과해서 끝까지 돌면 이식이 실패한 것이다.**
    """
    entry = _entry_points(tmp_path)[name]
    with pytest.raises(ExternalVendorCallError):
        entry()
    capsys.readouterr()
    assert blocked_state == [], f"아웃바운드 연결 시도가 있었다: {blocked_state}"


def test_gate_error_names_every_stage(blocked_state, tmp_path, capsys):
    """멈춘 지점이 단계 이름을 말해야 이전 범위를 목록으로 정할 수 있다."""
    seen: set[str] = set()
    for entry in _entry_points(tmp_path).values():
        try:
            entry()
        except ExternalVendorCallError as exc:
            message = str(exc)
            for stage in ("relevance_gate", "extraction", "eval_judge"):
                if stage in message:
                    seen.add(stage)
    capsys.readouterr()
    # 진입점은 **첫** 지점에서 멈추므로 한 번에 전부 드러나지는 않는다.
    # 추출 단계는 게이트 뒤에 있어 여기서는 보이지 않는다 — 그것이 정상이고,
    # 전체 목록은 docs/handoff 의 실측 표에 있다.
    assert {"relevance_gate", "eval_judge"} <= seen, seen
