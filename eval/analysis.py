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

__all__ = [
    "SLOT_NAMES",
    "AXES",
    "axis_stats",
    "slot_verdicts",
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
    )
