import datetime
from typing import Any, Dict, Iterable, List, Optional


MAX_RETRIES_DEFAULT = 3


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def message_to_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if content is not None:
        return str(content)
    return str(message)


def normalize_messages(messages: Iterable[Any]) -> List[str]:
    return [message_to_text(message) for message in messages]


def merge_state(previous: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(previous)
    for key, value in update.items():
        if key == "messages":
            prev_messages = merged.get("messages", [])
            if not isinstance(prev_messages, list):
                prev_messages = []

            new_messages: List[str]
            if isinstance(value, list):
                new_messages = normalize_messages(value)
            else:
                new_messages = normalize_messages([value])

            merged["messages"] = prev_messages + new_messages
            continue

        if key == "execution_logs":
            prev_logs = merged.get("execution_logs", [])
            if not isinstance(prev_logs, list):
                prev_logs = []

            if isinstance(value, list):
                new_logs = [str(item) for item in value]
            else:
                new_logs = [str(value)]
            merged["execution_logs"] = prev_logs + new_logs
            continue

        merged[key] = value

    return merged


def get_deploy_log_from_messages(messages: Iterable[Any]) -> str:
    for message in reversed(list(messages)):
        text = message_to_text(message)
        if text.startswith("[Deploy"):
            return text
    return ""


def truncate_text(value: str, limit: int = 1200) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...(truncated)"


def _first_nonempty_line(value: str) -> str:
    for line in value.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate.lstrip("#").strip()
    return ""


def _summarize_source_files(source_code: Any, *, max_items: int = 4) -> str:
    if not isinstance(source_code, dict) or not source_code:
        return "no files"

    names = sorted(str(name) for name in source_code.keys())
    visible = names[:max_items]
    more_count = max(0, len(names) - len(visible))
    if more_count > 0:
        return f"{', '.join(visible)} (+{more_count})"
    return ", ".join(visible)


def _qa_status(qa_report: str) -> str:
    if "STATUS: PASS" in qa_report:
        return "PASS"
    if "STATUS: FAIL" in qa_report:
        return "FAIL"
    return "UNKNOWN"


def role_message_content(role: str, state: Dict[str, Any]) -> str:
    if role == "pm":
        return truncate_text(state.get("pm_document", "") or "")
    if role == "architect":
        return truncate_text(state.get("architecture_document", "") or "")
    if role == "developer":
        source_code = state.get("source_code") or {}
        if isinstance(source_code, dict):
            return f"Generated {len(source_code)} files."
        return "Developer step finished."
    if role == "tool_execution":
        logs = state.get("execution_logs") or []
        if isinstance(logs, list) and logs:
            return truncate_text(str(logs[-1]), limit=800)
        return "Tool execution finished."
    if role == "qa":
        return truncate_text(state.get("qa_report", "") or "", limit=1000)
    if role == "github_deploy":
        deploy_log = get_deploy_log_from_messages(state.get("messages", []))
        return truncate_text(deploy_log, limit=400)
    return ""


def role_chat_content(role: str, state: Dict[str, Any], next_role: Optional[str]) -> str:
    target = next_role or "supervisor"

    if role == "pm":
        pm_document = str(state.get("pm_document", "") or "")
        headline = _first_nonempty_line(pm_document) or str(state.get("user_requirement", "") or "")
        return (
            f"@{target} 요구사항 문서 정리 완료. 핵심 목표: "
            f"{truncate_text(headline, limit=240)}"
        )

    if role == "architect":
        architecture_document = str(state.get("architecture_document", "") or "")
        headline = _first_nonempty_line(architecture_document) or "아키텍처 초안 작성 완료"
        return (
            f"@{target} 설계안 전달합니다. 핵심 구조: "
            f"{truncate_text(headline, limit=240)}"
        )

    if role == "developer":
        source_code = state.get("source_code") or {}
        file_count = len(source_code) if isinstance(source_code, dict) else 0
        file_preview = _summarize_source_files(source_code)
        return (
            f"@{target} 구현 완료. 생성 파일 {file_count}개 "
            f"({truncate_text(file_preview, limit=200)})."
        )

    if role == "tool_execution":
        logs = state.get("execution_logs") or []
        latest_log = ""
        if isinstance(logs, list) and logs:
            latest_log = _first_nonempty_line(str(logs[-1]))
        summary = latest_log or "실행 로그 요약 없음"
        return (
            f"@{target} 샌드박스 실행/검증 완료. 요약: "
            f"{truncate_text(summary, limit=220)}"
        )

    if role == "qa":
        qa_report = str(state.get("qa_report", "") or "")
        status = _qa_status(qa_report)
        revision_count = int(state.get("revision_count", 0))
        qa_headline = _first_nonempty_line(qa_report) or "QA 결과 요약 없음"
        return (
            f"@{target} QA 결과 {status} (revision={revision_count}). "
            f"{truncate_text(qa_headline, limit=220)}"
        )

    if role == "github_deploy":
        deploy_log = get_deploy_log_from_messages(state.get("messages", []))
        deploy_headline = _first_nonempty_line(deploy_log) or "배포 로그 없음"
        return (
            f"@{target} 배포 단계 종료. "
            f"{truncate_text(deploy_headline, limit=220)}"
        )

    return truncate_text(f"@{target} 단계 완료", limit=220)


def initial_network_state(run_id: str, prompt: str, selected_model: str) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "mode": "network_chat",
        "status": "queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "next_role": "pm",
        "user_requirement": prompt,
        "selected_model": selected_model,
        "revision_count": 0,
        "current_active_agent": "start",
        "pm_document": "",
        "architecture_document": "",
        "source_code": {},
        "qa_report": "",
        "total_tokens": 0,
        "total_cost": 0.0,
        "execution_logs": [],
        "messages": [],
    }


def build_safe_event(
    run_id: str,
    state: Dict[str, Any],
    *,
    status: Optional[str] = None,
    from_role: Optional[str] = None,
    to_role: Optional[str] = None,
    event_type: str = "agent_message",
    note: str = "",
) -> Dict[str, Any]:
    deploy_log = get_deploy_log_from_messages(state.get("messages", []))
    return {
        "run_id": run_id,
        "event_type": event_type,
        "from_role": from_role,
        "to_role": to_role,
        "status": status or state.get("status", "running"),
        "active_node": state.get("current_active_agent"),
        "pm_spec": state.get("pm_document"),
        "architecture_spec": state.get("architecture_document"),
        "current_logs": "\n".join(state.get("execution_logs", [])) if state.get("execution_logs") else "",
        "source_code": state.get("source_code"),
        "qa_report": state.get("qa_report"),
        "github_deploy_log": deploy_log,
        "total_tokens": state.get("total_tokens", 0),
        "total_cost": state.get("total_cost", 0.0),
        "selected_model": state.get("selected_model"),
        "message": note,
        "updated_at": now_iso(),
    }


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return str(value)
