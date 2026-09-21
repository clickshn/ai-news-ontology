# Session 06 핸드오프 — F2: 수집기가 무기한 멈추지 않게 만들었다

- **날짜:** 2026-09-21
- **범위:** session-05 §5 의 **F2** 처리. 원인 확인 → 수정 → 변이 검사 → 실피드 확인.
  ADR 없음(§4), 결정 로그 **D-069 · D-070**.
- **브랜치:** `feat/f2-collector-timeout` (**push 하지 않음**)
- **테스트:** `.venv/Scripts/python.exe -m pytest tests/ -q` → **487 passed** (474 → +13 신규)
- **게이트 검사 46건 통과 0 실패** (변동 없음)
- **LLM API 호출 0건.** 공개 RSS 피드에 GET 5건 — 실호출 성격과 이유는 §8
- **새 의존성 0개** (`urllib` 은 표준 라이브러리)

> **다음 세션이 먼저 읽을 곳:** **§3(고친 것이 타임아웃만이 아니다)**,
> **§5(남은 결함 9건 — F13 이 새로 늘었다)**, §6(결정 대기).

---

## 1. 무엇을 했나

| 파일 | 변경 |
|---|---|
| `collectors/rss.py` | `_fetch_feed_bytes()` 신규 — `urllib` 로 timeout 을 걸어 bytes 를 받고 `feedparser.parse(data)` 에 넘긴다. `FETCH_TIMEOUT_S`·`FEED_MAX_BYTES`·`USER_AGENT`·`FEED_ACCEPT` 상수, `FeedTooLargeError`. `fetch_source`/`collect` 에 `timeout` 인자, CLI 에 `--timeout` |
| `tests/test_rss_collector.py` | **신규 13건.** `fetch_source` 테스트가 0건이었다 |
| `collectors/README.md` | "HTTP 를 feedparser 에 맡기지 않는다" 절 + 실패 로깅 규칙 1줄 |
| `README.md` | 결정 로그 **D-069 · D-070** |

---

## 2. 원인 → 수정 → 재검증

**원인은 "타임아웃을 안 걸었다"가 아니라 "걸 자리가 없었다"다.**

```
inspect.signature(feedparser.parse)
  → (url_file_stream_or_string, etag, modified, agent, referrer,
     handlers, request_headers, response_headers, resolve_relative_uris, sanitize_html)
socket.getdefaulttimeout() → None
```

`parse` 에 timeout 인자가 없고 전역 소켓 타임아웃도 `None` 이다. 그래서 연결만
받고 응답을 시작하지 않는 서버에 붙으면 **영원히** 기다리고, 그 사이 stderr 에
아무것도 안 나온다. 실패가 아니라 **정지**라서, 증상만 보고는 어느 피드인지도
알 수 없다.

수정은 HTTP 를 `urllib` 로 가져와 feedparser 에는 bytes 만 넘기는 것이다.

### 변이 검사 — 새 테스트가 실제로 잡는지 확인했다

| 변이 | 실패 |
|---|---|
| `urlopen(..., timeout=timeout)` → `timeout=12` 고정 | 3건 (실소켓 1 + 전달 2) |
| 타임아웃/일반 오류 구분 제거 | 3건 |
| 크기 상한 제거 | 1건 |
| `HTTPError` 분기 제거 | 1건 |
| 빈 Content-Type 을 무조건 전달 | 1건 |
| 복원 후 | **13/13 통과** |

### 실제 피드로 확인했다

`python -m collectors.rss --limit 1 --timeout 15` → **5소스 5건 전부 수집.**
UA 를 바꿨는데 403 이 없는지가 확인 대상이었다(§3). 본문 길이 분포도
`config.yaml` 의 `body_quality` 표와 같다 (DeepMind·HF 는 0자).

---

## 3. 🚩 고친 것이 타임아웃만이 아니다 — UA 가 같이 딸려 왔다

**HTTP 를 가져오면 feedparser 가 해 주던 일도 같이 가져온다.** 핸드오프에 적힌
F2 는 타임아웃 하나였지만, `parse(url)` 을 버리는 순간 세 가지가 함께 움직인다.

1. **User-Agent.** feedparser 는 자기 UA 를 붙여 요청했다. 그 자리를 비우면
   urllib 기본값 `Python-urllib/3.x` 로 나가고, **일부 CDN 이 이걸 403 으로
   막는다.** 안 붙였으면 "타임아웃을 고쳤더니 피드가 안 들어온다"가 됐을 것이다.
   `USER_AGENT` 로 명시했고 `export/urls.py` 의 `ARXIV_USER_AGENT` 와 형식을 맞췄다.
2. **HTTP 상태.** 4xx/5xx 가 `HTTPError` 로 올라오므로
   `getattr(feed, "status", None)` 우회가 사라졌다 — **의도한 부수 효과**다.
3. **Content-Type.** 인코딩 판정 힌트라 feedparser 에 그대로 넘기는데,
   **헤더가 없을 때 빈 문자열을 넘기면 안 된다.** `"" is not an XML media type`
   으로 bozo 가 서서, 항목 없는 정상 피드가 **파싱 실패로 오인된다.**
   있을 때만 넘긴다 (`test_missing_content_type_...`).

> **왜 안 걸렸나.** `fetch_source` 에 테스트가 **0건**이었다. F1 때와 같은 모양이고
> (session-05 §3), 이번에도 결함을 찾은 것은 코드가 아니라 사람의 눈이었다.
>
> 그래서 이번 테스트의 축은 **"timeout 인자를 넘겼는가"가 아니라 "실제로
> 끊기는가"**로 잡았다. 전자는 인자 이름만 맞으면 통과하는데, F2 는 인자 이름이
> 아니라 동작의 부재였다. `test_timeout_actually_fires_against_a_silent_server` 가
> 루프백에 **응답하지 않는 서버**를 띄우고 붙는다. 이 테스트에는 단정이 세 개인데
> `elapsed >= 0.5`(실제로 기다렸다)와 로그 문구 대조가 없으면 **연결 거부로도
> 통과한다** — 그러면 재는 것이 타임아웃이 아니라 "실패하면 빈 리스트"가 된다.

---

## 4. 설계 판단 — 왜 이렇게 했나

**ADR 을 남기지 않았다.** 되돌리기 비용 기준에 걸리지 않는다 — 새 의존성 0개
(`urllib` 은 표준 라이브러리), 데이터 형식·식별자 불변, 외부 서비스 선택 아님,
되돌리기는 이 함수 하나를 원복하는 일이다. 결정 로그 D-069·D-070 으로 남겼다.
**다른 판단이면 여기가 아니라 `adr-skill:adr-recorder` 로 갔어야 한다.**

- **`requests` 를 넣지 않았다.** 타임아웃 하나 때문에 의존성을 들이는 거래가
  안 맞는다. 필요한 것은 `urlopen(timeout=)` 한 줄이다.
- **`socket.setdefaulttimeout()` 을 쓰지 않았다.** 프로세스 전역에 걸려
  수집과 무관한 호출까지 영향권에 들어온다. 범위가 문제에 비해 너무 넓다.
- **타임아웃을 `config.yaml` 에 올리지 않았다** (D-070). 미러가 하나 느는 일이고
  (D-002 가 경계하는 것), 소스마다 다르게 줄 근거가 아직 없다. 함수 인자로
  열어 두면 테스트가 0.5초로 낮춰 실제 동작을 잴 수 있고 지금 필요한 건 그것뿐이다.
- **크기 상한(`FEED_MAX_BYTES` 8MB)을 같이 넣었다.** 시간만 막고 크기를 열어 두면
  끝나지 않는 스트림에 메모리가 나간다. **잘라서 파싱하지 않고 실패로 다룬다** —
  잘린 XML 을 넘기면 원인이 "파싱 실패"로 **잘못 기록된다.**
- **재시도는 여전히 없다.** 모듈이 원래 적어 둔 방침 그대로다 — 어떤 실패가 실제로
  잦은지 모르는 상태에서 만든 재시도 정책은 대개 틀린다. 이제 실패가 로그로 남으니
  양상을 관찰할 수 있게 됐다.

### ⚠️ 이 수정이 보장하지 **않는** 것

`FETCH_TIMEOUT_S` 는 **소켓 연산 1회**(연결 / 1회 수신)의 상한이지 **총 소요
시간의 상한이 아니다.** 조금씩 끝없이 흘려보내는 서버는 수신할 때마다 시간을
새로 받아 전체가 이 값보다 길어질 수 있다. 막으려던 것("응답을 시작하지 않는
서버")은 끊긴다. 총 예산까지 거는 것은 §6 결정 대기로 남겼다 —
**"타임아웃을 걸었다"를 "총 시간이 묶였다"로 읽지 말 것.**

---

## 5. 🚩 남은 결함 — F2 가 빠지고 F13 이 늘었다

**session-04 §4 표가 여전히 유일한 기록이다** (리뷰 원문은 레포에 없다). 지우지 말 것.

| | 위치 | 내용 | 심각도 | 상태 |
|---|---|---|---|---|
| ~~F1~~ | `config.yaml` | ~~롤백 경로 파손~~ | 🔴 | ✅ session-05 완료 |
| ~~F2~~ | `collectors/rss.py` | ~~타임아웃 없음 → 무기한 블로킹~~ | 🔴 | ✅ **이번 세션 완료** (부분 한계는 §4 말미) |
| **F13** | `export/urls.py:69` | 🆕 **F2 와 같은 결함이 여기에도 있다** — 아래 | 🔴 | 미처리 |
| **F3** | `export/replay.py:178-179` | 전송 실패를 스키마 실패로 집계 | 🟡 | 미처리 |
| **F4** | `export/usage.py:23` | 모델명을 안 봄 → gemma 실행에 없는 달러 비용 | 🟡 | 미처리 |
| **F5** | `export/replay.py` | 테스트 0건 — 사전 등록 수치를 만든 측정 도구가 미검증 | 🟡 | 미처리 |
| **F9** | `README.md:204,231` | `D-003` 두 줄, 상태가 서로 다름 | 🟡 | 미처리 |
| **F6** | `extraction/llm.py:363` | `VENDOR_ONLY_PARAMS` 죽은 상수 | 🟢 | 미처리 (session-05 §7 참고) |
| **F7** | `pyproject.toml:16` | `anthropic` core dep + 틀린 주석 | 🟢 | 미처리 (session-05 §7) |
| **F8** | `obsidian_writer/mapper.py:113` | `_wikilink` 가 `#` 미처리 | 🟢 | 미처리 |
| **F10** | `docs/governance.md` | `data/replays/` 가 로컬 산출물 표에 없음 | 🟢 | 미처리 |

### 🆕 F13 — `export/urls.py` 에 같은 결함이 남아 있다

`_parse_with_retry()` 가 `feedparser.parse(url, agent=...)` 를 그대로 부른다.
**F2 와 똑같이 타임아웃이 없다.** 그런데 여기는 더 나쁘다:

- 빈 응답이면 `ARXIV_RETRY_DELAYS_S = (5, 20, 60)` 으로 **재시도한다.** 응답 없는
  서버라면 타임아웃 없는 요청 4번 + `sleep` 85초가 얹힌다.
- arXiv rate limit 때문에 재시도를 공격적으로 못 두는 자리라, **끊고 다시 거는
  것과 끝까지 기다리는 것 중 무엇이 맞는지가 F2 와 다르다.** 그래서 이번 커밋에
  같이 넣지 않았다 — 같은 수정을 복사하면 그 판단을 건너뛰게 된다.
- `collectors.rss` 의 `_fetch_feed_bytes` 를 재사용할지, `export/urls.py` 에
  따로 둘지도 같이 정해야 한다. 지금 의존 방향은 `export → collectors` 뿐이다.

### 권고 순서

| 순서 | 항목 | 이유 |
|---|---|---|
| 1 | **F4 + F3** | 둘 다 "틀린 수치가 의사결정에 들어가는" 종류. F4 는 2단계 220건 추정에 직결 |
| 2 | **F13** | F2 와 같은 종류인데 **재시도가 얹혀 있어 더 오래 멈춘다.** 기억이 남아 있을 때 |
| 3 | **F5** | 다음 대조 실행 **전에** |
| 4 | **F9** | 결정 로그 무결성은 이 레포의 핵심 자산 |
| 5 | F6 · F7 · F8 · F10 | 묶어서 한 커밋 |

### 확인 명령 (이번 세션 것만 갱신)

```bash
# F2 재발 탐지 — 실제로 끊기는가 / 실패가 로그에 남는가
.venv/Scripts/python.exe -m pytest tests/test_rss_collector.py -q

# F13 — export 쪽에 아직 남은 같은 결함
grep -n "feedparser.parse" export/urls.py collectors/rss.py

# 나머지 확인 명령은 session-04 §4 그대로 유효
```

---

## 6. 결정 대기

session-03 §7 · session-04 §5 · session-05 §6 의 항목은 **전부 그대로 유효하다.**
중복해 옮기지 않고 가리킨다. 이번 세션에서 **새로 생긴 것만** 적는다.

| 항목 | 내용 |
|---|---|
| 🆕 **총 소요 시간에도 상한을 걸까** | 지금 상한은 소켓 연산 1회짜리다. 조금씩 흘려보내는 서버는 안 끊긴다(§4 말미). 총 예산을 걸려면 청크 수신 루프 + 마감 시각이 필요하고, **실제로 그런 서버를 만난 적은 없다.** 없는 위협에 코드를 늘리는 쪽과, "걸었다"는 말이 실제보다 넓게 읽히는 쪽 중 어느 게 나쁜지의 문제다 |
| 🆕 **실패를 stderr 말고 관측 로그에 남길까** | 지금은 `[fail]`/`[skip]` 이 stderr 로만 간다. 수집 실패율은 **소스 품질 지표**인데(`body_quality` 실측과 같은 계열) 파이프를 안 받으면 사라진다. `observability/logs/` 에 소스별로 쌓으면 D-013/D-014 를 갱신할 근거가 된다. 다만 이 레포에 로깅 계층이 없고, session-05 의 `dropped_params` 항목과 **같은 질문**이다 — 따로 답하지 말 것 |
| 🆕 **`USER_AGENT` 가 두 곳에 생겼다** | `collectors/rss.py: USER_AGENT` 와 `export/urls.py: ARXIV_USER_AGENT`. 지금은 용도가 갈려서(피드 수집 / 계약 export) 일부러 나눠 뒀지만, F13 을 처리하면 합칠지 정해야 한다 |
| **F12 — ADR 스킬 우회** | 변화 없음. 이번 세션은 ADR 을 만들지 않아 스킬을 부를 자리가 없었다 (§4) |
| **`VLLM_BASE` 두 레포 중복** | 변화 없음 |

---

## 7. 커버리지 — 이번에 심은 테스트가 무엇을 재는가

| 축 | 테스트 |
|---|---|
| **실제로 끊기는가** | `test_timeout_actually_fires_against_a_silent_server` (루프백 실소켓) |
| 타임아웃이 로그로 남는가 | `..._during_read_...`, `..._during_connect_...` (두 경로로 온다) |
| 타임아웃 값이 전달되는가 | `test_timeout_reaches_urlopen`, `test_collect_passes_timeout_to_each_source` |
| **피드 1개가 전체를 죽이지 않는가** | `test_one_dead_feed_does_not_stop_the_rest` (F2 의 증상 자체) |
| HTTP 상태 · 일반 오류 · 크기 상한 | `..._http_error_...`, `..._generic_url_error_...`, `..._oversized_...` |
| 회귀(bytes 경로가 같은 결과를 내는가) | `..._same_raw_items`, `..._user_agent`, `..._content_type`, `..._limit_...` |

---

## 8. 승인 게이트

| # | 항목 | 이번 세션 |
|---|---|---|
| 1 | **대상 엔드포인트가 내부 vLLM 인가** | **해당 없음 — LLM 호출 0건.** `config.yaml: llm.provider` 는 `vllm` 그대로, 손대지 않았다 |
| 2 | 나가는 데이터 | **없음.** 아래 실호출은 전부 GET 이고 본문을 보내지 않는다 |
| 3~6 | 목적 / 모델명 / 건수 / 비용 | 해당 없음 (모델 호출 아님) |

`.claude/external-llm-approved` 를 만들지 않았다. 새 의존성 0개.

> **실호출을 하나 했다는 것은 적어 둔다.** `python -m collectors.rss --limit 1` 로
> `config.yaml` 에 있는 공개 RSS 피드 5곳에 GET 을 보냈다. LLM API 가 아니라
> 이 모듈의 본래 동작이고 비용도 외부 상태 변경도 없지만, **UA 를 바꾼 수정이라
> 실호출로만 확인되는 부분(403)이 있었다**는 것이 이유다. 나머지는 전부 루프백
> 소켓과 목으로 확인했다.

---

## 9. 상태

| 항목 | 상태 |
|---|---|
| 변경 코드 | `collectors/rss.py` |
| 변경 설정 | **없음** (D-070 — 타임아웃을 `config.yaml` 에 올리지 않았다) |
| 신규 테스트 | `tests/test_rss_collector.py` — 13건 |
| 신규 문서 | 이 파일 |
| 변경 문서 | `README.md` (D-069·D-070), `collectors/README.md` |
| ADR | **없음** — 근거는 §4 |
| 테스트 | **487 passed** (474 → +13) / 게이트 검사 **46 통과 0 실패** |
| LLM API 호출 | **0건** (공개 RSS GET 5건은 §8) |
| 커밋 | 있음. **push 는 사용자 확인 후** |

## 주의 (이월, 여전히 유효)

- 테스트는 가상환경으로: `.venv/Scripts/python.exe -m pytest tests/ -q`.
- **실제 Obsidian Vault 에 절대 쓰지 않는다.** 관측 로그·보존소·`data/replays/` 도 같다.
- 실행에 쓰인 프롬프트 버전은 **수정하지 않는다.** 새 버전 파일을 만든다.
- 문서·규칙에 벤더 호출 표현을 쓸 때는 **Write/Edit 도구**로 쓴다. 명령줄에 넣으면
  훅이 그 명령 자체를 차단한다.
- 셸 스크립트는 **LF** 로 유지한다. CRLF 면 훅이 조용히 안 돈다.
- **push 전에** `git log --oneline` 으로 확인한다.
