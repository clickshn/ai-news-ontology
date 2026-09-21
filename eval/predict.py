"""골든셋 입력으로 **추출을 다시 돌려** 채점용 예측 JSONL 을 만든다.

## 왜 `export/replay.py` 를 쓰지 않나

`replay` 는 보존소(`data/extractions/`)의 `raw_item` 을 읽는다. **골든셋 3건은
거기 없다** — 골든셋은 사람이 확정한 라벨이지 수집 산출물이 아니고, 세 항목의
URL 은 보존소 30건 어디에도 나오지 않는다.

넣으면 되지 않느냐는 생각이 먼저 드는데, 그렇게 하지 않는다. **보존소는 export
코퍼스의 입력이고 MARA 가 그 산출물을 읽는다**(ADR-015, 그쪽 ADR-018). 측정용
항목을 거기 섞으면 코퍼스로 흘러든다. 그래서 골든셋 → 예측 경로를 따로 둔다.

    입력 = eval/golden_set/*.json 의 `input`  (원문의 유일한 사본)
    출력 = {"item_id": ..., "ontology": {...}} JSONL  (eval.runner 가 읽는 형식)

## 🔴 골든셋은 프롬프트 입력을 전부 보존하지 않는다

`extraction.extractor.build_variables` 는 `source_name` 과 `published_at` 을
프롬프트에 넣는데, 골든셋 파일에는 `제목`·`본문`·`source_url` 만 있다. 즉 **재추출은
원래 실행과 정확히 같은 입력이 아니다.**

여기서는 둘을 `source_url` 의 호스트와 골든셋 id 의 날짜 접두사에서 **유도**하고,
유도했다는 사실을 산출물(`meta.json` 의 `derived_inputs`)에 남긴다. 유도값을
보존값처럼 쓰면 "같은 입력 · 다른 모델"이라는 말이 근거 없이 성립한다.

**유도에 실패하면 그 자리에서 멈춘다.** 기본값을 넣고 진행하면 프롬프트에 들어간
값이 무엇인지 모르는 채로 점수가 나온다.

## 실행

    # 예측만 만든다 (실제 호출이 일어난다)
    python -m eval.predict --out eval/scores/preds.jsonl

    # 무엇이 나갈지만 본다 — 호출 0건
    python -m eval.predict --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from collectors.base import RawItem
from collectors.rss import load_config, rss_sources
from eval.runner import DEFAULT_GOLDEN_SET_DIR, load_golden_set, prompt_sha256
from eval.schema import GoldenItem
from extraction.extractor import (
    PROMPT_DIR,
    DEFAULT_PROMPT,
    build_variables,
    load_prompt,
)
from extraction.llm import SchemaMismatchError, client_from_config
from extraction.schema import NewsOntology

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "eval" / "scores" / "preds.jsonl"

#: 스키마 실패로 세는 **유일한** 실패 이름. `export/replay.py` 와 같은 값을 쓴다 —
#: 두 실행기의 실패 분류가 갈리면 같은 사건이 다른 이름으로 집계된다.
SCHEMA_FAILURE = "SchemaMismatchError"


class PredictError(Exception):
    """유도할 수 없는 입력. **기본값으로 때우지 않는다** (모듈 설명 참고)."""


# ---------------------------------------------------------------------------
# 보존되지 않은 입력의 유도
# ---------------------------------------------------------------------------
def _registrable(host: str) -> str:
    """`export.arxiv.org` 와 `arxiv.org` 를 같은 것으로 보기 위한 뒤 두 라벨.

    완전한 public-suffix 구현이 아니다. 이 함수가 보는 것은 `config.yaml` 에
    적힌 소스 몇 개뿐이고, 여기서 의존성을 늘릴 이유가 없다.
    """
    labels = host.lower().strip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def resolve_source_name(source_url: str, config: dict[str, Any]) -> str:
    """`source_url` 의 호스트로 `config.yaml` 의 소스 이름을 찾는다.

    Raises:
        PredictError: 못 찾거나 **둘 이상 맞는** 경우. 모호한 채로 하나를 고르면
            프롬프트에 들어간 `source_name` 이 무엇인지 나중에 알 수 없다.
    """
    host = _registrable(urlsplit(source_url).hostname or "")
    if not host:
        raise PredictError(f"호스트를 읽을 수 없습니다: {source_url!r}")

    matches = sorted(
        {
            str(src["name"])
            for src in rss_sources(config)
            if src.get("name")
            and src.get("url")
            and _registrable(urlsplit(str(src["url"])).hostname or "") == host
        }
    )
    if not matches:
        raise PredictError(
            f"{source_url!r} 의 호스트({host})에 해당하는 소스가 config.yaml 에 없습니다. "
            "source_name 을 유도할 수 없습니다"
        )
    if len(matches) > 1:
        raise PredictError(
            f"{source_url!r} 의 호스트({host})에 소스가 여럿 맞습니다: {matches}. "
            "어느 이름이 프롬프트로 나갈지 확정할 수 없습니다"
        )
    return matches[0]


def resolve_published_at(item_id: str) -> date:
    """골든셋 id 의 `YYYYMMDD` 접두사를 발행일로 읽는다.

    Raises:
        PredictError: 접두사가 날짜가 아닌 경우.
    """
    head = item_id.split("-", 1)[0]
    try:
        return datetime.strptime(head, "%Y%m%d").date()
    except ValueError as exc:
        raise PredictError(
            f"골든셋 id {item_id!r} 앞에서 날짜를 읽을 수 없습니다 — "
            "published_at 을 유도할 수 없습니다"
        ) from exc


def raw_item_from_golden(golden: GoldenItem, config: dict[str, Any]) -> tuple[RawItem, dict[str, str]]:
    """골든셋 1건 → `RawItem` + **무엇을 유도했는지**.

    제목·본문·URL 은 골든셋에 있는 값 그대로 쓴다. **한 글자도 손보지 않는다** —
    정규화하면 그 순간 입력이 달라지고, 이 실행이 재는 것이 달라진다.
    """
    source_name = resolve_source_name(golden.source_url, config)
    published_at = resolve_published_at(golden.id)
    item = RawItem(
        url=golden.source_url,
        title=golden.input.title,
        body=golden.input.body,
        source_name=source_name,
        published_at=published_at,
    )
    derived = {
        "source_name": f"{source_name} (config.yaml 의 호스트 일치에서 유도)",
        "published_at": f"{published_at.isoformat()} (골든셋 id 의 날짜 접두사에서 유도)",
    }
    return item, derived


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
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


def _persist_raw(client: Any, item_id: str, raw_dir: Path) -> dict[str, Any]:
    """응답 원본을 **변환하기 전에** 내린다 (D-052).

    `export/replay.py: _persist_raw` 와 같은 규칙이다. 토큰 비용이 0 이어도
    같다 — 규칙을 비용으로 정당화하면 비용이 0 인 곳에서 규칙이 사라진다.
    잃는 것은 돈이 아니라 **같은 입력에 대한 그 모델의 응답**이다.
    """
    raws = list(getattr(client, "last_raw_responses", []) or [])
    if not raws:
        # 전송이 실패해 응답이 하나도 없다. 빈 파일을 남기지 않는다 — 다음
        # 실행이 그걸 "응답 0건짜리 결과"로 읽는다.
        return {}
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in item_id)
    _write_json(raw_dir / f"{safe}.json", raws)
    # finish_reason 은 "모델이 스키마를 못 지켰다"와 "토큰이 모자라 잘렸다"를
    # 가르는 유일한 신호다. 섞으면 실패율이 모델이 아니라 max_tokens 를 잰다.
    return {
        "finish_reasons": [
            (r.get("choices") or [{}])[0].get("finish_reason") for r in raws
        ]
    }


def predict_one(
    golden: GoldenItem,
    client: Any,
    prompt: Any,
    config: dict[str, Any],
    raw_dir: Path,
) -> dict[str, Any]:
    """1건 추출. **실패해도 예외를 밖으로 내지 않고 결과에 적는다.**

    스키마 실패는 오류가 아니라 측정 대상이다 — 한 건이 터졌다고 실행이 멈추면
    실패율을 셀 수 없다. 단, 입력 유도 실패(`PredictError`)는 다르다. 그건
    **무엇을 보냈는지 모르는 상태**라 측정 자체가 성립하지 않으므로 올린다.
    """
    item, derived = raw_item_from_golden(golden, config)
    system, user = prompt.render(**build_variables(item))

    row: dict[str, Any] = {
        "item_id": golden.id,
        "url": str(item.url),
        "source_name": item.source_name,
        "derived_inputs": derived,
        "prompt_name": prompt.name,
        "prompt_sha256": prompt_sha256(prompt.name, prompt_dir=PROMPT_DIR),
        "model": getattr(client, "model", None),
        "draft_source": golden.draft_source,
        "labeling_guideline": golden.labeling_guideline,
    }

    try:
        try:
            result = client.parse_into(system=system, user=user, output_model=NewsOntology)
        finally:
            # --- 원본을 먼저 내린다 (D-052) ---------------------------------
            # 아래 `row |=` 들보다 **앞**이어야 한다. 행을 만드는 것이 곧 변환이고,
            # 검증되지 않은 변환 코드와 다시 만들 수 없는 결과를 같은 트랜잭션에
            # 두지 않는다.
            row |= _persist_raw(client, golden.id, raw_dir)
    except SchemaMismatchError as exc:
        row |= {
            "ok": False,
            "failure": SCHEMA_FAILURE,
            "attempts": exc.attempts,
            "last_error": str(exc.last_error)[:2000],
            "ontology": None,
        }
    except Exception as exc:  # noqa: BLE001 — 전송 계층 실패도 결과에 남긴다
        # 전송 실패는 **모델에게 물어보지 못한 건**이다. 스키마 실패와 같은 칸에
        # 세면 회선이 나쁜 날의 실행이 "모델이 계약을 못 지킨다"로 기록된다 (D-075).
        row |= {
            "ok": False,
            "failure": type(exc).__name__,
            "last_error": str(exc)[:2000],
            "ontology": None,
        }
    else:
        row |= {
            "ok": True,
            "failure": None,
            "attempts": result.attempts,
            "usage": asdict(result.usage),
            "ontology": result.value.model_dump(mode="json", by_alias=True),
        }
    return row


def failure_kind(row: dict[str, Any]) -> str | None:
    """`None` = 성공 / `"schema"` = 계약 위반 / `"transport"` = **못 물어봤다.**

    `export/replay.py: failure_kind` 와 같은 판정이다.
    """
    if row.get("ok"):
        return None
    return "schema" if row.get("failure") == SCHEMA_FAILURE else "transport"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """행 목록 → 집계. 순수 함수라 저장된 결과로 다시 계산할 수 있다.

    **전송 실패는 분모에서 뺀다.** 남겨 두면 회선이 나쁜 날일수록 스키마
    실패율이 낮게 나온다 — 재지 못한 것이 잘한 것으로 읽힌다 (D-050).
    """
    kinds = [failure_kind(r) for r in rows]
    transport = sum(1 for k in kinds if k == "transport")
    schema = sum(1 for k in kinds if k == "schema")
    measured = len(rows) - transport
    truncated = sum(
        1 for r in rows if "length" in (r.get("finish_reasons") or [])
    )
    return {
        "item_count": len(rows),
        "measured_items": measured,
        "transport_failures": transport,
        "schema_failures": schema,
        # 전부 전송 실패면 `0.0` 이 아니라 None 이다 — 0% 는 "다 성공했다"로 읽힌다.
        "schema_failure_rate": (schema / measured) if measured else None,
        "truncated": truncated,
        "retried": sum(max(0, (r.get("attempts") or 1) - 1) for r in rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="골든셋 입력으로 재추출해 채점용 예측 JSONL 을 만든다"
    )
    parser.add_argument("--golden-set", default=str(DEFAULT_GOLDEN_SET_DIR))
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="예측 JSONL 경로")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--include-drafts", action="store_true", help="초안도 추출 (권장하지 않음)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="무엇이 나갈지만 출력한다. **호출 0건** — 게이트 앞에서 건수를 확인할 때 쓴다",
    )
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    config = load_config()
    golden_items = load_golden_set(args.golden_set, include_drafts=args.include_drafts)
    prompt = load_prompt(args.prompt)

    if args.dry_run:
        print(f"골든셋 {len(golden_items)}건 · 프롬프트 {prompt.name}")
        print(f"예상 호출: {len(golden_items)}건 (재시도 제외)")
        for golden in golden_items:
            item, derived = raw_item_from_golden(golden, config)
            print(f"\n- {golden.id}")
            print(f"    url          = {item.url}")
            print(f"    source_name  = {derived['source_name']}")
            print(f"    published_at = {derived['published_at']}")
            print(f"    본문 {len(item.body)}자 / 제목 {len(item.title)}자")
        print("\n호출 0건. 실제 실행은 --dry-run 없이.")
        return 0

    out_path = Path(args.out)
    raw_dir = out_path.parent / "raw"
    client = client_from_config(config, stage="extraction")

    rows = [predict_one(g, client, prompt, config, raw_dir) for g in golden_items]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            if row.get("ontology") is None:
                continue
            f.write(
                json.dumps(
                    {"item_id": row["item_id"], "ontology": row["ontology"]},
                    ensure_ascii=False,
                )
                + "\n"
            )

    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": ((config.get("llm") or {}).get("provider")),
        "model": getattr(client, "model", None),
        "prompt_name": prompt.name,
        "prompt_sha256": prompt_sha256(prompt.name, prompt_dir=PROMPT_DIR),
        # 설정에 남아 있지만 vLLM 으로 나가지 않은 벤더 전용 파라미터 (D-080).
        # 알리지 않으면 effort 가 걸린 실행과 안 걸린 실행이 구분되지 않는다.
        "omitted_vendor_params": list(getattr(client, "omitted_vendor_params", ()) or ()),
        "golden_set": args.golden_set,
        **summarize(rows),
    }
    _write_json(out_path.parent / "predict-meta.json", meta)
    _write_json(out_path.parent / "predict-rows.json", rows)

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"\n예측 {sum(1 for r in rows if r.get('ontology'))}건 → {out_path}")
    print(f"응답 원본 → {raw_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
