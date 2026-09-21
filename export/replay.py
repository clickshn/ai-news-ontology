"""보존소의 입력을 **다른 프로바이더로 다시 추출**한다 — 대조 실행기 (ADR-018).

## 왜 별도 실행기인가

`export.runner collect` 는 수집 → 게이트 → 추출 → 보존을 한다. 대조에 필요한 것은
그 중 **추출 하나**뿐이다. 수집을 다시 하면 입력이 달라져 "모델만 바꿨다"가 깨지고,
게이트를 다시 태우면 어느 단계의 차이인지 구분할 수 없다.

    입력 = data/extractions/*.json 의 raw_item  (생산자 ADR-015 로 이미 보존돼 있다)
    변수 = llm.provider 하나

MARA 사전 등록 §2 의 "같은 입력 · 다른 모델" 단일 변수 비교가 이 실행기의 계약이다.
**외부 API 를 한 번도 더 부르지 않는다** — 벤더 산출물은 이미 디스크에 있다.

## 원본을 먼저 쓴다 (D-052)

응답을 받으면 **변환하기 전에** `raw/` 아래로 내린다. 검증되지 않은 변환 코드와
값비싼 결과를 같은 트랜잭션에 두지 않는다. 여기서는 토큰 비용이 0 이지만 규칙을
비용으로 정당화하면 비용이 0 인 곳에서 규칙이 사라진다 — 잃는 것은 돈이 아니라
**같은 입력에 대한 그 모델의 응답**이고, 그건 다시 만들 수 없다.

## 실행

    # 소표본 (형식 확정용)
    python -m export.replay --limit 3 --run-id sample

    # 본 실행
    python -m export.replay --run-id main
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collectors.base import RawItem
from collectors.rss import load_config
from export.store import DEFAULT_STORE_DIR, ExtractionStore, safe_filename
from extraction.extractor import DEFAULT_PROMPT, build_variables, load_prompt
from extraction.llm import SchemaMismatchError, client_from_config
from extraction.schema import NewsOntology

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPLAY_DIR = PROJECT_ROOT / "data" / "replays"

#: 스키마 실패로 세는 **유일한** 실패 이름. `replay_one` 이 `failure` 에 적는다.
SCHEMA_FAILURE = "SchemaMismatchError"


def raw_item_from_stored(payload: dict[str, Any]) -> RawItem:
    """보존된 `raw_item` 을 그대로 되살린다.

    **여기서 값을 손보지 않는다.** 정규화·트리밍을 한 글자라도 하면 그 순간
    입력이 달라지고 대조가 "모델 교체"가 아니게 된다.
    """
    return RawItem.model_validate(payload["raw_item"])


def _write_json(path: Path, data: Any) -> None:
    """원자적 쓰기. 부분 파일이 남으면 다음 실행이 그걸 결과로 읽는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _persist_raw(client: Any, doc_id: str, out_dir: Path) -> dict[str, Any]:
    """응답 원본을 **변환하기 전에** 내린다 (D-052). 반환값은 행에 합칠 파생 필드.

    호출부가 이것을 `finally` 에서 부르는 이유는 순서가 규칙이기 때문이다 —
    행을 만드는 코드가 터져도 응답은 이미 디스크에 있다. 잃는 것은 돈이 아니라
    **같은 입력에 대한 그 모델의 응답**이고, 다시 부르면 그건 다른 응답이다.
    """
    raws = list(getattr(client, "last_raw_responses", []) or [])
    if not raws:
        # 전송이 실패해 응답이 하나도 없는 경우다. 빈 파일을 남기지 않는다 —
        # 다음 실행이 그걸 "응답 0건짜리 결과"로 읽는다.
        return {}
    _write_json(out_dir / "raw" / safe_filename(doc_id), raws)
    # finish_reason 은 "모델이 스키마를 못 지켰다"와 "토큰이 모자라 잘렸다"를
    # 가르는 유일한 신호다. 둘을 같은 실패로 세면 ②의 수치가 디코딩 설정이
    # 아니라 max_tokens 를 재게 된다.
    return {"finish_reasons": [(r.get("choices") or [{}])[0].get("finish_reason") for r in raws]}


def replay_one(
    stored: dict[str, Any],
    client: Any,
    prompt: Any,
    out_dir: Path,
) -> dict[str, Any]:
    """1건 재추출. 실패해도 예외를 밖으로 내지 않고 결과에 적는다.

    격리(`SchemaMismatchError`)는 **오류가 아니라 측정 대상**이다 (사전 등록 §3②).
    한 건이 터졌다고 실행이 멈추면 실패율을 셀 수 없다.
    """
    doc_id = stored["doc_id"]
    item = raw_item_from_stored(stored)
    system, user = prompt.render(**build_variables(item))

    row: dict[str, Any] = {
        "doc_id": doc_id,
        "url": str(item.url),
        "source_name": item.source_name,
        "prompt_name": prompt.name,
        "model": getattr(client, "model", None),
        "baseline_model": (stored.get("extraction") or {}).get("model"),
        "baseline_prompt": (stored.get("extraction") or {}).get("prompt_name"),
        "baseline_prompt_sha256": (stored.get("extraction") or {}).get("prompt_sha256"),
    }

    try:
        try:
            result = client.parse_into(system=system, user=user, output_model=NewsOntology)
        finally:
            # --- 원본을 먼저 내린다 (D-052) ---------------------------------
            # 아래 `row |=` 들보다 **앞**이어야 한다. 행을 만드는 것이 곧 변환이고,
            # 검증되지 않은 변환 코드와 값비싼 결과를 같은 트랜잭션에 두지 않는다.
            row |= _persist_raw(client, doc_id, out_dir)
    except SchemaMismatchError as exc:
        # 이 이름을 `failure_kind` 가 읽는다. **한쪽만 바꾸면 비율이 조용히 틀린다.**
        row |= {
            "ok": False,
            "failure": SCHEMA_FAILURE,
            "attempts": exc.attempts,
            "last_error": str(exc.last_error)[:2000],
        }
        ontology = None
    except Exception as exc:  # noqa: BLE001 — 전송 계층 실패도 결과에 남긴다
        # 여기 남는 이름은 **전송 실패**로 분류된다 (D-075). 모델에게 물어보지
        # 못한 건이라 스키마 실패율의 분자에도 분모에도 들어가지 않는다.
        row |= {"ok": False, "failure": type(exc).__name__, "last_error": str(exc)[:2000]}
        ontology = None
    else:
        ontology = result.value
        row |= {
            "ok": True,
            "failure": None,
            "attempts": result.attempts,
            "usage": asdict(result.usage),
            "ontology": ontology.model_dump(mode="json", by_alias=True),
        }

    return row


def failure_kind(row: dict[str, Any]) -> str | None:
    """`None` = 성공 / `"schema"` = 모델이 계약을 못 지켰다 / `"transport"` = **못 물어봤다.**

    둘을 가르는 것이 이 함수의 전부다. 같은 칸에 세면 **네트워크가 끊긴 날의 실행이
    "모델이 스키마를 못 지킨다"로 기록된다** — 사전 등록 §3② 가 재려는 것은 모델의
    계약 준수이고, 전송 실패는 그 축의 관측이 아니라 **관측 실패**다.
    """
    if row.get("ok"):
        return None
    return "schema" if row.get("failure") == SCHEMA_FAILURE else "transport"


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """행 목록 → 집계. 실행과 분리해 둔 이유는 **이 계산을 직접 재기 위해서다.**

    ## 분모에서 전송 실패를 뺀다

    전송이 실패한 건은 모델에게 **물어보지 못한 건**이다. 분모에 남겨 두면 회선이
    나쁜 날일수록 실패율이 **낮게** 나온다 — 재지 못한 것이 "잘한 것"으로 읽힌다.
    D-050 과 같은 계산이다: 재지 못한 것을 잰 것처럼 쓰지 않는다. 그래서 전부 전송
    실패면 비율은 `0.0` 이 아니라 **`None`** 이고, 분모(`measured_items`)를 같이
    적어 **몇 건을 재고 나온 비율인지** 보이게 한다.
    """
    kinds = [failure_kind(row) for row in rows]
    schema_failures = kinds.count("schema")
    transport_failures = kinds.count("transport")
    measured = len(rows) - transport_failures
    return {
        "item_count": len(rows),
        "measured_items": measured,
        "schema_failures": schema_failures,
        "transport_failures": transport_failures,
        "schema_failure_rate": round(schema_failures / measured, 4) if measured else None,
        "retried": sum(1 for row in rows if (row.get("attempts") or 1) > 1),
        "truncated": sum(1 for row in rows if "length" in (row.get("finish_reasons") or [])),
    }


def run(
    *,
    store_dir: Path = DEFAULT_STORE_DIR,
    out_root: Path = DEFAULT_REPLAY_DIR,
    run_id: str | None = None,
    limit: int | None = None,
    prompt_name: str = DEFAULT_PROMPT,
    config: dict[str, Any] | None = None,
) -> Path:
    config = config or load_config()
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = out_root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    store = ExtractionStore(store_dir)
    prompt = load_prompt(prompt_name)
    client = client_from_config(config, stage="extraction")

    doc_ids = sorted(store.doc_ids())
    if limit is not None:
        doc_ids = doc_ids[:limit]

    rows: list[dict[str, Any]] = []
    # 0건 실행에도 누적본을 남긴다. 없으면 실행 디렉터리의 구성이 건수에 따라
    # 달라져서, 읽는 쪽이 "rows.json 이 없는 실행"을 따로 다뤄야 한다.
    _write_json(out_dir / "rows.json", rows)
    for index, doc_id in enumerate(doc_ids, start=1):
        stored = store.load(doc_id).payload
        print(f"[{index}/{len(doc_ids)}] {doc_id}", file=sys.stderr, flush=True)
        row = replay_one(stored, client, prompt, out_dir)
        rows.append(row)
        # 매 건마다 누적본을 갱신한다. 중간에 죽어도 거기까지는 남는다.
        _write_json(out_dir / "rows.json", rows)

    summary = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": ((config.get("llm") or {}).get("provider")),
        "model": getattr(client, "model", None),
        "prompt_name": prompt.name,
        **summarize_rows(rows),
    }
    _write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="보존된 입력을 다른 프로바이더로 재추출한다")
    parser.add_argument("--store", default=str(DEFAULT_STORE_DIR))
    parser.add_argument("--out", default=str(DEFAULT_REPLAY_DIR))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--limit", type=int, default=None, help="앞에서부터 N건만")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = parser.parse_args(argv)

    run(
        store_dir=Path(args.store),
        out_root=Path(args.out),
        run_id=args.run_id,
        limit=args.limit,
        prompt_name=args.prompt,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
