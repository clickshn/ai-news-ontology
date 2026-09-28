"""반복 채점 결과의 집계.

judge 는 결정적이지 않다(D-042). 그래서 같은 요약을 N회 채점한 뒤 **분포**로
읽어야 하는데, `runner.summarize` 는 평균만 낸다 — 평균만 보면 "3.0"이 매번
3이었는지 1과 5를 오간 결과인지 구분되지 않는다.

여기 있는 것은 전부 **순수 함수**다. API 를 부르지 않으므로 채점 결과 파일만
있으면 몇 번이든 다시 돌려 볼 수 있고, 집계 기준을 고치는 데 비용이 들지 않는다
(`judge` 호출과 대조 로직을 가른 것과 같은 이유 — `eval/README.md`).
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence

from eval.schema import AxisStats, ItemScore, RepeatStats
from extraction.vllm import count_length_stops

__all__ = [
    "SLOT_NAMES",
    "AXES",
    "axis_stats",
    "slot_verdicts",
    "slot_states",
    "stated_denominator",
    "completeness_from_states",
    "repeat_stats",
]

AXES = ("faithfulness", "completeness", "concision")

#: v3 루브릭이 정의한 핵심 정보 4슬롯 (D-045).
SLOT_NAMES = {1: "사건", 2: "범위", 3: "정도", 4: "경위"}

#: 근거에서 슬롯을 가리키는 표기. 모델이 원문자를 쓰지 않을 수 있어 몇 가지를 함께 받는다.
_SLOT_MARKERS = {
    1: r"①|\(1\)|\b1\s*번?\s*슬롯|슬롯\s*1\b",
    2: r"②|\(2\)|\b2\s*번?\s*슬롯|슬롯\s*2\b",
    3: r"③|\(3\)|\b3\s*번?\s*슬롯|슬롯\s*3\b",
    4: r"④|\(4\)|\b4\s*번?\s*슬롯|슬롯\s*4\b",
}

#: 미충족을 먼저 본다 — "미충족" 은 "충족" 을 부분 문자열로 포함한다.
_NEGATIVE = re.compile(r"미충족|불충족|미달|채우지\s*못|채우지\s*않|빠졌|없[다음]")
_POSITIVE = re.compile(r"충족|채웠|담[겼고]|밝[혔히]")

#: 슬롯 판정 구간의 끝. 다음 슬롯 마커, 또는 총계로 넘어가는 화살표에서 끊는다.
#
#: 고정 글자 수로 자르면 안 된다. 모델은 "②범위(avformat_open_input·av_read_frame 을
#: 신뢰할 수 없는 데이터에 호출하는 모든 FFmpeg 연동 애플리케이션) 미충족" 처럼 괄호에
#: 근거를 길게 달고 **판정어를 맨 뒤에** 둔다 — 창을 짧게 잡으면 판정어가 밖으로 밀려
#: '언급 없음' 으로 잘못 읽힌다. 실제로 첫 측정에서 이 때문에 준수율이 절반으로 나왔다.
_TALLY = re.compile(r"→|->")

#: 마커도 화살표도 더 없을 때의 backstop. 슬롯 표기 없는 산문으로 새는 것을 막는다.
_VERDICT_BACKSTOP = 200


def slot_verdicts(rationale: str) -> dict[int, bool | None]:
    """완결성 근거에서 슬롯별 충족 여부를 읽어낸다.

    **점수를 다시 계산하려는 것이 아니다.** v3 은 근거에 슬롯별 판정을 밝히라고
    요구하는데(D-045), 모델이 그 지시를 따랐는지를 점수와 **독립적으로** 보기
    위한 것이다. 지시를 안 따랐다면 점수가 몇이든 "루브릭이 틀렸다"가 아니라
    "루브릭이 적용되지 않았다"로 읽어야 하고, 두 경우의 후속 조치가 다르다.

    Returns:
        `{슬롯번호: True(충족) | False(미충족) | None(언급 없음/판정 불명)}`
    """
    verdicts: dict[int, bool | None] = {n: None for n in SLOT_NAMES}
    if not rationale:
        return verdicts

    # 마커 위치를 모두 모아 두고, 각 마커의 구간을 다음 마커 직전까지로 자른다.
    hits: list[tuple[int, int]] = []  # (위치, 슬롯번호)
    for slot, pattern in _SLOT_MARKERS.items():
        for match in re.finditer(pattern, rationale):
            hits.append((match.end(), slot))
    if not hits:
        return verdicts
    hits.sort()

    positions = [pos for pos, _ in hits]
    for index, (pos, slot) in enumerate(hits):
        nxt = positions[index + 1] if index + 1 < len(positions) else len(rationale)
        # 마지막 슬롯은 근거 끝까지 이어지는데, 그 뒤에 "→ 1슬롯만 충족" 같은 **총계**가
        # 붙는다. 총계의 '충족' 을 슬롯 판정으로 읽으면 미충족이 충족으로 뒤집힌다.
        tally = _TALLY.search(rationale, pos, nxt)
        end = min(nxt, tally.start() if tally else nxt, pos + _VERDICT_BACKSTOP)
        window = rationale[pos:end]
        if _NEGATIVE.search(window):
            verdict: bool | None = False
        elif _POSITIVE.search(window):
            verdict = True
        else:
            verdict = None
        # 같은 슬롯이 여러 번 언급되면 **판정이 있는 쪽**을 남긴다.
        if verdicts[slot] is None:
            verdicts[slot] = verdict
    return verdicts


#: v4 슬롯 판정어 (D-090). **가장 먼저 나오는 판정어**를 그 슬롯의 판정으로 읽는다 —
#: v4 는 판정어를 콜론 바로 뒤에 쓰게 하고, 그 뒤 요소별 설명에 "없음"·"미충족" 같은
#: 말이 또 나올 수 있기 때문이다. 대안의 순서가 곧 우선순위다: 같은 위치에서
#: "부분 충족" 은 `부분` 으로, "미충족" 은 `충족` 보다 먼저 잡힌다.
_STATE_TOKEN = re.compile(r"분모\s*(?:에서\s*)?제외|부분|미충족|불충족|충족")
_STATE_OF = {"부분": "partial", "미충족": "none", "불충족": "none", "충족": "full"}

#: 근거에 적힌 분모. v4 는 "분모 4", v3 은 "분모는 4슬롯" 또는 "3/4 슬롯" 으로 쓴다.
_DENOMINATOR = re.compile(r"분모\s*(?:는|은|가|=|:)?\s*(\d)")
_RATIO = re.compile(r"\d(?:\.\d)?\s*/\s*(\d)")

#: v4 슬롯 점수 (D-090)
_STATE_POINTS = {"full": 1.0, "partial": 0.5, "none": 0.0}


def _slot_windows(rationale: str) -> dict[int, list[str]]:
    """슬롯 마커마다 판정 구간을 자른다. `slot_verdicts` 와 같은 경계 규칙이다."""
    windows: dict[int, list[str]] = {n: [] for n in SLOT_NAMES}
    hits: list[tuple[int, int]] = []
    for slot, pattern in _SLOT_MARKERS.items():
        for match in re.finditer(pattern, rationale):
            hits.append((match.end(), slot))
    hits.sort()
    positions = [pos for pos, _ in hits]
    for index, (pos, slot) in enumerate(hits):
        nxt = positions[index + 1] if index + 1 < len(positions) else len(rationale)
        tally = _TALLY.search(rationale, pos, nxt)
        end = min(nxt, tally.start() if tally else nxt, pos + _VERDICT_BACKSTOP)
        windows[slot].append(rationale[pos:end])
    return windows


def slot_states(rationale: str) -> dict[int, str | None]:
    """완결성 근거에서 슬롯별 **4상태** 판정을 읽는다 (v4, D-090).

    `slot_verdicts` 는 충족/미충족 이분법이라 "부분"을 충족으로 읽는다(부분 문자열에
    '충족' 이 있다). v3 기록의 재집계 값을 바꾸지 않으려고 그쪽은 그대로 두고 여기를
    따로 둔다.

    Returns:
        `{슬롯번호: "full" | "partial" | "none" | "excluded" | None(판정어 없음)}`
    """
    states: dict[int, str | None] = {n: None for n in SLOT_NAMES}
    if not rationale:
        return states
    for slot, windows in _slot_windows(rationale).items():
        for window in windows:
            match = _STATE_TOKEN.search(window)
            if match is None:
                continue
            token = match.group(0)
            states[slot] = "excluded" if token.startswith("분모") else _STATE_OF[token]
            break  # 같은 슬롯이 여러 번 언급되면 판정어가 처음 읽힌 쪽을 남긴다
    return states


def stated_denominator(rationale: str) -> int | None:
    """근거가 밝힌 분모. 적혀 있지 않으면 None.

    **점수를 다시 계산하려는 것이 아니다.** v3 의 비결정성은 분모에서 났고(D-088),
    형식 검사(`slot_adherence`)는 그걸 못 잡았다 — 4슬롯을 다 열거한 뒤 하나를
    뺐기 때문이다. 분모를 따로 읽어야 라벨과 대조할 수 있다.
    """
    if not rationale:
        return None
    match = _DENOMINATOR.search(rationale)
    if match:
        return int(match.group(1))
    ratios = _RATIO.findall(rationale)
    return int(ratios[-1]) if ratios else None


def completeness_from_states(states: dict[int, str | None]) -> int | None:
    """슬롯 판정에 v4 점수표를 적용한다 (D-090). 판정이 하나라도 비면 None.

    judge 가 낸 점수와 대조하는 용도다 — 같은 판정에서 다른 점수가 나왔다면
    "판정이 흔들렸다"가 아니라 **"표를 적용하지 않았다"** 이다.
    """
    if any(state is None for state in states.values()):
        return None
    if states[1] == "excluded":
        return None  # ①사건은 분모에서 뺄 수 없다 — 라벨 스키마가 막는 상태다
    if states[1] == "none":
        return 1
    included = [state for state in states.values() if state != "excluded"]
    ratio = sum(_STATE_POINTS[state] for state in included) / len(included)
    if ratio >= 1:
        return 5
    if ratio >= 0.75:
        return 4
    if ratio >= 0.5:
        return 3
    return 2


def axis_stats(axis: str, scores: Sequence[int]) -> AxisStats | None:
    """한 축의 점수 목록 -> 분포 통계. 표본이 없으면 None."""
    if not scores:
        return None
    counter = Counter(scores)
    # 동점이면 낮은 점수를 최빈값으로 삼는다 — 채점을 관대한 쪽으로 반올림하지 않는다.
    top = max(counter.values())
    mode = min(score for score, count in counter.items() if count == top)
    return AxisStats(
        axis=axis,
        n=len(scores),
        mean=round(statistics.fmean(scores), 3),
        stdev=round(statistics.stdev(scores), 3) if len(scores) > 1 else None,
        mode=mode,
        minimum=min(scores),
        maximum=max(scores),
        distribution={str(score): counter[score] for score in sorted(counter)},
    )


def repeat_stats(scores: Iterable[ItemScore], *, item_id: str | None = None) -> RepeatStats:
    """같은 항목의 반복 채점 결과를 하나로 집계한다.

    judge 가 실패한 회차는 축 통계에서 빠지고 `failures` 로만 센다. 실패를 0점으로
    세면 분포가 아래로 끌려가는데, 그건 "낮게 채점됐다"가 아니라 "재지 못했다"다
    (`eval/README.md` — 잴 수 없었던 지표는 0 이 아니라 null).
    """
    scores = list(scores)
    if item_id is None:
        item_id = scores[0].item_id if scores else ""
    mine = [s for s in scores if s.item_id == item_id]

    judged = [s for s in mine if s.judgement is not None]
    # **호출하지 않은 것과 호출이 실패한 것을 같은 값으로 세지 않는다.** `--no-judge`
    # 로 돌린 회차까지 실패로 세면 "judge 가 N회 실패했다"는 거짓 기록이 남는다
    # (`eval/README.md` — 잴 수 없었던 지표는 0 이 아니라 null).
    failures = sum(
        1
        for s in mine
        if s.judgement is None and any(e.startswith("judge 실패") for e in s.errors)
    )

    per_axis: dict[str, list[int]] = {axis: [] for axis in AXES}
    for score in judged:
        for axis in AXES:
            per_axis[axis].append(getattr(score.judgement, axis).score)

    prompts = {s.metadata.judge_prompt for s in judged if s.metadata.judge_prompt}
    judge_prompt = prompts.pop() if len(prompts) == 1 else None

    adherence: float | None = None
    fill_rate: dict[str, float] = {}
    if judged:
        verdict_rows = [slot_verdicts(s.judgement.completeness.rationale) for s in judged]
        full = sum(1 for row in verdict_rows if all(v is not None for v in row.values()))
        adherence = round(full / len(judged), 3)
        for slot, name in SLOT_NAMES.items():
            filled = sum(1 for row in verdict_rows if row[slot] is True)
            fill_rate[f"{slot}{name}"] = round(filled / len(judged), 3)

    human = next((s.human_summary_scores for s in mine if s.human_summary_scores), None)

    # --- v4 과정 검사. 분모 준수는 라벨이 있으면 **루브릭과 관계없이** 잰다.
    labeled = next((s.labeled_denominator for s in mine if s.labeled_denominator), None)
    denominator_adherence: float | None = None
    state_adherence: float | None = None
    table_adherence: float | None = None
    state_counts: dict[str, dict[str, int]] = {}
    if judged:
        rationales = [s.judgement.completeness.rationale for s in judged]
        if labeled is not None:
            same = sum(1 for r in rationales if stated_denominator(r) == labeled)
            denominator_adherence = round(same / len(judged), 3)
        state_rows = [slot_states(r) for r in rationales]
        complete = [
            (row, s) for row, s in zip(state_rows, judged) if all(v is not None for v in row.values())
        ]
        state_adherence = round(len(complete) / len(judged), 3)
        if complete:
            agree = sum(
                1 for row, s in complete if completeness_from_states(row) == s.judgement.completeness.score
            )
            table_adherence = round(agree / len(complete), 3)
        for slot, name in SLOT_NAMES.items():
            counter = Counter(row[slot] or "unread" for row in state_rows)
            state_counts[f"{slot}{name}"] = dict(sorted(counter.items()))

    return RepeatStats(
        item_id=item_id,
        judge_prompt=judge_prompt,
        n=len(judged),
        faithfulness=axis_stats("faithfulness", per_axis["faithfulness"]),
        completeness=axis_stats("completeness", per_axis["completeness"]),
        concision=axis_stats("concision", per_axis["concision"]),
        human_scores=human,
        slot_adherence=adherence,
        slot_fill_rate=fill_rate,
        failures=failures,
        labeled_denominator=labeled,
        denominator_adherence=denominator_adherence,
        state_adherence=state_adherence,
        score_table_adherence=table_adherence,
        slot_state_counts=state_counts,
        length_stops=count_length_stops(
            {"stop_kinds": s.judge_stop_kinds, "finish_reasons": s.judge_finish_reasons}
            for s in mine
        ),
    )
