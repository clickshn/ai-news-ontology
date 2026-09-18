"""외부 LLM 벤더 호출 게이트 검사 (ADR-017).

두 계층을 **각각** 검사하고, 마지막에 **둘이 갈라지지 않았는지**를 대조한다.

  - 도구 계층: `.claude/hooks/check-external-llm.sh` — 셸 케이스 스위트를 그대로 돌린다
  - 코드 계층: `extraction/egress.py` + `AnthropicClient.__init__`
  - 대조: 훅의 호스트 목록은 `egress.py` 목록의 **복제본**이다. 복제본이 갈라지면
    "코드에서는 막히는데 명령줄에서는 통과"하는 구멍이 생기므로 여기서 대조한다.

⚠️ 셸 스위트는 케이스마다 훅 프로세스를 **실제로 띄운다.** 느리지만 모킹하지 않는다 —
모킹하면 "읽는 것과 돌려 보는 것은 다르다"는 이 게이트의 존재 이유를 스스로 어긴다.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from extraction import egress
from extraction.egress import (
    EXTERNAL_LLM_HOST_SUBSTRINGS,
    EXTERNAL_LLM_HOSTS,
    ExternalEndpointError,
    ExternalVendorCallError,
    approval_path,
    assert_internal_endpoint,
    assert_vendor_call_allowed,
    host_of,
    is_approved,
    match_external_vendor,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK = PROJECT_ROOT / ".claude" / "hooks" / "check-external-llm.sh"
CASES_SH = PROJECT_ROOT / "tests" / "gate" / "external_llm_cases.sh"
CASES_TXT = PROJECT_ROOT / "tests" / "gate" / "external_llm_cases.txt"
SETTINGS = PROJECT_ROOT / ".claude" / "settings.json"


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    """승인 파일 검사를 임시 디렉터리로 돌린다.

    **실제 레포의 `.claude/external-llm-approved` 를 만들거나 지우지 않는다.**
    테스트가 게이트를 진짜로 열어 두고 끝나면 그것 자체가 사고다.
    """
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(egress, "PROJECT_ROOT", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. 도구 계층 — 케이스 스위트를 실제로 돌린다
# ---------------------------------------------------------------------------
def test_hook_case_suite_passes():
    """차단 / 오탐 대조 / 알려진 우회 / 승인 파일 / fail-closed 전부."""
    # 경로를 상대경로로 넘긴다. Windows 절대경로(`C:\...`)를 bash 인자로 주면
    # 백슬래시가 이스케이프로 먹혀 경로가 뭉개진다. cwd 가 레포 루트이므로
    # 스크립트 기본값(`CLAUDE_PROJECT_DIR:-.`)이 그대로 맞는다.
    proc = subprocess.run(
        ["bash", "tests/gate/external_llm_cases.sh"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"게이트 케이스 실패:\n{proc.stdout}\n{proc.stderr}"


def test_case_file_has_both_polarities():
    """차단 케이스만 있는 검사는 '전부 차단'인 고장 난 게이트도 통과시킨다."""
    rows = _case_rows()
    blocked = [r for r in rows if r[0] == "2"]
    allowed = [r for r in rows if r[0] == "0"]
    assert len(blocked) >= 15, "차단 케이스가 너무 적다"
    assert len(allowed) >= 15, "오탐 대조가 너무 적다 — 이 레포에서는 특히 중요하다"


def test_known_bypasses_are_pinned_as_passing():
    """막지 못하는 것은 산문이 아니라 기대값으로 고정한다.

    이 줄들이 언젠가 exit 2 가 되면 그건 실패가 아니라 개선이다 — 그때 기대값을 바꾼다.
    """
    rows = _case_rows()
    bypasses = [r for r in rows if "우회" in r[1]]
    assert len(bypasses) >= 5, "알려진 우회가 케이스 파일에 고정돼 있지 않다"
    for want, desc, _cmd in bypasses:
        assert want == "0", f"우회 케이스는 통과가 기대값이다: {desc}"


def _case_rows() -> list[tuple[str, str, str]]:
    rows = []
    for line in CASES_TXT.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        want, desc, cmd = line.split("|", 2)
        rows.append((want, desc, cmd))
    return rows


def test_shell_files_are_lf():
    """CRLF 로 체크아웃되면 셔뱅이 깨져 훅이 **조용히 안 돈다** (.gitattributes)."""
    for path in (HOOK, CASES_SH):
        assert b"\r\n" not in path.read_bytes(), f"{path.name} 에 CRLF 가 있다"


def test_hook_is_registered_for_both_shells():
    """PowerShell 도구는 MARA session-07 에서 실제로 쓰인 우회 경로다."""
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings["hooks"]["PreToolUse"]
    matchers = [e.get("matcher", "") for e in entries]
    assert any("Bash" in m and "PowerShell" in m for m in matchers), (
        f"Bash·PowerShell 양쪽에 걸려 있지 않다: {matchers}"
    )
    commands = [h["command"] for e in entries for h in e["hooks"]]
    assert any("check-external-llm.sh" in c for c in commands)


def test_hook_does_not_carry_over_producer_run_rule():
    """MARA 훅의 `PRODUCER_RUN` 은 여기서 의미가 뒤집힌다 — 이식하면 안 된다.

    그대로 복사하면 이 레포가 자기 파이프라인을 자기가 막고, 그러면 승인 없이
    한 번 돌려 벤더 호출 지점을 드러내는 검사(§5.2-5) 자체가 불가능해진다.
    """
    text = HOOK.read_text(encoding="utf-8")
    assert "PRODUCER_RUN=" not in text
    assert 'matched="생산자 추출 파이프라인 실행"' not in text


# ---------------------------------------------------------------------------
# 2. 두 계층의 거부 목록 대조 — 복제본이 갈라지는 것을 막는다
# ---------------------------------------------------------------------------
def test_hook_host_denylist_matches_python_denylist():
    """훅은 셸이라 Python 목록을 임포트할 수 없어 복제해 들고 있다.

    복제본이 갈라지면 "코드에서는 막히는데 명령줄에서는 통과"하는 구멍이 생긴다.
    """
    hook_hosts = _hook_host_set()
    python_hosts = set(EXTERNAL_LLM_HOSTS) | set(EXTERNAL_LLM_HOST_SUBSTRINGS)
    assert hook_hosts == python_hosts, (
        f"훅에만 있음: {sorted(hook_hosts - python_hosts)} / "
        f"egress.py 에만 있음: {sorted(python_hosts - hook_hosts)}"
    )


def _hook_host_set() -> set[str]:
    """훅의 `VENDOR_HOSTS` 정규식을 구체적인 호스트 집합으로 되돌린다."""
    text = HOOK.read_text(encoding="utf-8")
    m = re.search(r"^VENDOR_HOSTS='([^']*)'", text, re.MULTILINE)
    assert m, "훅에서 VENDOR_HOSTS 를 찾지 못했다"
    hosts: set[str] = set()
    for expanded in _expand_alternations(m.group(1)):
        for part in expanded.split("|"):
            hosts.add(part.replace("\\.", "."))
    return hosts


def _expand_alternations(pattern: str) -> list[str]:
    """`a\\.(x|y)` → [`a\\.x`, `a\\.y`]. 그룹을 전부 풀 때까지 재귀한다."""
    m = re.search(r"\(([^()]*)\)", pattern)
    if not m:
        return [pattern]
    out: list[str] = []
    for alt in m.group(1).split("|"):
        out.extend(_expand_alternations(pattern[: m.start()] + alt + pattern[m.end() :]))
    return out


# ---------------------------------------------------------------------------
# 3. 코드 계층 — 기본 차단 + 승인 파일
# ---------------------------------------------------------------------------
def test_vendor_call_blocked_without_approval(isolated_root):
    with pytest.raises(ExternalVendorCallError):
        assert_vendor_call_allowed(stage="extraction", model="claude-opus-5")


def test_vendor_call_allowed_with_approval_file(isolated_root):
    approval_path().write_text("", encoding="utf-8")
    assert is_approved()
    assert_vendor_call_allowed(stage="extraction", model="claude-opus-5")


def test_error_names_the_stage(isolated_root):
    """단계 이름이 메시지에 없으면 §5.2-5 의 실측 목록을 만들 수 없다."""
    with pytest.raises(ExternalVendorCallError) as exc:
        assert_vendor_call_allowed(stage="relevance_gate", model="claude-haiku-4-5-20251001")
    assert "relevance_gate" in str(exc.value)


def test_no_environment_variable_opens_the_gate(isolated_root, monkeypatch):
    """환경변수 우회로를 두지 않았다. 셸 한 줄로 켜지고 아무 데도 남지 않는다."""
    for name in ("EXTERNAL_LLM_APPROVED", "ALLOW_EXTERNAL_LLM", "SKIP_EGRESS_CHECK"):
        monkeypatch.setenv(name, "1")
    with pytest.raises(ExternalVendorCallError):
        assert_vendor_call_allowed(stage="extraction")


# ---------------------------------------------------------------------------
# 4. 클라이언트 생성 지점
# ---------------------------------------------------------------------------
def test_real_client_construction_is_blocked(isolated_root):
    from extraction.llm import AnthropicClient

    with pytest.raises(ExternalVendorCallError):
        AnthropicClient(model="claude-opus-5", stage="extraction")


def test_mock_client_injection_still_works(isolated_root):
    """테스트의 목 주입은 네트워크로 나가지 않는다 — 막으면 안 된다."""
    from extraction.llm import AnthropicClient

    client = AnthropicClient(model="claude-opus-5", client=object())
    assert client.model == "claude-opus-5"


def test_injecting_a_real_sdk_object_is_not_a_loophole(isolated_root):
    """`client=` 한 글자가 게이트 전체의 우회로가 되면 안 된다."""
    import anthropic

    from extraction.llm import AnthropicClient

    sdk = anthropic.Anthropic(api_key="not-a-real-key")
    with pytest.raises(ExternalVendorCallError):
        AnthropicClient(model="claude-opus-5", client=sdk, stage="extraction")


def test_from_config_carries_the_stage(isolated_root):
    from extraction.llm import AnthropicClient

    config = {"llm": {"relevance_gate": {"model": "claude-haiku-4-5-20251001"}}}
    with pytest.raises(ExternalVendorCallError) as exc:
        AnthropicClient.from_config(config, stage="relevance_gate")
    assert "relevance_gate" in str(exc.value)


def test_judge_client_carries_the_eval_stage(isolated_root):
    """judge 도 벤더 호출 지점이다 — 채점하는 행위 자체가 같은 위반이 된다.

    ⚠️ **judge 는 이제 내부 vLLM 을 가리킨다** (ADR-018). 그래서 여기서 `provider`
    를 명시적으로 `anthropic` 으로 되돌려 검사한다 — 되돌아갔을 때 게이트가 여전히
    이 지점을 단계 이름과 함께 잡는지가 이 테스트의 계약이다.
    """
    from eval.runner import judge_client_from_config

    config = {"llm": {"provider": "anthropic"}, "eval": {"judge_model": "claude-opus-5"}}
    with pytest.raises(ExternalVendorCallError) as exc:
        judge_client_from_config(config)
    assert "eval_judge" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. 엔드포인트 목적지 판정
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://api.anthropic.com/v1",
        "https://api.openai.com/v1/chat/completions",
        "https://my-resource.openai.azure.com/openai",
        "https://bedrock-runtime.ap-northeast-2.amazonaws.com/model/m/invoke",
        "https://generativelanguage.googleapis.com",
        "http://user:pass@api.anthropic.com:443/v1",
        "https://API.ANTHROPIC.COM/v1",
    ],
)
def test_vendor_endpoints_are_rejected(url):
    with pytest.raises(ExternalEndpointError):
        assert_internal_endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "https://vllm.internal.example/v1",
        "http://localhost:8000/v1",
        "https://anthropic.com.internal.example/v1",  # 접미사가 아니라 앞부분일 뿐
        "not a url at all",
    ],
)
def test_non_vendor_endpoints_pass(url):
    assert_internal_endpoint(url)


def test_error_message_does_not_leak_the_url():
    """엔드포인트에는 인증정보가 섞일 수 있다. 진단에 필요한 건 어느 벤더인가뿐이다."""
    with pytest.raises(ExternalEndpointError) as exc:
        assert_internal_endpoint("https://user:secret@api.anthropic.com/v1")
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://api.anthropic.com/v1", "api.anthropic.com"),
        ("http://user:pass@host.example:8080/path", "host.example"),
        ("host.example", "host.example"),
        ("", ""),
    ],
)
def test_host_of(url, expected):
    assert host_of(url) == expected


def test_subdomain_of_a_vendor_host_matches():
    assert match_external_vendor("https://eu.api.anthropic.com/v1") == "api.anthropic.com"


def test_endpoint_override_precedence(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("VLLM_BASE", "https://vllm.internal.example/v1")
    assert egress.endpoint_from_env() == "https://vllm.internal.example/v1"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.openai.com/v1")
    assert egress.endpoint_from_env() == "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# 6. 승인 파일은 커밋되면 안 된다
# ---------------------------------------------------------------------------
def test_approval_file_is_gitignored():
    """커밋되면 '1회용'이 아니게 된다."""
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/external-llm-approved" in ignored


def test_repo_has_no_approval_file_left_behind():
    """세션이 게이트를 열어 둔 채 끝나는 것을 여기서 잡는다."""
    left = PROJECT_ROOT / ".claude" / "external-llm-approved"
    assert not left.exists(), (
        "승인 파일이 레포에 남아 있습니다. 게이트가 열린 상태입니다 — 지우세요."
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
