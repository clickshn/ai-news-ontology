# ai-news-ontology 프로젝트 가이드

AI 뉴스/논문을 온톨로지 스키마로 구조화하고, LLM-judge 기반 eval로 요약 품질을
검증하며, Obsidian Vault에 마크다운으로 적재하는 개인 파이프라인. 상세 아키텍처와
설계 결정은 README.md의 "결정 로그" 참고.

## API 호출 규칙 (중요, 항상 적용)

실제 Anthropic API를 호출하는 작업(LLM 추출, relevance gate, eval judge 실행,
프롬프트 테스트용 실호출 등)을 하기 전에는 반드시 먼저 다음을 알려주고 확인을 받을 것:
- 목적 (무엇을 위한 호출인지)
- 사용 모델 (예: claude-haiku-4-5-20251001, claude-opus-5)
- 호출 건수
- 예상 비용 (토큰 추정치 기반)

확인 없이 API를 호출하지 않는다. 코드 작성, 파일 읽기/쓰기, mock 기반 테스트 실행은
확인 없이 진행해도 된다 — 실제 API 호출만 예외.

**모델마다 받는 파라미터가 다르다.** Opus 5는 `temperature`를 400으로 거부하고
(`deprecated for this model`), Haiku 4.5는 `output_config.effort`를 거부한다.
단계별 설정은 `config.yaml: llm.{relevance_gate,extraction}`에 나뉘어 있다
(D-018, D-033).

## 작업 원칙

### 프롬프트
- 실행에 쓰인 프롬프트 버전(예: `extract_ontology.v2.md`)은 **수정하지 않는다.**
  변경이 필요하면 새 버전 파일을 만든다 (D-010, D-023). 파일명 관례는
  `{용도}.{버전}.md`이고 `extraction/prompts/README.md`에 정리돼 있다.
- 통제어휘는 프롬프트에 하드코딩하지 않고 `schema.py` Enum에서 런타임 주입한다 (D-012).

### 스키마와 어휘
- 통제어휘(`extraction/schema.py`의 Enum)와 `config.yaml`의 미러는 항상 동기화
  상태를 유지하고, `tests/test_vocab_sync.py`로 검증한다. `schema.md`의 표도 같이
  고쳐야 하지만 그건 자동 검증 대상이 아니다.
- 어휘를 쪼개거나 이름을 바꾸면 **과거 노트의 라벨이 소급해서 틀린 것이 된다.**
  판단 기준이 바뀌는 정도라면 스키마가 아니라 문서·프롬프트 층에서 좁힌다 (D-022).

### 결정 로그
- 새로운 설계 판단은 README.md 결정 로그에 `D-XXX` 형식으로 기록한다. 근거와
  검토했던 대안을 함께 남긴다.
- 판단을 번복할 때 **기존 항목을 지우지 않는다.** 상태를 `번복됨` / `부분 번복`으로
  바꾸고 사유를 남긴 뒤, 새 결정을 별도 항목으로 추가한다 (예: D-003 → D-029).

### git
- main에 직접 커밋하지 않는다. `feat/<주제>` 형식의 브랜치에서 작업한 뒤
  `git merge --ff-only`로 병합한다.
- 커밋 메시지는 Conventional Commits 형식이고 **요약과 본문 모두 영어로** 쓴다
  (포트폴리오 공개 레포). 본문은 무엇을·왜를 2~4줄로, 관련 결정 로그 번호를 참조한다.
- 논리 단위로 커밋을 나눈다. 한 파일이 여러 단위에 걸치면 hunk 단위로 쪼개되,
  억지로 쪼개지 말고 가장 관련 있는 커밋에 합쳐도 된다.
- **push는 사용자가 직접 확인 후 진행한다.** 커밋까지만 만들고 `git log --oneline`을
  보여준 뒤 멈춘다.

### 커밋 전 확인
매 커밋 직전에 staged 내용을 검사한다.
- `git status`에 `.env`가 없는지
- staged diff에 API 키 패턴(`sk-ant-`), 개인 로컬 경로(`C:\`, Vault 디렉터리 이름,
  OS 사용자명)가 하드코딩돼 있지 않은지 grep으로 재확인
- 커밋 author 이메일이 GitHub noreply 주소인지 (`git config user.email`). 공개
  레포의 커밋 메타데이터는 누구나 조회할 수 있고, 지우려면 히스토리 재작성이 필요하다
- 실행 산출물(`observability/logs/*.jsonl`, `eval/scores/*`)이 잡히지 않았는지

### 테스트
- 가상환경으로 돌린다: `.venv/Scripts/python.exe -m pytest tests/ -q`.
  시스템 파이썬에는 `feedparser` 등이 없어 수집 모듈 import에서 깨진다.
- **실제 Obsidian Vault에 절대 쓰지 않는다.** 모든 쓰기 테스트는 `tmp_path` 안에서만
  일어난다. 같은 이유로 관측 로그 테스트도 실제 `observability/logs/`를 건드리지
  않는다 — 건드리면 `occurrence_count` 누적이 오염된다.
- 동작을 의도적으로 바꿔 기존 테스트가 깨졌다면, 되돌리지 말고 테스트를 새 계약에
  맞게 갱신한다.

### eval / 골든셋
- **모델 출력은 정답이 아니다.** 파이프라인 실행 결과를 골든셋으로 승격하려면 사람이
  라벨링을 확정해야 한다. 그러지 않으면 eval이 "모델이 스스로를 채점하는" 구조가 된다.
- 판단이 갈렸던 경계 사례를 우선 넣는다. 쉬운 건 100개보다 경계 사례 20개가 낫다.
