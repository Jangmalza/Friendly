import os
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from app.api.network_common import (
    PARALLEL_DEVELOPMENT_PHASE,
    NETWORK_ROLES,
    clear_approval_context,
    network_run_summary,
    normalize_pending_next_roles,
    resolve_network_role,
    resolve_reject_target_role,
)
from app.api.schemas import ApprovalDecisionRequest, DLQRequeueRequest
from app.distributed.common import build_safe_event, initial_network_state, now_iso


async def start_network_run(*, broker: Any, prompt: str, selected_model: str) -> Dict[str, Any]:
    run_id = uuid4().hex
    state = initial_network_state(run_id, prompt, selected_model)

    try:
        await broker.ping()
        await broker.save_state(run_id, state)
        await broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="queued",
                from_role="supervisor",
                to_role="pm",
                event_type="workflow_status",
                note="Distributed workflow queued.",
            ),
        )
        await broker.send_task(
            "pm",
            {
                "run_id": run_id,
                "from_role": "supervisor",
                "_attempt": 1,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to start distributed workflow: {exc}") from exc

    return {
        "status": "queued",
        "run_id": run_id,
        "selected_model": selected_model,
        "ws_path": f"/ws/network/{run_id}",
    }


async def get_network_run_summary(*, broker: Any, run_id: str) -> Dict[str, Any]:
    state = await broker.load_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return network_run_summary(run_id, state)


async def decide_network_approval(
    *,
    broker: Any,
    run_id: str,
    req: ApprovalDecisionRequest,
) -> Dict[str, Any]:
    state = await broker.load_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")

    acquired = await broker.try_acquire_approval_decision(run_id, ttl_sec=30)
    if not acquired:
        raise HTTPException(status_code=409, detail="Another approval decision is in progress for this run")

    try:
        # Reload state inside lock to avoid stale approval decisions.
        state = await broker.load_state(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Run not found")

        current_status = str(state.get("status") or "").strip().lower()
        if current_status != "waiting_approval":
            raise HTTPException(
                status_code=409,
                detail=f"Run is not waiting for approval. current_status={state.get('status', 'unknown')}",
            )

        action = (req.action or "").strip().lower()
        if action not in {"approve", "reject"}:
            raise HTTPException(status_code=400, detail="action must be one of: approve, reject")

        approval_stage_raw = str(state.get("approval_stage") or "").strip()
        approval_stage = approval_stage_raw if approval_stage_raw in NETWORK_ROLES else None
        pending_next_roles = normalize_pending_next_roles(state.get("approval_pending_next_roles"))

        actor = (req.actor or "").strip() or "director"
        note = (req.note or "").strip()
        decided_at = now_iso()

        state["approval_last_action"] = action
        state["approval_last_stage"] = approval_stage
        state["approval_last_actor"] = actor
        state["approval_last_note"] = note
        state["approval_last_decision_at"] = decided_at

        if action == "approve":
            if not pending_next_roles:
                raise HTTPException(status_code=400, detail="No pending next roles found for approval routing")

            clear_approval_context(state)
            state["updated_at"] = now_iso()

            note_suffix = f" note={note}" if note else ""
            if len(pending_next_roles) > 1:
                state["status"] = "running"
                state["next_role"] = "parallel_wait"
                state["parallel_phase"] = PARALLEL_DEVELOPMENT_PHASE
                state["parallel_expected_roles"] = pending_next_roles
                state["parallel_completed_roles"] = []

                await broker.save_state(run_id, state)
                await broker.reset_parallel_phase(
                    run_id,
                    PARALLEL_DEVELOPMENT_PHASE,
                    roles=pending_next_roles,
                    clear_candidates=True,
                )

                await broker.append_event(
                    run_id,
                    build_safe_event(
                        run_id,
                        state,
                        status="running",
                        from_role=actor,
                        to_role="parallel_wait",
                        event_type="workflow_status",
                        note=(
                            f"[approval] approved stage={approval_stage or '-'}; "
                            f"parallel fan-out started: {', '.join(pending_next_roles)}.{note_suffix}"
                        ),
                    ),
                )
                await broker.append_event(
                    run_id,
                    build_safe_event(
                        run_id,
                        state,
                        status="running",
                        from_role=actor,
                        to_role="parallel_wait",
                        event_type="agent_chat",
                        note=(
                            f"@parallel-team 승인 완료. 다음 역할: {', '.join(pending_next_roles)}."
                            f"{(' 코멘트: ' + note) if note else ''}"
                        ),
                    ),
                )

                for role_name in pending_next_roles:
                    await broker.send_task(
                        role_name,
                        {
                            "run_id": run_id,
                            "from_role": actor,
                            "queued_at": now_iso(),
                            "_attempt": 1,
                        },
                    )

                return {
                    "run_id": run_id,
                    "action": action,
                    "status": state["status"],
                    "next_role": state["next_role"],
                    "next_roles": pending_next_roles,
                }

            next_role = pending_next_roles[0]
            state["status"] = "queued"
            state["next_role"] = next_role
            await broker.save_state(run_id, state)

            await broker.append_event(
                run_id,
                build_safe_event(
                    run_id,
                    state,
                    status="queued",
                    from_role=actor,
                    to_role=next_role,
                    event_type="workflow_status",
                    note=(
                        f"[approval] approved stage={approval_stage or '-'}; "
                        f"routed to {next_role}.{note_suffix}"
                    ),
                ),
            )
            await broker.append_event(
                run_id,
                build_safe_event(
                    run_id,
                    state,
                    status="queued",
                    from_role=actor,
                    to_role=next_role,
                    event_type="agent_chat",
                    note=(
                        f"@{next_role} 디렉터 승인으로 작업 재개합니다."
                        f"{(' 코멘트: ' + note) if note else ''}"
                    ),
                ),
            )

            task_entry_id = await broker.send_task(
                next_role,
                {
                    "run_id": run_id,
                    "from_role": actor,
                    "queued_at": now_iso(),
                    "_attempt": 1,
                },
            )
            return {
                "run_id": run_id,
                "action": action,
                "status": state["status"],
                "next_role": next_role,
                "task_entry_id": task_entry_id,
            }

        target_role = resolve_reject_target_role(approval_stage, req.reject_to_role)

        clear_approval_context(state)
        state["status"] = "queued"
        state["next_role"] = target_role
        state["parallel_phase"] = None
        state["parallel_expected_roles"] = []
        state["parallel_completed_roles"] = []
        state["updated_at"] = now_iso()
        await broker.save_state(run_id, state)

        note_suffix = f" note={note}" if note else ""
        await broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="queued",
                from_role=actor,
                to_role=target_role,
                event_type="workflow_status",
                note=(
                    f"[approval] rejected stage={approval_stage or '-'}; "
                    f"rerouted to {target_role}.{note_suffix}"
                ),
            ),
        )
        await broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="queued",
                from_role=actor,
                to_role=target_role,
                event_type="agent_chat",
                note=(
                    f"@{target_role} 디렉터 반려로 재작업을 요청합니다."
                    f"{(' 코멘트: ' + note) if note else ''}"
                ),
            ),
        )

        task_entry_id = await broker.send_task(
            target_role,
            {
                "run_id": run_id,
                "from_role": actor,
                "queued_at": now_iso(),
                "_attempt": 1,
            },
        )
        return {
            "run_id": run_id,
            "action": action,
            "status": state["status"],
            "next_role": target_role,
            "task_entry_id": task_entry_id,
        }
    finally:
        await broker.release_approval_decision(run_id)


async def get_network_events(*, broker: Any, run_id: str, limit: int) -> Dict[str, Any]:
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    state = await broker.load_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")

    history = await broker.read_event_history(run_id, count=limit)
    return {
        "run_id": run_id,
        "status": state.get("status", "unknown"),
        "events": [payload for _, payload in history],
    }


async def get_admin_queue_stats(*, broker: Any, role: Optional[str]) -> Dict[str, Any]:
    group_name = (os.environ.get("NETWORK_TASK_GROUP") or "network-workers").strip() or "network-workers"

    roles: List[str]
    if role:
        roles = [resolve_network_role(role)]
    else:
        roles = list(NETWORK_ROLES)

    queues: List[Dict[str, Any]] = []
    for item in roles:
        queue_stats = await broker.get_task_group_stats(item, group_name)
        queues.append(queue_stats)

    return {
        "group_name": group_name,
        "roles": roles,
        "queues": queues,
    }


async def get_admin_dlq(*, broker: Any, role: str, limit: int) -> Dict[str, Any]:
    normalized_role = resolve_network_role(role)

    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    entries = await broker.read_dlq_entries(normalized_role, count=limit, reverse=True)
    return {
        "role": normalized_role,
        "count": len(entries),
        "entries": [{"entry_id": entry_id, "payload": payload} for entry_id, payload in entries],
    }


async def requeue_admin_dlq_entry(*, broker: Any, role: str, req: DLQRequeueRequest) -> Dict[str, Any]:
    source_role = resolve_network_role(role)
    target_role = resolve_network_role(req.target_role or source_role)

    entry = await broker.get_dlq_entry(source_role, req.entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"DLQ entry '{req.entry_id}' not found for role '{source_role}'")

    _, payload = entry
    raw_task = payload.get("task")
    task: Dict[str, Any] = dict(raw_task) if isinstance(raw_task, dict) else {}

    run_id = str(task.get("run_id") or payload.get("run_id") or "").strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="DLQ entry does not include run_id")

    task["run_id"] = run_id
    task["from_role"] = "dlq_redrive"
    task["queued_at"] = now_iso()
    task.pop("last_error", None)
    task.pop("last_failed_at", None)

    if req.reset_attempt:
        task["_attempt"] = 1
    else:
        try:
            task["_attempt"] = max(1, int(task.get("_attempt", 1)))
        except (TypeError, ValueError):
            task["_attempt"] = 1

    state = await broker.load_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Run state '{run_id}' not found")

    clear_approval_context(state)
    state["status"] = "queued"
    state["next_role"] = target_role
    state["updated_at"] = now_iso()
    await broker.save_state(run_id, state)
    await broker.append_event(
        run_id,
        build_safe_event(
            run_id,
            state,
            status="queued",
            from_role="supervisor",
            to_role=target_role,
            event_type="workflow_status",
            note=(
                f"DLQ redrive: entry={req.entry_id}, source_role={source_role}, "
                f"target_role={target_role}"
            ),
        ),
    )

    task_entry_id = await broker.send_task(target_role, task)

    deleted = 0
    if req.delete_from_dlq:
        deleted = await broker.delete_dlq_entry(source_role, req.entry_id)

    return {
        "status": "requeued",
        "run_id": run_id,
        "source_role": source_role,
        "target_role": target_role,
        "task_entry_id": task_entry_id,
        "dlq_entry_id": req.entry_id,
        "dlq_deleted": bool(deleted),
    }


async def stream_network_events(*, broker: Any, websocket: WebSocket, run_id: str) -> None:
    await websocket.accept()

    try:
        state = await broker.load_state(run_id)
        if state is None:
            await websocket.send_json({"error": "Run not found", "run_id": run_id})
            return

        history = await broker.read_event_history(run_id, count=300)
        last_id = "$"
        for event_id, payload in history:
            last_id = event_id
            await websocket.send_json(payload)

        while True:
            events = await broker.read_events(run_id, last_id, block_ms=1000, count=100)
            for event_id, payload in events:
                last_id = event_id
                await websocket.send_json(payload)

            state = await broker.load_state(run_id)
            if state is None:
                await websocket.send_json({"run_id": run_id, "status": "failed", "error": "Run state lost"})
                return

            if state.get("status") in {"completed", "failed"} and not events:
                await websocket.send_json(
                    build_safe_event(
                        run_id,
                        state,
                        status=state.get("status", "completed"),
                        from_role="supervisor",
                        event_type="workflow_status",
                        note="Distributed workflow finished.",
                    )
                )
                return

    except WebSocketDisconnect:
        print(f"[network_ws] Client disconnected: {run_id}")
    except Exception as exc:
        await websocket.send_json({"run_id": run_id, "status": "failed", "error": str(exc)})
