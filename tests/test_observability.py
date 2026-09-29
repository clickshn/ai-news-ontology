"""관측 레이어 검증 (실제 API 호출 없음, 실제 로그 경로에 쓰지 않음).

모든 파일 쓰기는 `tmp_path` 안에서만 일어난다. 이 테스트가 프로젝트의
`observability/logs/` 를 건드리면 실행할 때마다 누적 카운트가 오염된다 —
바로 D-035 에서 validator 대신 파이프라인 층에 기록을 둔 이유와 같은 문제다.

여기서 고정하는 계약:
  1. 스킵은 **append** — 같은 항목을 두 번 버리면 두 줄이 된다 (감사 로그)
  2. 미등록 기업은 **upsert** — 같은 이름이 세 번 나오면 한 줄에 count=3 (작업 큐)
  3. 관측 실패가 **파이프라인 실패가 되지 않는다** (D-008)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from collectors.base import RawItem
from extraction.extractor import record_unknown_companies
from extraction.schema import NewsOntology
from observability.events import (
    InMemoryObserver,
    JSONLObserver,
    MultiObserver,
    NullObserver,
    PipelineObserver,
    SkipRecord,
    UnknownCompanyRecord,
    observer_from_config,
)

# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------
ONTOLOGY_PAYLOAD = {
    "요약": "LLM 보조로 만든 퍼저가 오픈소스 프로젝트에서 버그를 찾아낸 사례다. 재현 조건과 원인 코드가 함께 공개됐다.",
    "기술영역": ["Application/Product"],
    "발표유형": "Community/Discussion",
    "관련기업": [
        {"원문표기": "FFmpeg", "역할": "영향 대상"},   # 미등록
        {"원문표기": "오픈AI", "역할": "발표 주체"},   # 등록됨 -> 기록되지 않아야 한다
    ],
    "관련기존기술": ["Fuzzing"],
    "영향도": {"점수": 2, "근거": "국소적 버그지만 AI 보조 퍼징의 실사용 사례라 참고 가치가 있다."},
}


@pytest.fixture
def item() -> RawItem:
    return RawItem(
        url="https://news.hada.io/topic?id=33001",
        title="바이브코딩으로 만든 퍼저가 FFmpeg의 0 나누기 버그를 발견",
        body="FFmpeg의 Sony PS2 VPK 디먹서에서 0 나누기 버그가 발견됨",
        source_name="GeekNews",
        published_at=date(2026, 8, 29),
        collected_at=date(2026, 8, 29),
        tags=("ko", "aggregator"),
    )


@pytest.fixture
def observer(tmp_path) -> JSONLObserver:
    return JSONLObserver(tmp_path / "logs")


def skip(url: str = "https://news.hada.io/topic?id=32998") -> SkipRecord:
    return SkipRecord(
        url=url,
        title="EasyEffects를 모든 Linux 배포판에 포함해야 함",
        source_name="GeekNews",
        reason="리눅스 오디오 도구 소개로 AI나 자동화와 접점이 없다",
        model="claude-haiku-4-5-20251001",
        prompt_name="relevance_gate.v1.md",
    )


def unknown(name: str, article: str = "https://news.hada.io/topic?id=33001"):
    return UnknownCompanyRecord(raw_name=name, source_article=article)


def read_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# 1. 프로토콜 준수
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("factory", [NullObserver, InMemoryObserver, MultiObserver])
def test_builtin_observers_satisfy_protocol(factory):
    assert isinstance(factory(), PipelineObserver)


def test_jsonl_observer_satisfies_protocol(observer):
    assert isinstance(observer, PipelineObserver)


# ---------------------------------------------------------------------------
# 2. 스킵 — append-only (D-036)
# ---------------------------------------------------------------------------
def test_skip_is_appended_as_jsonl(observer):
    observer.record_skip(skip())

    rows = read_lines(observer.skips_path)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://news.hada.io/topic?id=32998"
    assert rows[0]["stage"] == "relevance_gate"
    assert rows[0]["model"] == "claude-haiku-4-5-20251001"


def test_same_skip_twice_makes_two_lines(observer):
    """감사 로그이므로 합치지 않는다 — 두 번 버렸으면 두 사건이다."""
    observer.record_skip(skip())
    observer.record_skip(skip())
    assert len(read_lines(observer.skips_path)) == 2


def test_skip_log_keeps_korean_readable(observer):
    """`ensure_ascii=False` — 사람이 열어 보는 파일이다."""
    observer.record_skip(skip())
    assert "리눅스" in observer.skips_path.read_text(encoding="utf-8")


def test_skips_round_trip(observer):
    observer.record_skip(skip())
    loaded = observer.load_skips()
    assert len(loaded) == 1
    assert loaded[0].reason.startswith("리눅스")
    assert loaded[0].decided_at.tzinfo is not None


def test_log_directory_is_created_on_demand(tmp_path):
    observer = JSONLObserver(tmp_path / "a" / "b" / "logs")
    assert not observer.skips_path.parent.exists()
    observer.record_skip(skip())
    assert observer.skips_path.exists()


# ---------------------------------------------------------------------------
# 3. 미등록 기업 — upsert (D-036)
# ---------------------------------------------------------------------------
def test_unknown_company_is_written_once(observer):
    observer.record_unknown_company(unknown("FFmpeg"))

    rows = read_lines(observer.unknown_companies_path)
    assert len(rows) == 1
    assert rows[0]["raw_name"] == "FFmpeg"
    assert rows[0]["occurrence_count"] == 1
    assert rows[0]["source_article"] == "https://news.hada.io/topic?id=33001"


def test_three_occurrences_become_one_line_with_count_three(observer):
    """같은 이름을 세 번 기록하면 줄은 하나, count 는 3 이어야 한다."""
    for _ in range(3):
        observer.record_unknown_company(unknown("FFmpeg"))

    rows = read_lines(observer.unknown_companies_path)
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 3


def test_first_occurrence_wins_on_merge(observer):
    """합칠 때 `first_seen`/`source_article` 은 처음 본 값을 유지한다."""
    observer.record_unknown_company(unknown("Cursor", "https://news.hada.io/topic?id=33003"))
    first_seen = read_lines(observer.unknown_companies_path)[0]["first_seen"]

    observer.record_unknown_company(unknown("Cursor", "https://news.hada.io/topic?id=99999"))

    row = read_lines(observer.unknown_companies_path)[0]
    assert row["first_seen"] == first_seen
    assert row["source_article"] == "https://news.hada.io/topic?id=33003"
    assert row["occurrence_count"] == 2


def test_different_names_get_their_own_lines(observer):
    for name in ("FFmpeg", "Cursor", "SpaceX"):
        observer.record_unknown_company(unknown(name))

    rows = read_lines(observer.unknown_companies_path)
    assert [r["raw_name"] for r in rows] == ["FFmpeg", "Cursor", "SpaceX"]
    assert all(r["occurrence_count"] == 1 for r in rows)


def test_case_and_space_variants_merge(observer):
    """정규화와 **같은 동치 관계**로 합친다.

    `normalization_key` 가 흡수하는 차이는 alias 사전도 똑같이 흡수하므로,
    이 셋은 사전에 한 줄만 추가하면 전부 해결된다. 별도 줄로 두면 큐가
    부풀고 `occurrence_count` 가 과소 계상된다.
    """
    for name in ("FFmpeg", "ffmpeg", "FF mpeg"):
        observer.record_unknown_company(unknown(name))

    rows = read_lines(observer.unknown_companies_path)
    assert len(rows) == 1
    assert rows[0]["raw_name"] == "FFmpeg"      # 처음 본 표기를 보존
    assert rows[0]["occurrence_count"] == 3


def test_unrelated_names_are_not_merged(observer):
    """퍼지 매칭을 하지 않는다 — `OpenAI` 와 `OpenAI Korea` 는 별개다 (D-025)."""
    observer.record_unknown_company(unknown("Foo Labs"))
    observer.record_unknown_company(unknown("Foo Labs Korea"))
    assert len(read_lines(observer.unknown_companies_path)) == 2


def test_counts_survive_a_new_observer_instance(tmp_path):
    """실행이 끝나도 카운트가 이어져야 한다 — 파일이 상태의 전부다."""
    log_dir = tmp_path / "logs"
    JSONLObserver(log_dir).record_unknown_company(unknown("FFmpeg"))
    JSONLObserver(log_dir).record_unknown_company(unknown("FFmpeg"))

    rows = read_lines(log_dir / "unknown_companies.jsonl")
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 2


def test_rewrite_leaves_no_temp_file(observer):
    for _ in range(3):
        observer.record_unknown_company(unknown("FFmpeg"))
    assert list(observer.log_dir.glob(".tmp-*")) == []


def test_hand_edited_duplicate_lines_are_merged_on_read(observer):
    """사람이 파일을 편집해 같은 이름이 두 줄이 됐어도 다음 기록에서 합쳐진다."""
    observer.unknown_companies_path.parent.mkdir(parents=True, exist_ok=True)
    observer.unknown_companies_path.write_text(
        json.dumps({"raw_name": "FFmpeg", "source_article": "u", "occurrence_count": 2}) + "\n"
        + json.dumps({"raw_name": "ffmpeg", "source_article": "u", "occurrence_count": 5}) + "\n",
        encoding="utf-8",
    )
    loaded = observer.load_unknown_companies()
    assert len(loaded) == 1
    assert next(iter(loaded.values())).occurrence_count == 7


# ---------------------------------------------------------------------------
# 4. 관측 실패가 파이프라인 실패가 되지 않는다 (D-008)
# ---------------------------------------------------------------------------
def test_broken_json_line_is_skipped_not_raised(observer, capsys):
    observer.unknown_companies_path.parent.mkdir(parents=True, exist_ok=True)
    observer.unknown_companies_path.write_text("이건 JSON이 아니다\n", encoding="utf-8")

    observer.record_unknown_company(unknown("FFmpeg"))

    assert "[warn]" in capsys.readouterr().err
    rows = read_lines(observer.unknown_companies_path)
    assert [r["raw_name"] for r in rows] == ["FFmpeg"]


def test_line_without_raw_name_is_skipped(observer, capsys):
    observer.unknown_companies_path.parent.mkdir(parents=True, exist_ok=True)
    observer.unknown_companies_path.write_text('{"occurrence_count": 3}\n', encoding="utf-8")

    assert observer.load_unknown_companies() == {}
    assert "[warn]" in capsys.readouterr().err


def test_write_failure_warns_but_does_not_raise(observer, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise OSError("디스크가 가득 찼습니다")

    monkeypatch.setattr("builtins.open", boom)
    observer.record_skip(skip())        # 예외가 새어 나오면 이 줄에서 실패한다
    assert "[warn]" in capsys.readouterr().err


def test_missing_file_reads_as_empty(observer):
    assert observer.load_skips() == []
    assert observer.load_unknown_companies() == {}


# ---------------------------------------------------------------------------
# 5. MultiObserver
# ---------------------------------------------------------------------------
def test_multi_observer_fans_out(tmp_path):
    memory = InMemoryObserver()
    jsonl = JSONLObserver(tmp_path / "logs")
    multi = MultiObserver(memory, jsonl)

    multi.record_skip(skip())
    multi.record_unknown_company(unknown("FFmpeg"))

    assert len(memory.skips) == 1
    assert len(memory.unknown_companies) == 1
    assert len(read_lines(jsonl.skips_path)) == 1
    assert len(read_lines(jsonl.unknown_companies_path)) == 1


def test_empty_multi_observer_is_a_no_op():
    MultiObserver().record_skip(skip())


# ---------------------------------------------------------------------------
# 6. 설정 -> observer (D-008 폴백)
# ---------------------------------------------------------------------------
def test_disabled_config_falls_back_to_null_observer():
    assert isinstance(observer_from_config({"observability": {"enabled": False}}), NullObserver)


@pytest.mark.parametrize("config", [None, {}, {"observability": None}, {"observability": {}}])
def test_missing_config_falls_back_to_null_observer(config):
    """블록이 없으면 켜지지 않는다 — 관측은 명시적으로 켜는 기능이다."""
    assert isinstance(observer_from_config(config), NullObserver)


def test_enabled_config_builds_jsonl_observer(tmp_path):
    observer = observer_from_config(
        {"observability": {"enabled": True, "log_dir": "logs"}},
        project_root=tmp_path,
    )
    assert isinstance(observer, JSONLObserver)
    assert observer.log_dir == tmp_path / "logs"


def test_absolute_log_dir_is_used_as_is(tmp_path):
    observer = observer_from_config(
        {"observability": {"enabled": True, "log_dir": str(tmp_path / "elsewhere")}},
        project_root=tmp_path / "ignored",
    )
    assert observer.log_dir == tmp_path / "elsewhere"


def test_filenames_are_configurable(tmp_path):
    observer = observer_from_config(
        {
            "observability": {
                "enabled": True,
                "log_dir": "logs",
                "skips_filename": "s.jsonl",
                "unknown_companies_filename": "u.jsonl",
            }
        },
        project_root=tmp_path,
    )
    assert observer.skips_path.name == "s.jsonl"
    assert observer.unknown_companies_path.name == "u.jsonl"


def test_repo_config_enables_observability():
    """실제 config.yaml 이 관측을 켜 두고 있는지 (배선이 죽어 있으면 무의미하다)."""
    from pathlib import Path

    import yaml

    config = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8")
    )
    assert config["observability"]["enabled"] is True
    assert config["observability"]["log_dir"] == "observability/logs"


# ---------------------------------------------------------------------------
# 7. 파이프라인 배선 — resolved=False 인 것만 기록한다 (D-035)
# ---------------------------------------------------------------------------
def test_only_unresolved_companies_are_recorded(item):
    ontology = NewsOntology.model_validate(ONTOLOGY_PAYLOAD)
    memory = InMemoryObserver()

    record_unknown_companies(ontology, item, memory)

    assert [r.raw_name for r in memory.unknown_companies] == ["FFmpeg"]
    assert memory.unknown_companies[0].source_article == str(item.url)


def test_recording_uses_raw_not_canonical(item):
    """사전에 넣을 대상은 **원문 표기**다. canonical 은 미등록이면 raw 와 같다."""
    payload = {**ONTOLOGY_PAYLOAD, "관련기업": [{"원문표기": "오픈 AI 코리아", "역할": "발표 주체"}]}
    memory = InMemoryObserver()

    record_unknown_companies(NewsOntology.model_validate(payload), item, memory)

    assert memory.unknown_companies[0].raw_name == "오픈 AI 코리아"


def test_pipeline_records_unknown_company_end_to_end(item, tmp_path):
    """게이트 통과 -> 추출 -> 미등록 기업이 파일까지 도달하는지."""
    from extraction.extractor import process_item
    from extraction.llm import StructuredResult, Usage

    class FakeClient:
        def __init__(self, payload):
            self.payload = payload

        def parse_into(self, *, system, user, output_model):
            return StructuredResult(
                value=output_model.model_validate(self.payload),
                usage=Usage(input_tokens=1, output_tokens=1, model="fake"),
            )

    observer = JSONLObserver(tmp_path / "logs")
    process_item(
        item,
        gate_client=FakeClient({"관련있음": True, "근거": "AI 보조 도구의 실사용 사례다"}),
        extraction_client=FakeClient(ONTOLOGY_PAYLOAD),
        observer=observer,
    )

    rows = read_lines(observer.unknown_companies_path)
    assert [r["raw_name"] for r in rows] == ["FFmpeg"]
    assert not observer.skips_path.exists(), "게이트를 통과했는데 스킵이 기록됐다"


def test_skipped_item_records_no_unknown_company(item, tmp_path):
    """게이트에서 걸리면 추출을 안 하므로 기업 정보 자체가 없다."""
    from extraction.extractor import process_item
    from extraction.llm import StructuredResult, Usage

    class FakeClient:
        def __init__(self, payload):
            self.payload = payload

        def parse_into(self, *, system, user, output_model):
            return StructuredResult(
                value=output_model.model_validate(self.payload),
                usage=Usage(input_tokens=1, output_tokens=1, model="fake"),
            )

    observer = JSONLObserver(tmp_path / "logs")
    process_item(
        item,
        gate_client=FakeClient({"관련있음": False, "근거": "AI와 접점이 없는 글이다"}),
        extraction_client=FakeClient(ONTOLOGY_PAYLOAD),
        observer=observer,
    )

    assert len(read_lines(observer.skips_path)) == 1
    assert not observer.unknown_companies_path.exists()


# ---------------------------------------------------------------------------
# 8. 레코드 직렬화
# ---------------------------------------------------------------------------
def test_unknown_company_record_round_trips():
    record = UnknownCompanyRecord(
        raw_name="FFmpeg",
        source_article="https://news.hada.io/topic?id=33001",
        first_seen=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
        occurrence_count=4,
    )
    restored = UnknownCompanyRecord.from_dict(record.to_dict())
    assert restored == record


def test_unknown_company_record_fields():
    data = unknown("FFmpeg").to_dict()
    assert set(data) == {
        "raw_name", "source_article", "first_seen", "occurrence_count", "article_count", "articles",
    }
    assert isinstance(data["first_seen"], str)


def test_broken_timestamp_falls_back_to_now():
    restored = UnknownCompanyRecord.from_dict(
        {"raw_name": "FFmpeg", "source_article": "u", "first_seen": "어제쯤"}
    )
    assert restored.first_seen.tzinfo is not None


# ---------------------------------------------------------------------------
# 8. 괄호 병기 충돌 (D-109) — 사전 보강 큐에 두 후보가 보인다
# ---------------------------------------------------------------------------
CONFLICT_INDEX = {"딥마인드": "Google DeepMind", "구글": "Google", "엔비디아": "NVIDIA", "nvidia": "NVIDIA"}


@pytest.fixture
def conflict_index(monkeypatch):
    import extraction.normalize as normalize

    monkeypatch.setattr(normalize, "load_alias_index", lambda *a, **k: CONFLICT_INDEX)


def test_conflict_is_recorded_with_both_candidates(item, conflict_index, capsys):
    payload = {
        **ONTOLOGY_PAYLOAD,
        "관련기업": [
            {"원문표기": "딥마인드(구글)", "역할": "발표 주체"},
            {"원문표기": "엔비디아(NVIDIA)", "역할": "협력사"},  # 풀리므로 기록되지 않는다
            {"원문표기": "LG전자(대표이사 류재철)", "역할": "협력사"},  # 미등록, 충돌 아님
        ],
    }
    memory = InMemoryObserver()

    record_unknown_companies(NewsOntology.model_validate(payload), item, memory)

    rows = {r.raw_name: r.conflict for r in memory.unknown_companies}
    assert rows == {"딥마인드(구글)": ("Google DeepMind", "Google"), "LG전자(대표이사 류재철)": ()}
    assert "괄호 병기 충돌" in capsys.readouterr().err


def test_conflict_survives_the_jsonl_round_trip_and_merge(observer, conflict_index):
    # 충돌이 성립하는 사전 아래서 돈다. 실제 사전에는 "구글"이 없어 `딥마인드(구글)` 이
    # 풀리고, 풀리는 줄은 기록할 때 큐에서 빠진다 (D-114).
    record = UnknownCompanyRecord(raw_name="딥마인드(구글)", source_article="a", conflict=("Google DeepMind", "Google"))
    observer.record_unknown_company(UnknownCompanyRecord(raw_name="딥마인드(구글)", source_article="b"))
    observer.record_unknown_company(record)

    row = observer.load_unknown_companies()[record.key]

    assert row.occurrence_count == 2
    assert row.source_article == "b"  # 첫 등장이 이긴다
    assert row.conflict == ("Google DeepMind", "Google")  # 충돌은 최근 판정


def test_records_without_conflict_keep_the_old_line_shape(observer):
    observer.record_unknown_company(UnknownCompanyRecord(raw_name="FFmpeg", source_article="a"))
    line = json.loads(observer.unknown_companies_path.read_text(encoding="utf-8").splitlines()[0])
    assert "conflict" not in line


# ---------------------------------------------------------------------------
# 9. 사전 보강 기준 — 서로 다른 기사 3건 · 풀리는 줄 정리 (D-114)
# ---------------------------------------------------------------------------
from observability.events import ALIAS_CANDIDATE_MIN_ARTICLES  # noqa: E402


def test_mentions_in_one_article_are_one_article(observer):
    """첫 전량 실행의 유일한 2회(`스페이스X`)는 한 기사 안의 중복이었다."""
    observer.record_unknown_company(unknown("스페이스X", "https://a/1"))
    observer.record_unknown_company(unknown("스페이스X", "https://a/1"))

    row = next(iter(observer.load_unknown_companies().values()))
    assert row.occurrence_count == 2 and row.article_count == 1


def test_distinct_articles_are_counted_across_writes(observer):
    for n in (1, 2, 1, 3):
        observer.record_unknown_company(unknown("Qwen", f"https://a/{n}"))

    row = next(iter(observer.load_unknown_companies().values()))
    assert row.article_count == 3 and row.articles == ("https://a/1", "https://a/2", "https://a/3")
    assert row.source_article == "https://a/1"  # 첫 등장이 이긴다


def test_candidates_need_three_distinct_articles(observer):
    """2건은 같은 사건을 두 소스가 다룬 경우일 수 있다 (session-16)."""
    assert ALIAS_CANDIDATE_MIN_ARTICLES == 3
    for n in (1, 2):
        observer.record_unknown_company(unknown("Two", f"https://a/{n}"))
    for n in (1, 1, 1, 1):
        observer.record_unknown_company(unknown("Repeated", f"https://a/{n}"))
    for n in (1, 2, 3):
        observer.record_unknown_company(unknown("Three", f"https://a/{n}"))

    assert [r.raw_name for r in observer.alias_candidates()] == ["Three"]


def test_old_lines_without_articles_count_as_one_article(observer):
    """`articles` 이전 줄의 언급 수가 몇 기사에서 왔는지는 복원할 수 없다."""
    observer.unknown_companies_path.parent.mkdir(parents=True, exist_ok=True)
    observer.unknown_companies_path.write_text(
        json.dumps({"raw_name": "Qwen", "source_article": "https://a/1", "occurrence_count": 5}) + "\n",
        encoding="utf-8",
    )
    observer.record_unknown_company(unknown("Qwen", "https://a/2"))

    row = next(iter(observer.load_unknown_companies().values()))
    assert row.occurrence_count == 6 and row.article_count == 2


def test_hand_edited_duplicates_merge_their_articles(observer):
    observer.unknown_companies_path.parent.mkdir(parents=True, exist_ok=True)
    observer.unknown_companies_path.write_text(
        json.dumps({"raw_name": "Qwen", "source_article": "https://a/1", "articles": ["https://a/1", "https://a/2"]})
        + "\n" + json.dumps({"raw_name": "qwen", "source_article": "https://a/3"}) + "\n",
        encoding="utf-8",
    )
    row = next(iter(observer.load_unknown_companies().values()))
    assert row.article_count == 3


def test_names_the_dictionary_now_resolves_leave_the_queue_on_write(observer, conflict_index):
    """D-109 이후 `엔비디아(NVIDIA)` 가 풀리는데도 큐에 남아 있었다."""
    observer.unknown_companies_path.parent.mkdir(parents=True, exist_ok=True)
    observer.unknown_companies_path.write_text(
        json.dumps({"raw_name": "엔비디아(NVIDIA)", "source_article": "https://a/1"}) + "\n",
        encoding="utf-8",
    )
    observer.record_unknown_company(unknown("FFmpeg"))

    assert [r.raw_name for r in observer.load_unknown_companies().values()] == ["FFmpeg"]


def test_prune_removes_resolved_and_keeps_conflicts(observer, conflict_index):
    observer.unknown_companies_path.parent.mkdir(parents=True, exist_ok=True)
    observer.unknown_companies_path.write_text(
        "".join(
            json.dumps({"raw_name": name, "source_article": "https://a/1"}) + "\n"
            for name in ("엔비디아(NVIDIA)", "딥마인드(구글)", "FFmpeg")
        ),
        encoding="utf-8",
    )

    removed = observer.prune_resolved()

    assert removed == ["엔비디아(NVIDIA)"]
    # 충돌은 풀린 것이 아니다 — 사람이 판정할 항목이라 남는다
    assert [r.raw_name for r in observer.load_unknown_companies().values()] == ["딥마인드(구글)", "FFmpeg"]


def test_prune_on_a_clean_queue_does_not_rewrite(observer, conflict_index):
    observer.record_unknown_company(unknown("FFmpeg"))
    before = observer.unknown_companies_path.stat().st_mtime_ns
    assert observer.prune_resolved() == []
    assert observer.unknown_companies_path.stat().st_mtime_ns == before
