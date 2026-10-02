"""MARA 코퍼스 37건 메타 backfill — 채우기 전용 보존소에서 추출만 하고 37건만 내보낸다 (ADR-026).

    # 대상 목록 대조 (API 호출 없음). --write 면 목록 파일을 다시 쓴다
    python -m export.backfill targets --mara-root ../multiagent-research-lab

    # 입력 구성 확인 (API 호출 없음)
    python -m export.backfill plan --mara-root ../multiagent-research-lab --doc-id ...

    # 추출 (API 호출). 상한은 승인받은 호출 수를 그대로 적는다
    python -m export.backfill run --mara-root ../multiagent-research-lab --doc-id ... --max-calls 6
    python -m export.backfill run --mara-root ../multiagent-research-lab --rest --max-calls 68

    # 37건 선택 export + 계약 §12.2 자체검사 (API 호출 없음)
    python -m export.backfill export --mara-root ../multiagent-research-lab

## 운영 경로와 갈라 두는 것

- **보존소:** `data/backfill/mara-37/extractions/`. 운영 보존소(`DEFAULT_STORE_DIR`)
  나 그 아래를 주면 시작하지 않는다 — 거기서 export 하면 파이프라인분이 함께 나간다.
- **관측:** 남기지 않는다. 미등록 기업 큐(D-035)와 게이트 스킵 기록(D-016)은 운영
  실행의 큐다. MARA 표본이 섞이면 별칭 추가 기준("기사 3건", D-114)이 흔들린다.
  `record_unknown_companies` 를 부르지 않고, 게이트를 부르지 않으니 스킵도 없다.
- **게이트:** 부르지 않는다. 문서 구성 37건은 고정이고 메타만 다시 만든다. 원래 게이트
  이력이 있는 23건은 그 이력(`gate_model` · `gate_prompt_version`)을 MARA jsonl 에서
  옮기고, 14건은 `gate: null`. **이번 실행이 게이트를 생략했다는 사실은 export
  레코드가 아니라 실행 매니페스트(`<보존소>/_runs/`)에 남긴다** — 계약 1.0 유지.
- **시각:** 07:10 ~ 09:10(KST) 에는 시작하지 않는다. 08:10 자동 실행과 같은 vLLM
  엔드포인트를 쓰기 때문이다.

MARA 레포는 **읽기만 한다.**
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from collectors.base import RawItem
from collectors.rss import load_config
from export import conformance
from export.contract import CONTRACT_VERSION
from export.doc_id import doc_id_for
from export.exporter import KST, ExportResult, build_records, write_export
from export.mara_seed import collect_seed_docs, load_snapshot
from export.runner import _store_payload, prompt_sha256
from export.store import DEFAULT_STORE_DIR, PROJECT_ROOT, ExtractionStore
from extraction.extractor import DEFAULT_PROMPT, extract_ontology, load_prompt
from extraction.llm import LLMClient, SchemaMismatchError, client_from_config

#: ADR-026 의 범위. 다르면 MARA 코퍼스가 바뀐 것이고, 그건 이 실행의 전제가 깨진 것이다.
EXPECTED_TARGETS = 37

RUN_KIND = "backfill"
TARGETS_PATH = Path(__file__).resolve().parent / "mara_backfill_targets.json"
DEFAULT_BACKFILL_DIR = PROJECT_ROOT / "data" / "backfill" / "mara-37"
DEFAULT_BACKFILL_STORE = DEFAULT_BACKFILL_DIR / "extractions"
DEFAULT_BACKFILL_OUT = DEFAULT_BACKFILL_DIR / "corpus"
RUNS_SUBDIR = "_runs"

#: 항목당 스키마 시도 최대치 (`extraction/vllm.py: parse_into` 의 2회).
#: 호출 상한 = 이 값 × 건수. 전송 재전송(`max_retries`)은 같은 요청의 재전송이라 따로 센다.
ATTEMPTS_PER_ITEM = 2

#: 08:10 자동 실행 전후 1시간 (handoff session-19 §8).
QUIET_START = time(7, 10)
QUIET_END = time(9, 10)

#: 옮기는 게이트 이력에 **없는** 키. 레코드 스키마에 없고 MARA jsonl 에도 없다.
GATE_KEYS_NOT_CARRIED = ("is_relevant", "reason", "prompt_sha256", "attempts", "usage")


class BackfillError(RuntimeError):
    """backfill 의 전제가 깨졌다. LLM 을 부르기 전에 멈춘다."""


# ---------------------------------------------------------------------------
# MARA 읽기 (읽기 전용)
# ---------------------------------------------------------------------------
def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mara_export_records(mara_root: Path | str) -> dict[str, tuple[dict[str, Any], Path]]:
    """MARA `data/corpus/*/*.jsonl` 의 레코드를 doc_id -> (레코드, 파일) 로. **쓰지 않는다.**"""
    records: dict[str, tuple[dict[str, Any], Path]] = {}
    pattern = str(Path(mara_root) / "data" / "corpus" / "*" / "*.jsonl")
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record["doc_id"] in records:
                    raise BackfillError(f"MARA export 사이 doc_id 중복: {record['doc_id']}")
                records[record["doc_id"]] = (record, Path(path))
    return records


def derive_targets(mara_root: Path | str) -> dict[str, Any]:
    """MARA 색인 코퍼스 = 스냅샷 doc_id ∪ export 의 `indexable=true` doc_id.

    37 이 아니면 예외. 결과에는 원천 파일의 sha256 을 함께 싣는다 — 목록 파일과
    대조할 때 "MARA 쪽 파일이 바뀌었나"를 doc_id 집합보다 먼저 드러내려고.
    """
    root = Path(mara_root)
    snapshot = load_snapshot(root)
    exported = mara_export_records(root)
    indexable = {d for d, (r, _) in exported.items() if r.get("indexable")}
    targets = sorted(set(snapshot) | indexable)
    if len(targets) != EXPECTED_TARGETS:
        raise BackfillError(
            f"MARA 색인 코퍼스가 {len(targets)}건입니다 (기대 {EXPECTED_TARGETS}). "
            f"스냅샷 {len(snapshot)} · export 색인 가능 {len(indexable)}"
        )
    files = sorted(
        {p for _, p in exported.values()}
        | {Path(p) for p in glob.glob(str(root / "data" / "corpus" / "*" / "*.json"))}
    )
    return {
        "range": "ADR-026",
        "counts": {
            "total": len(targets),
            "snapshot": len(snapshot),
            "export_indexable": len(indexable),
            "both": len(set(snapshot) & indexable),
            "gate_history": len(indexable),
            "gate_null": len(set(snapshot) - indexable),
        },
        "source_files": {
            p.relative_to(root).as_posix(): _sha256_file(p) for p in files
        },
        "doc_ids": [
            {
                "doc_id": d,
                "origin": (
                    "snapshot+export" if d in snapshot and d in indexable
                    else "snapshot" if d in snapshot
                    else "export"
                ),
            }
            for d in targets
        ],
    }


def load_targets(path: Path = TARGETS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def target_ids(targets: dict[str, Any]) -> list[str]:
    return [entry["doc_id"] for entry in targets["doc_ids"]]


def check_targets(mara_root: Path | str, path: Path = TARGETS_PATH) -> dict[str, Any]:
    """커밋된 목록과 지금 MARA 에서 유도한 목록이 같은지. 다르면 예외."""
    committed = load_targets(path)
    derived = derive_targets(mara_root)
    if derived != committed:
        diff = sorted(set(target_ids(derived)) ^ set(target_ids(committed)))
        changed = sorted(
            k for k in set(derived["source_files"]) | set(committed["source_files"])
            if derived["source_files"].get(k) != committed["source_files"].get(k)
        )
        raise BackfillError(
            f"대상 목록이 MARA 와 다릅니다 — doc_id 차이 {diff}, 바뀐 원천 파일 {changed}. "
            "MARA 코퍼스가 바뀌었으면 ADR-026 의 범위부터 다시 본다"
        )
    return committed


# ---------------------------------------------------------------------------
# 입력 구성
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BackfillInput:
    """추출할 1건 + 옮길 게이트 이력."""

    doc_id: str
    item: RawItem
    origin: str  # "snapshot" | "export" | "snapshot+export"
    gate: dict[str, Any] | None


def _item_from_record(record: dict[str, Any]) -> RawItem:
    """MARA jsonl 레코드 -> `RawItem`. source_name · 제목 · 날짜 · 언어는 기존 값 그대로.

    `tags` 는 언어 선언(`lang`)만 되살린다 — 레코드의 `lang` 은 태그의 `ko` 여부로만
    정해지고(`export/record.py: declared_lang`), 추출 프롬프트는 태그를 쓰지 않는다.
    """
    return RawItem(
        url=record["url"],
        title=record["title"],
        body=record["text"],
        source_name=record["source_name"],
        published_at=date.fromisoformat(record["published_at"]) if record.get("published_at") else None,
        collected_at=date.fromisoformat(record["collected_at"]),
        tags=("ko",) if record.get("lang") == "ko" else (),
    )


def _carried_gate(record: dict[str, Any], path: Path, mara_root: Path) -> dict[str, Any]:
    prov = record["provenance"]
    if not prov.get("gate_model"):
        raise BackfillError(f"{record['doc_id']}: MARA 레코드에 게이트 이력이 없습니다")
    return {
        "model": prov["gate_model"],
        "prompt_name": prov["gate_prompt_version"],
        "carried_from": path.relative_to(mara_root).as_posix(),
        "not_carried": list(GATE_KEYS_NOT_CARRIED),
    }


def build_inputs(
    mara_root: Path | str,
    doc_ids: Sequence[str],
    *,
    collected_at: date | None = None,
) -> list[BackfillInput]:
    """대상 doc_id 의 입력을 MARA 에서 만든다. **운영 보존소는 읽지 않는다.**

    - export 에 색인 가능 레코드가 있으면(23건) 그 레코드에서. 게이트 이력을 옮긴다.
      스냅샷에도 있는 2건은 text 가 스냅샷과 같은지 확인한다.
    - 스냅샷에만 있으면(14건) 스냅샷에서(`export/mara_seed.py` 와 같은 모양). `gate: None`.
    """
    root = Path(mara_root)
    snapshot = load_snapshot(root)
    exported = mara_export_records(root)
    inputs: list[BackfillInput] = []
    for doc_id in doc_ids:
        hit = exported.get(doc_id)
        if hit and hit[0].get("indexable"):
            record, path = hit
            if doc_id in snapshot and record["text"] != snapshot[doc_id]["text"]:
                raise BackfillError(f"{doc_id}: export text 가 스냅샷 text 와 다릅니다")
            inputs.append(
                BackfillInput(
                    doc_id=doc_id,
                    item=_item_from_record(record),
                    origin="snapshot+export" if doc_id in snapshot else "export",
                    gate=_carried_gate(record, path, root),
                )
            )
        elif doc_id in snapshot:
            (item,) = collect_seed_docs(root, [doc_id], collected_at=collected_at)
            inputs.append(BackfillInput(doc_id=doc_id, item=item, origin="snapshot", gate=None))
        else:
            raise BackfillError(f"{doc_id}: MARA 스냅샷에도 색인 가능 export 에도 없습니다")
    for entry in inputs:
        actual = doc_id_for(str(entry.item.url))
        if actual != entry.doc_id:
            raise BackfillError(f"{entry.doc_id}: 입력 URL 이 다른 doc_id({actual})를 만듭니다")
    return inputs


# ---------------------------------------------------------------------------
# 가드
# ---------------------------------------------------------------------------
def assert_backfill_store(store_dir: Path | str) -> Path:
    """운영 보존소나 그 아래면 멈춘다."""
    resolved = Path(store_dir).resolve()
    operational = DEFAULT_STORE_DIR.resolve()
    if resolved == operational or resolved.is_relative_to(operational):
        raise BackfillError(f"운영 보존소에는 쓰지 않습니다: {resolved}")
    return resolved


def assert_outside_quiet_window(now: datetime) -> None:
    local = now.astimezone(KST).time()
    if QUIET_START <= local < QUIET_END:
        raise BackfillError(
            f"{local:%H:%M} 은 자동 실행 전후 1시간(07:10 ~ 09:10)입니다. 이 시간에는 시작하지 않습니다"
        )


def endpoint_sha256(url: str) -> str:
    """`pipeline/approval.py: endpoint_sha256` 과 같은 식. URL 은 남기지 않는다."""
    return hashlib.sha256(url.strip().rstrip("/").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def runs_dir(store: ExtractionStore) -> Path:
    """실행 매니페스트 자리. 보존소의 `*.json` 목록(`ExtractionStore.__iter__`)에 걸리지 않게 한 단계 아래."""
    return store.directory / RUNS_SUBDIR


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)


def run_backfill(
    inputs: Sequence[BackfillInput],
    *,
    targets: dict[str, Any],
    store: ExtractionStore,
    client: LLMClient,
    max_calls: int,
    prompt_name: str = DEFAULT_PROMPT,
    endpoint_hash: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(KST),
) -> dict[str, Any]:
    """게이트 없이 추출만 하고 받은 즉시 보존한다. 실행 매니페스트를 돌려준다.

    호출 상한은 시작 전에 최악값(`ATTEMPTS_PER_ITEM` × 건수)으로 대조한다. 넘으면
    한 건도 부르지 않는다 — 중간에 끊기면 "몇 건이 됐는지"가 상한 해석에 섞인다.
    """
    assert_backfill_store(store.directory)
    started = now()
    assert_outside_quiet_window(started)

    wanted = set(target_ids(targets))
    outside = [entry.doc_id for entry in inputs if entry.doc_id not in wanted]
    if outside:
        raise BackfillError(f"대상 목록 밖의 doc_id: {outside}")
    stored = set(store.doc_ids())
    already = [entry.doc_id for entry in inputs if entry.doc_id in stored]
    if already:
        raise BackfillError(f"이미 보존된 doc_id 를 다시 부르지 않습니다: {already}")
    worst = ATTEMPTS_PER_ITEM * len(inputs)
    if worst > max_calls:
        raise BackfillError(
            f"{len(inputs)}건의 최대 호출 {worst} 이 상한 {max_calls} 을 넘습니다. 승인 규모를 다시 확인한다"
        )

    prompt = load_prompt(prompt_name)
    prompt_sha = prompt_sha256(prompt_name)
    run_id = started.strftime("backfill-%Y%m%d-%H%M%S")
    manifest_path = runs_dir(store) / f"{run_id}.json"
    manifest: dict[str, Any] = {
        "run_kind": RUN_KIND,
        "run_id": run_id,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": None,
        "status": "running",
        "basis": "ADR-026",
        "gate": {
            "called": False,
            "reason": (
                "ADR-026: 문서 구성 37건은 고정이고 메타만 다시 추출한다. "
                "원래 게이트 이력이 있는 레코드는 그 이력을 옮기고(MARA jsonl provenance), "
                "없는 레코드는 null 로 둔다. 이 생략은 export 레코드에 싣지 않는다 (계약 1.0 유지)"
            ),
            "history_carried": sorted(e.doc_id for e in inputs if e.gate),
            "history_null": sorted(e.doc_id for e in inputs if not e.gate),
        },
        "contract_version": CONTRACT_VERSION,
        "extraction": {
            "model": getattr(client, "model", None),
            "prompt_version": prompt_name,
            "prompt_sha256": prompt_sha,
            "prompt_sha256_definition": "LF 정규화 (ADR-027)",
            "endpoint_sha256": endpoint_hash,
        },
        "observability": "기록 안 함 — 미등록 기업 큐(D-035)·게이트 스킵(D-016)은 운영 큐다",
        "limits": {
            "max_calls": max_calls,
            "attempts_per_item": ATTEMPTS_PER_ITEM,
            "transport_retries": "시도마다 별도 (max_retries), 상한에 넣지 않는다",
        },
        "targets": {
            "file": TARGETS_PATH.name,
            "counts": targets["counts"],
            "doc_ids": target_ids(targets),
        },
        "this_run": [entry.doc_id for entry in inputs],
        "results": [],
        "calls_used": 0,
    }
    _write_json(manifest_path, manifest)

    try:
        for entry in inputs:
            result_row: dict[str, Any] = {"doc_id": entry.doc_id, "origin": entry.origin}
            try:
                result = extract_ontology(entry.item, client, prompt=prompt)
            except SchemaMismatchError as exc:
                manifest["calls_used"] += exc.attempts
                result_row.update(status="schema_fail", attempts=exc.attempts, error=exc.last_error)
                manifest["results"].append(result_row)
                _write_json(manifest_path, manifest)
                print(f"[fail] 스키마 검증 실패 {entry.doc_id}: {exc.last_error}", file=sys.stderr)
                continue
            except Exception as exc:
                # 전송 오류 등. 몇 번 나갔는지 모르므로 최악값으로 센다.
                manifest["calls_used"] += ATTEMPTS_PER_ITEM
                result_row.update(status="error", error=f"{type(exc).__name__}: {exc}")
                manifest["results"].append(result_row)
                raise

            extraction_payload = {
                "prompt_name": result.prompt_name,
                "prompt_sha256": prompt_sha,
                "model": result.usage.model,
                "attempts": result.attempts,
                "extracted_at": now().isoformat(timespec="seconds"),
                "ontology": json.loads(result.ontology.model_dump_json()),
                "usage": vars(result.usage),
            }
            payload = _store_payload(
                entry.item, doc_id=entry.doc_id, gate=entry.gate, extraction=extraction_payload
            )
            payload["run_kind"] = RUN_KIND
            payload["backfill_run_id"] = run_id
            payload["backfill_origin"] = entry.origin
            path = store.save(payload)

            manifest["calls_used"] += result.attempts
            result_row.update(
                status="ok",
                attempts=result.attempts,
                model=result.usage.model,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )
            manifest["results"].append(result_row)
            _write_json(manifest_path, manifest)
            print(f"[ok] {entry.doc_id} -> {path.name} (시도 {result.attempts})", file=sys.stderr)
        manifest["status"] = "done"
    except BaseException:
        manifest["status"] = "aborted"
        raise
    finally:
        manifest["finished_at"] = now().isoformat(timespec="seconds")
        _write_json(manifest_path, manifest)
    return manifest


# ---------------------------------------------------------------------------
# 선택 export
# ---------------------------------------------------------------------------
def validate_backfill_store(store: ExtractionStore, targets: dict[str, Any]) -> None:
    """보존소가 정확히 대상 37건이고, 전부 backfill · 한 모델 · 지금의 v4 LF 해시인지."""
    wanted = set(target_ids(targets))
    entries = list(store)
    ids = [e.doc_id for e in entries]
    problems: list[str] = []
    if len(ids) != len(wanted) or set(ids) != wanted:
        problems.append(
            f"보존소 {len(ids)}건 · 빠진 것 {sorted(wanted - set(ids))} · 목록 밖 {sorted(set(ids) - wanted)}"
        )
    not_backfill = [e.doc_id for e in entries if e.payload.get("run_kind") != RUN_KIND]
    if not_backfill:
        problems.append(f"backfill 표시 없음 {not_backfill}")
    models = {e.extraction.get("model") for e in entries}
    if len(models) > 1:
        problems.append(f"추출 모델이 하나가 아닙니다 {sorted(map(str, models))}")
    prompts = {(e.extraction.get("prompt_name"), e.extraction.get("prompt_sha256")) for e in entries}
    for name, sha in prompts:
        if sha != prompt_sha256(name):
            problems.append(f"{name} 해시 {str(sha)[:12]} 가 LF 정규화 실측과 다릅니다")
    if problems:
        raise BackfillError("; ".join(problems))


def export_backfill(
    store: ExtractionStore,
    targets: dict[str, Any],
    *,
    out_dir: Path | str,
    config_version: object,
    export_id: str | None = None,
) -> ExportResult:
    """대상 37건만 레코드로 만들어 쓴다. 건수·집합이 어긋나면 쓰지 않는다."""
    assert_backfill_store(store.directory)
    validate_backfill_store(store, targets)
    wanted = set(target_ids(targets))
    records = [r for r in build_records(store, config_version=config_version) if r["doc_id"] in wanted]
    if len(records) != EXPECTED_TARGETS or {r["doc_id"] for r in records} != wanted:
        raise BackfillError(f"레코드 {len(records)}건 — {EXPECTED_TARGETS}건이 아니면 내보내지 않는다")
    if any(r["contract_version"] != CONTRACT_VERSION for r in records):
        raise BackfillError("contract_version 이 계약 1.0 이 아닌 레코드가 있다")
    return write_export(records, out_dir=out_dir, export_id=export_id, config_version=config_version)


def self_check(
    result: ExportResult,
    *,
    store: ExtractionStore,
    mara_root: Path | str,
    out_dir: Path | str,
    config_version: object,
) -> list[conformance.Check]:
    """계약 §12.2 자체검사. 스냅샷 arXiv 16건이 병합 경로를 타야 한다(새 doc_id 0)."""
    bundle = conformance.load_bundle(out_dir, result.export_id)
    snapshot_ids = sorted(load_snapshot(mara_root))
    return conformance.run_checks(
        bundle,
        store=store,
        mara_root=mara_root,
        injected=snapshot_ids,
        expected_total=EXPECTED_TARGETS,
        config_version=config_version,
        seeded=snapshot_ids,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _select(targets: dict[str, Any], store: ExtractionStore, doc_ids: Sequence[str], rest: bool) -> list[str]:
    if bool(doc_ids) == rest:
        raise BackfillError("--doc-id 와 --rest 중 하나만 준다")
    if rest:
        stored = set(store.doc_ids())
        return [d for d in target_ids(targets) if d not in stored]
    return list(doc_ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MARA 코퍼스 37건 메타 backfill (ADR-026)")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mara-root", required=True, help="multiagent-research-lab 경로 (읽기 전용)")
        p.add_argument("--store-dir", default=str(DEFAULT_BACKFILL_STORE))
        p.add_argument("--config", default=None)

    p_targets = sub.add_parser("targets", help="대상 목록 대조 (API 호출 없음)")
    common(p_targets)
    p_targets.add_argument("--write", action="store_true", help="목록 파일을 MARA 기준으로 다시 쓴다")

    for name, help_text in (("plan", "입력 구성 확인 (API 호출 없음)"), ("run", "추출 (API 호출)")):
        p = sub.add_parser(name, help=help_text)
        common(p)
        p.add_argument("--doc-id", nargs="*", default=[])
        p.add_argument("--rest", action="store_true", help="아직 보존되지 않은 대상 전부")
        if name == "run":
            p.add_argument("--max-calls", type=int, required=True, help="승인받은 최대 호출 수")

    p_export = sub.add_parser("export", help="37건 선택 export + 자체검사 (API 호출 없음)")
    common(p_export)
    p_export.add_argument("--out-dir", default=str(DEFAULT_BACKFILL_OUT))
    p_export.add_argument("--export-id", default=None)

    args = parser.parse_args(argv)
    config = load_config(args.config) if args.config else load_config()
    store = ExtractionStore(assert_backfill_store(args.store_dir))

    if args.command == "targets":
        if args.write:
            derived = derive_targets(args.mara_root)
            TARGETS_PATH.write_text(
                json.dumps(derived, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            print(f"썼다: {TARGETS_PATH} ({derived['counts']})")
            return 0
        committed = check_targets(args.mara_root)
        print(f"일치: {committed['counts']}")
        return 0

    targets = check_targets(args.mara_root)

    if args.command in ("plan", "run"):
        selected = _select(targets, store, args.doc_id, args.rest)
        inputs = build_inputs(args.mara_root, selected)
        print(f"대상 {len(inputs)}건 · 최대 호출 {ATTEMPTS_PER_ITEM * len(inputs)} · 게이트 호출 0")
        for entry in inputs:
            gate = entry.gate["model"] if entry.gate else "null"
            print(
                f"  [{entry.origin}] {entry.doc_id}  gate={gate}  "
                f"{entry.item.source_name} · {entry.item.title[:50]} ({len(entry.item.body)}자)"
            )
        if args.command == "plan":
            return 0
        from extraction.llm import resolved_vllm_endpoint

        client = client_from_config(config, stage="extraction")
        manifest = run_backfill(
            inputs,
            targets=targets,
            store=store,
            client=client,
            max_calls=args.max_calls,
            endpoint_hash=endpoint_sha256(resolved_vllm_endpoint(config)),
        )
        ok = sum(1 for r in manifest["results"] if r["status"] == "ok")
        print(f"{manifest['run_id']}: 성공 {ok}/{len(inputs)} · 호출 {manifest['calls_used']}/{args.max_calls}")
        return 0 if ok == len(inputs) else 1

    config_version = config.get("version", 1)
    result = export_backfill(
        store, targets, out_dir=args.out_dir, config_version=config_version, export_id=args.export_id
    )
    for source, path in result.jsonl_paths.items():
        print(f"{source}: {path} ({sum(1 for r in result.records if r['source'] == source)}줄)")
    print(f"manifest: {result.manifest_path}")
    checks = self_check(
        result, store=store, mara_root=args.mara_root, out_dir=args.out_dir, config_version=config_version
    )
    print(conformance.format_report(checks))
    return 1 if any(c.status == conformance.FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
