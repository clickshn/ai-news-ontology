"""export 실행 CLI — 수집·게이트·추출·보존(`collect`)과 형식 변환(`export`)을 나눈다.

    # 1단계 표본 구성 확인 (API 호출 없음)
    python -m export.runner plan --take "GeekNews=10" --urls https://arxiv.org/abs/2412.05449v1

    # 실제 호출 (게이트 + 추출). 결과는 받는 즉시 data/extractions/ 에 보존된다
    python -m export.runner collect --take "GeekNews=10" --max-extractions 30

    # 보존된 결과만으로 JSONL + manifest 생성 (API 호출 없음)
    python -m export.runner export

**두 단계를 한 명령으로 합치지 않는다.** 합치면 형식이 틀렸을 때 다시 돌리는
동작이 추출까지 다시 돌리는 동작과 같은 이름을 갖게 되고, 그 순간 계약 §12.3 이
막으려던 "형식 결함에 LLM 비용을 또 내는 구조"가 되살아난다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from collectors.base import RawItem
from collectors.rss import collect as collect_feeds
from collectors.rss import load_config
from export.doc_id import doc_id_for
from export.exporter import DEFAULT_OUT_DIR, KST, build_records, write_export
from export.mara_seed import collect_seed_docs
from export.store import DEFAULT_STORE_DIR, STORE_SCHEMA, ExtractionStore
from export.urls import collect_urls
from extraction.extractor import (
    DEFAULT_PROMPT,
    GATE_PROMPT,
    PROMPT_DIR,
    check_relevance,
    extract_ontology,
    load_prompt,
)
from extraction.llm import AnthropicClient, SchemaMismatchError

# 1단계는 30건이다. 실수로 250건을 돌리는 것을 코드가 막는다 — 승인 게이트를
# 사람의 기억에만 맡기지 않는다 (MARA ADR-018 Implementation, 계약 §12).
DEFAULT_MAX_EXTRACTIONS = 30

# 게이트에서 걸러지는 항목이 있으므로 목표 건수보다 넉넉히 수집한다. GeekNews 는
# 최근 50건 중 약 40%가 비-AI 다 (D-014).
DEFAULT_POOL_FACTOR = 3


def prompt_sha256(filename: str, *, prompt_dir: Path = PROMPT_DIR) -> str:
    """프롬프트 **파일 원본**의 sha256.

    `load_prompt()` 가 주석을 걷어낸 뒤의 문자열이 아니라 디스크의 바이트를
    센다. 계약 §12.2-3 이 "생산자 프롬프트 파일의 실제 sha256" 과 대조하기
    때문이고, 그래야 MARA 쪽에서 파일만 받아 같은 값을 재현할 수 있다.
    """
    return hashlib.sha256((prompt_dir / filename).read_bytes()).hexdigest()


def parse_take(values: Sequence[str]) -> dict[str, int]:
    """--take 'GeekNews=10' 를 {소스명: 건수} 로."""
    plan: dict[str, int] = {}
    for value in values or ():
        name, _, count = value.rpartition("=")
        if not name or not count.isdigit():
            raise ValueError(f"--take 형식은 소스명=건수 입니다: {value!r}")
        plan[name] = int(count)
    return plan


def read_urls(args_urls: Sequence[str], urls_file: str | None) -> list[str]:
    urls = list(args_urls or ())
    if urls_file:
        text = Path(urls_file).read_text(encoding="utf-8")
        urls += [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
    return urls


@dataclass
class Selection:
    """표본 1건 + 어떤 경로로 들어왔는지."""

    item: RawItem
    origin: str  # "urls" | "feed"


def _pool(config: dict[str, Any], source_name: str, wanted: int, pool_factor: int) -> list[RawItem]:
    limit = max(wanted + 5, wanted * pool_factor)
    return list(collect_feeds(config, source_name=source_name, limit_per_source=limit))


def build_plan(
    config: dict[str, Any],
    *,
    take: dict[str, int],
    urls: Sequence[str],
    pool_factor: int = DEFAULT_POOL_FACTOR,
    seed_from_mara: str | None = None,
    seed_doc_ids: Sequence[str] = (),
) -> tuple[list[Selection], dict[str, list[RawItem]]]:
    """표본 후보를 모은다. **API 는 호출하지 않는다.**

    주입 경로가 둘인 이유는 원문 출처가 다르기 때문이다. `--urls` 는 arXiv API 에서
    받아오고, `--seed-from-mara` 는 MARA 스냅샷에서 읽는다(1단계 검증 전용,
    `export/mara_seed.py` 참고). 둘을 한 옵션으로 합치면 레코드의 원문이 어디서
    왔는지가 명령줄에서 사라진다.

    Returns:
        (주입분, 소스명별 피드 후보 풀)
    """
    injected = [Selection(item=item, origin="urls") for item in collect_urls(urls)]
    if seed_doc_ids:
        if not seed_from_mara:
            raise ValueError("--seed-doc-id 를 쓰려면 --seed-from-mara 경로가 필요합니다")
        injected += [
            Selection(item=item, origin="mara-seed")
            for item in collect_seed_docs(seed_from_mara, seed_doc_ids)
        ]
    pools = {name: _pool(config, name, wanted, pool_factor) for name, wanted in take.items()}
    return injected, pools


def _store_payload(
    item: RawItem,
    *,
    doc_id: str,
    gate: dict[str, Any] | None,
    extraction: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": STORE_SCHEMA,
        "doc_id": doc_id,
        "stored_at": datetime.now(KST).isoformat(timespec="seconds"),
        "raw_item": json.loads(item.model_dump_json()),
        "gate": gate,
        "extraction": extraction,
    }


def run_collect(
    config: dict[str, Any],
    *,
    take: dict[str, int],
    urls: Sequence[str],
    store: ExtractionStore,
    max_extractions: int = DEFAULT_MAX_EXTRACTIONS,
    pool_factor: int = DEFAULT_POOL_FACTOR,
    extraction_prompt_name: str = DEFAULT_PROMPT,
    gate_prompt_name: str = GATE_PROMPT,
    skip_stored: bool = True,
    seed_from_mara: str | None = None,
    seed_doc_ids: Sequence[str] = (),
) -> list[str]:
    """게이트 -> 추출 -> 보존. 이미 보존된 doc_id 는 기본적으로 건너뛴다.

    보존된 것을 건너뛰는 이유는 재실행 비용이다. 같은 30건을 다시 돌리면
    같은 돈을 또 낸다 (계약 §12.3).
    """
    injected, pools = build_plan(
        config,
        take=take,
        urls=urls,
        pool_factor=pool_factor,
        seed_from_mara=seed_from_mara,
        seed_doc_ids=seed_doc_ids,
    )

    gate_prompt = load_prompt(gate_prompt_name)
    extraction_prompt = load_prompt(extraction_prompt_name)
    gate_sha = prompt_sha256(gate_prompt_name)
    extraction_sha = prompt_sha256(extraction_prompt_name)

    gate_client = AnthropicClient.from_config(config, stage="relevance_gate")
    extraction_client = AnthropicClient.from_config(config, stage="extraction")

    seen: set[str] = set(store.doc_ids()) if skip_stored else set()
    stored_ids: list[str] = []
    budget = max_extractions

    def process(item: RawItem) -> bool:
        """1건 처리. 추출까지 갔으면 True."""
        nonlocal budget
        doc_id = doc_id_for(str(item.url))
        if doc_id in seen:
            print(f"[skip] 이미 보존됨: {doc_id}", file=sys.stderr)
            return False
        if budget <= 0:
            return False

        relevance = check_relevance(item, gate_client, prompt=gate_prompt)
        gate_payload = {
            "prompt_name": relevance.prompt_name,
            "prompt_sha256": gate_sha,
            "model": relevance.usage.model,
            "is_relevant": relevance.gate.is_relevant,
            "reason": relevance.gate.reason,
            "usage": vars(relevance.usage),
        }
        if not relevance.is_relevant:
            print(f"[gate] 스킵 {doc_id}: {relevance.gate.reason}", file=sys.stderr)
            seen.add(doc_id)
            return False

        try:
            result = extract_ontology(item, extraction_client, prompt=extraction_prompt)
        except SchemaMismatchError as exc:
            # 부분 결과를 보존하지 않는다. 형식이 아니라 내용이 비었다는 뜻이라
            # 재-export 로는 복구되지 않는다.
            print(f"[fail] 스키마 검증 실패 {doc_id}: {exc.last_error}", file=sys.stderr)
            seen.add(doc_id)
            return False

        extraction_payload = {
            "prompt_name": result.prompt_name,
            "prompt_sha256": extraction_sha,
            "model": result.usage.model,
            "attempts": result.attempts,
            "extracted_at": datetime.now(KST).isoformat(timespec="seconds"),
            "ontology": json.loads(result.ontology.model_dump_json()),
            "usage": vars(result.usage),
        }
        path = store.save(
            _store_payload(item, doc_id=doc_id, gate=gate_payload, extraction=extraction_payload)
        )
        seen.add(doc_id)
        stored_ids.append(doc_id)
        budget -= 1
        print(f"[ok] {doc_id} -> {path.name} (남은 예산 {budget})", file=sys.stderr)
        return True

    for selection in injected:
        process(selection.item)

    for source_name, wanted in take.items():
        taken = 0
        for item in pools.get(source_name, []):
            if taken >= wanted or budget <= 0:
                break
            if process(item):
                taken += 1
        if taken < wanted:
            print(
                f"[warn] {source_name}: 목표 {wanted}건 중 {taken}건만 확보했습니다 "
                "(게이트 스킵 또는 후보 부족)",
                file=sys.stderr,
            )

    return stored_ids


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="config.yaml 경로")
    parser.add_argument("--store-dir", default=str(DEFAULT_STORE_DIR), help="추출 보존소 경로")


def _add_sampling(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--take", action="append", default=[], help="소스명=건수")
    parser.add_argument("--urls", nargs="*", default=[], help="직접 주입할 URL (arXiv만)")
    parser.add_argument("--urls-file", default=None, help="URL 목록 파일")
    parser.add_argument(
        "--seed-from-mara",
        default=None,
        help="MARA 레포 경로 (읽기 전용). 1단계 검증 전용 주입 경로",
    )
    parser.add_argument(
        "--seed-doc-id",
        nargs="*",
        default=[],
        help="MARA 스냅샷에서 가져올 doc_id (예: arXiv:2412.05449v1)",
    )
    parser.add_argument("--pool-factor", type=int, default=DEFAULT_POOL_FACTOR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="출력 계약 v1 export (MARA ADR-018)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="표본 구성만 확인한다 (API 호출 없음)")
    _add_common(p_plan)
    _add_sampling(p_plan)

    p_collect = sub.add_parser("collect", help="게이트+추출을 실행하고 원본을 보존한다 (API 호출)")
    _add_common(p_collect)
    _add_sampling(p_collect)
    p_collect.add_argument(
        "--max-extractions",
        type=int,
        default=DEFAULT_MAX_EXTRACTIONS,
        help="정밀 추출 호출 상한. 승인받은 규모를 코드로 고정한다",
    )
    p_collect.add_argument("--prompt", default=DEFAULT_PROMPT)
    p_collect.add_argument("--gate-prompt", default=GATE_PROMPT)

    p_export = sub.add_parser("export", help="보존된 결과로 JSONL+manifest 생성 (API 호출 없음)")
    _add_common(p_export)
    p_export.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p_export.add_argument("--export-id", default=None, help="지정하면 그 ID 로 덮어쓴다")

    args = parser.parse_args(argv)
    config = load_config(args.config) if args.config else load_config()
    store = ExtractionStore(args.store_dir)

    if args.command == "plan":
        injected, pools = build_plan(
            config,
            take=parse_take(args.take),
            urls=read_urls(args.urls, args.urls_file),
            pool_factor=args.pool_factor,
            seed_from_mara=args.seed_from_mara,
            seed_doc_ids=args.seed_doc_id,
        )
        print(f"주입 {len(injected)}건")
        for selection in injected:
            print(
                f"  [{selection.origin}] {doc_id_for(str(selection.item.url))}  "
                f"{selection.item.title[:55]} ({len(selection.item.body)}자)"
            )
        for name, pool in pools.items():
            print(f"{name}: 후보 {len(pool)}건")
        return 0

    if args.command == "collect":
        stored = run_collect(
            config,
            take=parse_take(args.take),
            urls=read_urls(args.urls, args.urls_file),
            store=store,
            max_extractions=args.max_extractions,
            pool_factor=args.pool_factor,
            extraction_prompt_name=args.prompt,
            gate_prompt_name=args.gate_prompt,
            seed_from_mara=args.seed_from_mara,
            seed_doc_ids=args.seed_doc_id,
        )
        print(f"보존 {len(stored)}건")
        return 0 if stored else 1

    records = build_records(store, config_version=config.get("version", 1))
    if not records:
        print("보존소가 비어 있습니다. 먼저 collect 를 실행하세요.", file=sys.stderr)
        return 1
    result = write_export(
        records,
        out_dir=args.out_dir,
        export_id=args.export_id,
        config_version=config.get("version", 1),
    )
    for source, path in result.jsonl_paths.items():
        print(f"{source}: {path} ({sum(1 for r in records if r['source'] == source)}줄)")
    print(f"manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
