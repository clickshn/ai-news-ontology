# ADR-027: export 계약의 `provenance.prompt_sha256` 은 줄바꿈을 LF 로 정규화한 프롬프트 파일 바이트의 sha256 이다

- **Status:** Accepted (사용자, 2026-10-02). 구현의 main 병합은 별도 판단이다
- **Date:** 2026-10-02
- **Decision:** `provenance.prompt_sha256` 을 "디스크 파일의 원시 바이트"가 아니라 **CRLF 를 LF 로 바꾼 바이트의 sha256** 으로 정의한다. 이 값은 git 에 커밋된 내용의 해시와 같아 어느 클론에서도 재현된다. 이미 나간 CRLF 값(`9ff5a9d3…` 등)과 새 값의 대응을 아래 표로 고정한다
- **Scope:** `export/runner.py: prompt_sha256` (export 레코드와 보존소 `extraction.prompt_sha256` / `gate.prompt_sha256` 의 출처) · MARA 출력 계약 §3.3 의 `prompt_sha256` 정의
- **Decision Source:** Human

---

## Context

### Problem

`export/runner.py: prompt_sha256` 은 프롬프트 파일의 원시 바이트를 센다. 운영 작업본은 `.gitattributes` 가 `eol=lf` 인데도 CRLF 로 체크아웃돼 있어(`git ls-files --eol` → `i/lf w/crlf`) 이 값이 CRLF 바이트의 해시가 된다. 그래서 보존소의 모든 항목이 LF 클론에서 재현되지 않는 값을 갖고 있다. 계약 §12.2-3 이 "MARA 가 파일만 받아 같은 값을 재현"하는 것을 뜻한다면 지금 깨져 있다(session-14 §7 에서 이월).

같은 문제를 eval 쪽은 이미 LF 정규화로 풀었고(D-098, `eval/runner.py: prompt_sha256`), 자동 실행 승인 지문도 LF 정규화를 쓴다(`pipeline/approval.py: sha256_text_lf`). export 만 원시 바이트로 남아 있었다 — MARA 계약 값이라 그쪽과 순서를 맞추려고 일부러 손대지 않았던 것이다.

### Constraints

- `pipeline/runner.py`(`GUARD_FILES`)가 `export.runner.prompt_sha256` 을 import 해 보존소에 기록한다. 이 함수를 바꾸면 병합 뒤 운영 파이프라인이 보존소에 남기는 해시도 바뀐다. `export/runner.py` 는 `GUARD_FILES` 에 없어서 자동 실행의 승인 대조는 이 변화를 잡지 않는다.
- 운영 체크아웃은 코드를 바꾸지 않는다(ADR-026 Constraints). 구현은 ADR-026 의 worktree 브랜치에서 한다.
- ADR-026 의 backfill 은 37건 레코드를 전부 새로 만든다. 정의를 바꿀 때 한 코퍼스 안에 옛 값과 새 값이 섞이지 않는 유일한 시점이다.

## Decision

### Selected

- **Technology:** sha256, 입력 = `파일 바이트.replace(b"\r\n", b"\n")`.
- **Architecture:** export 레코드의 `prompt_sha256` 은 보존소에 기록된 값을 그대로 옮긴다(`export/record.py`). 따라서 정의는 보존소에 쓰는 시점(`export/runner.py: prompt_sha256`) 한 곳에서 바뀐다.
- **Implementation:** 64자 전체를 유지한다(계약 예시와 같은 길이). eval 쪽 16자 축약과는 별개다.

옛 값 ↔ 새 값 대응 (운영 작업본 CRLF 바이트 / LF 정규화 = 커밋 내용, 2026-10-02 실측):

| 프롬프트 | 옛 값 (CRLF 바이트) | 새 값 (LF 정규화) |
|---|---|---|
| `extract_ontology.v1.md` | `f4af9820a146643dae488e1cf7bf6346fc21ac725092660509417ef41d081b9d` | `c952f42ab210b29286aa25b8a103d7e78f206688e0ce901cf6079984b267f2ff` |
| `extract_ontology.v2.md` | `a172fb595f0795885fed1c6c28ac6d9165d01200234abed4024f92743f08bcd2` | `23cd50f451871cbd20101531bd4c2badf6149cca03157b80f1a882f9d0346029` |
| `extract_ontology.v3.md` | `4ddb1b1b4c69165fc3284bda00cdee2de51237a0478c73dd4f782d0bd6e01f03` | `03e3e3cace584e8d5914b53a68451838600785438e95b9fed01623a47085d394` |
| `extract_ontology.v4.md` | `9ff5a9d39156b3d35a7b3ffa805261c78dfa262f390d051419cfaaa1711c5976` | `25b17f445bec91a2fa4336b20dbac214b09c87fcb1c8493c17fcdd5b65e57bc8` |
| `relevance_gate.v1.md` | `d9b80e55f951d727e78ff704bbf98e017f2b722da89ff669b987435331b8ed56` | `18890c1ab01d0776d66696f64949fb505509bde6fee58bccbc4019f7b8affe1d` |

MARA 에 이미 들어간 export(`ontology-20260917-120733`) 30건의 `prompt_sha256` 은 전부 v4 의 옛 값 `9ff5a9d3…` 이다.

## Rationale

1. 재현성이 이 필드의 존재 이유다. "어느 프롬프트로 만들었는가"를 MARA 가 파일만 받아 확인할 수 있어야 하는데, 원시 바이트는 체크아웃 설정에 따라 같은 내용에서 다른 값을 낸다.
2. 레포 안의 다른 두 해시(eval D-098, 승인 지문)가 이미 LF 정규화다. export 만 다르면 같은 프롬프트에 해시가 둘 생긴다.
3. ADR-026 의 backfill 이 37건을 전부 새로 찍으므로, 지금 바꾸면 MARA 코퍼스 안에서 정의가 섞이지 않는다.

## Evidence

- **Experiment:** v4 의 LF 정규화 해시 `25b17f44…` = `git show HEAD:extraction/prompts/extract_ontology.v4.md | sha256sum`. gate v1 도 같다(`18890c1a…`). 운영 보존소 232건의 `extraction.prompt_sha256` 은 232/232 `9ff5a9d3…`, `gate.prompt_sha256` 은 232/232 `d9b80e55…`(2026-10-02).

## Alternatives

### 원시 바이트 정의를 유지한다 (현행)

- **Pros:** 코드·계약 변경이 없다. 기존 레코드 값과 이어진다.
- **Cons:** LF 클론에서 재현되지 않는다. 작업본 줄바꿈이 바뀌면 프롬프트가 같아도 값이 바뀐다.
- **Rejected because:** 계약이 요구하는 재현성을 지금도 만족하지 못한다(session-14 §7).

## Consequences

### Positive

- `prompt_sha256` 이 체크아웃 줄바꿈과 무관해지고, 커밋 내용만으로 검증된다.
- export · eval · 승인 지문의 프롬프트 해시 정의가 하나가 된다(길이는 다르다).

### Negative

- 병합 뒤 운영 보존소에는 옛 값(병합 전 232건+)과 새 값(병합 뒤)이 섞인다. 같은 v4 프롬프트가 두 해시로 보인다 — 위 대응표가 그 둘을 잇는 유일한 기록이다.
- 계약 문서 §3.3 의 정의 문구를 고쳐야 한다(MARA 반입 세션).

### Risks

- `pipeline/runner.py` 가 이 함수를 쓰므로, 병합하면 운영 보존소 기록이 바뀌는데 자동 실행의 승인 대조는 이를 잡지 않는다(`export/runner.py` 는 `GUARD_FILES` 밖). 병합은 사용자가 자동 실행 관찰 세션 뒤에 정한다. 그때 함께 정할 항목: **`export/runner.py` 를 `GUARD_FILES` 에 넣을지** — 넣으면 이 함수의 변경이 자동 실행을 멈추고 재승인을 요구하고(의도한 비용, ADR-025), 넣지 않으면 운영 보존소 기록을 바꾸는 코드가 승인 대조 밖에 남는다.
- 대응표 밖의 옛 값(앞으로 만들 프롬프트)은 생기지 않는다 — 새 프롬프트는 처음부터 새 정의로 찍힌다.

## Implementation

- [ ] `export/runner.py: prompt_sha256` 을 LF 정규화로 변경 (ADR-026 worktree 브랜치)
- [ ] 테스트: CRLF 파일과 LF 파일이 같은 값을 내는지, v4 가 `25b17f44…` 인지
- [ ] `export/conformance.py` 의 `prompt_sha256` 대조가 새 정의로 통과하는지
- [ ] MARA 계약 §3.3 정의 문구 수정 (MARA 반입 세션, 계약 먼저)
- [ ] main 병합 판단 (자동 실행 관찰 세션 뒤, 사용자) — `export/runner.py` 의 `GUARD_FILES` 편입 여부 포함

## Reversibility

- **Reversible:** Yes
- **Rollback:** 함수를 원시 바이트로 되돌린다. 새 정의로 찍힌 레코드는 대응표로 옛 값에 대응시킬 수 있다.
- **Migration Cost:** Low

## References

- **Related ADR:** ADR-026(MARA 코퍼스 backfill) · ADR-004(프롬프트 파일 버전 관리) · ADR-025(승인 지문, `sha256_text_lf`) · MARA ADR-018(출력 계약)
- **Documentation:** `docs/handoff/session-14.md` §7 · `docs/handoff/session-19.md` §2.2 S1 · README D-098
