# extraction/

원문 1건 → **한국어 요약 + 온톨로지 5필드**(`NewsNote`) 구조화.

| 파일 | 상태 | 역할 |
|---|---|---|
| `schema.py` | 구현됨 | 온톨로지 스키마 **단일 출처(SSoT)**. `schema.md` 와 1:1 대응 |
| `llm.py` | 구현됨 | `LLMClient` Protocol + `AnthropicClient` (구조화 출력 + 1회 재시도) |
| `extractor.py` | 구현됨 | 프롬프트 조립 → LLM 호출 → pydantic 검증 |
| `prompts/` | 구현됨 | 추출 프롬프트. `judge_prompts/` 와 동일한 `{용도}.{버전}.md` 관례 |
| `normalize.py` | 구현됨 | `config.yaml: company_aliases` 기반 기업명 정규화. **퍼지 매칭 없음** (D-025) |

## 구조화 출력 방식

`client.messages.parse(output_format=NewsOntology)` — pydantic 모델을 그대로
넘기고 검증된 인스턴스를 받는다. strict tool use 를 쓰지 않은 이유는
결정 로그 **D-009** 참고.

실패 처리:
- **스키마 불일치** → 검증 오류를 프롬프트에 되먹여 1회 재시도 → 그래도 실패하면
  `SchemaMismatchError`. **부분 결과를 반환하지 않는다.**
- **API 오류** → 연결 오류/429/5xx 는 SDK 가 백오프 재시도. 그 뒤에도 실패하면
  `anthropic` 예외를 그대로 올린다 (400 을 삼키면 원인이 가려진다).

## 확인용 실행

```bash
python -m extraction.extractor --source "arXiv cs.CL (Atom API)" --index 0
```

## 규칙
- **LLM 출력을 신뢰하지 않는다.** 항상 `NewsNote.model_validate()` 를 통과시킨다.
- `ValidationError` 는 오류 메시지를 프롬프트에 되먹여 1회 재시도하고, 그래도
  실패하면 노트를 쓰지 않고 격리 로그로 남긴다. (조용한 실패 금지)
- 통제어휘(`기술영역`, `발표유형`)는 Enum 이다. 어휘 밖 값 = 실패.
- 자유 필드(`관련기업`, `관련기존기술`)는 원문 표기를 보존한 뒤 정규화한다.

## 스키마 변경 시
`extraction/schema.py` → `schema.md` → `config.yaml: ontology` 셋을 **함께** 고친다.
