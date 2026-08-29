# obsidian_writer/

`NewsOntology` → Obsidian Vault 마크다운 노트.

| 파일 | 상태 | 역할 |
|---|---|---|
| `mapper.py` | 구현됨 | 한국어 alias → 영문 frontmatter 키 매핑, YAML 직렬화, 본문 렌더링 |
| `writer.py` | 구현됨 | 슬러그·경로 결정, 충돌 해소, 원자적 쓰기 |

## 출력 형태

```markdown
---
title: 바이브코딩으로 만든 퍼저가 FFmpeg의 0 나누기 버그를 발견
date: '2026-08-29'
source: GeekNews
source_url: https://news.hada.io/topic?id=33001
tech_domain:
- Application/Product
release_type: Community/Discussion
companies:
- FFmpeg
prior_art:
- Fuzzing
- Vibe Coding
impact_score: 2
impact_rationale: 특정 디먹서의 국소적 크래시 버그로 파급 범위는 좁지만, ...
extraction_model: claude-opus-5
prompt_version: extract_ontology.v3.md
processed_at: '2026-08-29T21:04:29.527274+09:00'
---

## 요약

(2~3문장. NewsOntology.summary)

## 관련 개념

- [[Fuzzing]]
- [[Vibe Coding]]
- [[FFmpeg]]

## 원문

[GeekNews에서 보기](https://news.hada.io/topic?id=33001)
```

파일명: `{date}-{source}-{slug}.md`
예) `2026-08-29-geeknews-바이브코딩으로-만든-퍼저가-ffmpeg의.md`

## 설계 근거

| 항목 | 선택 | 결정 로그 |
|---|---|---|
| frontmatter 키 | **영문**. Dataview 쿼리에서 한글 키는 매번 따옴표가 필요하다 | D-029 (D-003 부분 번복) |
| `영향도` | 중첩 객체가 아니라 `impact_score` + `impact_rationale` 두 키로 편다 | D-007 |
| `companies` / 위키링크 | **canonical** 을 쓴다. 원문표기로 링크하면 정규화한 것이 그래프에서 도로 갈라진다 | D-030 |
| 한글 파일명 | 로마자로 옮기지 않는다. 표기법이 여러 개라 실행마다 달라지고 사람이 못 읽는다 | D-031 |
| 충돌 | 같은 기사(`source_url` 일치)면 `skip`, 슬러그만 겹치면 `-2`, `-3` | D-028 |

## 규칙

- **Vault 는 사용자의 실제 데이터다.** 기본 충돌 정책은 `skip` 이고, 덮어쓰기는
  `config.yaml: output.on_conflict` 에서 명시적으로 켜야만 동작한다.
- 쓰기는 **같은 디렉터리**의 temp 파일 → `os.replace` 로 원자적으로.
  `os.replace` 는 같은 볼륨에서만 원자적이라 시스템 임시 폴더를 쓰면 보장이 깨진다.
- 파일명에서 Windows 금지문자(`\ / : * ? " < > |`)와 끝의 점·공백을 제거한다.
- YAML 직렬화에 `allow_unicode=True` 필수. 없으면 한글이 `\uXXXX` 로 깨진다.
- 개행은 LF 로 고정. Vault 를 git 으로 동기화할 때 diff 가 흔들리지 않게.

## 알려진 한계

사용자가 노트의 frontmatter 를 통째로 지우면 `source_url` 로 동일성을 판정할 수
없어, 재실행 시 같은 기사의 노트가 `-2` 로 하나 더 생긴다.

## 테스트

`tests/test_obsidian_writer.py`. 모든 쓰기는 `tmp_path` 안에서만 일어난다 —
`write_note` 가 `output_dir` 를 필수 인자로 받으므로 실수로 실제 Vault 를 건드릴
경로가 없다.
