"""외부 LLM 벤더 호출 차단 — 코드 계층 (ADR-017).

## 왜 이 레포에 있나

이 레포는 `multiagent-research-lab`(MARA)의 코퍼스를 만드는 **생산자**다. MARA 의
전제는 **외부 LLM 벤더 비의존**이고, MARA Session 0.5a 에서 그 전제가 깨졌을 때
**실제로 벤더를 부른 코드가 여기 있었다.** 규칙은 저쪽에 있고 위반은 이쪽에서 났다.
MARA 의 훅은 MARA 세션에서만 돌기 때문에, 장치가 없으면 이 레포에서 같은 일이
그대로 다시 가능하다. 그래서 같은 게이트를 이쪽에 이식했다 (MARA ADR-021 §5.2).

## 기본값은 차단이다 — 경고가 아니다

이 레포의 벤더 호출은 **정상 경로가 아니라 승인받고 쓰는 예외 경로**다. 추출 단계의
vLLM 이전이 끝날 때까지 그렇다. 기본 차단 + 1회용 승인 파일이 그 상태를 정확히
표현한다 — 기본값은 금지이고, 예외는 매번 사람이 열고 닫는다.

⛔ **경고·기록만 두는 안은 기각했다.** 0.5a 의 게이트도 **있었고 통과했다.** 실패는
"장치가 없었다"가 아니라 **"장치가 확인할 것을 묻지 않았다"** 였다. 경고는 답을
요구하지 않는 장치이므로 같은 계열의 실패를 한 번 더 만든다 — 로그에 남아도
아무도 멈추지 않으면 그건 게이트가 아니다.

## 두 종류의 검사가 있다

1. `assert_vendor_call_allowed()` — **벤더 클라이언트를 만들려는 행위 자체**를 막는다.
   이 레포의 모든 단계(관련성 게이트 / 추출 / eval judge)가 벤더를 가리키고 있어
   "목적지가 벤더인가"는 물을 필요조차 없다. 물어야 하는 것은 "승인이 있는가"다.
2. `assert_internal_endpoint()` — `base_url` 을 갈아끼워 목적지를 돌리는 경로를 막는다.
   vLLM 이전 뒤에 1번의 무게가 여기로 옮겨 온다. **지금 넣어 두는 이유는 이전 작업
   자체가 이 함수가 지키는 자리를 건드리는 일이기 때문이다.**

⚠️ **호스트 목록은 거부 목록(denylist)이다. 구조상 불완전하다.**
새 벤더나 사설 프록시 호스트는 여기 없으므로 통과한다. 목록을 늘리는 것으로 이 한계가
없어지지 않는다. 이 레포에서 실제 방어선 역할을 하는 것은 1번(기본 차단)이고,
호스트 목록은 그것을 우회하는 한 가지 경로를 막는 2차선이다.

`.claude/hooks/check-external-llm.sh` 가 같은 목록을 **독립적으로 복제**해서 들고 있다.
참조가 아니라 복제인 이유는 훅이 Python 임포트 없이 도는 셸 스크립트이기 때문이고,
두 목록이 갈라지는 것은 `tests/test_external_llm_gate.py` 가 대조해서 막는다.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 1회용 승인 파일. `.gitignore` 대상이다. 있으면 통과, 없으면 차단.
APPROVAL_FILENAME = ".claude/external-llm-approved"

# 호스트 접미사. `host == h` 또는 `host.endswith("." + h)` 이면 일치로 본다.
EXTERNAL_LLM_HOSTS: tuple[str, ...] = (
    "api.anthropic.com",
    "api.openai.com",
    "openai.azure.com",
    "generativelanguage.googleapis.com",
    "aiplatform.googleapis.com",
    "api.mistral.ai",
    "api.cohere.ai",
    "api.cohere.com",
    "api.groq.com",
    "api.together.xyz",
    "api.perplexity.ai",
    "api.deepseek.com",
    "api.x.ai",
    "openrouter.ai",
    "api.upstage.ai",
    "api.moonshot.cn",
    "open.bigmodel.cn",
    "api.z.ai",
    "dashscope.aliyuncs.com",
    "clovastudio.stream.ntruss.com",
    "clovastudio.apigw.ntruss.com",
)

# 호스트 **부분 문자열**. 리전이 호스트 중간에 들어가 접미사로 못 잡는 것들
# (예: `bedrock-runtime.ap-northeast-2.amazonaws.com`).
EXTERNAL_LLM_HOST_SUBSTRINGS: tuple[str, ...] = (
    "bedrock-runtime.",
    "bedrock-agent-runtime.",
)


class ExternalVendorCallError(RuntimeError):
    """외부 LLM 벤더를 부르려 했고 승인이 없다. 호출 전에 막는다."""


class ExternalEndpointError(ExternalVendorCallError):
    """엔드포인트가 외부 LLM 벤더를 가리킨다. 호출 전에 막는다."""


# ---------------------------------------------------------------------------
# 승인 파일
# ---------------------------------------------------------------------------
def approval_path(project_root: Path | str | None = None) -> Path:
    """1회용 승인 파일 경로."""
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    return root / APPROVAL_FILENAME


def is_approved(project_root: Path | str | None = None) -> bool:
    """승인 파일이 있으면 True.

    환경변수로 여는 우회로를 **두지 않았다.** 환경변수는 셸 한 줄로 켜지고 그 줄이
    어디에도 남지 않는다. 파일은 만들고 지우는 행위가 필요하고, 남아 있으면 보인다.
    """
    return approval_path(project_root).is_file()


# ---------------------------------------------------------------------------
# 1. 벤더 호출 자체
# ---------------------------------------------------------------------------
def assert_vendor_call_allowed(
    *,
    stage: str,
    model: str | None = None,
    project_root: Path | str | None = None,
) -> None:
    """승인 파일이 없으면 `ExternalVendorCallError` 를 올린다.

    Args:
        stage: 어느 호출 지점인가. 예: `relevance_gate`, `extraction`, `eval_judge`.
            **이 값이 예외 메시지에 들어가는 것이 이 함수의 핵심이다.** 승인 없이
            파이프라인을 한 번 돌리면 벤더를 부르는 지점이 하나씩 이름과 함께
            드러나고, 그 목록이 vLLM 이전 범위를 정하는 근거가 된다 (MARA §5.2-5).
        model: 모델명. 진단용이고 **판정 기준이 아니다** — 같은 모델 이름이 벤더
            엔드포인트에도 내부 엔드포인트에도 있을 수 있다.
    """
    if is_approved(project_root):
        return
    where = f"{stage}" + (f" (model={model})" if model else "")
    raise ExternalVendorCallError(
        f"외부 LLM 벤더 클라이언트를 만들려 했습니다: {where}.\n"
        "이 레포의 벤더 호출은 정상 경로가 아니라 승인받고 쓰는 예외 경로입니다 "
        "(ADR-017, MARA ADR-021).\n"
        "진행하려면 docs/governance.md 의 승인 게이트 6항목을 사용자에게 제시해 "
        f"승인을 받은 뒤 `{APPROVAL_FILENAME}` 를 만들고, 끝나면 지우세요."
    )


# ---------------------------------------------------------------------------
# 2. 엔드포인트 목적지
# ---------------------------------------------------------------------------
def host_of(url: str) -> str:
    """URL 에서 호스트만 뽑는다. 스킴·포트·경로·인증정보를 버린다.

    파싱 실패를 예외로 만들지 않는다 — 이 함수는 검사 경로에 있고, 여기서 터지면
    **검사가 없는 것과 같은 상태**가 되기 때문이다. 이상한 입력은 빈 문자열이 되어
    "일치 없음"이 된다.
    """
    rest = url.split("://", 1)[-1]
    authority = rest.split("/", 1)[0]
    # user:pass@host 형태에서 호스트만.
    authority = authority.rsplit("@", 1)[-1]
    return authority.split(":", 1)[0].strip().lower()


def match_external_vendor(url: str) -> str | None:
    """외부 LLM 벤더로 알려진 호스트면 일치한 패턴을 돌려준다. 아니면 None."""
    host = host_of(url)
    if not host:
        return None
    for suffix in EXTERNAL_LLM_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return suffix
    for fragment in EXTERNAL_LLM_HOST_SUBSTRINGS:
        if fragment in host:
            return fragment
    return None


def assert_internal_endpoint(url: str | None, *, field: str = "base_url") -> None:
    """외부 LLM 벤더 호스트면 `ExternalEndpointError` 를 던진다.

    에러 메시지에 URL 전체를 넣지 않는다 — 엔드포인트에는 인증정보가 섞일 수 있고,
    진단에 필요한 것은 **어느 벤더인가**뿐이다.
    """
    if not url:
        return
    matched = match_external_vendor(url)
    if matched is None:
        return
    raise ExternalEndpointError(
        f"{field} 가 외부 LLM 벤더 호스트를 가리킵니다 (일치: {matched}). "
        "이 레포가 만드는 코퍼스의 소비자(MARA)는 외부 LLM 벤더 비의존이 전제입니다 "
        "(ADR-017, MARA ADR-021)."
    )


def endpoint_from_env() -> str | None:
    """환경에 명시된 LLM 엔드포인트 override. 없으면 None.

    SDK 가 읽는 이름을 그대로 본다 — 여기서 이름을 새로 만들면 실제로 쓰이는
    경로와 검사하는 경로가 갈라진다.
    """
    for name in ("ANTHROPIC_BASE_URL", "LLM_BASE_URL", "VLLM_BASE"):
        value = os.environ.get(name)
        if value:
            return value
    return None
