"""JSONL + manifest 쓰기 (계약 §2, §5, §11.3).

출력은 두 갈래다.

    data/corpus/arxiv/<export_id>.jsonl
    data/corpus/news/<export_id>.jsonl
    data/corpus/<export_id>.manifest.json

경로 모양은 MARA 쪽 `data/corpus/<source>/` 와 같게 맞춰 뒀다. 0.5b 에서 파일을
그대로 옮기면 되고, 옮기는 사람이 경로를 손으로 고치면서 실수할 자리를 없앤다.

**manifest 에 계약에 없는 키를 추가하지 않는다.** 항목 추가는 `contract_version`
minor 인상 사안이다(§9). "어느 파일이 이 manifest 소속인가" 같은 편의 정보를
넣고 싶어지지만, 그건 디렉터리 구조가 이미 답하고 있다.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from export.contract import CONTRACT_VERSION, LICENSE_NOTE, vocab_snapshot, vocab_version
from export.record import build_record
from export.store import PROJECT_ROOT, ExtractionStore

DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "corpus"

# export_id 는 생산자 export 실행 시각(KST)이다 (계약 §2.1).
KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class ExportResult:
    export_id: str
    manifest_path: Path
    jsonl_paths: dict[str, Path]
    records: list[dict[str, Any]]
    manifest: dict[str, Any]


def make_export_id(now: datetime | None = None) -> str:
    return (now or datetime.now(KST)).strftime("ontology-%Y%m%d-%H%M%S")


def exporter_tag(project_root: Path = PROJECT_ROOT) -> str:
    """`ai-news-ontology@<git sha>`. git 을 읽지 못하면 `unknown` 으로 둔다.

    여기서 예외를 올리지 않는 이유: export 산출물의 유용성이 git 가용성에
    묶이면 안 된다. 다만 값을 지어내지도 않는다 — 모르면 모른다고 적는다.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"ai-news-ontology@{sha or 'unknown'}"


def build_counts(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """계약 §11.3 의 `counts`.

    이 블록이 계약 §8.1 의 필드 소비 등급을 확정하는 **유일한 1차 근거**다
    (상위 `release_type` 이 80% 초과면 필터 후보에서 내린다 등). 판정 임계값은
    데이터를 보기 전에 계약에 사전 등록돼 있다 — 분포를 본 뒤 기준을 정하면
    어떤 결과에도 해석을 붙일 수 있기 때문이다.
    """
    records = list(records)
    by_source: Counter[str] = Counter()
    by_source_name: Counter[str] = Counter()
    by_release_type: Counter[str] = Counter()
    by_tech_domain: Counter[str] = Counter()
    indexable = 0
    companies_unresolved = 0

    for record in records:
        by_source[record["source"]] += 1
        by_source_name[record["source_name"]] += 1
        by_release_type[record["ontology"]["release_type"]] += 1
        for domain in record["ontology"]["tech_domains"]:
            by_tech_domain[domain] += 1
        if record["indexable"]:
            indexable += 1
        # 레코드 수가 아니라 **표기 건수**를 센다. 사전 보강 우선순위는
        # "몇 개 기사에 나왔나"가 아니라 "몇 번 나왔나"로 정한다 (D-035/D-036).
        companies_unresolved += sum(
            1 for company in record["ontology"]["companies"] if not company["resolved"]
        )

    return {
        "total": len(records),
        "indexable": indexable,
        # v1 에서 색인에서 빠지는 유일한 사유가 본문 0자다 (text_origin 은 한 값뿐).
        "excluded_empty_text": len(records) - indexable,
        "by_source": dict(sorted(by_source.items())),
        "by_source_name": dict(sorted(by_source_name.items())),
        "by_release_type": dict(sorted(by_release_type.items())),
        "by_tech_domain": dict(sorted(by_tech_domain.items())),
        "companies_unresolved": companies_unresolved,
    }


def build_manifest(
    records: Iterable[dict[str, Any]],
    *,
    export_id: str,
    exported_at: str,
    config_version: object,
    exporter: str,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "export_id": export_id,
        "exported_at": exported_at,
        "exporter": exporter,
        "license_note": LICENSE_NOTE,
        "counts": build_counts(records),
        "vocab": vocab_snapshot(config_version),
    }


def build_records(store: ExtractionStore, *, config_version: object) -> list[dict[str, Any]]:
    """보존소 전체를 레코드로 옮긴다. 순서는 보존소의 파일명 정렬 순서다."""
    version = vocab_version(config_version)
    return [build_record(stored, vocab_version=version) for stored in store]


def write_export(
    records: list[dict[str, Any]],
    *,
    out_dir: Path | str = DEFAULT_OUT_DIR,
    export_id: str | None = None,
    now: datetime | None = None,
    config_version: object = 1,
    project_root: Path = PROJECT_ROOT,
) -> ExportResult:
    """레코드를 source 별 JSONL 로 쓰고 manifest 를 남긴다.

    **레코드가 0건인 source 의 파일은 만들지 않는다.** 빈 파일이 있으면 MARA 쪽
    로더가 "수집은 됐는데 전부 걸러진 것"과 "애초에 없는 것"을 구분할 수 없다.
    """
    now = now or datetime.now(KST)
    export_id = export_id or make_export_id(now)
    out_dir = Path(out_dir)

    by_source: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_source.setdefault(record["source"], []).append(record)

    jsonl_paths: dict[str, Path] = {}
    for source, source_records in sorted(by_source.items()):
        path = out_dir / source / f"{export_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # 인코딩 UTF-8, 개행 \n, ensure_ascii=false (계약 §2).
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for record in source_records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        jsonl_paths[source] = path

    manifest = build_manifest(
        records,
        export_id=export_id,
        exported_at=now.isoformat(timespec="seconds"),
        config_version=config_version,
        exporter=exporter_tag(project_root),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / f"{export_id}.manifest.json"
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return ExportResult(
        export_id=export_id,
        manifest_path=manifest_path,
        jsonl_paths=jsonl_paths,
        records=records,
        manifest=manifest,
    )
