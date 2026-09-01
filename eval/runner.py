"""골든셋 대조 + LLM-judge 채점 실행기.

## 두 축은 코드에서도 분리돼 있다 (D-006)

| 함수 | 축 | API 호출 |
|---|---|---|
| `compare_ontology` | 통제어휘 필드 대조 | **없음.** 순수 함수 |
| `judge_summary` | 요약 품질 | 있음 (`eval.judge_model`) |
| `evaluate_item` | 둘을 합침 | judge 클라이언트를 넘길 때만 |

`judge_client=None` 이면 대조만 하고 요약 채점은 건너뛴다. 이게 기본값인 이유는
CLAUDE.md 의 API 호출 규칙 때문이다 — 채점기를 돌리는 것만으로 비용이 발생하면
안 되고, 실호출은 명시적으로 켜야 한다.

## 실행

```bash
# 대조만 (API 호출 없음)
python -m eval.runner --predictions eval/scores/preds.jsonl --no-judge

# judge 포함 (실제 API 호출 — CLAUDE.md 규칙에 따라 사전 확인 필요)
python -m eval.runner --predictions eval/scores/preds.jsonl --judge
```
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from eval.analysis import repeat_stats
from eval.schema import (
    FieldScore,
    GoldenItem,
    ItemScore,
    JudgeUsage,
    RunMetadata,
    RunSummary,
    SummaryJudgement,
)
from extraction.extractor import Prompt, load_prompt
from extraction.llm import LLMClient, StructuredResult
from extraction.normalize import normalization_key
from extraction.schema import NewsOntology

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JUDGE_PROMPT_DIR = PROJECT_ROOT / "eval" / "judge_prompts"
DEFAULT_JUDGE_PROMPT = "summary_quality.v3.md"
DEFAULT_GOLDEN_SET_DIR = PROJECT_ROOT / "eval" / "golden_set"
DEFAULT_SCORES_DIR = PROJECT_ROOT / "eval" / "scores"


class EvalError(Exception):
    """채점을 진행할 수 없다."""


# ---------------------------------------------------------------------------
# 골든셋 로딩
# ---------------------------------------------------------------------------
def load_golden_set(
    directory: Path | str = DEFAULT_GOLDEN_SET_DIR,
    *,
    include_drafts: bool = False,
) -> list[GoldenItem]:
    """골든셋 디렉터리의 `*.json` 을 읽는다.

    **기본적으로 `status: draft` 는 제외한다.** 초안은 모델 출력을 옮겨 적은
    것이라 정답이 아니고, 섞이면 eval 이 모델의 자기 채점이 된다(D-039).
    `TEMPLATE.json` 처럼 밑줄/대문자로 시작하는 파일은 라벨링 서식이므로 건너뛴다.

    Raises:
        EvalError: JSON 이나 스키마가 깨진 파일이 있는 경우. 조용히 넘기지
            않는다 — 정답 세트가 부분적으로 로드되면 accuracy 가 거짓말을 한다.
    """
    directory = Path(directory)
    items: list[GoldenItem] = []
    drafts: list[str] = []

    for path in sorted(directory.glob("*.json")):
        if path.stem.startswith(("_", "TEMPLATE")):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvalError(f"{path.name}: 읽을 수 없습니다 ({exc})") from exc
        try:
            item = GoldenItem.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError 포함
            raise EvalError(f"{path.name}: 골든셋 스키마에 맞지 않습니다\n{exc}") from exc

        if item.status != "confirmed" and not include_drafts:
            drafts.append(path.name)
            continue
        items.append(item)

    if drafts:
        print(
            f"[info] 초안 {len(drafts)}건을 건너뜁니다 (사람 확정 대기): "
            f"{', '.join(drafts)}",
            file=sys.stderr,
        )
    return items


# ---------------------------------------------------------------------------
# 축 1. 골든셋 대조 (API 호출 없음)
# ---------------------------------------------------------------------------
def _key_set(values: Iterable[str]) -> set[str]:
    """표기 노이즈를 흡수한 비교용 집합.

    `normalize.py` 와 **같은 동치 관계**를 쓴다. 정규화가 흡수하는 차이를
    채점기가 오답으로 세면, 사전을 고쳐야 할 문제를 모델 탓으로 돌리게 된다.
    """
    return {normalization_key(v) for v in values if v}


def _jaccard(expected: set[str], actual: set[str]) -> float:
    if not expected and not actual:
        return 1.0
    union = expected | actual
    return len(expected & actual) / len(union) if union else 1.0


def compare_ontology(golden: GoldenItem, actual: NewsOntology) -> list[FieldScore]:
    """정답 라벨과 모델 출력을 필드별로 대조한다. **순수 함수다.**

    필드마다 채점 방식이 다른 이유는 필드의 성질이 다르기 때문이다(D-040).
    `graded=False` 인 필드는 기록만 되고 임계값 판정에 들어가지 않는다.
    """
    expected = golden.expected
    scores: list[FieldScore] = []

    # 기술영역 — 다중값(최대 3). 완전일치만 보면 2/3 맞춘 것과 0/3 이 같아진다.
    exp_domains = {d.value for d in expected.tech_domains}
    act_domains = {d.value for d in actual.tech_domains}
    scores.append(
        FieldScore(
            field="기술영역",
            expected=sorted(exp_domains),
            actual=sorted(act_domains),
            score=round(_jaccard(exp_domains, act_domains), 4),
            method="jaccard",
        )
    )

    # 발표유형 — 단일값 통제어휘. 맞거나 틀리거나다.
    scores.append(
        FieldScore(
            field="발표유형",
            expected=expected.release_type.value,
            actual=actual.release_type.value,
            score=1.0 if expected.release_type == actual.release_type else 0.0,
            method="exact",
        )
    )

    # 영향도 — 1~5 순서형. 인접 오차를 완전 오답으로 치면 정보가 사라진다.
    if expected.impact is None:
        scores.append(
            FieldScore(
                field="영향도",
                expected=None,
                actual=actual.impact.score,
                score=0.0,
                method="ordinal",
                graded=False,
                note="정답 점수가 비어 있어 채점하지 않았다",
            )
        )
    else:
        diff = abs(expected.impact.score - actual.impact.score)
        scores.append(
            FieldScore(
                field="영향도",
                expected=expected.impact.score,
                actual=actual.impact.score,
                score={0: 1.0, 1: 0.5}.get(diff, 0.0),
                method="ordinal(±1=0.5)",
                note=f"절대오차 {diff}",
            )
        )

    # 관련기업 — 정규화된 대표명으로 비교한다(D-030 과 같은 이유).
    exp_companies = _key_set(expected.companies)
    act_companies = _key_set(c.canonical or c.raw for c in actual.companies)
    scores.append(
        FieldScore(
            field="관련기업",
            expected=sorted(expected.companies),
            actual=sorted(c.canonical or c.raw for c in actual.companies),
            score=round(_jaccard(exp_companies, act_companies), 4),
            method="jaccard(canonical)",
            graded=False,
            note="엔티티 경계(오픈소스 프로젝트 포함 여부)가 아직 관찰 중이라 기록만 한다",
        )
    )

    # 관련기존기술 — 자유 태그. 정답 집합이 닫히지 않아 accuracy 에 넣지 않는다.
    exp_prior = _key_set(expected.prior_art)
    act_prior = _key_set(actual.prior_art)
    scores.append(
        FieldScore(
            field="관련기존기술",
            expected=sorted(expected.prior_art),
            actual=sorted(actual.prior_art),
            score=round(_jaccard(exp_prior, act_prior), 4),
            method="jaccard",
            graded=False,
            note="자유 태그라 정답 집합이 닫히지 않는다 — 추이만 본다",
        )
    )

    return scores


# ---------------------------------------------------------------------------
# 축 2. LLM-judge (API 호출)
# ---------------------------------------------------------------------------
def build_judge_variables(golden: GoldenItem, summary: str) -> dict[str, Any]:
    """judge 프롬프트 치환 변수.

    `source_text` 는 제목과 본문을 합친 것이다. 판정 근거가 되는 원문이 곧
    골든셋의 `input` 이어야, 나중에 같은 점수를 재현할 수 있다.
    """
    body = golden.input.body.strip()
    source_text = golden.input.title if not body else f"{golden.input.title}\n\n{body}"
    return {"source_text": source_text, "summary": summary}


def judge_summary(
    golden: GoldenItem,
    summary: str,
    client: LLMClient,
    *,
    prompt: Prompt | None = None,
) -> tuple[SummaryJudgement, str, JudgeUsage]:
    """요약 1건을 루브릭으로 채점한다. **실제 API 호출이 일어난다.**

    사용량을 함께 돌려주는 이유는 비용이 이 프로젝트의 실질적인 제약이기
    때문이다(D-019). 채점 1회가 얼마였는지 결과에 남지 않으면, eval 을 매
    실행마다 돌릴지 프롬프트 변경 시에만 돌릴지 판단할 근거가 없다.

    Returns:
        `(채점 결과, 사용한 프롬프트 파일명, 사용량)`
    """
    prompt = prompt or load_judge_prompt()
    system, user = prompt.render(**build_judge_variables(golden, summary))

    result: StructuredResult[SummaryJudgement] = client.parse_into(
        system=system, user=user, output_model=SummaryJudgement
    )
    usage = result.usage
    return (
        result.value,
        prompt.name,
        JudgeUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            thinking_tokens=usage.thinking_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            latency_s=usage.latency_s,
        ),
    )


def load_judge_prompt(filename: str = DEFAULT_JUDGE_PROMPT) -> Prompt:
    """judge 루브릭을 읽는다. 추출 프롬프트와 같은 로더·같은 관례를 쓴다."""
    return load_prompt(filename, prompt_dir=JUDGE_PROMPT_DIR)


def prompt_sha256(filename: str, *, prompt_dir: Path = JUDGE_PROMPT_DIR) -> str | None:
    """프롬프트 파일 해시. 파일명만으로는 내용이 바뀐 걸 잡지 못한다."""
    path = prompt_dir / filename
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def judge_client_from_config(config: Mapping[str, Any], **overrides: Any):
    """`eval.judge_model` 로 judge 클라이언트를 만든다.

    judge 모델은 `llm:` 이 아니라 `eval:` 블록에 있다 — 단계별 모델(D-015)과
    달리 이건 파이프라인이 아니라 **채점 설정**이기 때문이다.

    `temperature` 는 설정하지 않는다. 현재 judge 모델(Opus 5)이 이 파라미터를
    400 으로 거부하기 때문이고(D-033), 그래서 **게이트(D-032)처럼 판정을
    결정적으로 만들 수단이 지금은 없다.** 같은 요약을 두 번 채점하면 점수가
    갈릴 수 있으므로, 추이를 볼 때 1점 차이를 유의미하게 읽으면 안 된다.
    """
    from extraction.llm import AnthropicClient

    eval_cfg = (config or {}).get("eval") or {}
    llm_cfg = (config or {}).get("llm") or {}
    model = eval_cfg.get("judge_model") or llm_cfg.get("judge_model") or "claude-opus-5"

    kwargs: dict[str, Any] = {"model": model, "max_tokens": 4000, "effort": "high"}
    kwargs.update(overrides)
    return AnthropicClient(**kwargs)


# ---------------------------------------------------------------------------
# 한 건 채점
# ---------------------------------------------------------------------------
def evaluate_item(
    golden: GoldenItem,
    actual: NewsOntology,
    *,
    judge_client: LLMClient | None = None,
    judge_prompt: Prompt | None = None,
    extraction_model: str | None = None,
    extraction_prompt: str | None = None,
    repeat_index: int = 0,
) -> ItemScore:
    """골든셋 1건 + 모델 출력 1건 -> 채점 결과.

    `judge_client` 가 None 이면 **API 호출 없이** 대조만 한다. judge 호출이
    실패해도 대조 결과는 살린다 — 요약 채점을 못 했다고 필드 정확도까지 잃을
    이유가 없다.
    """
    field_scores = compare_ontology(golden, actual)

    judgement: SummaryJudgement | None = None
    judge_prompt_name: str | None = None
    judge_usage: JudgeUsage | None = None
    errors: list[str] = []

    if judge_client is not None:
        try:
            judgement, judge_prompt_name, judge_usage = judge_summary(
                golden, actual.summary, judge_client, prompt=judge_prompt
            )
        except Exception as exc:
            errors.append(f"judge 실패: {type(exc).__name__}: {exc}")

    if judgement is not None and golden.human_summary_scores is None:
        errors.append("사람 점수가 없어 judge 자기 편향을 확인할 수 없다")

    return ItemScore(
        item_id=golden.id,
        source_url=golden.source_url,
        repeat_index=repeat_index,
        field_scores=field_scores,
        judgement=judgement,
        judge_usage=judge_usage,
        human_summary_scores=golden.human_summary_scores,
        metadata=RunMetadata(
            extraction_model=extraction_model,
            extraction_prompt=extraction_prompt,
            extraction_prompt_sha256=(
                prompt_sha256(extraction_prompt, prompt_dir=PROJECT_ROOT / "extraction" / "prompts")
                if extraction_prompt
                else None
            ),
            judge_model=getattr(judge_client, "model", None) if judge_client else None,
            judge_prompt=judge_prompt_name,
            judge_prompt_sha256=prompt_sha256(judge_prompt_name) if judge_prompt_name else None,
        ),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------
def summarize(
    scores: Sequence[ItemScore],
    *,
    run_id: str,
    thresholds: Mapping[str, float] | None = None,
) -> RunSummary:
    """실행 전체를 집계하고 임계값 판정을 낸다.

    표본이 없는 지표는 **0 이 아니라 None** 이다. "점수가 0점"과 "잴 수 없었다"를
    같은 값으로 표현하면 임계값 판정이 거짓말을 한다.
    """
    thresholds = dict(thresholds or {})
    notes: list[str] = []

    # 반복 채점은 **같은 항목을 여러 번 부른 것**이므로 항목 수로 세지 않는다.
    # 10회 반복을 10건으로 세면 표본이 늘어난 것처럼 보이는데, 늘어난 것은
    # judge 호출 횟수이지 골든셋이 아니다 (D-043 이 경계한 바로 그 착시).
    item_ids = list(dict.fromkeys(s.item_id for s in scores))
    per_item = {
        item_id: [s for s in scores if s.item_id == item_id] for item_id in item_ids
    }
    stats = [repeat_stats(rows, item_id=item_id) for item_id, rows in per_item.items()]
    judge_calls = sum(1 for s in scores if s.judgement is not None)
    repeat = max((len(rows) for rows in per_item.values()), default=1)

    # 필드 대조는 API 를 부르지 않아 반복 회차마다 같은 값이 나온다. 회차를 그대로
    # 평균에 넣으면 항목 하나가 반복 횟수만큼의 가중치를 갖게 되므로, **항목 안에서
    # 먼저 평균을 낸 뒤 항목끼리 평균**한다. 반복이 없으면 기존 동작과 같다.
    accuracies: list[float] = []
    for rows in per_item.values():
        row_scores = [r.field_accuracy for r in rows if r.field_accuracy is not None]
        if row_scores:
            accuracies.append(sum(row_scores) / len(row_scores))
    field_accuracy = round(sum(accuracies) / len(accuracies), 4) if accuracies else None

    faiths = [s.judgement.faithfulness.score for s in scores if s.judgement is not None]
    faithfulness = round(sum(faiths) / len(faiths), 4) if faiths else None

    if not scores:
        notes.append("채점된 항목이 없다 — 골든셋에 confirmed 항목이 있는지 확인할 것")
    if faithfulness is None and scores:
        notes.append("judge 를 돌리지 않아 요약 품질은 판정하지 않았다")
    if any(s.human_summary_scores is None for s in scores):
        notes.append("사람 점수가 없는 항목이 있어 judge 자기 편향은 확인되지 않았다")

    passed: dict[str, bool] = {}
    if field_accuracy is not None and "field_accuracy" in thresholds:
        passed["field_accuracy"] = field_accuracy >= thresholds["field_accuracy"]
    if faithfulness is not None and "summary_faithfulness" in thresholds:
        passed["summary_faithfulness"] = faithfulness >= thresholds["summary_faithfulness"]

    if repeat > 1:
        notes.append(
            f"항목당 {repeat}회 반복 채점 — 골든셋은 {len(item_ids)}건이다. "
            "반복은 judge 흔들림 폭(D-042)을 재는 것이지 표본을 늘리는 것이 아니다"
        )

    return RunSummary(
        run_id=run_id,
        item_count=len(item_ids),
        judge_call_count=judge_calls,
        repeat=repeat,
        repeat_stats=stats,
        field_accuracy=field_accuracy,
        summary_faithfulness=faithfulness,
        thresholds=thresholds,
        passed=passed,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------
def write_scores(
    scores: Sequence[ItemScore],
    summary: RunSummary,
    *,
    scores_dir: Path | str = DEFAULT_SCORES_DIR,
) -> Path:
    """`eval/scores/{run_id}.jsonl` 로 원자적으로 쓴다.

    마지막 줄이 집계(`"type": "summary"`)다. 항목별 줄과 같은 파일에 두는 이유:
    집계만 따로 두면 어떤 항목 집합에서 나온 수치인지 나중에 확인할 수 없다.
    """
    scores_dir = Path(scores_dir)
    scores_dir.mkdir(parents=True, exist_ok=True)
    path = scores_dir / f"{summary.run_id}.jsonl"

    lines = [
        json.dumps({"type": "item", **s.model_dump(mode="json")}, ensure_ascii=False)
        for s in scores
    ]
    lines.append(
        json.dumps({"type": "summary", **summary.model_dump(mode="json")}, ensure_ascii=False)
    )

    fd, tmp_name = tempfile.mkstemp(dir=scores_dir, prefix=".tmp-", suffix=".jsonl")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def new_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def load_predictions(path: Path | str) -> dict[str, NewsOntology]:
    """`{item_id: NewsOntology}` 형태의 모델 출력을 읽는다.

    JSONL 한 줄이 `{"item_id": ..., "ontology": {...}}` 다. 추출을 다시 돌리지
    않고 채점만 반복할 수 있어야 한다 — 재추출은 매번 비용이고, 채점 로직을
    고칠 때마다 API 를 부르는 건 CLAUDE.md 규칙과 어긋난다.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"예측 파일을 열 수 없습니다: {path} ({exc.strerror})") from exc

    predictions: dict[str, NewsOntology] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            predictions[row["item_id"]] = NewsOntology.model_validate(row["ontology"])
        except Exception as exc:
            raise EvalError(f"{path.name}:{lineno} 예측을 읽을 수 없습니다 ({exc})") from exc
    return predictions


def load_config(path: Path | str = PROJECT_ROOT / "config.yaml") -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="골든셋 대조 + (선택) LLM-judge 채점")
    parser.add_argument("--golden-set", default=None, help="골든셋 디렉터리")
    parser.add_argument("--predictions", required=True, help="모델 출력 JSONL")
    parser.add_argument("--scores-dir", default=None, help="결과를 쓸 디렉터리")
    parser.add_argument("--include-drafts", action="store_true", help="초안도 채점 (권장하지 않음)")
    parser.add_argument(
        "--judge-prompt",
        default=DEFAULT_JUDGE_PROMPT,
        help=f"judge 루브릭 파일명 (기본 {DEFAULT_JUDGE_PROMPT}). 루브릭 간 비교에 쓴다",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "항목당 judge 반복 호출 횟수. judge 는 결정적이지 않으므로(D-042) "
            "흔들림 폭을 보려면 2회 이상이 필요하다. **호출 수·비용이 배로 늘어난다**"
        ),
    )
    judge = parser.add_mutually_exclusive_group()
    judge.add_argument(
        "--judge",
        action="store_true",
        help="요약 품질까지 채점한다. **실제 API 호출이 일어난다**",
    )
    judge.add_argument("--no-judge", action="store_true", help="대조만 한다 (기본)")
    args = parser.parse_args(argv)

    # 결과에 한국어와 em-dash 가 섞여 있는데 Windows 기본 콘솔은 cp949 다.
    # 재설정하지 않으면 **채점이 끝나고 파일까지 쓴 뒤** 출력에서만 죽는다.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    config = load_config()
    eval_cfg = config.get("eval") or {}
    golden_dir = args.golden_set or eval_cfg.get("golden_set_path") or DEFAULT_GOLDEN_SET_DIR
    scores_dir = args.scores_dir or eval_cfg.get("scores_path") or DEFAULT_SCORES_DIR

    try:
        golden_items = load_golden_set(golden_dir, include_drafts=args.include_drafts)
        predictions = load_predictions(args.predictions)
    except EvalError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1

    if not golden_items:
        print(
            "[중단] 채점할 confirmed 골든셋 항목이 없습니다. "
            "초안을 확정하거나 --include-drafts 로 확인만 해 보세요.",
            file=sys.stderr,
        )
        return 1

    if args.repeat < 1:
        print("[중단] --repeat 는 1 이상이어야 합니다", file=sys.stderr)
        return 1
    if args.repeat > 1 and not args.judge:
        print(
            "[중단] --repeat 는 --judge 와 함께 씁니다. 필드 대조는 API 를 부르지 않아 "
            "회차마다 같은 값이 나옵니다",
            file=sys.stderr,
        )
        return 1

    try:
        judge_prompt = load_judge_prompt(args.judge_prompt) if args.judge else None
    except (OSError, ValueError) as exc:
        print(f"[중단] 루브릭을 읽을 수 없습니다: {exc}", file=sys.stderr)
        return 1

    judge_client = judge_client_from_config(config) if args.judge else None
    if judge_client is not None:
        gradable = sum(1 for i in golden_items if i.id in predictions)
        print(
            f"[judge] model={judge_client.model} rubric={args.judge_prompt} "
            f"항목={gradable}건 x {args.repeat}회 = 호출 {gradable * args.repeat}회 — 실제 API 호출",
            file=sys.stderr,
        )

    scores: list[ItemScore] = []
    for item in golden_items:
        actual = predictions.get(item.id)
        if actual is None:
            print(f"[skip] {item.id}: 예측이 없습니다", file=sys.stderr)
            continue
        for index in range(args.repeat):
            score = evaluate_item(
                item,
                actual,
                judge_client=judge_client,
                judge_prompt=judge_prompt,
                repeat_index=index,
            )
            scores.append(score)
            if judge_client is not None:
                # 회차마다 즉시 보고한다. N회가 몇 분씩 걸리는데 끝까지 침묵하면
                # 중간에 실패가 누적돼도 알 수 없다.
                axis = score.judgement.completeness.score if score.judgement else "실패"
                print(f"  [{index + 1}/{args.repeat}] {item.id} 완결성={axis}", file=sys.stderr)

    summary = summarize(scores, run_id=new_run_id(), thresholds=eval_cfg.get("thresholds") or {})
    path = write_scores(scores, summary, scores_dir=scores_dir)

    print(json.dumps(summary.model_dump(mode="json"), ensure_ascii=False, indent=2))
    print(f"\n결과: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
