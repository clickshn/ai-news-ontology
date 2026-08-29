# extraction/prompts/

추출 프롬프트. `eval/judge_prompts/` 와 **동일한 버저닝 관례**를 따른다.

- 파일명: `{용도}.{버전}.md` (예: `extract_ontology.v1.md`)
- 기존 파일을 **수정하지 않는다.** 바꿀 일이 생기면 `v2` 를 새로 만든다.
  과거 eval 점수가 어떤 프롬프트에서 나온 것인지 추적 가능해야 하기 때문.
- 프롬프트 본문은 `# System` / `# User` 두 섹션으로 나눈다. 로더가 이 헤더를
  기준으로 잘라 쓴다 (`extraction/extractor.py: load_prompt`).
- 치환 변수는 `{{name}}` 형식.

## 통제어휘 동기화
프롬프트 안의 어휘 목록은 `extraction/schema.py` 의 Enum 을 **런타임에 주입**한다
(`{{tech_domain_list}}`, `{{release_type_list}}`). 프롬프트에 어휘를 하드코딩하면
네 번째 미러가 생기고 드리프트 지점이 하나 더 늘어난다. (결정 로그 D-012)
