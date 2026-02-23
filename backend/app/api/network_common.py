from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.workflow.nodes import get_default_model


NETWORK_ROLES = (
    "pm",
    "architect",
    "developer_backend",
    "developer_frontend",
    "developer",
    "tool_execution",
    "qa",
    "github_deploy",
)
PARALLEL_DEVELOPMENT_PHASE = "parallel_developer"


def network_run_summary(run_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "status": state.get("status", "unknown"),
        "next_role": state.get("next_role"),
        "parallel_phase": state.get("parallel_phase"),
        "parallel_expected_roles": state.get("parallel_expected_roles", []),
        "parallel_completed_roles": state.get("parallel_completed_roles", []),
        "approval_stage": state.get("approval_stage"),
        "approval_requested_at": state.get("approval_requested_at"),
        "approval_pending_next_roles": state.get("approval_pending_next_roles", []),
        "approval_last_action": state.get("approval_last_action"),
        "approval_last_stage": state.get("approval_last_stage"),
        "approval_last_actor": state.get("approval_last_actor"),
        "approval_last_note": state.get("approval_last_note", ""),
        "approval_last_decision_at": state.get("approval_last_decision_at"),
        "selected_model": state.get("selected_model", get_default_model()),
        "active_node": state.get("current_active_agent"),
        "total_tokens": state.get("total_tokens", 0),
        "total_cost": state.get("total_cost", 0.0),
        "updated_at": state.get("updated_at"),
    }


def resolve_network_role(role: str) -> str:
    normalized = (role or "").strip()
    if normalized not in NETWORK_ROLES:
        allowed = ", ".join(NETWORK_ROLES)
        raise HTTPException(status_code=400, detail=f"Unsupported role '{normalized}'. Allowed: {allowed}")
    return normalized


def normalize_pending_next_roles(raw_roles: Any) -> List[str]:
    if not isinstance(raw_roles, list):
        return []

    normalized: List[str] = []
    seen = set()
    for item in raw_roles:
        role = str(item).strip()
        if not role or role in seen:
            continue
        if role in NETWORK_ROLES:
            normalized.append(role)
            seen.add(role)
    return normalized


def clear_approval_context(state: Dict[str, Any]) -> None:
    state["approval_stage"] = None
    state["approval_requested_at"] = None
    state["approval_pending_next_roles"] = []


def resolve_reject_target_role(approval_stage: Optional[str], requested_role: Optional[str]) -> str:
    if approval_stage:
        normalized_requested = (requested_role or "").strip()
        if normalized_requested and normalized_requested != approval_stage:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"reject_to_role must be '{approval_stage}' for approval_stage='{approval_stage}'. "
                    "Cross-stage reroute is not allowed."
                ),
            )
        return approval_stage

    normalized_requested = (requested_role or "").strip()
    if not normalized_requested:
        raise HTTPException(status_code=400, detail="reject_to_role is required when approval_stage is missing")

    resolved = resolve_network_role(normalized_requested)
    if resolved != "pm":
        raise HTTPException(
            status_code=400,
            detail="For legacy runs without approval_stage, reject_to_role must be 'pm'.",
        )
    return resolved
