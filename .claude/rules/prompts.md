---
paths:
  - "extraction/prompts/**"
  - "eval/judge_prompts/**"
---
# 프롬프트 컨벤션
- **실행에 쓰인 버전 파일은 수정하지 않는다.** 변경이 필요하면 새 버전 파일을
  만든다 (D-010, D-023). 프롬프트가 바뀌면 eval 점수가 바뀌는데, 파일을 덮어쓰면
  "어떤 프롬프트로 잰 점수인가"에 답할 수 없다.
- 파일명은 `{용도}.{버전}.md`. `# System` / `# User` 두 섹션이 이 순서로 있어야
  `load_prompt()` 가 읽는다.
- **통제어휘 목록을 하드코딩하지 않는다.** `{{tech_domain_list}}` 처럼 두고
  `schema.py` Enum 에서 런타임 주입한다 (D-012).
- 새 버전을 만들면 `extraction/prompts/README.md` 의 표에 무엇이 달라졌는지 적는다.
