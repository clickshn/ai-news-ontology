# ADR-005: 소스별 본문 커버리지를 실측해 표기하고 GeekNews 를 추가한다

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision:** RSS 소스마다 `body_quality` 를 실측해 `config.yaml` 에 표기하고, 슬라이스 검증 기준 소스를 arXiv Atom API 로 둔다. 본문 커버리지를 보강하기 위해 GeekNews 를 소스에 추가한다.
- **Scope:** ai-news-ontology (collectors / 코퍼스 구성)
- **Decision Source:** Human

> 이관: README 결정 로그 **D-013**, **D-014**. 같은 실측에서 나온 한 판단의 두 면
> (무엇을 알게 됐나 / 그래서 무엇을 바꿨나)이라 하나로 묶었다.

---

## Context

### Problem

피드마다 본문을 주는 정도가 다르다. 이것을 모르는 채 파이프라인을 만들면 추출 품질이
소스에 좌우되는데 그 사실이 어디에도 드러나지 않는다.

### Constraints

- 본문 fetch 단계가 없다. 피드가 주는 텍스트가 입력의 전부다.
- arXiv RSS 공지 피드(`rss.arxiv.org`)는 주말·공휴일에 0건이라 슬라이스 검증에 못 쓴다.
  API 쿼리는 언제든 최근 논문과 초록 전문을 준다.
- `title_only` 피드가 많아 본문 커버리지가 부족했다.

## Decision

### Selected

- **Technology:** feedparser + `config.yaml` 의 `sources.rss` 목록
- **Architecture:** 소스마다 `body_quality`(`full` / `truncated` / `oneline` /
  `partial` / `title_only`)를 **실측값으로** 표기한다. 추측이 아니라 소스당 50건
  (arXiv 는 20건) 수집 후 본문 길이 중앙값이다.
- **Implementation:** 슬라이스 검증 기준 소스를 `arXiv cs.CL (Atom API)` 로 두고,
  GeekNews(`news.hada.io/rss/news`)를 `truncated` 로 추가한다.

## Evidence

- **Production Data:** 2026-08-29 실측, 소스당 50건(arXiv 20건)의 본문 길이 중앙값.

  | 소스 | 건수 | 최소 | 중앙 | 최대 | 0자 |
  |---|---:|---:|---:|---:|---:|
  | arXiv cs.CL (Atom API) | 20 | 979 | 1339 | 1895 | 0 |
  | GeekNews | 50 | 48 | 160 | 185 | 0 |
  | OpenAI News | 50 | 109 | 149 | 180 | 0 |
  | Google DeepMind Blog | 50 | 0 | 97 | 210 | 15 |
  | Hugging Face Blog | 50 | 0 | 0 | 0 | 50 |

  GeekNews 는 50건 중 **40건이 말줄임으로 잘려** 있다.

## Rationale

1. **벤더 블로그 피드 다수가 본문을 주지 않는다.** 본문 fetch 단계 없이는 추출 품질이
   소스에 좌우되므로, 어느 소스가 얼마나 주는지를 설정 파일에 남겨 둔다.
2. GeekNews 는 중앙값 160자에 **0자 항목이 없어** DeepMind(97자, 15/50 이 0자)·
   HF(전부 0자)보다 안정적이다. 한국어 소스라 기존 영어 소스와 표기 다양성도 확보된다.

## Consequences

### Positive

- 출력 계약의 색인 정책과 `MIN_TEXT_CHARS` 논의가 이 실측 위에서 이뤄진다
  (MARA ADR-018 Evidence).
- 한국어 문서가 코퍼스에 들어오는 경로가 생겼다.

### Negative

- GeekNews 는 AI 전용 소스가 아니다 — 최근 50건 중 약 60%만 AI 관련이라 비-AI 항목을
  거르는 단계가 따로 필요해졌다 (ADR-006).
- 본문 0자 소스(HF Blog)는 색인 대상이 하나도 없다.

### Risks

- 실측값은 2026-08-29 기준이다. 피드가 정책을 바꾸면 표기가 낡는다.

## Reversibility

- **Reversible:** Yes
- **Rollback:** 소스를 `config.yaml` 에서 빼면 된다. 이미 만든 노트는 남는다.
- **Migration Cost:** Low

## References

- **Related ADR:** ADR-006(관련성 게이트)
- **Documentation:** `README.md` 결정 로그 D-013·D-014, `config.yaml`,
  `collectors/README.md`
