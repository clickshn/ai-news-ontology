"""SessionStart 훅 `load-handoff.sh` 검사.

훅은 "최신 handoff" 를 수정 시각(`ls -t`)으로 골랐고, 에디터가 옛 handoff 를
건드리자 session-01 이 주입됐다. **훅은 정상 종료했고 아무 신호도 없었다** —
F2·F11 과 같은 계열이다(장치는 도는데 틀린 것을 고르고 아무도 모른다).

게이트 테스트(`test_external_llm_gate.py`)와 같은 이유로 훅 프로세스를 **실제로
띄운다.** 읽는 것과 돌려 보는 것은 다르다.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK = PROJECT_ROOT / ".claude" / "hooks" / "load-handoff.sh"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    # 이 테스트는 Claude Code 세션 안에서도 돈다. 그 환경의 CLAUDE_PROJECT_DIR 이
    # 남아 있으면 훅이 tmp 가 아니라 **실제 레포**를 읽는다.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    return subprocess.run(
        ["bash", str(HOOK.relative_to(PROJECT_ROOT)).replace("\\", "/")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _handoffs(root: Path, *names: str) -> Path:
    # 훅 스크립트를 tmp 로 복사하지 않고 레포의 것을 상대경로로 부르기 위해
    # tmp 안에 같은 상대 위치로 링크 대신 복사본을 둔다.
    hook_dst = root / ".claude" / "hooks" / "load-handoff.sh"
    hook_dst.parent.mkdir(parents=True)
    hook_dst.write_bytes(HOOK.read_bytes())
    handoff = root / "docs" / "handoff"
    handoff.mkdir(parents=True)
    for name in names:
        (handoff / name).write_text(f"BODY OF {name}\n", encoding="utf-8")
    return handoff


def _touch_later(path: Path) -> None:
    later = time.time() + 60
    os.utime(path, (later, later))


def test_picks_highest_session_number_not_newest_mtime(tmp_path):
    """재현 — 옛 handoff 의 수정 시각이 가장 최근이어도 번호가 가장 큰 파일을 고른다."""
    handoff = _handoffs(tmp_path, "session-01.md", "session-12.md")
    _touch_later(handoff / "session-01.md")

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "BODY OF session-12.md" in proc.stdout
    assert "BODY OF session-01.md" not in proc.stdout


def test_prints_selected_file_and_basis_on_first_line(tmp_path):
    _handoffs(tmp_path, "session-03.md", "session-04.md")

    first = _run(tmp_path).stdout.splitlines()[0]

    assert "docs/handoff/session-04.md" in first
    assert "세션 번호" in first


def test_warns_when_newest_mtime_differs_from_selected(tmp_path):
    """틀린 파일이 선택될 **뻔한** 상태를 눈에 보이게 한다 — 조용히 넘어가지 않는다."""
    handoff = _handoffs(tmp_path, "session-01.md", "session-12.md")
    _touch_later(handoff / "session-01.md")

    out = _run(tmp_path).stdout

    assert "⚠️" in out
    assert "docs/handoff/session-01.md" in out


def test_no_warning_when_selected_is_also_newest(tmp_path):
    handoff = _handoffs(tmp_path, "session-01.md", "session-02.md")
    _touch_later(handoff / "session-02.md")

    assert "⚠️" not in _run(tmp_path).stdout


def test_numeric_not_lexical_order(tmp_path):
    """사전순이면 session-9 가 session-10 을 이긴다."""
    _handoffs(tmp_path, "session-9.md", "session-10.md")

    assert "BODY OF session-10.md" in _run(tmp_path).stdout


def test_ignores_non_numbered_files(tmp_path):
    handoff = _handoffs(tmp_path, "session-02.md", "session-draft.md", "notes.md")
    _touch_later(handoff / "session-draft.md")

    out = _run(tmp_path).stdout

    assert "BODY OF session-02.md" in out
    assert "BODY OF session-draft.md" not in out


def test_missing_handoff_says_so_and_exits_zero(tmp_path):
    _handoffs(tmp_path)

    proc = _run(tmp_path)

    assert proc.returncode == 0
    assert "찾지 못했다" in proc.stdout


def test_hook_is_lf():
    """CRLF 로 체크아웃되면 셔뱅이 깨져 훅이 **조용히 안 돈다** (governance)."""
    assert b"\r\n" not in HOOK.read_bytes()
