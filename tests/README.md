# tests/

`pytest`. 아직 로직이 없으므로 지금은 **계약 테스트**만 대상으로 한다.

| 테스트 | 검증 내용 | 상태 |
|---|---|---|
| `test_vocab_sync.py` | `extraction/schema.py` 의 Enum ↔ `config.yaml: ontology` 일치 | TODO |
| `test_schema_contract.py` | 한국어 alias round-trip, 통제어휘 밖 값 거부, 영향도 1~5 경계 | TODO |
| `test_golden_set_format.py` | `eval/golden_set/*.json` 이 기대 형식을 지키는지 | TODO |

`test_vocab_sync.py` 가 특히 중요하다. 스키마·문서·설정 세 곳에 같은 어휘가
적혀 있는 구조라서, 드리프트를 사람 눈으로 막을 수 없다. (결정 로그 D-002)
