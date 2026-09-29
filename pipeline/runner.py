"""파이프라인 오케스트레이션 — 수집 → 게이트 → 추출 → 보존 → 적재 (ADR-022).

    # 무엇을 부를지만 본다 (API 호출 없음)
    python -m pipeline plan --gate-limit "GeekNews=3" --extract-limit "GeekNews=2"

    # 실제 실행. 상한은 소스별·단계별로 코드가 막는다
    python -m pipeline run --gate-limit "GeekNews=3" --extract-limit "GeekNews=2"

    # 보존소 → Vault 적재만 (API 호출 없음). 적재 실패 복구가 이 경로다
    python -m pipeline load

## 기존 진입점 위에 얹는 층이다

`export.runner` 의 plan / collect / export 는 그대로 있다. 항목 1건의 게이트·추출·
보존은 `export.runner.run_gate` / `run_extraction` 을 **같이 쓴다** — 복사하면
보존 형식이 두 벌이 되고, 한쪽만 고쳐진다 (D-072 와 같은 이유).

## 재시도 지점

각 단계는 다음 단계 전에 산출물을 영속한다 (D-052 의 일반화). 그래서 재시도는
**영속된 산출물이 없는 첫 단계부터** 다시 하면 된다.

| 실패 | 다음 실행 |
|---|---|
| 수집 | 피드에 남아 있으면 다시 잡힌다 |
| 게이트 오류 | 게이트부터 |
| 게이트 스킵 | 다시 판정하지 않는다 (게이트 프롬프트·모델이 바뀌면 다시) |
| 추출 스키마 실패 | 추출부터. `QUARANTINE_AFTER` 회면 격리 |
| 추출 전송 오류 | 추출부터. 횟수를 세지 않는다 |
| 적재 실패 | 보존소에서 적재만. LLM 0건 |

## 부분 실패

소스 하나, 항목 하나의 실패는 기록하고 넘어간다. 엔드포인트가 죽은 경우만
다르다 — 한 LLM 단계에서 전송 오류가 `BREAKER_THRESHOLD` 회 연속이면 그 실행의
그 단계를 멈춘다. 항목마다 타임아웃을 기다리며 상한을 태우지 않기 위해서다.
적재는 LLM 을 부르지 않으므로 차단기와 무관하게 돈다.

⚠️ `ExternalVendorCallError` 는 부분 실패가 아니다. 목적지 검사가 막은 것이고
(ADR-017), 항목 오류로 삼키면 규칙 위반이 "실패 1건"으로 읽힌다. 그대로 올린다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from collectors.base import RawItem
from collectors.rss import collect as collect_feeds
from collectors.rss import load_config, rss_sources
from export.doc_id import doc_id_for
from export.exporter import KST
from export.runner import parse_take, prompt_sha256, run_extraction, run_gate
from export.store import DEFAULT_STORE_DIR, PROJECT_ROOT, ExtractionStore
from extraction.egress import ExternalVendorCallError
from extraction.extractor import DEFAULT_PROMPT, GATE_PROMPT, load_prompt
from extraction.llm import LLMClient, SchemaMismatchError, client_from_config
from extraction.schema import NewsOntology
from observability.events import NullObserver, PipelineObserver, observer_from_config
from obsidian_writer.mapper import NoteContext
from obsidian_writer.writer import resolve_output_dir, vault_index, write_note
from pipeline.ledger import DEFAULT_LEDGER_DIR, Ledger, LedgerEntry, now_iso

DEFAULT_RUNS_DIR = PROJECT_ROOT / "data" / "pipeline" / "runs"

# 실행 전체의 천장. **지정하지 않으면 소스별 상한의 합**이다 — 승인받는 숫자가 곧
# 코드가 막는 숫자여야 한다. session-15 에서 승인 요청에 plan 의 *예상치*(게이트 ≤9)를
# 적었는데, plan 때 파싱에 실패하던 소스가 실행 때 살아나 12건을 불렀다. 소스별 상한은
# 지켜졌지만 전역 천장이 30 이라 아무것도 막지 않았다. 아래 값은 합이 이것을 넘을 때의
# 절대 천장이다.
HARD_MAX_GATE_CALLS = 30
HARD_MAX_EXTRACTIONS = 15

BREAKER_THRESHOLD = 3

STAGES = ("relevance_gate", "extraction")


# ---------------------------------------------------------------------------
# 설정 해석
# ---------------------------------------------------------------------------
def configured_model(config: dict[str, Any], stage: str) -> str | None:
    """이 단계가 **부를** 모델. 응답의 `usage.model` 이 아니라 설정값이다.

    게이트 판정의 유효성을 호출 전에 정해야 하므로 응답에서 읽을 수 없다.
    """
    llm = config.get("llm") or {}
    section = llm.get(stage) or {}
    provider = (llm.get("provider") or "vllm").strip().lower()
    return section.get("vendor_model" if provider == "anthropic" else "model")


def gate_key_for(config: dict[str, Any], gate_prompt_name: str) -> str:
    return f"{gate_prompt_name}|{configured_model(config, 'relevance_gate')}"


@dataclass(frozen=True)
class Limits:
    gate: dict[str, int]
    extract: dict[str, int]
    max_gate_calls: int | None = None
    max_extractions: int | None = None

    def __post_init__(self) -> None:
        # frozen 이라 object.__setattr__ 로 기본값(소스별 합)을 채운다.
        if self.max_gate_calls is None:
            object.__setattr__(self, "max_gate_calls", min(sum(self.gate.values()), HARD_MAX_GATE_CALLS))
        if self.max_extractions is None:
            object.__setattr__(self, "max_extractions", min(sum(self.extract.values()), HARD_MAX_EXTRACTIONS))

    @property
    def sources(self) -> list[str]:
        """상한이 걸린 소스만 돈다 — 상한 없는 소스를 기본값으로 부르지 않는다."""
        return list(dict.fromkeys([*self.gate, *self.extract]))


def validate_sources(config: dict[str, Any], names: Sequence[str]) -> None:
    known = {s.get("name") for s in rss_sources(config)}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"config.yaml 에 없는 소스: {unknown}. 가능한 값: {sorted(known)}")


# ---------------------------------------------------------------------------
# 항목 상태
# ---------------------------------------------------------------------------
STORED_OUTSIDE = "stored_outside_pipeline"
STORED = "stored"
QUARANTINED = "quarantined"
GATE_SKIPPED = "gate_skipped"
NEEDS_EXTRACTION = "needs_extraction"
NEEDS_GATE = "needs_gate"


def classify(entry: LedgerEntry | None, *, stored: bool, gate_key: str) -> str:
    """항목 1건이 어느 단계에서 멈춰 있는가. `plan` 과 `run` 이 같은 판정을 쓴다.

    보존소에 있는데 원장에 없으면 **파이프라인 밖에서 만든 것**이다 (예: session
    0.5a 의 `claude-opus-5` 30건). 재추출도 적재도 하지 않는다 — 사용자 결정이다.
    """
    if stored:
        return STORED if entry is not None else STORED_OUTSIDE
    if entry is None:
        return NEEDS_GATE
    if entry.quarantined:
        return QUARANTINED
    verdict = entry.gate_verdict(gate_key)
    if verdict is None:
        return NEEDS_GATE
    return NEEDS_EXTRACTION if verdict else GATE_SKIPPED


# ---------------------------------------------------------------------------
# 실행 상태
# ---------------------------------------------------------------------------
@dataclass
class Breaker:
    """단계별 연속 전송 오류 차단기."""

    threshold: int = BREAKER_THRESHOLD
    streak: Counter = field(default_factory=Counter)
    tripped: set[str] = field(default_factory=set)

    def is_open(self, stage: str) -> bool:
        return stage in self.tripped

    def ok(self, stage: str) -> None:
        self.streak[stage] = 0

    def fail(self, stage: str) -> None:
        self.streak[stage] += 1
        if self.streak[stage] >= self.threshold and stage not in self.tripped:
            self.tripped.add(stage)
            print(
                f"[breaker] {stage}: 전송 오류 {self.threshold}회 연속 — 이 실행의 "
                f"{stage} 호출을 멈춘다. 적재는 계속한다",
                file=sys.stderr,
            )


class LazyClients:
    """클라이언트는 **처음 부를 때** 만든다. 부를 일이 없는 실행은 만들지 않는다."""

    def __init__(self, config: dict[str, Any], factory: Callable[..., LLMClient]):
        self._config = config
        self._factory = factory
        self._clients: dict[str, LLMClient] = {}

    def __call__(self, stage: str) -> LLMClient:
        if stage not in self._clients:
            self._clients[stage] = self._factory(self._config, stage=stage)
        return self._clients[stage]


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


# ---------------------------------------------------------------------------
# 수집·게이트·추출
# ---------------------------------------------------------------------------
@dataclass
class RunReport:
    run_id: str
    started_at: str
    sources: dict[str, Counter] = field(default_factory=dict)
    load: Counter = field(default_factory=Counter)
    stale: list[str] = field(default_factory=list)
    breaker_tripped: list[str] = field(default_factory=list)
    finished_at: str | None = None

    FAILURE_KEYS = ("source_error", "gate_error", "extract_schema_failed", "extract_transport_error")

    @property
    def failures(self) -> int:
        per_source = sum(t[k] for t in self.sources.values() for k in self.FAILURE_KEYS)
        return per_source + self.load["failed"]

    @property
    def produced(self) -> int:
        return sum(t["extracted"] for t in self.sources.values()) + self.load["written"]

    def exit_code(self) -> int:
        """0 = 실패 없음 · 3 = 부분 실패 · 1 = 실패만 있고 산출 0건."""
        if self.failures == 0:
            return 0
        return 3 if self.produced else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "sources": {name: dict(t) for name, t in self.sources.items()},
            "load": dict(self.load),
            "stale": self.stale,
            "breaker_tripped": self.breaker_tripped,
            "failures": self.failures,
            "produced": self.produced,
            "exit_code": self.exit_code(),
        }


def run_llm_stages(
    config: dict[str, Any],
    *,
    limits: Limits,
    ledger: Ledger,
    store: ExtractionStore,
    report: RunReport,
    observer: PipelineObserver,
    clients: LazyClients,
    extraction_prompt_name: str = DEFAULT_PROMPT,
    gate_prompt_name: str = GATE_PROMPT,
    breaker: Breaker | None = None,
) -> None:
    """상한이 걸린 소스마다 수집 → 게이트 → 추출 → 보존. 적재는 하지 않는다."""
    breaker = breaker or Breaker()
    gate_prompt = load_prompt(gate_prompt_name)
    extraction_prompt = load_prompt(extraction_prompt_name)
    gate_sha = prompt_sha256(gate_prompt_name)
    extraction_sha = prompt_sha256(extraction_prompt_name)
    gate_key = gate_key_for(config, gate_prompt_name)
    current_extraction = (extraction_prompt_name, configured_model(config, "extraction"))

    totals = Counter()
    handled: set[str] = set()

    for source_name in limits.sources:
        tally = report.sources.setdefault(source_name, Counter())
        gate_limit = limits.gate.get(source_name, 0)
        extract_limit = limits.extract.get(source_name, 0)

        # 소스 하나가 죽어도 나머지는 흐른다. collectors 는 자기 실패를 이미
        # 삼키고 로그를 남기지만, 여기까지 올라온 예외도 같은 대접을 한다.
        try:
            items: list[RawItem] = list(collect_feeds(config, source_name=source_name))
        except Exception as exc:
            print(f"[fail] {source_name}: 수집 실패 — {_error_text(exc)}", file=sys.stderr)
            tally["source_error"] += 1
            continue
        tally["collected"] = len(items)
        if not items:
            print(f"[warn] {source_name}: 0건 (피드가 비었거나 수집 실패 — 위 로그 참고)", file=sys.stderr)

        for item in items:
            doc_id = doc_id_for(str(item.url))
            if doc_id in handled:
                tally["duplicate_in_run"] += 1
                continue
            handled.add(doc_id)

            entry = ledger.get(doc_id)
            state = classify(entry, stored=store.path_for(doc_id).exists(), gate_key=gate_key)

            if state == STORED:
                stored = store.load(doc_id).extraction
                if (stored.get("prompt_name"), stored.get("model")) != current_extraction:
                    report.stale.append(doc_id)
            if state in (STORED, STORED_OUTSIDE, QUARANTINED, GATE_SKIPPED):
                tally[state] += 1
                continue

            entry = entry or LedgerEntry(
                doc_id=doc_id, url=str(item.url), title=item.title, source_name=source_name
            )

            if state == NEEDS_GATE:
                if (
                    breaker.is_open("relevance_gate")
                    or tally["gate_calls"] >= gate_limit
                    or totals["gate_calls"] >= limits.max_gate_calls
                ):
                    tally["deferred_gate"] += 1
                    continue
                tally["gate_calls"] += 1
                totals["gate_calls"] += 1
                try:
                    payload = run_gate(
                        item,
                        client=clients("relevance_gate"),
                        prompt=gate_prompt,
                        prompt_sha=gate_sha,
                        observer=observer,
                    )
                except ExternalVendorCallError:
                    raise
                except Exception as exc:
                    if not isinstance(exc, SchemaMismatchError):
                        breaker.fail("relevance_gate")
                    entry.gate_error = _error_text(exc)
                    ledger.put(entry)
                    tally["gate_error"] += 1
                    print(f"[fail] 게이트 {doc_id}: {entry.gate_error}", file=sys.stderr)
                    continue
                breaker.ok("relevance_gate")
                entry.gate, entry.gate_key, entry.gate_error = payload, gate_key, None
                ledger.put(entry)  # 추출 전에 판정을 먼저 남긴다
                if not payload["is_relevant"]:
                    tally["gate_skipped_now"] += 1
                    print(f"[gate] 스킵 {doc_id}: {payload['reason']}", file=sys.stderr)
                    continue
                tally["gate_passed"] += 1

            if (
                breaker.is_open("extraction")
                or tally["extract_calls"] >= extract_limit
                or totals["extract_calls"] >= limits.max_extractions
            ):
                tally["deferred_extraction"] += 1
                continue
            tally["extract_calls"] += 1
            totals["extract_calls"] += 1
            try:
                path = run_extraction(
                    item,
                    doc_id=doc_id,
                    gate_payload=entry.gate,
                    client=clients("extraction"),
                    prompt=extraction_prompt,
                    prompt_sha=extraction_sha,
                    store=store,
                    observer=observer,
                )
            except ExternalVendorCallError:
                raise
            except SchemaMismatchError as exc:
                entry.record_extract_failure(_error_text(exc))
                ledger.put(entry)
                tally["extract_schema_failed"] += 1
                if entry.quarantined:
                    tally["quarantined_now"] += 1
                print(
                    f"[fail] 추출 스키마 {doc_id} ({entry.extract_failures}회"
                    f"{', 격리' if entry.quarantined else ''}): {exc.last_error}",
                    file=sys.stderr,
                )
                continue
            except Exception as exc:
                breaker.fail("extraction")
                entry.extract_error = _error_text(exc)
                ledger.put(entry)
                tally["extract_transport_error"] += 1
                print(f"[fail] 추출 전송 {doc_id}: {entry.extract_error}", file=sys.stderr)
                continue
            breaker.ok("extraction")
            entry.extract_error = None
            ledger.put(entry)
            tally["extracted"] += 1
            print(f"[ok] {doc_id} -> {path.name}", file=sys.stderr)

    report.breaker_tripped = sorted(breaker.tripped)


# ---------------------------------------------------------------------------
# 적재
# ---------------------------------------------------------------------------
def _note_inputs(store: ExtractionStore, doc_id: str) -> tuple[NewsOntology, NoteContext]:
    stored = store.load(doc_id)
    extraction = stored.extraction
    item = RawItem.model_validate(stored.raw_item)
    ontology = NewsOntology.model_validate(extraction["ontology"])
    # 보존소의 추출 시각을 쓴다 — 다시 적재해도 같은 노트(같은 파일명)가 나온다.
    context = NoteContext(
        item=item,
        extraction_model=extraction.get("model"),
        prompt_version=extraction.get("prompt_name"),
        processed_at=datetime.fromisoformat(extraction["extracted_at"]),
    )
    return ontology, context


def load_pending(
    config: dict[str, Any],
    *,
    ledger: Ledger,
    store: ExtractionStore,
    output_dir: Path,
    report: RunReport,
    rewrite_missing: bool = False,
    doc_ids: Sequence[str] = (),
) -> None:
    """원장에 있고 보존소에 추출이 있는 항목을 Vault 에 쓴다. **LLM 을 부르지 않는다.**

    원장에 없는 보존소 항목은 대상이 아니다 (`classify` 의 `STORED_OUTSIDE`).
    """
    output = config.get("output") or {}
    on_conflict = output.get("on_conflict", "skip")
    template = output.get("filename_template", "{date}-{source}-{slug}.md")
    index = vault_index(output_dir, key=doc_id_for)
    wanted = set(doc_ids)

    for entry in ledger:
        if wanted and entry.doc_id not in wanted:
            continue
        if not store.path_for(entry.doc_id).exists():
            continue
        status = (entry.load or {}).get("status")
        existing = index.get(entry.doc_id)

        if existing is not None and on_conflict == "skip":
            if status not in ("written", "exists"):
                entry.load = {"status": "exists", "note": existing.name, "at": now_iso()}
                ledger.put(entry)
            report.load["exists"] += 1
            continue
        if existing is None and status in ("written", "deleted_by_user") and not rewrite_missing:
            # 썼던 노트가 없다 = 사용자가 지웠다. Vault 는 상태 저장소가 아니다 (ADR-022).
            if status == "written":
                entry.load = {**entry.load, "status": "deleted_by_user", "at": now_iso()}
                ledger.put(entry)
            report.load["deleted_by_user"] += 1
            continue

        try:
            ontology, context = _note_inputs(store, entry.doc_id)
            result = write_note(
                ontology,
                context,
                output_dir=output_dir,
                on_conflict=on_conflict,
                filename_template=template,
                existing_path=existing,
            )
        except Exception as exc:
            entry.load = {"status": "failed", "error": _error_text(exc), "at": now_iso()}
            ledger.put(entry)
            report.load["failed"] += 1
            print(f"[fail] 적재 {entry.doc_id}: {entry.load['error']}", file=sys.stderr)
            continue

        if result.written:
            entry.load = {"status": "written", "note": result.path.name, "at": now_iso()}
            report.load["written"] += 1
            print(f"[note] {entry.doc_id} -> {result.path.name}", file=sys.stderr)
        else:
            entry.load = {"status": "exists", "note": result.path.name, "at": now_iso()}
            report.load["exists"] += 1
        ledger.put(entry)


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def new_report() -> RunReport:
    now = datetime.now(KST)
    return RunReport(run_id=now.strftime("pipeline-%Y%m%d-%H%M%S"), started_at=now.isoformat(timespec="seconds"))


def write_report(report: RunReport, runs_dir: Path) -> Path:
    report.finished_at = now_iso()
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{report.run_id}.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run_pipeline(
    config: dict[str, Any],
    *,
    limits: Limits,
    ledger: Ledger,
    store: ExtractionStore,
    output_dir: Path,
    runs_dir: Path,
    observer: PipelineObserver | None = None,
    client_factory: Callable[..., LLMClient] = client_from_config,
    rewrite_missing: bool = False,
    extraction_prompt_name: str = DEFAULT_PROMPT,
    gate_prompt_name: str = GATE_PROMPT,
) -> RunReport:
    validate_sources(config, limits.sources)
    report = new_report()
    try:
        run_llm_stages(
            config,
            limits=limits,
            ledger=ledger,
            store=store,
            report=report,
            observer=observer or NullObserver(),
            clients=LazyClients(config, client_factory),
            extraction_prompt_name=extraction_prompt_name,
            gate_prompt_name=gate_prompt_name,
        )
        # LLM 단계가 어떻게 끝났든 적재는 돈다 — 이미 보존된 것을 날리지 않는다.
        load_pending(
            config,
            ledger=ledger,
            store=store,
            output_dir=output_dir,
            report=report,
            rewrite_missing=rewrite_missing,
        )
    finally:
        write_report(report, runs_dir)
    return report


def plan(
    config: dict[str, Any],
    *,
    limits: Limits,
    ledger: Ledger,
    store: ExtractionStore,
    gate_prompt_name: str = GATE_PROMPT,
) -> dict[str, dict[str, Any]]:
    """소스별 상태 도수와 **최대** 호출 건수. API 는 부르지 않는다 (피드 HTTP 만)."""
    validate_sources(config, limits.sources)
    gate_key = gate_key_for(config, gate_prompt_name)
    out: dict[str, dict[str, Any]] = {}
    for source_name in limits.sources:
        states = Counter()
        for item in collect_feeds(config, source_name=source_name):
            doc_id = doc_id_for(str(item.url))
            states[classify(ledger.get(doc_id), stored=store.path_for(doc_id).exists(), gate_key=gate_key)] += 1
        gate_max = min(states[NEEDS_GATE], limits.gate.get(source_name, 0))
        extract_max = min(states[NEEDS_EXTRACTION] + gate_max, limits.extract.get(source_name, 0))
        out[source_name] = {"states": dict(states), "gate_calls_max": gate_max, "extract_calls_max": extract_max}
    return out


def _print_report(report: RunReport) -> None:
    for name, tally in report.sources.items():
        print(f"{name}: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    print("load: " + (", ".join(f"{k}={v}" for k, v in sorted(report.load.items())) or "(없음)"))
    if report.stale:
        print(f"stale(현재 프롬프트·모델과 다른 보존 결과, 재추출 안 함): {len(report.stale)}건")
    if report.breaker_tripped:
        print(f"breaker: {report.breaker_tripped}")
    print(f"failures={report.failures} produced={report.produced} exit={report.exit_code()}")


def _add_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--gate-limit", action="append", default=[], help="소스명=게이트 호출 상한")
    parser.add_argument("--extract-limit", action="append", default=[], help="소스명=추출 호출 상한")
    parser.add_argument("--max-gate-calls", type=int, default=None, help="기본: 소스별 상한의 합")
    parser.add_argument("--max-extractions", type=int, default=None, help="기본: 소스별 상한의 합")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    parser.add_argument("--store-dir", default=str(DEFAULT_STORE_DIR))
    parser.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))


def _add_vault(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault-dir", default=None, help="지정하지 않으면 .env 의 OBSIDIAN_VAULT_PATH")
    parser.add_argument("--rewrite-missing", action="store_true", help="사용자가 지운 노트도 다시 쓴다")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="수집 → 게이트 → 추출 → 보존 → 적재 (ADR-022)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="상태와 최대 호출 건수만 본다 (API 호출 없음)")
    _add_common(p_plan)
    _add_limits(p_plan)

    p_run = sub.add_parser("run", help="전 단계 실행 (API 호출)")
    _add_common(p_run)
    _add_limits(p_run)
    _add_vault(p_run)
    p_run.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))

    p_load = sub.add_parser("load", help="보존소 → Vault 적재만 (API 호출 없음)")
    _add_common(p_load)
    _add_vault(p_load)
    p_load.add_argument("--doc-id", nargs="*", default=[])
    p_load.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))

    p_release = sub.add_parser("release", help="격리를 푼다 (다음 run 에서 추출을 다시 시도)")
    p_release.add_argument("doc_id", nargs="+")
    p_release.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))

    args = parser.parse_args(argv)
    ledger = Ledger(args.ledger_dir)

    if args.command == "release":
        for doc_id in args.doc_id:
            entry = ledger.get(doc_id)
            if entry is None:
                print(f"원장에 없음: {doc_id}", file=sys.stderr)
                continue
            entry.quarantined, entry.extract_failures = False, 0
            ledger.put(entry)
            print(f"격리 해제: {doc_id}")
        return 0

    config = load_config(args.config) if args.config else load_config()
    store = ExtractionStore(args.store_dir)

    if args.command == "plan":
        limits = Limits(parse_take(args.gate_limit), parse_take(args.extract_limit), args.max_gate_calls, args.max_extractions)
        result = plan(config, limits=limits, ledger=ledger, store=store)
        for name, info in result.items():
            print(f"{name}: {info['states']}  게이트 ≤{info['gate_calls_max']}  추출 ≤{info['extract_calls_max']}")
        # 예상치는 **지금 피드 상태**에 달려 있고 실행 때 달라진다. 승인받을 숫자는 상한이다.
        print(
            f"현재 피드 기준 예상: 게이트 ≤{sum(i['gate_calls_max'] for i in result.values())}"
            f"  추출 ≤{sum(i['extract_calls_max'] for i in result.values())}"
        )
        print(
            f"승인 대상 상한(코드가 막는 값): 게이트 {limits.max_gate_calls}  추출 {limits.max_extractions}"
            "  (논리 호출. 스키마 재시도로 각각 최대 2배)"
        )
        return 0

    output_dir = Path(args.vault_dir) if args.vault_dir else resolve_output_dir(config=config)

    if args.command == "load":
        report = new_report()
        load_pending(
            config, ledger=ledger, store=store, output_dir=output_dir, report=report,
            rewrite_missing=args.rewrite_missing, doc_ids=args.doc_id,
        )
        write_report(report, Path(args.runs_dir))
        _print_report(report)
        return report.exit_code()

    limits = Limits(parse_take(args.gate_limit), parse_take(args.extract_limit), args.max_gate_calls, args.max_extractions)
    if not limits.sources:
        print("--gate-limit / --extract-limit 로 소스를 하나 이상 지정하세요.", file=sys.stderr)
        return 2
    report = run_pipeline(
        config,
        limits=limits,
        ledger=ledger,
        store=store,
        output_dir=output_dir,
        runs_dir=Path(args.runs_dir),
        observer=observer_from_config(config),
        rewrite_missing=args.rewrite_missing,
    )
    _print_report(report)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
