"""적재(Publish) 레이어.

역할: 검증을 통과한 NewsNote 를 Obsidian Vault 안의 마크다운 파일로 쓴다.
     온톨로지 필드는 YAML frontmatter 로 나가고, 관계형 필드는
     `[[위키링크]]` 로 나가서 Obsidian 그래프 뷰에서 탐색 가능해진다.

구성:
    mapper.py     한국어 alias -> 영문 frontmatter 키 매핑, YAML 직렬화, 본문 렌더링
    writer.py     슬러그/경로 결정 + 원자적 쓰기(temp -> rename) + 충돌 정책

설계 메모:
    - Vault 는 사용자의 실제 데이터다. 기본 충돌 정책은 `skip` 이고,
      덮어쓰기는 config.yaml 에서 명시적으로 켜야만 동작한다.
    - 쓰기는 원자적으로 한다. 중간에 죽어서 반쪽 노트가 남으면 안 된다.
    - frontmatter 키는 pydantic alias(한국어)를 그대로 쓴다. Obsidian 의
      Properties UI 에서 한국어 키가 그대로 보이는 편이 검색에 낫다.
"""
