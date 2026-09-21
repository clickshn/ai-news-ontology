"""`docs/adr/` 의 **구조**를 고정한다 — `test_decision_log.py` 와 같은 층위.

## 왜 있나 (F12)

ADR 의 번호·형식 규칙은 `adr-recorder` 스킬 안에 있다. 그래서 스킬을 거치지
않고 직접 쓰면 **맞는지 확인해 주는 주체가 없다.** session-03 의 ADR-018·019 가
그렇게 쓰였고, 결과적으로 대부분 맞았다는 사실이 오히려 나쁜 신호였다 —
틀렸어도 알 수 없었다.

여기서 재는 것은 **구조이지 내용이 아니다.** 결정이 옳은지, 근거가 참인지,
Status 가 실제 구현 상태와 맞는지는 이 테스트가 알 수 없다. 잡는 것은 사람
눈이 잘 놓치는 기계적 결함(번호 중복·구멍 · 제목과 파일명 불일치 · 빠진 머리말
필드 · 닫히지 않은 어휘 · 없는 섹션 · 템플릿에 없는 섹션)뿐이다.

## 스킬을 대신하지 않는다

이 테스트가 통과한다고 ADR 을 직접 써도 된다는 뜻이 **아니다.** 스킬은 형식
외에 "무엇을 되묻고 무엇을 추측하지 않을지"까지 들고 있고, 그건 파일을 보고
잴 수 있는 것이 아니다. 이 테스트가 막는 것은 **형식이 틀린 채로 남는 것**
하나다 (governance "ADR 기록").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ADR_DIR = Path(__file__).resolve().parent.parent / "docs" / "adr"

#: 파일명 규칙 (`docs/adr/README.md`). slug 는 소문자·숫자·하이픈.
FILENAME = re.compile(r"^ADR-(\d{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")

#: 첫 줄 제목. 번호는 파일명과 같아야 한다.
TITLE = re.compile(r"^# ADR-(\d{3}): \S")

#: 머리말 필드 한 줄. `- **Name:** value`
FIELD = re.compile(r"^- \*\*([A-Za-z/ ]+):\*\*\s*(.*)$")

#: 스킬 템플릿이 요구하는 머리말 필드와 **순서**. Confidence 는 조건부라 뺀다.
REQUIRED_FIELDS = ("Status", "Date", "Decision", "Scope", "Decision Source")

#: 템플릿의 Status 는 `Proposed` 이고, 이관분은 `Accepted` 다
#: (`adr-recorder` 생성 규칙). 번복은 기존 ADR 을 지우지 않고 상태를 바꾼다.
STATUSES = ("Proposed", "Accepted", "Superseded", "Deprecated", "Rejected")

#: Decision Source 와 Confidence 의 닫힌 어휘. 뒤에 괄호로 맥락을 붙이는 것은
#: 허용한다 (ADR-017 · ADR-018 이 그렇게 쓴다) — 머리말의 **첫 낱말**만 본다.
DECISION_SOURCES = ("Human", "AI-Inferred", "Code-Inferred")
CONFIDENCES = ("High", "Medium", "Low")

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: `## ` 섹션. 템플릿에 있는 이름만 쓴다 — "템플릿의 섹션 헤더와 필드명은
#: 고정한다. 새 섹션이나 필드를 임의로 추가하지 않는다" (스킬 생성 규칙).
TEMPLATE_SECTIONS = (
    "Context",
    "Decision",
    "Rationale",
    "Evidence",
    "Alternatives",
    "Consequences",
    "Implementation",
    "Reversibility",
    "Review Trigger",
    "References",
    "AI/ML Details",
)

#: 조건부 섹션을 뺀 나머지 — 템플릿이 `<!-- 필수 -->` 로 표시한 것들.
REQUIRED_SECTIONS = ("Context", "Decision", "Rationale", "Consequences")

#: 전제가 바뀐 ADR 에 붙이는 섹션. governance "ADR 기록"이 **새 ADR 로 덮지
#: 말고 이것을 추가하라**고 정한 것이라 템플릿 밖이어도 허용한다 (ADR-018).
AMENDMENT = re.compile(r"^Amendment \d+( —.*)?$")

SECTION = re.compile(r"^## (.+?)\s*$")


class Adr:
    """한 ADR 파일. 필요한 것만 훑어 둔다."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.name
        text = path.read_text(encoding="utf-8")
        self.lines = text.splitlines()
        self.fields: dict[str, str] = {}
        self.field_order: list[str] = []
        self.sections: list[str] = []
        for line in self.lines:
            if (m := FIELD.match(line)) and m.group(1) not in self.fields:
                self.fields[m.group(1)] = m.group(2).strip()
                self.field_order.append(m.group(1))
            elif m := SECTION.match(line):
                self.sections.append(m.group(1))

    @property
    def number(self) -> int:
        return int(self.name[4:7])

    def __repr__(self) -> str:  # 실패 메시지에 파일명이 보이게
        return self.name


def adr_files() -> list[Path]:
    return sorted(p for p in ADR_DIR.glob("ADR-*.md"))


@pytest.fixture(scope="module")
def adrs() -> list[Adr]:
    paths = adr_files()
    assert paths, "docs/adr/ 에서 ADR 파일을 하나도 못 읽었다"
    return [Adr(p) for p in paths]


def test_filenames_follow_the_convention(adrs):
    """`ADR-NNN-slug.md`. 번호 폭이 흔들리면 정렬과 다음 번호 계산이 깨진다."""
    assert [a.name for a in adrs if not FILENAME.match(a.name)] == []


def test_numbers_are_unique(adrs):
    """같은 번호가 두 파일에 있으면 안 된다.

    결정 로그의 F9 와 같은 결함이다 — 인용하는 쪽이 어느 문서를 가리키는지
    알 수 없게 된다. ADR 은 서로를 `Related ADR` 로 인용하므로 더 직접적이다.
    """
    seen: dict[int, list[str]] = {}
    for adr in adrs:
        seen.setdefault(adr.number, []).append(adr.name)
    assert {n: names for n, names in seen.items() if len(names) > 1} == {}


def test_numbers_are_contiguous_from_one(adrs):
    """ADR-001 부터 구멍 없이 이어져야 한다.

    구멍은 파일을 지웠거나 번호를 건너뛴 것이고, 어느 쪽이든 **다음 번호를
    세는 방법**이 깨진다. 스킬은 기존 파일 목록을 세어 다음 번호를 정한다.
    """
    numbers = sorted(a.number for a in adrs)
    assert numbers == list(range(1, len(numbers) + 1))


def test_title_number_matches_the_filename(adrs):
    """첫 줄 제목의 번호가 파일명과 같아야 한다.

    복사해서 새 ADR 을 만들 때 가장 흔하게 남는 자국이고, 본문만 읽는
    사람에게는 **파일명이 보이지 않아서** 끝까지 안 걸린다.
    """
    bad = []
    for adr in adrs:
        m = TITLE.match(adr.lines[0] if adr.lines else "")
        if not m or int(m.group(1)) != adr.number:
            bad.append((adr.name, adr.lines[0] if adr.lines else "(빈 파일)"))
    assert bad == []


def test_required_header_fields_are_present_and_ordered(adrs):
    """머리말 5필드가 전부 있고 템플릿 순서를 지킨다.

    순서까지 재는 이유는 ADR 을 **표처럼 훑기** 때문이다 — 20개를 나란히
    놓고 Status·Date 를 눈으로 읽을 때 자리가 흔들리면 그 읽기가 깨진다.
    """
    bad = []
    for adr in adrs:
        present = [f for f in adr.field_order if f in REQUIRED_FIELDS]
        if present != list(REQUIRED_FIELDS):
            bad.append((adr.name, present))
    assert bad == []


def test_status_vocabulary_is_closed(adrs):
    """Status 는 정해진 낱말 중 하나다."""
    bad = [(a.name, a.fields.get("Status")) for a in adrs if a.fields.get("Status") not in STATUSES]
    assert bad == []


def test_dates_are_iso(adrs):
    """Date 는 `YYYY-MM-DD`. 이관분은 이관일이 아니라 원본 결정일이다."""
    bad = [(a.name, a.fields.get("Date")) for a in adrs if not DATE.match(a.fields.get("Date", ""))]
    assert bad == []


def test_decision_source_vocabulary_is_closed(adrs):
    """Decision Source 는 `Human` / `AI-Inferred` / `Code-Inferred` 로 시작한다.

    뒤에 맥락을 붙이는 것은 허용한다(ADR-017 의 "Human (MARA session-11 §5 의
    작업 지시)"). 낱말 자체를 닫는 이유는 **Confidence 의 유무가 여기서
    갈리기 때문**이다 — 아래 테스트가 그 규칙을 잰다.
    """
    bad = [
        (a.name, a.fields.get("Decision Source"))
        for a in adrs
        if not a.fields.get("Decision Source", "").startswith(DECISION_SOURCES)
    ]
    assert bad == []


def test_confidence_follows_decision_source(adrs):
    """`Decision Source: Human` 이면 Confidence 를 생략하고, 아니면 적는다.

    스킬 생성 규칙 그대로다. 사람이 내린 결정에 신뢰도를 매기는 것은 의미가
    없고, **추론으로 채운 결정에는 그 값이 얼마나 단단한지가 결정의 일부**다.
    """
    bad = []
    for adr in adrs:
        human = adr.fields.get("Decision Source", "").startswith("Human")
        confidence = adr.fields.get("Confidence")
        if human and confidence is not None:
            bad.append((adr.name, "Human 인데 Confidence 가 있다"))
        elif not human and confidence not in CONFIDENCES:
            bad.append((adr.name, f"Confidence={confidence!r}"))
    assert bad == []


def test_required_sections_exist(adrs):
    """Context · Decision · Rationale · Consequences 는 조건 없이 있어야 한다."""
    bad = [
        (a.name, [s for s in REQUIRED_SECTIONS if s not in a.sections])
        for a in adrs
        if not set(REQUIRED_SECTIONS) <= set(a.sections)
    ]
    assert bad == []


def test_sections_come_from_the_template(adrs):
    """템플릿에 없는 섹션을 임의로 만들지 않는다.

    예외는 `Amendment N` 하나다. governance "ADR 기록"이 **전제가 바뀌면 새
    ADR 로 덮지 말고 Amendment 를 추가하라**고 정했으므로, 그건 임의 추가가
    아니라 규칙이 요구한 섹션이다 (ADR-018 이 그 사례다).
    """
    bad = [
        (a.name, s)
        for a in adrs
        for s in a.sections
        if s not in TEMPLATE_SECTIONS and not AMENDMENT.match(s)
    ]
    assert bad == []
