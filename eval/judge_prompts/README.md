# eval/judge_prompts/

LLM-judge 루브릭 프롬프트. **프롬프트도 코드다** — 버전 관리하고, 바꾸면 점수를
다시 돌린다.

## 파일 규칙
- `summary_faithfulness.v1.md` 처럼 `{축}.{버전}.md`
- 기존 파일을 **수정하지 말고** 버전을 올린다. 과거 점수의 해석 가능성을 지키기 위함.
- `eval/scores/` 에는 사용한 프롬프트 파일명과 해시를 함께 남긴다.

## 루브릭 작성 원칙
- 점수 구간마다 **관측 가능한 기준**을 쓴다. "좋다/나쁘다"가 아니라 "원문에 없는
  사실이 N개 있다" 처럼.
- 근거를 먼저 쓰게 하고 점수를 마지막에 내게 한다.
- 위치 편향을 줄이려면 비교 채점보다 단일 항목 절대 채점을 기본으로 한다.

## 버전

| 파일 | 완결성 점수를 누가 내나 | 비고 |
|---|---|---|
| v1 ~ v3 | judge | v3 = 4슬롯 (D-045). **기본값** |
| v4 · v5 | judge | 라벨로 분모·요소 고정 (D-089). v4 퇴화(D-093), v5 한 줄 형식(D-094) |
| **v6** | **코드** — judge 는 요소 판정만 | ADR-021, D-097. 출력 모델 `ElementJudgement`. `eval.runner.CODE_SCORED_RUBRICS` 에 이름으로 등록돼 있다 |

**v6 이후 점수와 v5 이하 점수를 직접 비교하지 않는다.** 결과 행의
`completeness_scored_by` 가 `code` 인지 `judge` 인지 먼저 본다.

## `prompt_sha256` 대응표 — 해시 방식이 session-14 에 바뀌었다 (D-098)

`eval.runner.prompt_sha256` 은 **줄바꿈을 LF 로 맞춘 내용**을 해시한다
(`metadata.prompt_hash_scheme = "lf"`). 그 전에는 **작업본 원시 바이트**였고
(`prompt_hash_scheme` 없음), 같은 커밋이 체크아웃 줄바꿈에 따라 다른 값을 냈다.

**이미 기록된 해시는 고치지 않는다.** 아래 표로 대조한다. 왼쪽 값은 CRLF 로 체크아웃된
작업본에서 기록된 값이고, LF 체크아웃에서 기록된 값은 이미 오른쪽과 같다.

| 파일 | 원시 (이 작업본) | `lf` | 기록된 곳 |
|---|---|---|---|
| `summary_quality.v1.md` | `40a3ba69f8bc79b5` | `d668b58ee8ec5955` | |
| `summary_quality.v2.md` | `c19772b2cdfe116f` | `e21dcefd3f6b988f` | |
| `summary_quality.v3.md` | `70d7ed2fa0d235b2` | `670f4eccdad2574a` | session-12 · 13 결과 파일, v4·v5 사전 등록 |
| `summary_quality.v4.md` | `723f9b3e603d0314` | 같다 (LF 체크아웃) | v4 사전 등록 |
| `summary_quality.v5.md` | `d29cda1ba37e9f37` | 같다 (LF 체크아웃) | v5 사전 등록 |
| `extract_ontology.v4.md` | `9ff5a9d39156b3d3` | `25b17f445bec91a2` | session-03 · 12 기록, `preds.jsonl` 메타 |
| `extract_ontology.v1~v3.md` · `relevance_gate.v1.md` | `f4af…` · `a172…` · `4ddb…` · `d9b8…` | `c952…` · `23cd…` · `03e3…` · `1889…` | |

⚠️ **export 계약의 `provenance.prompt_sha256`(`export/runner.py`)은 이 변경 밖이다.** 전체 길이
원시 바이트 해시이고 MARA 계약 §12.2-3 이 정한 값이라, 바꾸려면 계약 문서가 먼저다.
