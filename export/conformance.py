"""1단계 적합성 자체검사 — 계약 §12.2 의 13항목.

## 이 검사기의 한계를 먼저 적는다

계약 §7 의 검증은 **MARA 가 import 에서 강제하는 것**이다. 이 모듈은 그 규칙을
생산자 쪽에서 **재구현**한 것이고, 따라서 **여기를 통과했다는 사실은 MARA 로더도
통과한다는 근거가 아니다.** 같은 명세를 두 사람이 각자 구현하면 갈리는 지점이
생기는데, 이 검사기는 그 갈림을 볼 수 없다 — 자기가 이해한 명세와 자기가 만든
출력을 대조하기 때문이다.

이 검사기가 실제로 잡는 것은 **생산자 쪽 출력이 계약 문서와 어긋나는 경우**다.
그것만으로도 30건 비용을 두 번 내지 않게 하는 값은 한다. 계약 §12.2 의 최종
판정은 **0.5b 에서 MARA 로더로 다시** 내린다.

MARA 레포는 **읽기만 한다.** 코퍼스 스냅샷과 골든셋을 열어 병합 결과를
계산하지만 한 바이트도 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import glob
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from export.contract import (
    CONTRACT_VERSION,
    TEXT_MAX_CHARS,
    TEXT_ORIGIN_SOURCE,
    compute_indexable,
)
from export.exporter import build_records
from export.runner import prompt_sha256
from export.store import DEFAULT_STORE_DIR, ExtractionStore

PASS = "PASS"
FAIL = "FAIL"
PENDING = "PENDING"

# 계약 §3.1 의 최상위 17개.
REQUIRED_TOP_LEVEL = (
    "contract_version", "doc_id", "source", "source_name", "url", "title", "lang",
    "text", "text_origin", "text_chars", "text_truncated", "locator",
    "published_at", "collected_at", "ontology", "provenance", "indexable",
)
# 계약 §3.3. 누락은 경고가 아니라 실패다.
REQUIRED_PROVENANCE = (
    "extraction_model", "prompt_version", "prompt_sha256", "extracted_at", "vocab_version",
)
ALLOWED_SOURCES = ("arxiv", "news")


@dataclass
class Check:
    number: int
    name: str
    status: str
    detail: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == PASS


@dataclass
class Bundle:
    """검사 대상 한 벌 — export 산출물 + 보존소."""

    records: list[dict[str, Any]]
    manifest: dict[str, Any]
    jsonl_paths: list[Path]
    manifest_path: Path


def load_bundle(out_dir: Path | str, export_id: str) -> Bundle:
    out_dir = Path(out_dir)
    manifest_path = out_dir / f"{export_id}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    records: list[dict[str, Any]] = []
    jsonl_paths: list[Path] = []
    for source in ALLOWED_SOURCES:
        path = out_dir / source / f"{export_id}.jsonl"
        if not path.exists():
            continue
        jsonl_paths.append(path)
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
    return Bundle(records=records, manifest=manifest, jsonl_paths=jsonl_paths, manifest_path=manifest_path)


# ---------------------------------------------------------------------------
# MARA 쪽 읽기 (읽기 전용)
# ---------------------------------------------------------------------------
def mara_corpus_doc_ids(mara_root: Path | str) -> dict[str, dict[str, Any]]:
    """MARA 의 arXiv 스냅샷을 doc_id -> 문서로 읽는다. **쓰지 않는다.**"""
    docs: dict[str, dict[str, Any]] = {}
    pattern = str(Path(mara_root) / "data" / "corpus" / "*" / "*.json")
    for path in sorted(glob.glob(pattern)):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for doc in payload.get("documents", []):
            docs[doc["doc_id"]] = doc
    return docs


def mara_expected_doc_ids(mara_root: Path | str) -> list[tuple[str, str]]:
    """골든셋의 `(case_id, doc_id)` 목록."""
    path = Path(mara_root) / "docs" / "eval" / "golden-set.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    pairs: list[tuple[str, str]] = []
    for case in payload.get("cases", []):
        for doc_id in (case.get("expect") or {}).get("expected_doc_ids") or []:
            pairs.append((case["id"], doc_id))
    return pairs


def simulate_merge(
    snapshot: dict[str, dict[str, Any]], records: Iterable[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """계약 §4.3 의 병합을 그대로 흉내 낸다.

    같은 `doc_id` 가 양쪽에 있으면 **온톨로지 메타데이터만 병합**하고 `text` 는
    기존 스냅샷 쪽을 유지한다. arXiv API 초록(중앙값 1,339자)이 피드 발췌보다
    항상 길거나 같기 때문이다. `url` 은 정규화된 값으로 갱신한다 — 기존 스냅샷의
    arXiv URL 이 `http://` 라 그대로 두면 같은 문서의 링크 표기가 둘로 갈린다.

    Returns:
        (병합된 코퍼스, 병합이 일어난 doc_id 목록)
    """
    merged = {doc_id: dict(doc) for doc_id, doc in snapshot.items()}
    merged_ids: list[str] = []
    for record in records:
        doc_id = record["doc_id"]
        existing = merged.get(doc_id)
        if existing is None:
            merged[doc_id] = {
                "doc_id": doc_id,
                "title": record["title"],
                "text": record["text"],
                "locator": record["locator"],
                "url": record["url"],
                "ontology": record["ontology"],
            }
            continue
        existing["ontology"] = record["ontology"]
        existing["url"] = record["url"]  # text 는 건드리지 않는다
        merged_ids.append(doc_id)
    return merged, merged_ids


# ---------------------------------------------------------------------------
# 13항목
# ---------------------------------------------------------------------------
def check_01_e2e(bundle: Bundle, expected_total: int) -> Check:
    problems: list[str] = []
    for i, record in enumerate(bundle.records, 1):
        missing = [k for k in REQUIRED_TOP_LEVEL if k not in record]
        if missing:
            problems.append(f"{i}번째 줄 누락 키 {missing}")
        if record.get("contract_version") != CONTRACT_VERSION:
            problems.append(f"{i}번째 줄 contract_version={record.get('contract_version')!r}")
        if record.get("source") not in ALLOWED_SOURCES:
            problems.append(f"{i}번째 줄 source={record.get('source')!r}")
    if len(bundle.records) != expected_total:
        problems.append(f"레코드 {len(bundle.records)}줄 (기대 {expected_total})")
    if not bundle.manifest_path.exists():
        problems.append("manifest 없음")

    # 계약 §7-2. 마지막 값으로 덮어쓰지 않고 **실패**시킨다 — 조용히 덮어쓰면
    # 코퍼스 규모 수치가 거짓이 된다. arXiv 병합(§4.3)은 export 안이 아니라
    # import 시점의 예외이므로 여기서는 중복을 허용하지 않는다.
    doc_ids = [r.get("doc_id") for r in bundle.records]
    duplicates = sorted({d for d in doc_ids if doc_ids.count(d) > 1})
    if duplicates:
        problems.append(f"doc_id 중복 {duplicates[:5]}")

    detail = (
        f"JSONL {len(bundle.records)}줄 + manifest 1개, 최상위 17키·타입 검사 통과. "
        "적재 검증은 **생산자 측 재구현**이며 MARA 로더 판정은 0.5b 몫이다"
    )
    return Check(1, "e2e 흐름", FAIL if problems else PASS, "; ".join(problems) or detail)


def check_02_provenance(bundle: Bundle) -> Check:
    bad: list[str] = []
    for record in bundle.records:
        prov = record.get("provenance") or {}
        for key in REQUIRED_PROVENANCE:
            value = prov.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                bad.append(f"{record['doc_id']}:{key}")
    return Check(
        2,
        "provenance 5키",
        FAIL if bad else PASS,
        f"결손 {len(bad)}건 {bad[:5]}" if bad else f"{len(bundle.records)}줄 전부 5키 존재·비어있지 않음",
    )


def check_03_prompt_sha(bundle: Bundle) -> Check:
    actual: dict[str, str] = {}
    bad: list[str] = []
    for record in bundle.records:
        prov = record["provenance"]
        version = prov["prompt_version"]
        if version not in actual:
            try:
                actual[version] = prompt_sha256(version)
            except OSError as exc:
                bad.append(f"{version}: 파일 없음 ({exc})")
                actual[version] = ""
        if prov["prompt_sha256"] != actual[version]:
            bad.append(f"{record['doc_id']}: {prov['prompt_sha256'][:12]} != {actual[version][:12]}")
    used = ", ".join(f"{k}={v[:12]}…" for k, v in actual.items())
    return Check(
        3,
        "prompt_sha256 실측 일치",
        FAIL if bad else PASS,
        "; ".join(bad[:5]) if bad else f"프롬프트 파일 실측 해시와 일치 ({used})",
    )


def check_04_text_origin(bundle: Bundle) -> Check:
    bad = [r["doc_id"] for r in bundle.records if r.get("text_origin") != TEXT_ORIGIN_SOURCE]
    return Check(
        4,
        "text_origin",
        FAIL if bad else PASS,
        f"{bad[:5]}" if bad else f'{len(bundle.records)}줄 전부 "{TEXT_ORIGIN_SOURCE}"',
    )


def check_05_arxiv_merge(bundle: Bundle, snapshot: dict[str, dict[str, Any]], injected: list[str]) -> Check:
    base_arxiv = [d for d in snapshot if d.startswith("arXiv:")]
    export_arxiv = [r["doc_id"] for r in bundle.records if r["source"] == "arxiv"]
    new_arxiv = [d for d in export_arxiv if d not in snapshot]
    merged, merged_ids = simulate_merge(snapshot, bundle.records)
    merged_arxiv = [d for d in merged if d.startswith("arXiv:")]

    problems: list[str] = []
    for doc_id in injected:
        if doc_id not in snapshot:
            problems.append(f"주입 {doc_id} 가 기존 스냅샷에 없다 — 새 doc_id 를 만들었다")
        elif doc_id not in merged_ids:
            problems.append(f"주입 {doc_id} 가 병합 경로를 타지 않았다")
        elif merged[doc_id]["text"] != snapshot[doc_id]["text"]:
            problems.append(f"{doc_id}: 병합이 기존 text 를 덮어썼다 (§4.3 위반)")
    expected = len(base_arxiv) + len(new_arxiv)
    if len(merged_arxiv) != expected:
        problems.append(f"병합 후 arXiv {len(merged_arxiv)}건 (기대 {expected})")

    return Check(
        5,
        "arXiv 중복 병합",
        FAIL if problems else PASS,
        "; ".join(problems)
        or (
            f"기존 {len(base_arxiv)} + 신규 {len(new_arxiv)} = {len(merged_arxiv)}건. "
            f"주입 {len(injected)}건이 새 doc_id 를 만들지 않고 병합됨(text 유지·url 갱신)"
        ),
    )


def check_06_golden_set(bundle: Bundle, snapshot: dict[str, dict[str, Any]], pairs: list[tuple[str, str]]) -> Check:
    merged, _ = simulate_merge(snapshot, bundle.records)
    broken = [f"{case}:{doc_id}" for case, doc_id in pairs if doc_id not in merged]
    kept_text = [
        f"{case}:{doc_id}"
        for case, doc_id in pairs
        if doc_id in merged and doc_id in snapshot and merged[doc_id]["text"] != snapshot[doc_id]["text"]
    ]
    problems = broken + kept_text
    return Check(
        6,
        "골든셋 보존",
        FAIL if problems else PASS,
        "; ".join(problems)
        or f"expected_doc_ids {len(pairs)}건 전부 병합 후에도 조회 가능하고 근거 text 가 그대로다",
    )


def check_07_determinism(bundle: Bundle, store: ExtractionStore, config_version: object) -> Check:
    first = build_records(store, config_version=config_version)
    second = build_records(store, config_version=config_version)
    a = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in first)
    b = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in second)
    ids_match = [r["doc_id"] for r in first] == [r["doc_id"] for r in bundle.records]
    problems = []
    if a != b:
        problems.append("2회 재-export 결과가 바이트 단위로 다르다")
    if not ids_match:
        problems.append("재-export 의 doc_id 순서/집합이 기존 산출물과 다르다")
    return Check(
        7,
        "doc_id 결정성",
        FAIL if problems else PASS,
        "; ".join(problems) or f"보존소에서 2회 재생성한 {len(first)}줄이 바이트 동일",
    )


def check_08_text_limits(bundle: Bundle) -> Check:
    bad = [
        f"{r['doc_id']}(len={len(r['text'])},chars={r['text_chars']})"
        for r in bundle.records
        if len(r["text"]) > TEXT_MAX_CHARS or r["text_chars"] != len(r["text"])
    ]
    lengths = sorted(r["text_chars"] for r in bundle.records)
    median = lengths[len(lengths) // 2] if lengths else 0
    return Check(
        8,
        "text 상한·text_chars",
        FAIL if bad else PASS,
        "; ".join(bad[:5])
        or f"전 줄 len(text)<={TEXT_MAX_CHARS}, text_chars==len(text). 최소 {lengths[0]} / 중앙 {median} / 최대 {lengths[-1]}자",
    )


def check_09_indexable(bundle: Bundle) -> Check:
    bad = [
        r["doc_id"]
        for r in bundle.records
        if compute_indexable(r["text_origin"], r["text_chars"]) != r["indexable"]
    ]
    false_by_source: dict[str, int] = {}
    for r in bundle.records:
        if not r["indexable"]:
            false_by_source[r["source_name"]] = false_by_source.get(r["source_name"], 0) + 1
    return Check(
        9,
        "indexable 재계산 일치",
        FAIL if bad else PASS,
        f"{bad[:5]}" if bad else f"{len(bundle.records)}줄 전부 일치. indexable=false: {false_by_source or '없음'}",
    )


def check_10_vocab(bundle: Bundle) -> Check:
    vocab = bundle.manifest.get("vocab") or {}
    domains = set(vocab.get("tech_domain") or ())
    releases = set(vocab.get("release_type") or ())
    bad: list[str] = []
    for r in bundle.records:
        onto = r["ontology"]
        if onto["release_type"] not in releases:
            bad.append(f"{r['doc_id']}: release_type={onto['release_type']}")
        outside = [d for d in onto["tech_domains"] if d not in domains]
        if outside:
            bad.append(f"{r['doc_id']}: tech_domains={outside}")
    return Check(
        10,
        "통제어휘 대조",
        FAIL if bad else PASS,
        "; ".join(bad[:5])
        or f"레코드 값 전부 manifest vocab 안 (기술영역 {len(domains)}개 / 발표유형 {len(releases)}개)",
    )


def check_11_published_null(bundle: Bundle) -> Check:
    empty = [r["doc_id"] for r in bundle.records if r.get("published_at") == ""]
    nulls = [r["doc_id"] for r in bundle.records if r.get("published_at") is None]
    return Check(
        11,
        "published_at null 처리",
        FAIL if empty else PASS,
        f"빈 문자열 {empty[:5]}" if empty else f"빈 문자열 0건, null {len(nulls)}건",
    )


def check_12_manifest_counts(bundle: Bundle, expected_total: int) -> Check:
    counts = bundle.manifest.get("counts") or {}
    problems = []
    if counts.get("total") != expected_total:
        problems.append(f"counts.total={counts.get('total')} (기대 {expected_total})")
    if counts.get("total") != len(bundle.records):
        problems.append(f"counts.total 이 실제 줄 수 {len(bundle.records)} 와 다르다")
    if counts.get("indexable", 0) + counts.get("excluded_empty_text", 0) != counts.get("total"):
        problems.append("indexable + excluded_empty_text != total")
    return Check(
        12,
        "manifest 합계",
        FAIL if problems else PASS,
        "; ".join(problems)
        or f"total={counts.get('total')}, indexable={counts.get('indexable')}, excluded_empty_text={counts.get('excluded_empty_text')}",
    )


def check_13_korean_indexed() -> Check:
    return Check(
        13,
        "한국어 레코드 색인",
        PENDING,
        "실제 Chroma 인덱스 적재와 lang='ko' 조회가 필요하다. MARA 로더·인덱스가 없는 "
        "상태에서 생산자 쪽이 판정할 수 있는 항목이 아니다 — 0.5b 대기",
    )


def run_checks(
    bundle: Bundle,
    *,
    store: ExtractionStore,
    mara_root: Path | str,
    injected: list[str],
    expected_total: int,
    config_version: object,
) -> list[Check]:
    snapshot = mara_corpus_doc_ids(mara_root)
    pairs = mara_expected_doc_ids(mara_root)
    return [
        check_01_e2e(bundle, expected_total),
        check_02_provenance(bundle),
        check_03_prompt_sha(bundle),
        check_04_text_origin(bundle),
        check_05_arxiv_merge(bundle, snapshot, injected),
        check_06_golden_set(bundle, snapshot, pairs),
        check_07_determinism(bundle, store, config_version),
        check_08_text_limits(bundle),
        check_09_indexable(bundle),
        check_10_vocab(bundle),
        check_11_published_null(bundle),
        check_12_manifest_counts(bundle, expected_total),
        check_13_korean_indexed(),
    ]


def format_report(checks: list[Check]) -> str:
    lines = ["| # | 항목 | 판정 | 근거 |", "|---|---|---|---|"]
    for check in checks:
        lines.append(f"| {check.number} | {check.name} | {check.status} | {check.detail} |")
    passed = sum(1 for c in checks if c.status == PASS)
    failed = sum(1 for c in checks if c.status == FAIL)
    pending = sum(1 for c in checks if c.status == PENDING)
    lines.append("")
    lines.append(f"PASS {passed} / FAIL {failed} / PENDING {pending}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="계약 §12.2 1단계 자체검사 (MARA 읽기 전용)")
    parser.add_argument("--export-id", required=True)
    parser.add_argument("--out-dir", default=str(Path("data") / "corpus"))
    parser.add_argument("--store-dir", default=str(DEFAULT_STORE_DIR))
    parser.add_argument("--mara-root", required=True, help="multiagent-research-lab 레포 경로 (읽기 전용)")
    parser.add_argument("--injected", nargs="*", default=[], help="--urls 로 주입한 doc_id")
    parser.add_argument("--expected-total", type=int, default=30)
    parser.add_argument("--config-version", default=1)
    args = parser.parse_args(argv)

    bundle = load_bundle(args.out_dir, args.export_id)
    checks = run_checks(
        bundle,
        store=ExtractionStore(args.store_dir),
        mara_root=args.mara_root,
        injected=list(args.injected),
        expected_total=args.expected_total,
        config_version=args.config_version,
    )
    print(format_report(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
