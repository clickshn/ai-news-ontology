"""완결성 점수 계산 — 요소 판정 → 슬롯 판정 → 점수 (루브릭 v6, ADR-021).

v5 까지는 judge 가 세 단계를 한 번에 했다. 뒤의 두 단계는 판단이 아니라 **계산**인데,
v5 에서 judge 가 "이름만" 요소를 슬롯 판정에서 "부분"으로 올리는 이탈을 10/10 같은
모양으로 냈다 (D-095, D-096). v6 부터 judge 는 요소 판정만 내고 나머지는 여기서 한다.

**여기가 판정 규칙의 정본이다.** `eval.analysis` 의 과정 검사(v4/v5 행 재집계)도 같은
함수를 쓴다 — 규칙이 두 곳에 있으면 v5 를 재는 잣대와 v6 가 계산하는 규칙이 갈라질 수 있다.

## 이 모듈이 없애지 않는 것

**"그 요소가 요약에 있는가"는 여전히 judge 의 판단이다.** v5 에서 #33001 ②(a) 가 10/10
`이름만` 으로 기운 것처럼, 그 판단은 흔들리지 않고 한쪽으로 기울 수 있다. 계산이
결정적이 되면 그 기울기가 **점수에 결정적으로 박힌다** (`docs/eval/rubric-v6-design.md` §4).

전부 순수 함수다.
"""

from __future__ import annotations

import re

from eval.schema import CompletenessComputation, CompletenessSlots

__all__ = [
    "ELEMENT_MARKS",
    "ELEMENT_VERDICTS",
    "SLOT_LABELS",
    "ElementParseError",
    "completeness_from_states",
    "compute_completeness",
    "parse_element_verdicts",
    "state_from_elements",
]

#: 요소 판정어 (v4 ~ v6 공통).
ELEMENT_VERDICTS = ("담김", "이름만", "없음")

#: 요소 표기. 슬롯당 최대 3개 (`CompletenessSlots`).
ELEMENT_MARKS = "abc"

#: 슬롯 표기. judge 에게 보이는 표(`eval.runner.render_completeness_slots`)와 같다.
SLOT_LABELS = {1: "①사건", 2: "②범위", 3: "③정도", 4: "④경위"}

#: 슬롯 점수 (D-090).
_STATE_POINTS = {"full": 1.0, "partial": 0.5, "none": 0.0}

#: v6 는 원문자 표기만 받는다 — 루브릭이 그렇게 쓰게 하고, 느슨하게 받으면 설명 속
#: "(1)" 같은 괄호 숫자를 슬롯 경계로 잘못 읽는다.
_CIRCLED = {"①": 1, "②": 2, "③": 3, "④": 4}
_CIRCLED_RE = re.compile("[①②③④]")
_ELEMENT_RE = re.compile(r"\(([abc])\)\s*(담김|이름만|없음)")


class ElementParseError(ValueError):
    """judge 근거에서 라벨의 요소 판정을 빠짐없이·모순 없이 읽을 수 없다.

    **기본값으로 채우지 않는다.** 빠진 요소를 `없음` 으로 두면 형식 실패가 낮은 점수로
    둔갑하고, 실패율 대신 점수 분포가 움직여 원인을 가를 수 없다.
    """


def state_from_elements(elements: dict[str, str]) -> str | None:
    """요소 판정에서 슬롯 판정을 낸다 (v4 ~ v6 의 표).

    전부 담김 = full, 담김이 하나 이상 = partial, 담김이 없음(이름만·없음뿐) = none.
    **이름만은 담김으로 세지 않는다** — v5 의 judge 가 슬롯 단계에서 어긴 바로 그 규칙이다.
    요소가 없으면 None.
    """
    if not elements:
        return None
    held = sum(1 for verdict in elements.values() if verdict == "담김")
    if held == len(elements):
        return "full"
    return "partial" if held else "none"


def completeness_from_states(states: dict[int, str | None]) -> int | None:
    """슬롯 판정에 점수표를 적용한다 (D-090). 판정이 하나라도 비면 None.

    | 조건 | 점수 |
    |---|---|
    | ①사건이 미충족 | 1 |
    | r = 1 | 5 |
    | 0.75 ≤ r < 1 | 4 |
    | 0.5 ≤ r < 0.75 | 3 |
    | 그 밖 | 2 |
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


def _segments(rationale: str) -> dict[int, str]:
    """원문자 슬롯 표기마다 다음 표기 직전까지를 자른다. 같은 표기가 두 번이면 실패."""
    hits = [(m.start(), _CIRCLED[m.group(0)]) for m in _CIRCLED_RE.finditer(rationale)]
    seen: set[int] = set()
    for _, slot in hits:
        if slot in seen:
            raise ElementParseError(f"{SLOT_LABELS[slot]} 표기가 두 번 나온다 — 어느 쪽이 판정인지 알 수 없다")
        seen.add(slot)
    segments: dict[int, str] = {}
    for index, (pos, slot) in enumerate(hits):
        end = hits[index + 1][0] if index + 1 < len(hits) else len(rationale)
        segments[slot] = rationale[pos + 1 : end]
    return segments


def parse_element_verdicts(rationale: str, slots: CompletenessSlots) -> dict[int, dict[str, str]]:
    """judge 근거에서 **라벨의 요소마다** 판정을 읽는다 (v6 형식).

    형식: `①사건: (a) 담김 | ②범위: (a) 이름만 (…) | ③정도: (a) 담김 / (b) 없음 | ④경위: 분모 제외`

    - 요소가 있는 슬롯은 표기가 있어야 하고, 읽힌 요소 표기 집합이 라벨의 (a)(b)… 와
      **정확히 같아야** 한다. 빠지거나 남으면 실패다.
    - 같은 요소에 판정이 둘 이상이고 서로 다르면 실패다.
    - 분모 제외 슬롯은 무엇이 적혀 있든 읽지 않는다.

    **v4/v5 의 `eval.analysis.element_verdicts` 와 다르다.** 그쪽은 사후 관찰용이라 읽히는
    만큼만 읽고, 200자 창으로 자른다. 이쪽은 점수의 입력이라 창이 없고 빠짐을 허용하지 않는다.

    Raises:
        ElementParseError
    """
    if not rationale or not rationale.strip():
        raise ElementParseError("완결성 근거가 비어 있다")
    segments = _segments(rationale)
    result: dict[int, dict[str, str]] = {}
    for slot, elements in slots.by_slot().items():
        if not elements:
            result[slot] = {}
            continue
        label = SLOT_LABELS[slot]
        if slot not in segments:
            raise ElementParseError(f"{label} 표기가 없다")
        expected = set(ELEMENT_MARKS[: len(elements)])
        found: dict[str, str] = {}
        for mark, verdict in _ELEMENT_RE.findall(segments[slot]):
            if mark in found and found[mark] != verdict:
                raise ElementParseError(f"{label}({mark}) 판정이 둘이다: {found[mark]} / {verdict}")
            found[mark] = verdict
        if set(found) != expected:
            missing = sorted(expected - set(found))
            extra = sorted(set(found) - expected)
            raise ElementParseError(
                f"{label} 요소가 라벨과 맞지 않는다 — 빠짐 {missing or '없음'}, 남음 {extra or '없음'}"
            )
        result[slot] = dict(sorted(found.items()))
    return result


def compute_completeness(rationale: str, slots: CompletenessSlots) -> CompletenessComputation:
    """judge 근거 + 라벨 → 완결성 점수와 그 경로.

    분모는 **라벨에서** 센다. judge 가 적은 분모는 읽지 않는다 — v6 에서는 judge 가 분모를
    적지 않는다.

    Raises:
        ElementParseError: 요소 판정을 읽을 수 없을 때. 점수를 내지 않는다.
    """
    elements = parse_element_verdicts(rationale, slots)
    states: dict[int, str | None] = {
        slot: ("excluded" if not verdicts else state_from_elements(verdicts))
        for slot, verdicts in elements.items()
    }
    score = completeness_from_states(states)
    if score is None:  # 라벨 스키마가 ①을 비울 수 없게 막으므로 여기 오면 규칙이 틀린 것이다
        raise ElementParseError(f"슬롯 판정에서 점수를 낼 수 없다: {states}")
    included = [s for s in states.values() if s != "excluded"]
    points = sum(_STATE_POINTS[s] for s in included)
    return CompletenessComputation(
        elements={SLOT_LABELS[slot]: verdicts for slot, verdicts in elements.items() if verdicts},
        states={SLOT_LABELS[slot]: state for slot, state in states.items()},
        points=points,
        denominator=slots.denominator,
        score=score,
    )
