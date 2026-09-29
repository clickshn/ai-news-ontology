"""경보가 사람에게 닿는 경로 — Vault 상태 노트 · SessionStart 훅용 상태 파일 · 토스트 (ADR-025).

## 사람이 이미 보는 곳에 둔다

사용자는 매일 Obsidian 을 열고, 개발은 Claude Code 세션에서 한다. 새로 봐야 할 곳을
만들면 그곳이 다음 stderr 가 된다.

## Vault 에는 파일 하나만 쓴다

`STATUS_NOTE_NAME` **하나만** Vault 정책(`skip`, D-028 · ADR-010)의 예외다. 파일명은
config 가 아니라 이 상수이고, 이 모듈에는 다른 이름으로 Vault 에 쓰는 경로가 없다.
두 번째 파일이 필요해지면 ADR-025 Amendment 가 선행 조건이다. 노트에는 LLM 출력
본문을 넣지 않는다 — 지표와 경보만.

## 실행 부재는 쓰는 쪽이 적을 수 없다

스케줄러가 안 돌면 이 모듈도 안 돈다. 그래서 상태 파일에는 마지막 실행 시각(epoch)만
적고, **나이 계산은 읽는 쪽(훅)이** 한다. 상태 노트에도 시각을 크게 적는다.

## 토스트는 보조다

잠금 화면·부재 중에는 놓친다. 실패해도 실행을 실패시키지 않고 결과만 돌려준다.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pipeline.alerts import CRITICAL, open_alerts

#: Vault 에 쓰는 **유일한** 비뉴스 파일. 바꾸거나 늘리지 않는다 (ADR-025).
STATUS_NOTE_NAME = "_pipeline-status.md"

_MARK = {"critical": "🔴", "warning": "🟠", "info": "⚪"}


def status_note_path(output_dir: Path) -> Path:
    return Path(output_dir) / STATUS_NOTE_NAME


def render_status_note(
    *,
    state: dict[str, Any],
    run: dict[str, Any],
    approval: dict[str, Any] | None,
    absent_after_hours: int,
) -> str:
    alerts = open_alerts(state)
    lines = [
        "---",
        "type: pipeline-status",
        f"updated: {run.get('finished_at') or run.get('started_at')}",
        "---",
        "",
        "> 이 노트는 파이프라인이 **매 자동 실행마다 덮어쓴다** (ADR-025). 편집해도 남지 않는다.",
        f"> 마지막 실행 시각이 {absent_after_hours}시간보다 오래됐으면 스케줄러가 안 돈 것이다.",
        "",
        "# 파이프라인 상태",
        "",
        f"- **마지막 실행:** {run.get('started_at')} (`{run.get('run_id')}`)",
        f"- **결과:** {run.get('outcome')} · 종료 코드 {run.get('exit_code')}",
    ]
    if approval:
        lines.append(f"- **상시 승인 만료:** {approval.get('expires_at')} — 갱신 때 노트 3건을 원문과 대조한다")
    else:
        lines.append("- **상시 승인:** 없음")
    lines += ["", f"## 열린 경보 ({len(alerts)})", ""]
    if not alerts:
        lines.append("없음.")
    for a in alerts:
        where = f" · {a['source']}" if a.get("source") else ""
        lines.append(
            f"- {_MARK.get(a['severity'], '')} **{a['kind']}**{where} — {a['detail']}"
            f" (연속 {a['consecutive']}회, 처음 {a['first_seen'][:10]})"
        )
    quality = run.get("quality") or {}
    if quality:
        lines += [
            "",
            "## 품질 지표 (기록만 — 판정 없음)",
            "",
            "| 소스 | 본문 중앙값 | 본문 0자 | 게이트 통과율 | 추출 | 재시도율 | 미등록 기업 | 관련기술 빈 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for name, q in quality.items():
            lines.append(
                f"| {name} | {_fmt(q.get('body_chars_median'))} | {_fmt(q.get('body_empty'))}"
                f" | {_fmt(q.get('gate_pass_rate'))} | {_fmt(q.get('extracted'))}"
                f" | {_fmt(q.get('schema_retry_rate'))} | {_fmt(q.get('unresolved_company_rate'))}"
                f" | {_fmt(q.get('prior_art_empty_rate'))} |"
            )
        lines += ["", "근거 없는 채움은 이 표로 보이지 않는다 — 표본 대조만이 잡는다 (D-107)."]
    return "\n".join(lines) + "\n"


def _fmt(value: Any) -> str:
    return "—" if value is None else str(value)


def write_status_note(output_dir: Path, text: str) -> Path:
    """Vault 에 상태 노트를 쓴다. **이 함수가 쓰는 경로는 `STATUS_NOTE_NAME` 하나뿐이다.**"""
    path = status_note_path(output_dir)
    if not Path(output_dir).is_dir():
        raise FileNotFoundError(f"노트 디렉터리가 없다: {output_dir}")
    path.write_text(text, encoding="utf-8")
    return path


def render_status_file(
    *,
    state: dict[str, Any],
    run: dict[str, Any],
    approval: dict[str, Any] | None,
    absent_after_hours: int,
    now: datetime,
) -> str:
    """SessionStart 훅이 읽는 평문. 앞 줄은 `키=값`, `---` 뒤는 그대로 출력할 줄."""
    lines = [
        f"last_run_epoch={int(now.timestamp())}",
        f"last_run_at={run.get('started_at')}",
        f"exit_code={run.get('exit_code')}",
        f"outcome={run.get('outcome')}",
        f"absent_after_hours={absent_after_hours}",
        f"approval_expires={(approval or {}).get('expires_at', '')}",
        "---",
    ]
    alerts = open_alerts(state)
    if not alerts:
        lines.append("열린 경보 없음")
    for a in alerts:
        where = f" · {a['source']}" if a.get("source") else ""
        lines.append(f"{_MARK.get(a['severity'], '')} {a['kind']}{where} — {a['detail']} (연속 {a['consecutive']}회)")
    return "\n".join(lines) + "\n"


def write_status_file(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 토스트
# ---------------------------------------------------------------------------
# 제목·본문은 명령줄이 아니라 **환경변수로** 넘긴다 — 따옴표 이스케이프 문제를 없애고,
# 경보 문구가 PowerShell 코드로 해석될 여지를 없앤다. 모듈 설치 없이 WinRT API 를 쓴다.
_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:AINO_TOAST_TITLE)) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode($env:AINO_TOAST_BODY)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def send_toast(title: str, body: str, *, runner: Callable[..., Any] = subprocess.run) -> str | None:
    """토스트를 띄운다. 성공이면 None, 실패면 사유 문자열. **예외를 올리지 않는다.**"""
    env = {**os.environ, "AINO_TOAST_TITLE": title[:120], "AINO_TOAST_BODY": body[:400]}
    try:
        result = runner(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _TOAST_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 — 토스트 실패가 실행을 실패시키지 않는다
        return f"{type(exc).__name__}: {exc}"[:300]
    if getattr(result, "returncode", 0) != 0:
        return (getattr(result, "stderr", "") or f"exit {result.returncode}").strip()[:300]
    return None


def toast_text(state: dict[str, Any]) -> tuple[str, str] | None:
    """심각 등급이 열려 있으면 (제목, 본문). 없으면 None — 토스트는 심각에만 보낸다."""
    critical = [a for a in open_alerts(state) if a["severity"] == CRITICAL]
    if not critical:
        return None
    head = critical[0]
    where = f" · {head['source']}" if head.get("source") else ""
    title = f"ai-news 파이프라인: 심각 {len(critical)}건"
    body = f"{head['kind']}{where} — {head['detail']}"
    if len(critical) > 1:
        body += f" 외 {len(critical) - 1}건. Vault 의 {STATUS_NOTE_NAME} 참고"
    return title, body
