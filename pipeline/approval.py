"""매일 자동 실행의 상시 승인 — 가부 항목을 고정하고 매번 대조한다 (ADR-025).

## config 상한은 승인이 아니다

상한은 **크기**를 재는 항목이다. 얼마나 부를지를 정할 뿐 해도 되는지를 가르지 않는다.
상한을 상시 승인으로 읽으면 MARA Session 0.5a 의 구조 — 2~6번은 맞았는데 1번이 없던
양식 — 를 자동 실행에서 다시 만든다 (ADR-017).

## 사람이 답한 것을 코드가 매번 대조한다

승인의 실체는 "이 목적지로, 이 데이터를, 이 모델·프롬프트로, 이만큼"이다. 그 값들을
`fingerprint()` 로 고정해 `.claude/scheduled-run-approved.json` 에 남기고, 자동 실행은
LLM 을 부르기 전에 현재 값과 대조한다. 같으면 승인받은 그 실행이고, 하나라도 다르면
승인받지 않은 다른 실행이다 → 멈춘다.

**1번(목적지)이 처음으로 긍정형으로 답해진다.** `assert_internal_endpoint` 는 거부
목록이라 "알려진 벤더가 아니다"까지만 말한다. 사설 프록시·오타 호스트·다른 내부
호스트, 작업 스케줄러 환경의 `VLLM_BASE`(OS 환경변수가 `.env` 를 이긴다)는 거기를
통과한다. 해석된 URL 의 해시를 고정해 두면 그것들이 전부 불일치로 드러난다.

## ⚠️ 막지 못하는 것

`GUARD_FILES` **밖**에 `client_from_config` 를 거치지 않는 클라이언트 생성 경로가 생기면
잡지 못한다. 자동 실행에는 훅 계층도 없다. 파일 목록을 늘리는 것으로 이 한계가 없어지지
않는다 (ADR-025 Risks).

기록 파일을 사람이 직접 편집하는 것도 막지 못한다. `approve-schedule` 의 TTY 요구는
에이전트가 비대화형으로 만드는 것을 막을 뿐이다. 막는 것은 governance 규칙이다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collectors.rss import rss_sources
from export.store import PROJECT_ROOT
from extraction.egress import APPROVAL_FILENAME as VENDOR_APPROVAL_FILENAME
from extraction.egress import is_approved as vendor_approval_present
from extraction.extractor import DEFAULT_PROMPT, GATE_PROMPT
from extraction.llm import resolved_vllm_endpoint

APPROVAL_SCHEMA = "scheduled-run-approval/1"

#: 상시 승인 기록. `.gitignore` 대상이다.
APPROVAL_FILENAME = ".claude/scheduled-run-approved.json"

#: 만료 일수. **config 가 아니라 코드 상수다** — config 는 대조 대상 파일이 아니라서,
#: 거기 두면 한 줄로 "결과를 아무도 안 보는 기간"이 조용히 늘어난다. 여기 두면
#: 바꾸는 순간 이 파일의 해시가 바뀌어 재승인이 필요해진다 (ADR-025).
APPROVAL_DAYS = 14

#: 승인·갱신 때 사람이 원문과 대조하는 노트 수.
SAMPLE_SIZE = 3

#: 목적지를 정하는 코드. 이 파일들이 바뀌는 것 자체가 목적지가 바뀔 수 있는 사건이다.
#: 사전 점검 모듈 자신(이 파일과 schedule.py)도 넣는다 — 점검을 고치는 것도 같은 사건이다.
GUARD_FILES: tuple[str, ...] = (
    "extraction/egress.py",
    "extraction/llm.py",
    "extraction/vllm.py",
    "pipeline/runner.py",
    "pipeline/approval.py",
    "pipeline/schedule.py",
)

PROMPT_DIR = PROJECT_ROOT / "extraction" / "prompts"
STAGES = ("relevance_gate", "extraction")


def approval_path(project_root: Path | str | None = None) -> Path:
    return Path(project_root or PROJECT_ROOT) / APPROVAL_FILENAME


# ---------------------------------------------------------------------------
# 지문
# ---------------------------------------------------------------------------
def sha256_text_lf(path: Path) -> str:
    """줄바꿈을 LF 로 정규화한 뒤의 sha256.

    체크아웃 줄바꿈이 바뀌면(작업본 CRLF, session-14 이월) 코드가 같아도 원시 바이트
    해시가 달라진다. 그 불일치는 목적지 변경이 아니므로 세지 않는다.
    """
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def endpoint_sha256(url: str) -> str:
    """엔드포인트 URL 의 해시. URL 자체가 자격증명이라 값은 남기지 않는다 (MARA ADR-010)."""
    return hashlib.sha256(url.strip().rstrip("/").encode("utf-8")).hexdigest()


def scheduled_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    """자동 실행이 도는 소스 = config 상한이 걸린 활성 소스."""
    return [s for s in rss_sources(config) if (s.get("limits") or {})]


def configured_limits(config: dict[str, Any]) -> dict[str, dict[str, int]]:
    gate: dict[str, int] = {}
    extract: dict[str, int] = {}
    for source in scheduled_sources(config):
        section = source.get("limits") or {}
        if "gate" in section:
            gate[source["name"]] = int(section["gate"])
        if "extract" in section:
            extract[source["name"]] = int(section["extract"])
    return {"gate": gate, "extract": extract}


def _configured_model(config: dict[str, Any], stage: str) -> str | None:
    # pipeline.runner.configured_model 과 같은 판정. runner 를 import 하면 순환이 된다.
    llm = config.get("llm") or {}
    section = llm.get(stage) or {}
    provider = (llm.get("provider") or "vllm").strip().lower()
    return section.get("vendor_model" if provider == "anthropic" else "model")


def fingerprint(
    config: dict[str, Any],
    *,
    project_root: Path | str | None = None,
    prompt_dir: Path | None = None,
    endpoint_resolver=resolved_vllm_endpoint,
) -> dict[str, Any]:
    """승인 대상 값을 대조 가능한 형태로. 엔드포인트는 해시만.

    엔드포인트를 해석하지 못하면(`VLLM_BASE` 없음 등) `endpoint_sha256` 이 None 이다 —
    예외로 올리지 않고 불일치로 드러나게 한다.
    """
    root = Path(project_root or PROJECT_ROOT)
    prompts = Path(prompt_dir or PROMPT_DIR)
    provider = ((config.get("llm") or {}).get("provider") or "vllm").strip().lower()
    try:
        endpoint = endpoint_sha256(endpoint_resolver(config)) if provider == "vllm" else None
    except Exception:
        endpoint = None
    return {
        "provider": provider,
        "endpoint_sha256": endpoint,
        "models": {stage: _configured_model(config, stage) for stage in STAGES},
        "prompts": {
            name: sha256_text_lf(prompts / name) if (prompts / name).is_file() else None
            for name in (GATE_PROMPT, DEFAULT_PROMPT)
        },
        "sources": sorted([s.get("name"), s.get("url")] for s in scheduled_sources(config)),
        "limits": configured_limits(config),
        "guard_files": {
            rel: sha256_text_lf(root / rel) if (root / rel).is_file() else None for rel in GUARD_FILES
        },
    }


# ---------------------------------------------------------------------------
# 대조
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Mismatch:
    """승인과 다른 것 하나. `detail` 에 엔드포인트 값을 넣지 않는다."""

    key: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "detail": self.detail}


def compare(approved: dict[str, Any], current: dict[str, Any]) -> list[Mismatch]:
    """승인 지문과 현재 지문의 차이. 상한은 **승인값 이하면 통과**, 나머지는 같아야 한다."""
    out: list[Mismatch] = []
    if current.get("provider") != approved.get("provider"):
        out.append(Mismatch("provider", f"{approved.get('provider')!r} → {current.get('provider')!r}"))
    if current.get("endpoint_sha256") is None:
        out.append(Mismatch("endpoint", "엔드포인트를 해석하지 못했다 (VLLM_BASE 없음 또는 provider 가 vllm 이 아님)"))
    elif current.get("endpoint_sha256") != approved.get("endpoint_sha256"):
        out.append(Mismatch("endpoint", "해석된 엔드포인트 URL 이 승인 때와 다르다 (값은 기록하지 않는다)"))
    for stage in STAGES:
        before = (approved.get("models") or {}).get(stage)
        after = (current.get("models") or {}).get(stage)
        if before != after:
            out.append(Mismatch(f"model.{stage}", f"{before!r} → {after!r}"))
    for name, sha in sorted((current.get("prompts") or {}).items()):
        if sha != (approved.get("prompts") or {}).get(name):
            out.append(Mismatch(f"prompt.{name}", "프롬프트 파일 내용이 승인 때와 다르다"))
    before_sources = {tuple(s) for s in approved.get("sources") or []}
    after_sources = {tuple(s) for s in current.get("sources") or []}
    # 추가와 URL 변경만 불일치다. 빠지는 것은 데이터 범위가 좁아지는 쪽이라 상한을
    # 낮추는 것과 같은 방향이다 — 통과시킨다.
    for name, _url in sorted(after_sources - before_sources):
        out.append(Mismatch("sources", f"승인에 없는 소스(또는 URL 변경): {name}"))
    for stage in ("gate", "extract"):
        approved_limits = ((approved.get("limits") or {}).get(stage)) or {}
        for name, value in sorted(((current.get("limits") or {}).get(stage) or {}).items()):
            ceiling = approved_limits.get(name)
            if ceiling is None or int(value) > int(ceiling):
                out.append(Mismatch(f"limits.{stage}", f"{name}: 승인 {ceiling} < 현재 {value}"))
    for rel, sha in sorted((current.get("guard_files") or {}).items()):
        if sha != (approved.get("guard_files") or {}).get(rel):
            out.append(Mismatch("guard_file", f"목적지 결정 코드가 승인 때와 다르다: {rel}"))
    return out


def load_record(project_root: Path | str | None = None) -> dict[str, Any] | None:
    path = approval_path(project_root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": "unreadable"}


def preflight(
    config: dict[str, Any],
    *,
    project_root: Path | str | None = None,
    now: datetime | None = None,
    prompt_dir: Path | None = None,
    endpoint_resolver=resolved_vllm_endpoint,
) -> list[Mismatch]:
    """LLM 호출 전 가부 판정. 빈 목록이면 통과. **순서가 고정이다** (ADR-025).

    1. 상시 승인 기록 존재 · 형식 · 만료
    2. 벤더 승인 파일 잔존 — 내용과 무관하게 **존재만으로** 불일치
    3. provider == vllm
    4. 지문 대조
    """
    now = now or datetime.now(timezone.utc)
    record = load_record(project_root)
    out: list[Mismatch] = []
    if record is None:
        out.append(Mismatch("approval", f"상시 승인 기록이 없다 ({APPROVAL_FILENAME}). `python -m pipeline approve-schedule`"))
    elif record.get("schema") != APPROVAL_SCHEMA or "fingerprint" not in record:
        out.append(Mismatch("approval", "상시 승인 기록의 형식을 읽지 못했다"))
        record = None
    else:
        try:
            expires = datetime.fromisoformat(record["expires_at"])
        except (KeyError, TypeError, ValueError):
            expires = None
        if expires is None or now >= expires:
            out.append(Mismatch("approval", f"상시 승인이 만료됐다 (만료 {record.get('expires_at')}). 표본 {SAMPLE_SIZE}건 대조 후 갱신"))
    if vendor_approval_present(project_root):
        out.append(Mismatch("vendor_approval", f"`{VENDOR_APPROVAL_FILENAME}` 가 남아 있다. 사람이 없는 실행은 예외 진행 상태를 이어받지 않는다"))
    current = fingerprint(config, project_root=project_root, prompt_dir=prompt_dir, endpoint_resolver=endpoint_resolver)
    if current["provider"] != "vllm":
        out.append(Mismatch("provider", f"llm.provider 가 {current['provider']!r} 다. 자동 실행은 내부 vLLM 에서만 돈다"))
    if record is not None:
        out.extend(m for m in compare(record["fingerprint"], current) if m.key != "provider" or current["provider"] == "vllm")
    return out


def new_record(
    fp: dict[str, Any], *, now: datetime, based_on_run: str, sample: Iterable[str]
) -> dict[str, Any]:
    return {
        "schema": APPROVAL_SCHEMA,
        "approved_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(days=APPROVAL_DAYS)).isoformat(timespec="seconds"),
        "based_on_run": based_on_run,
        "sample_reviewed": list(sample),
        "fingerprint": fp,
    }


def write_record(record: dict[str, Any], project_root: Path | str | None = None) -> Path:
    path = approval_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 선행 조건 — 상시 승인이 첫 전량 실행이 되면 안 된다
# ---------------------------------------------------------------------------
def full_manual_run(runs_dir: Path, limits: dict[str, dict[str, int]]) -> dict[str, Any] | None:
    """현재 config 상한과 **같은 상한**으로 돈 수동 실행(`mode == "config"`) 중 최신.

    없으면 None — 상시 승인을 만들지 않는다. 스케줄 실행(`mode == "scheduled"`)은
    세지 않는다. 사람이 결과를 본 실행이어야 하기 때문이다.
    """
    best: dict[str, Any] | None = None
    for path in sorted(Path(runs_dir).glob("pipeline-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        run_limits = data.get("limits") or {}
        if run_limits.get("mode") != "config":
            continue
        if run_limits.get("gate") != limits["gate"] or run_limits.get("extract") != limits["extract"]:
            continue
        if not data.get("finished_at"):
            continue
        best = data
    return best
