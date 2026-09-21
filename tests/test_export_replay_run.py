"""대조 실행기의 **실행 경로** (`export/replay.py`) — F5 의 잔여분.

## 이 파일과 `test_export_replay.py` 의 경계

| 파일 | 재는 것 |
|---|---|
| `test_export_replay.py` | **집계 계산** — 무엇을 스키마 실패로 세는가 (F3 / D-075) |
| 이 파일 | **실행과 디스크** — `run()` 의 흐름, `raw/` 선기록(D-052), 원자적 쓰기 |

둘을 한 파일에 두지 않은 이유는 목이 다르기 때문이다. 집계 쪽은 행 딕셔너리만
있으면 되고, 이쪽은 **보존소와 transport 와 파일시스템**이 다 필요하다.

## 왜 이것들인가

`export/replay.py` 가 만든 수치가 사전 등록 §3② 의 근거이고 ADR-018 의 Evidence 다.
그 수치를 만든 **도구 자체**는 테스트가 없었다 (F5). 여기서 고정하는 것은 셋이다.

1. **`run()` 이 무엇을 순회하고 무엇을 남기는가** — 한 건이 터져도 나머지를 잰다.
   한 건의 실패로 실행이 멈추면 **실패율 자체를 셀 수 없다.**
2. **`raw/` 선기록 (D-052)** — 응답은 변환보다 **먼저** 디스크로 내려간다. 잃는 것은
   돈이 아니라 *같은 입력에 대한 그 모델의 응답*이고, 그건 다시 만들 수 없다.
3. **원자적 쓰기** — 부분 파일이 남으면 다음 실행이 그걸 **결과로 읽는다.**

전부 `transport` 주입으로 돈다 (`tests/test_vllm_client.py` 와 같은 층). 쓰기는
`tmp_path` 안에서만 일어난다 — 실제 `data/replays/` 도 보존소도 건드리지 않는다.
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from collectors.base import RawItem
from export.replay import (
    DEFAULT_REPLAY_DIR,
    _write_json,
    main,
    raw_item_from_stored,
    replay_one,
    run,
    summarize_rows,
)
from export.store import DEFAULT_STORE_DIR, safe_filename
from extraction.extractor import DEFAULT_PROMPT, build_variables, load_prompt
from extraction.llm import StructuredResult, Usage
from extraction.vllm import VLLMClient

CONFIG: dict[str, Any] = {"llm": {"provider": "vllm", "extraction": {"model": "gemma-4-31B-it"}}}

#: 모델이 낸다고 가정하는 응답 본문. 키는 **한국어 alias** 다 (D-003).
ONTOLOGY = {
    "요약": "테스트용 요약 문장이다. 두 번째 문장도 있다.",
    "기술영역": ["Agent"],
    "발표유형": "Paper",
    "관련기업": [],
    "관련기존기술": ["LLM Agent"],
    "영향도": {"점수": 3, "근거": "테스트를 위한 근거 문장이다."},
}


def response(body: object = ONTOLOGY, *, finish: str = "stop", **extra: Any) -> dict[str, Any]:
    """vLLM 응답 1건. `extra` 로 우리가 안 보는 필드도 실어 본다."""
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {
        "id": "chatcmpl-테스트",
        "model": "gemma-4-31B-it",
        "choices": [{"finish_reason": finish, "message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        **extra,
    }


def scripted(*responses: Any, repeat: bool = False):
    """응답을 순서대로 돌려주는 transport. `BaseException` 은 그 자리에서 던진다."""
    queue = list(responses)
    sent: list[dict[str, Any]] = []

    def transport(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        sent.append(payload)
        item = queue[0] if (repeat and len(queue) == 1) else queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    transport.sent = sent  # type: ignore[attr-defined]
    return transport


def client(*responses: Any, repeat: bool = False, **kwargs: Any) -> VLLMClient:
    """transport 주입 클라이언트. **네트워크로 나가지 않는다.**"""
    kwargs.setdefault("model", "gemma-4-31B-it")
    return VLLMClient(transport=scripted(*responses, repeat=repeat), stage="extraction", **kwargs)


@pytest.fixture
def store3(make_store, make_payload):
    """보존소 3건. doc_id 는 파일명 정렬 순서가 눈에 보이게 고른다."""
    return make_store(
        [
            make_payload(doc_id="arXiv_2412.05449v1", url="https://arxiv.org/abs/2412.05449v1"),
            make_payload(doc_id="arXiv_2605.21404v1", url="https://arxiv.org/abs/2605.21404v1"),
            make_payload(doc_id="GeekNews_9001", url="https://news.hada.io/topic?id=9001"),
        ]
    )


@pytest.fixture
def replay(monkeypatch, tmp_path):
    """`run()` 을 `tmp_path` 안에서만 돌린다.

    갈아 끼우는 지점은 **`client_from_config` 하나**다. 프로바이더가 바뀌어도 이
    패치 대상이 그대로인 것이 팩토리를 둔 이유다 (ADR-018).
    """
    seen: list[tuple[dict[str, Any], str]] = []

    def go(store, llm_client, **kwargs: Any) -> Path:
        def factory(config, stage="extraction", **_kw):
            seen.append((config, stage))
            return llm_client

        monkeypatch.setattr("export.replay.client_from_config", factory)
        kwargs.setdefault("run_id", "t")
        return run(
            store_dir=store.directory,
            out_root=tmp_path / "replays",
            config=CONFIG,
            **kwargs,
        )

    go.seen = seen  # type: ignore[attr-defined]
    return go


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# run() — 무엇을 순회하고 무엇을 남기는가
# ---------------------------------------------------------------------------
class TestRun:
    def test_every_stored_item_is_replayed_once(self, store3, replay):
        out = replay(store3, client(response(), repeat=True))
        rows = read(out / "rows.json")
        assert [row["doc_id"] for row in rows] == sorted(store3.doc_ids())
        assert all(row["ok"] for row in rows)
        assert read(out / "summary.json")["item_count"] == 3

    def test_the_order_is_the_store_order_not_the_disk_order(self, store3, replay):
        """줄 순서가 실행마다 달라지면 두 실행을 **줄 단위로 비교할 수 없다.**"""
        first = read(replay(store3, client(response(), repeat=True)) / "rows.json")
        second = read(replay(store3, client(response(), repeat=True), run_id="t2") / "rows.json")
        assert (
            [r["doc_id"] for r in first]
            == [r["doc_id"] for r in second]
            == sorted(store3.doc_ids())
        )

    def test_limit_takes_the_first_n_of_that_order(self, store3, replay):
        out = replay(store3, client(response(), repeat=True), limit=2)
        rows = read(out / "rows.json")
        assert [row["doc_id"] for row in rows] == sorted(store3.doc_ids())[:2]

    def test_limit_zero_replays_nothing(self, store3, replay):
        """`0` 은 "제한 없음"이 아니다 — `None` 과 갈라져 있어야 한다."""
        transport_client = client(response(), repeat=True)
        out = replay(store3, transport_client, limit=0)
        assert read(out / "rows.json") == []
        assert transport_client._transport.sent == []

    def test_an_empty_store_still_writes_a_summary(self, make_store, replay):
        """0건 실행도 기록이다. 요약이 없으면 "안 돌았다"와 구분되지 않는다."""
        out = replay(make_store([]), client())
        summary = read(out / "summary.json")
        assert summary["item_count"] == 0
        assert summary["schema_failure_rate"] is None
        assert read(out / "rows.json") == []

    def test_the_summary_says_what_produced_it(self, store3, replay):
        """모델·프롬프트가 안 적히면 **무엇과 무엇을 비교한 수치인지** 알 수 없다."""
        summary = read(replay(store3, client(response(), repeat=True)) / "summary.json")
        assert summary["run_id"] == "t"
        assert summary["provider"] == "vllm"
        assert summary["model"] == "gemma-4-31B-it"
        assert summary["prompt_name"] == DEFAULT_PROMPT
        datetime.fromisoformat(summary["created_at"])  # 파싱되지 않으면 실패

    def test_the_summary_is_the_rows_on_disk_recomputed(self, store3, replay):
        """요약과 행이 갈라지면 **어느 쪽이 사실인지 판정할 방법이 없다.**"""
        out = replay(store3, client(response(bad := "{"), response(bad), response(), response()))
        rows = read(out / "rows.json")
        summary = read(out / "summary.json")
        assert summary.items() >= summarize_rows(rows).items()

    def test_one_failure_does_not_stop_the_run(self, store3, replay):
        """한 건이 터졌다고 멈추면 **실패율 자체를 셀 수 없다** (사전 등록 §3②)."""
        out = replay(
            store3,
            client(response(), OSError("연결이 끊겼다"), response(), response()),
        )
        rows = read(out / "rows.json")
        summary = read(out / "summary.json")
        assert len(rows) == 3
        assert [row["ok"] for row in rows] == [True, False, True]
        assert summary["transport_failures"] == 1
        assert summary["measured_items"] == 2
        assert summary["schema_failure_rate"] == 0.0

    def test_a_schema_failure_is_a_measurement_not_an_error(self, store3, replay):
        """격리는 **측정 대상**이다. 2회 실패해야 `SchemaMismatchError` 가 된다."""
        out = replay(
            store3,
            client(response(), response(), response("{"), response("{"), response(), response()),
        )
        summary = read(out / "summary.json")
        assert summary["schema_failures"] == 1
        assert summary["measured_items"] == 3
        assert summary["schema_failure_rate"] == round(1 / 3, 4)

    def test_rows_are_flushed_after_every_item(self, store3, replay, tmp_path):
        """중간에 죽어도 거기까지는 남는다 — 끝에 한 번만 쓰면 **비용만 날린다.**

        3번째에서 `KeyboardInterrupt` 로 죽인다. `replay_one` 의 `except Exception`
        이 잡지 못하는 종류라 실행이 그대로 멈춘다 — 실제 Ctrl-C 와 같은 경로다.
        """
        with pytest.raises(KeyboardInterrupt):
            replay(store3, client(response(), response(), KeyboardInterrupt()))

        rows = read(tmp_path / "replays" / "t" / "rows.json")
        assert [row["doc_id"] for row in rows] == sorted(store3.doc_ids())[:2]
        assert not (tmp_path / "replays" / "t" / "summary.json").exists()

    def test_the_client_is_built_once_for_the_extraction_stage(self, store3, replay):
        """게이트를 다시 태우면 **어느 단계의 차이인지** 구분할 수 없다 (모듈 설명)."""
        replay(store3, client(response(), repeat=True))
        assert replay.seen == [(CONFIG, "extraction")]

    def test_nothing_is_written_outside_the_run_directory(self, store3, replay, tmp_path):
        before = {p: p.read_bytes() for p in sorted(store3.directory.glob("*.json"))}
        out = replay(store3, client(response(), repeat=True))
        assert {p: p.read_bytes() for p in sorted(store3.directory.glob("*.json"))} == before
        assert [p.name for p in (tmp_path / "replays").iterdir()] == ["t"]
        assert out == tmp_path / "replays" / "t"

    def test_the_default_run_id_is_a_utc_timestamp(self, store3, replay):
        """`run_id` 를 안 주면 실행이 **서로 덮어쓰지 않아야** 한다."""
        out = replay(store3, client(response(), repeat=True), run_id=None)
        assert re.fullmatch(r"\d{8}T\d{6}Z", out.name)
        assert datetime.strptime(out.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)

    def test_the_prompt_name_is_recorded_on_every_row(self, store3, replay):
        """프롬프트가 바뀌면 모델만 바꾼 대조가 아니다 — 행마다 적어 둔다."""
        out = replay(store3, client(response(), repeat=True), prompt_name="extract_ontology.v3.md")
        rows = read(out / "rows.json")
        assert {row["prompt_name"] for row in rows} == {"extract_ontology.v3.md"}
        assert {row["baseline_prompt"] for row in rows} == {"extract_ontology.v4.md"}

    def test_the_summary_goes_to_stdout_and_progress_to_stderr(self, store3, replay, capsys):
        """stdout 이 **그대로 파싱되는 JSON** 이어야 파이프로 넘길 수 있다."""
        replay(store3, client(response(), repeat=True))
        captured = capsys.readouterr()
        assert json.loads(captured.out)["item_count"] == 3
        assert "arXiv_2412.05449v1" in captured.err


# ---------------------------------------------------------------------------
# raw/ 선기록 — D-052
# ---------------------------------------------------------------------------
class TestRawIsWrittenBeforeTheTransform:
    """**값비싼 결과와 검증되지 않은 변환 코드를 같은 트랜잭션에 두지 않는다.**

    여기서 값비싼 것은 돈이 아니라 *같은 입력에 대한 그 모델의 응답*이다. 변환이
    터져서 그것까지 같이 잃으면 **다시 만들 수 없다** — 다시 부르면 그건 다른 응답이다.
    """

    def test_every_response_lands_under_raw(self, store3, replay):
        out = replay(store3, client(response(), repeat=True))
        for doc_id in store3.doc_ids():
            assert (out / "raw" / safe_filename(doc_id)).is_file()

    def test_the_file_is_the_response_untouched(self, store3, replay):
        """우리가 안 읽는 필드도 그대로 남는다 — 나중에 무엇이 필요할지 모른다."""
        original = response(extra_field={"우리가": "안 보는 값"})
        out = replay(store3, client(original, repeat=True), limit=1)
        saved = read(out / "raw" / safe_filename(min(store3.doc_ids())))
        assert saved == [original]

    def test_a_retry_keeps_both_responses(self, store3, replay):
        """1차 응답을 버리면 **재시도가 무엇을 고쳤는지** 볼 수 없다."""
        first, second = response("{"), response()
        out = replay(store3, client(first, second), limit=1)
        doc_id = min(store3.doc_ids())
        assert read(out / "raw" / safe_filename(doc_id)) == [first, second]
        assert read(out / "rows.json")[0]["finish_reasons"] == ["stop", "stop"]

    def test_a_schema_failure_still_leaves_the_response(self, store3, replay):
        """격리된 건이야말로 원본이 필요하다 — **왜 못 지켰는지**가 거기 있다."""
        out = replay(store3, client(response("{"), response("{")), limit=1)
        doc_id = min(store3.doc_ids())
        assert read(out / "rows.json")[0]["failure"] == "SchemaMismatchError"
        assert len(read(out / "raw" / safe_filename(doc_id))) == 2

    def test_a_truncated_response_is_marked_by_its_own_finish_reason(self, store3, replay):
        """`length` 를 스키마 실패와 같이 세면 ②가 `max_tokens` 를 재게 된다."""
        out = replay(store3, client(response("{", finish="length"), response()), limit=1)
        summary = read(out / "summary.json")
        assert read(out / "rows.json")[0]["finish_reasons"] == ["length", "stop"]
        assert summary["truncated"] == 1
        assert summary["retried"] == 1

    def test_a_transport_failure_writes_no_raw_file(self, store3, replay):
        """빈 파일을 남기면 **다음 실행이 그걸 응답으로 읽는다.**"""
        out = replay(store3, client(OSError("연결이 끊겼다")), limit=1)
        assert not (out / "raw").exists()
        assert read(out / "rows.json")[0]["failure"] == "OSError"

    def test_the_response_is_on_disk_before_the_row_is_built(self, tmp_path, make_payload):
        """**이 파일의 핵심 검사.** 행을 만드는 코드가 터져도 응답은 이미 내려가 있다.

        `raw/` 기록이 변환 **뒤**에 있으면 이 검사가 빨개진다 — 그때 잃는 것이
        정확히 D-052 가 잃지 말라고 한 것이다.
        """

        class Exploding:
            """행을 만들 때 호출되는 변환. 검증되지 않은 코드를 대신한다."""

            def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("변환 코드가 터졌다")

        raws = [response()]

        class Client:
            model = "gemma-4-31B-it"
            last_raw_responses = raws

            def parse_into(self, *, system, user, output_model):
                return StructuredResult(value=Exploding(), usage=Usage(), attempts=1)

        stored = make_payload(doc_id="arXiv_2412.05449v1", url="https://arxiv.org/abs/2412.05449v1")
        with pytest.raises(RuntimeError, match="변환 코드"):
            replay_one(stored, Client(), load_prompt(DEFAULT_PROMPT), tmp_path)

        assert read(tmp_path / "raw" / safe_filename("arXiv_2412.05449v1")) == raws


# ---------------------------------------------------------------------------
# raw_item_from_stored — 한 글자도 손보지 않는다
# ---------------------------------------------------------------------------
class TestRawItemFromStored:
    """정규화·트리밍이 한 글자라도 들어가면 **대조가 "모델 교체"가 아니게 된다.**"""

    def test_a_stored_item_round_trips_to_itself(self, make_payload):
        payload = make_payload(doc_id="d", url="https://arxiv.org/abs/2412.05449v1")
        item = raw_item_from_stored(payload)
        assert item.model_dump(mode="json") == payload["raw_item"]

    def test_whitespace_and_line_endings_survive(self, make_payload):
        """`.strip()` 하나가 들어가면 **프롬프트에 실리는 본문이 달라진다.**"""
        body = "  첫 줄\r\n\r\n둘째 줄 — 끝에 공백  \n"
        title = "  제목에 앞뒤 공백이 있다  "
        payload = make_payload(doc_id="d", url="https://arxiv.org/abs/x", title=title, body=body)
        item = raw_item_from_stored(payload)
        assert item.title == title
        assert item.body == body

    def test_the_prompt_gets_the_body_verbatim(self, make_payload):
        body = "본문에 {중괄호} 와 % 와 \\ 가 들어 있다."
        payload = make_payload(doc_id="d", url="https://arxiv.org/abs/x", body=body)
        variables = build_variables(raw_item_from_stored(payload))
        assert variables["body"] == body
        assert body in load_prompt(DEFAULT_PROMPT).render(**variables)[1]

    def test_optional_fields_keep_their_absence(self, make_payload):
        """`None` 이 "오늘"로 채워지면 **없던 발행일이 생긴다.**"""
        payload = make_payload(doc_id="d", url="https://arxiv.org/abs/x", published_at=None)
        assert raw_item_from_stored(payload).published_at is None

    def test_tag_order_is_kept(self, make_payload):
        payload = make_payload(doc_id="d", url="https://arxiv.org/abs/x", tags=("ko", "AI", "ko"))
        assert raw_item_from_stored(payload).tags == ("ko", "AI", "ko")

    def test_the_url_is_the_one_field_that_is_re_parsed(self, make_payload):
        """**보존소 값은 이미 `RawItem` 을 거쳐 나온 값이라 고정점이다.**

        손으로 써 넣은 값만 정규화된다. 이 칸이 있다는 것을 적어 두는 이유는,
        보존소를 손으로 고치면 그 순간 "같은 입력"이 아니게 되기 때문이다.
        """
        payload = make_payload(doc_id="d", url="https://news.hada.io")
        assert str(raw_item_from_stored(payload).url) == "https://news.hada.io/"

        again = dict(payload, raw_item=raw_item_from_stored(payload).model_dump(mode="json"))
        assert raw_item_from_stored(again).model_dump(mode="json") == again["raw_item"]

    def test_a_missing_raw_item_fails_loudly(self):
        """조용히 빈 항목을 재추출하면 **그 행이 무엇을 잰 것인지 알 수 없다.**"""
        with pytest.raises(KeyError):
            raw_item_from_stored({"doc_id": "d"})

    def test_a_broken_raw_item_fails_loudly(self, make_payload):
        payload = make_payload(doc_id="d", url="https://arxiv.org/abs/x")
        payload["raw_item"].pop("title")
        with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
            raw_item_from_stored(payload)


# ---------------------------------------------------------------------------
# _write_json — 부분 파일이 남으면 다음 실행이 그걸 결과로 읽는다
# ---------------------------------------------------------------------------
class TestAtomicWrite:
    def test_it_creates_parent_directories(self, tmp_path):
        target = tmp_path / "깊이" / "더깊이" / "rows.json"
        _write_json(target, [{"doc_id": "d"}])
        assert read(target) == [{"doc_id": "d"}]

    def test_korean_stays_readable_and_lines_end_with_lf(self, tmp_path):
        """`\\r\\n` 으로 쓰면 같은 결과가 OS 마다 다른 바이트가 된다."""
        target = tmp_path / "rows.json"
        _write_json(target, {"요약": "한국어"})
        raw = target.read_bytes()
        assert "요약" in raw.decode("utf-8")
        assert b"\\uc694" not in raw
        assert b"\r\n" not in raw
        assert not raw.startswith(b"\xef\xbb\xbf")  # BOM

    def test_no_temp_file_survives_a_successful_write(self, tmp_path):
        _write_json(tmp_path / "rows.json", [1])
        assert [p.name for p in tmp_path.iterdir()] == ["rows.json"]

    def test_a_failed_write_leaves_nothing_behind(self, tmp_path):
        """반쯤 쓰인 파일이 남으면 **다음 실행이 그걸 결과로 읽는다.**"""
        target = tmp_path / "rows.json"
        with pytest.raises(TypeError):
            _write_json(target, {"직렬화 불가": object()})
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_failed_write_leaves_the_previous_file_intact(self, tmp_path):
        """**누적본이 이것 하나다.** 갱신에 실패해서 이전 것까지 잃으면 안 된다."""
        target = tmp_path / "rows.json"
        _write_json(target, [{"doc_id": "먼저 쓴 것"}])
        with pytest.raises(TypeError):
            _write_json(target, {"직렬화 불가": object()})
        assert read(target) == [{"doc_id": "먼저 쓴 것"}]
        assert [p.name for p in tmp_path.iterdir()] == ["rows.json"]

    def test_an_interrupt_mid_write_also_cleans_up(self, tmp_path, monkeypatch):
        """`except Exception` 이면 **Ctrl-C 가 임시 파일을 남긴다.**"""
        target = tmp_path / "rows.json"

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr("export.replay.json.dump", boom)
        with pytest.raises(KeyboardInterrupt):
            _write_json(target, [1])
        assert list(tmp_path.iterdir()) == []

    def test_the_temp_file_sits_next_to_the_target(self, tmp_path, monkeypatch):
        """다른 파일시스템이면 `replace` 가 **원자적이지 않다** (또는 실패한다)."""
        seen: list[str] = []
        real = tempfile.mkstemp

        def spy(*args: Any, **kwargs: Any):
            seen.append(kwargs.get("dir"))
            return real(*args, **kwargs)

        monkeypatch.setattr("export.replay.tempfile.mkstemp", spy)
        target = tmp_path / "out" / "rows.json"
        _write_json(target, [1])
        assert seen == [target.parent]

    def test_it_overwrites_in_place(self, tmp_path):
        """매 건 갱신이 append 가 아니라 **덮어쓰기**다 — 누적본은 한 파일이다."""
        target = tmp_path / "rows.json"
        _write_json(target, [1])
        _write_json(target, [1, 2])
        assert read(target) == [1, 2]
        assert [p.name for p in tmp_path.iterdir()] == ["rows.json"]


# ---------------------------------------------------------------------------
# CLI — 명령줄이 run() 의 어느 인자로 가는가
# ---------------------------------------------------------------------------
class TestCli:
    """**실행은 언제나 명령줄로 시작한다.** 여기서 인자가 어긋나면 위의 검사가
    전부 초록불이어도 실제 실행은 다른 것을 한다."""

    @pytest.fixture
    def calls(self, monkeypatch, tmp_path):
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr("export.replay.run", lambda **kwargs: seen.append(kwargs) or tmp_path)
        return seen

    def test_no_arguments_means_the_default_store_and_replay_dir(self, calls):
        assert main([]) == 0
        assert calls[0] == {
            "store_dir": DEFAULT_STORE_DIR,
            "out_root": DEFAULT_REPLAY_DIR,
            "run_id": None,
            "limit": None,
            "prompt_name": DEFAULT_PROMPT,
        }

    def test_every_flag_reaches_the_matching_argument(self, calls, tmp_path):
        flags = {
            "--store": str(tmp_path / "보존소"),
            "--out": str(tmp_path / "replays"),
            "--run-id": "sample",
            "--limit": "3",
            "--prompt": "extract_ontology.v3.md",
        }
        main([token for pair in flags.items() for token in pair])
        assert calls[0] == {
            "store_dir": tmp_path / "보존소",
            "out_root": tmp_path / "replays",
            "run_id": "sample",
            "limit": 3,
            "prompt_name": "extract_ontology.v3.md",
        }

    def test_the_limit_is_an_int_not_a_string(self, calls):
        """문자열이면 `doc_ids[:limit]` 이 **실행 도중에** 터진다."""
        main(["--limit", "3"])
        assert calls[0]["limit"] == 3


def test_the_item_the_store_holds_is_the_item_that_is_replayed(make_payload, tmp_path):
    """보존소 → `RawItem` → 프롬프트까지 **한 줄로 이어지는지** 끝에서 확인한다."""
    payload = make_payload(
        doc_id="arXiv_2412.05449v1",
        url="https://arxiv.org/abs/2412.05449v1",
        title="Ontology-Guided Extraction",
        body="본문 첫 문장이다.\n\n두 번째 문단이다.",
    )
    llm = client(response())
    row = replay_one(payload, llm, load_prompt(DEFAULT_PROMPT), tmp_path)

    # 보존소의 본문이 **그대로** 요청 payload 에 실렸는가
    sent_user = llm._transport.sent[0]["messages"][-1]["content"]
    assert payload["raw_item"]["body"] in sent_user
    assert payload["raw_item"]["title"] in sent_user
    assert row["url"] == str(RawItem.model_validate(payload["raw_item"]).url)
    assert row["source_name"] == payload["raw_item"]["source_name"]
    assert row["baseline_model"] == payload["extraction"]["model"]
    assert row["baseline_prompt_sha256"] == payload["extraction"]["prompt_sha256"]
