"""README 결정 로그(`D-XXX`)의 **구조**를 고정한다.

결정 로그는 이 레포에서 "왜 이렇게 했는가"의 정본이고, D-046 · D-049 처럼
**다른 결정이 근거로 인용하는** 문서다. 인용되는 문서가 한 결정을 두 줄에
서로 다른 상태로 적고 있으면 어느 쪽이 사실인지 판정할 방법이 없다 (F9).

여기서 재는 것은 **구조이지 내용이 아니다.** 근거가 참인지, 상태가 실제
코드와 맞는지는 이 테스트가 알 수 없다 — 잡는 것은 사람 눈이 잘 놓치는
기계적 결함(중복 번호 · 번호 구멍 · 닫히지 않은 상태어 · 없는 결정 참조)뿐이다.
`test_vocab_sync.py` 가 어휘 미러에 대해 하는 일과 같은 층위다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parent.parent / "README.md"

#: 결정 로그 표의 한 행. 파이프로 갈린 6칸 (ID · 날짜 · 결정 · 근거 · 대안 · 상태).
ROW = re.compile(r"^\| (D-\d{3}) \|(.*)\|\s*$")

#: 상태 칸의 **머리말**. 뒤에 사유가 붙는 것은 허용하지만(D-029), 상태어 자체는
#: governance 의 셋으로 닫는다 — 새 상태어를 쓰고 싶어지면 규칙을 먼저 고친다.
STATUSES = ("유효", "번복됨", "**번복됨**", "부분 번복", "**부분 번복**")

#: 본문 어디서든 결정을 가리키는 표기.
REF = re.compile(r"D-(\d{3})")


def decision_rows() -> list[tuple[str, list[str]]]:
    """(ID, 칸들) 목록. 표 순서 그대로 돌려준다."""
    rows = []
    for line in README.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            rows.append((m.group(1), [c.strip() for c in m.group(2).split("|")]))
    return rows


@pytest.fixture(scope="module")
def rows() -> list[tuple[str, list[str]]]:
    parsed = decision_rows()
    assert parsed, "결정 로그 표를 하나도 못 읽었다 — 표 형식이 바뀌었으면 ROW 를 고친다"
    return parsed


def test_ids_are_unique(rows):
    """같은 D 번호가 두 줄에 있으면 안 된다 — F9 가 정확히 이것이었다.

    번복은 **줄을 늘리는 것이 아니라 상태를 바꾸는 것**이다 (governance
    "결정 로그"). 줄이 늘면 두 줄의 상태가 갈릴 수 있고, 갈린 순간
    인용하는 쪽(D-046 · D-049)이 어느 줄을 가리키는지 알 수 없게 된다.
    """
    seen: dict[str, int] = {}
    for did, _ in rows:
        seen[did] = seen.get(did, 0) + 1
    assert [d for d, n in seen.items() if n > 1] == []


def test_ids_are_contiguous_from_one(rows):
    """D-001 부터 구멍 없이 이어져야 한다.

    구멍은 둘 중 하나다 — 행을 **지웠거나**(governance 금지), 번호를 건너뛰고
    새 결정을 달았거나. 어느 쪽이든 다음 번호를 세는 방법이 깨진다.
    """
    numbers = sorted(int(did[2:]) for did, _ in rows)
    assert numbers == list(range(1, len(numbers) + 1))


def test_status_vocabulary_is_closed(rows):
    """상태 칸은 정해진 상태어로 시작한다. 뒤에 붙는 사유는 자유다."""
    bad = [
        (did, cells[-1])
        for did, cells in rows
        if not cells[-1].startswith(STATUSES)
    ]
    assert bad == []


def test_reverted_rows_name_the_superseding_decision(rows):
    """`번복` 이 붙은 행은 **무엇이 대신하는지**를 같은 행에서 밝힌다.

    상태만 내려 두고 후속 결정을 적지 않으면, 읽는 쪽은 그 판단이 지금
    무엇으로 대체됐는지 표 전체를 훑어야 알 수 있다.
    """
    for did, cells in rows:
        if "번복" not in cells[-1]:
            continue
        refs = {f"D-{n}" for n in REF.findall(" ".join(cells))} - {did}
        assert refs, f"{did}: 번복 상태인데 대체 결정을 가리키지 않는다"


def test_references_point_at_existing_decisions(rows):
    """표 안에서 가리키는 D 번호가 전부 실재해야 한다.

    번호 한 자리가 틀리면 그 줄은 **없는 결정을 근거로** 서게 된다.
    문장은 여전히 읽히기 때문에 사람 눈으로는 걸리지 않는다.

    그 대가로 **표 안에는 예시용 가짜 번호를 쓸 수 없다.** 예외를 두면
    정확히 오타가 그 구멍으로 빠져나가므로, 느슨하게 두는 쪽을 택하지 않았다.
    """
    known = {did for did, _ in rows}
    dangling = sorted(
        {
            f"D-{n}"
            for _, cells in rows
            for n in REF.findall(" ".join(cells))
            if f"D-{n}" not in known
        }
    )
    assert dangling == []
