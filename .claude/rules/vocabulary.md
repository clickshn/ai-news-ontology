---
paths:
  - "extraction/schema.py"
  - "config.yaml"
  - "schema.md"
---
# 통제어휘 컨벤션
- 어휘의 SSoT 는 `extraction/schema.py` 의 Enum 이다. `config.yaml` 의 `ontology:`
  블록과 `schema.md` 의 표는 미러이며, 세 곳을 **함께** 고친다 (D-002).
  `tests/test_vocab_sync.py` 가 앞의 둘을 검증한다 — `schema.md` 는 자동 검증 대상이
  아니므로 사람이 챙긴다.
- **값 추가는 스키마에서, 판단 기준 변경은 문서·프롬프트에서** 한다 (D-021, D-022).
  값을 쪼개거나 이름을 바꾸면 이미 쌓인 노트의 라벨이 소급해서 틀린 것이 된다.
- 어휘는 MARA 출력 계약의 **정본**이다. 값 삭제·개명·쪼개짐은 그쪽 import 실패를
  일으킨다 (계약 §5). 추가는 통과한다.
