"""runner 의 원본 선기록과 회차별 즉시 쓰기 (D-052 를 runner 에도, D-092).

session-13 에서 v4 judge 가 4회 연속 실패하고 실행을 중단했는데 **실패 사유가 하나도
남지 않았다.** 응답은 클라이언트의 `last_raw_responses` 에만, 결과는 메모리에만 있었고
결과 파일은 실행 끝에 한 번에 쓰였다. `eval/predict.py`·`export/replay.py` 는 이미 지키던
규칙이다 — 같은 레포 안에서 API 를 부르는 경로 하나가 빠져 있었다.

실제 API 를 부르지 않는다. 응답 원본의 모양은 OpenAI 호환 응답을 흉내 낸다.
"""

from __future__ import annotations

import json

import pytest

from eval.runner import append_score, evaluate_item, score_items, summarize, write_scores
from eval.schema import GoldenItem
from extraction.llm import StructuredResult, Usage
from extraction.schema import NewsOntology
from tests.test_eval_runner import GOLDEN_PAYLOAD, JUDGEMENT_PAYLOAD, ONTOLOGY_PAYLOAD


def _response(content: str, finish: str) -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


class RawJudge:
    """`VLLMClient` 처럼 `last_raw_responses` 를 남기는 가짜.

    `script` 의 원소마다 한 회차: `"ok"` 는 성공, `"length"` 는 두 번 잘려 실패,
    `"interrupt"` 는 응답 하나를 받은 뒤 사용자가 중단한 경우다.
    """

    model = "fake-judge"

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.last_raw_responses: list[dict] = []
        self.calls = 0

    def parse_into(self, *, system, user, output_model):
        self.calls += 1
        step = self.script.pop(0)
        self.last_raw_responses = []
        if step == "ok":
            self.last_raw_responses.append(_response(json.dumps(JUDGEMENT_PAYLOAD), "stop"))
            return StructuredResult(
                value=output_model.model_validate(JUDGEMENT_PAYLOAD),
                usage=Usage(input_tokens=1, output_tokens=1, model=self.model),
            )
        if step == "length":
            self.last_raw_responses += [_response('{"faithfulness": {"rati', "length")] * 2
            raise ValueError("스키마 검증 실패 (2회)")
        if step == "interrupt":
            self.last_raw_responses.append(_response('{"faith', "length"))
            raise KeyboardInterrupt
        raise AssertionError(step)


@pytest.fixture
def golden() -> GoldenItem:
    return GoldenItem.model_validate(GOLDEN_PAYLOAD)


@pytest.fixture
def ontology() -> NewsOntology:
    return NewsOntology.model_validate(ONTOLOGY_PAYLOAD)


def test_raw_is_written_on_success(golden, ontology, tmp_path):
    score = evaluate_item(golden, ontology, judge_client=RawJudge(["ok"]), raw_dir=tmp_path)

    saved = json.loads((tmp_path / f"{golden.id}.r00.json").read_text(encoding="utf-8"))
    assert saved[0]["choices"][0]["finish_reason"] == "stop"
    assert score.judge_finish_reasons == ["stop"]


def test_raw_is_written_on_failure_with_finish_reasons(golden, ontology, tmp_path):
    """재현 — 실패한 회차의 응답과 사유가 남는다. session-13 에서 잃은 것이 이것이다."""
    score = evaluate_item(
        golden, ontology, judge_client=RawJudge(["length"]), raw_dir=tmp_path, repeat_index=3
    )

    assert score.judgement is None
    assert score.judge_finish_reasons == ["length", "length"]
    assert any("judge 실패" in e for e in score.errors)
    assert (tmp_path / f"{golden.id}.r03.json").exists()


def test_failed_row_records_rubric_name(golden, ontology, tmp_path):
    from eval.runner import load_judge_prompt

    prompt = load_judge_prompt("summary_quality.v3.md")
    score = evaluate_item(
        golden, ontology, judge_client=RawJudge(["length"]), judge_prompt=prompt, raw_dir=tmp_path
    )
    assert score.metadata.judge_prompt == "summary_quality.v3.md"


def test_raw_survives_interrupt(golden, ontology, tmp_path):
    """`except Exception` 은 중단을 잡지 않는다 — 그래서 `finally` 다."""
    with pytest.raises(KeyboardInterrupt):
        evaluate_item(golden, ontology, judge_client=RawJudge(["interrupt"]), raw_dir=tmp_path)
    assert (tmp_path / f"{golden.id}.r00.json").exists()


def test_no_file_without_responses(golden, ontology, tmp_path):
    class NoRaw(RawJudge):
        def parse_into(self, **kwargs):
            self.last_raw_responses = []
            raise ConnectionError("전송 실패")

    evaluate_item(golden, ontology, judge_client=NoRaw([]), raw_dir=tmp_path)
    assert list(tmp_path.iterdir()) == [], "응답이 없는데 빈 파일이 남았다"


def test_no_raw_without_raw_dir(golden, ontology, tmp_path):
    score = evaluate_item(golden, ontology, judge_client=RawJudge(["ok"]))
    assert score.judge_finish_reasons == []


def test_each_repeat_is_on_disk_before_the_next(golden, ontology, tmp_path):
    """재현 — 3회차에서 중단돼도 앞의 2회차는 결과 파일에 있다."""
    judge = RawJudge(["ok", "length", "interrupt"])
    with pytest.raises(KeyboardInterrupt):
        score_items(
            [golden], {golden.id: ontology},
            run_id="r1", scores_dir=tmp_path, judge_client=judge, repeat=3,
        )

    lines = (tmp_path / "r1.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    assert [r["repeat_index"] for r in rows] == [0, 1]
    assert rows[1]["judge_finish_reasons"] == ["length", "length"]
    assert all(r["type"] == "item" for r in rows), "중단된 실행에는 summary 줄이 없어야 한다"
    raw_dir = tmp_path / "raw" / "judge-r1"
    assert sorted(p.name for p in raw_dir.iterdir()) == [
        f"{golden.id}.r00.json", f"{golden.id}.r01.json", f"{golden.id}.r02.json",
    ]


def test_completed_run_ends_with_summary(golden, ontology, tmp_path):
    scores = score_items(
        [golden], {golden.id: ontology},
        run_id="r2", scores_dir=tmp_path, judge_client=RawJudge(["ok", "ok"]), repeat=2,
    )
    write_scores(scores, summarize(scores, run_id="r2"), scores_dir=tmp_path)

    rows = [json.loads(l) for l in (tmp_path / "r2.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["type"] for r in rows] == ["item", "item", "summary"], "줄이 중복되거나 빠졌다"


def test_contrast_only_run_writes_no_raw(golden, ontology, tmp_path):
    score_items([golden], {golden.id: ontology}, run_id="r3", scores_dir=tmp_path)
    assert not (tmp_path / "raw").exists()


def test_append_score_is_one_line(golden, ontology, tmp_path):
    path = tmp_path / "x.jsonl"
    append_score(evaluate_item(golden, ontology), path)
    append_score(evaluate_item(golden, ontology), path)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
