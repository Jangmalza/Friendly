import asyncio
import json
import os
from uuid import uuid4
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Tuple

from app.distributed.broker import RedisStreamBroker
from app.distributed.common import build_safe_event, initial_network_state, now_iso
from app.workflow.graph import agent_graph as graph
from app.workflow.nodes import get_available_models, get_default_model, get_fallback_models, resolve_model_name

app = FastAPI(title="AI Orchestrator API")
network_broker = RedisStreamBroker()
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

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"status": "ok", "message": "AI Orchestrator API is running"}


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await network_broker.close()


@app.get("/api/models")
def list_supported_models():
    return {
        "default_model": get_default_model(),
        "available_models": get_available_models(),
        "fallback_models": get_fallback_models(),
    }


def _parse_start_payload(raw_payload: str) -> Tuple[str, Optional[str]]:
    payload_text = raw_payload.strip()
    if not payload_text:
        return "", None

    try:
        parsed = json.loads(payload_text)
    except json.JSONDecodeError:
        return payload_text, None

    if isinstance(parsed, dict):
        prompt = ""
        for key in ("prompt", "user_requirement"):
            value = parsed.get(key)
            if isinstance(value, str):
                prompt = value.strip()
                break

        requested_model = None
        for key in ("model", "selected_model", "llm_model"):
            value = parsed.get(key)
            if isinstance(value, str):
                requested_model = value.strip()
                break

        return prompt, requested_model

    return payload_text, None


def _initial_state(prompt: str, selected_model: str) -> Dict[str, Any]:
    return {
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
        "messages": []
    }


def _network_run_summary(run_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
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


class NetworkStartRequest(BaseModel):
    prompt: str
    selected_model: Optional[str] = None


class DLQRequeueRequest(BaseModel):
    entry_id: str
    target_role: Optional[str] = None
    reset_attempt: bool = True
    delete_from_dlq: bool = True


class ApprovalDecisionRequest(BaseModel):
    action: str
    actor: Optional[str] = None
    note: Optional[str] = None
    reject_to_role: Optional[str] = None


def _resolve_network_role(role: str) -> str:
    normalized = (role or "").strip()
    if normalized not in NETWORK_ROLES:
        allowed = ", ".join(NETWORK_ROLES)
        raise HTTPException(status_code=400, detail=f"Unsupported role '{normalized}'. Allowed: {allowed}")
    return normalized


def _normalize_pending_next_roles(raw_roles: Any) -> List[str]:
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


def _clear_approval_context(state: Dict[str, Any]) -> None:
    state["approval_stage"] = None
    state["approval_requested_at"] = None
    state["approval_pending_next_roles"] = []


@app.post("/api/network/start")
async def start_network_workflow(req: NetworkStartRequest):
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    try:
        selected_model = resolve_model_name(req.selected_model)
    except ValueError as model_error:
        raise HTTPException(status_code=400, detail=str(model_error)) from model_error

    run_id = uuid4().hex
    state = initial_network_state(run_id, prompt, selected_model)

    try:
        await network_broker.ping()
        await network_broker.save_state(run_id, state)
        await network_broker.append_event(
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
        await network_broker.send_task(
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


@app.get("/api/network/runs/{run_id}")
async def get_network_run(run_id: str):
    state = await network_broker.load_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _network_run_summary(run_id, state)


@app.post("/api/network/runs/{run_id}/approval")
async def decide_network_run_approval(run_id: str, req: ApprovalDecisionRequest):
    state = await network_broker.load_state(run_id)
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
    pending_next_roles = _normalize_pending_next_roles(state.get("approval_pending_next_roles"))

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

        _clear_approval_context(state)
        state["updated_at"] = now_iso()

        note_suffix = f" note={note}" if note else ""
        if len(pending_next_roles) > 1:
            state["status"] = "running"
            state["next_role"] = "parallel_wait"
            state["parallel_phase"] = PARALLEL_DEVELOPMENT_PHASE
            state["parallel_expected_roles"] = pending_next_roles
            state["parallel_completed_roles"] = []

            await network_broker.save_state(run_id, state)
            await network_broker.reset_parallel_phase(
                run_id,
                PARALLEL_DEVELOPMENT_PHASE,
                roles=pending_next_roles,
                clear_candidates=True,
            )

            await network_broker.append_event(
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
            await network_broker.append_event(
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
                await network_broker.send_task(
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
        await network_broker.save_state(run_id, state)

        await network_broker.append_event(
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
        await network_broker.append_event(
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

        task_entry_id = await network_broker.send_task(
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

    target_role_candidate = req.reject_to_role or approval_stage
    if not target_role_candidate:
        raise HTTPException(status_code=400, detail="reject_to_role is required when approval_stage is missing")
    target_role = _resolve_network_role(target_role_candidate)

    _clear_approval_context(state)
    state["status"] = "queued"
    state["next_role"] = target_role
    state["parallel_phase"] = None
    state["parallel_expected_roles"] = []
    state["parallel_completed_roles"] = []
    state["updated_at"] = now_iso()
    await network_broker.save_state(run_id, state)

    note_suffix = f" note={note}" if note else ""
    await network_broker.append_event(
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
    await network_broker.append_event(
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

    task_entry_id = await network_broker.send_task(
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


@app.get("/api/network/runs/{run_id}/events")
async def get_network_run_events(run_id: str, limit: int = 100):
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    state = await network_broker.load_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Run not found")

    history = await network_broker.read_event_history(run_id, count=limit)
    return {
        "run_id": run_id,
        "status": state.get("status", "unknown"),
        "events": [payload for _, payload in history],
    }


@app.get("/api/network/admin/queues")
async def get_network_queue_stats(role: Optional[str] = None):
    group_name = (os.environ.get("NETWORK_TASK_GROUP") or "network-workers").strip() or "network-workers"
    roles: List[str]
    if role:
        roles = [_resolve_network_role(role)]
    else:
        roles = list(NETWORK_ROLES)

    queues: List[Dict[str, Any]] = []
    for item in roles:
        queue_stats = await network_broker.get_task_group_stats(item, group_name)
        queues.append(queue_stats)

    return {
        "group_name": group_name,
        "roles": roles,
        "queues": queues,
    }


@app.get("/api/network/admin/dlq/{role}")
async def get_network_dlq(role: str, limit: int = 100):
    normalized_role = _resolve_network_role(role)
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    entries = await network_broker.read_dlq_entries(normalized_role, count=limit, reverse=True)
    return {
        "role": normalized_role,
        "count": len(entries),
        "entries": [{"entry_id": entry_id, "payload": payload} for entry_id, payload in entries],
    }


@app.post("/api/network/admin/dlq/{role}/requeue")
async def requeue_network_dlq_entry(role: str, req: DLQRequeueRequest):
    source_role = _resolve_network_role(role)
    target_role = _resolve_network_role(req.target_role or source_role)

    entry = await network_broker.get_dlq_entry(source_role, req.entry_id)
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

    state = await network_broker.load_state(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Run state '{run_id}' not found")

    _clear_approval_context(state)
    state["status"] = "queued"
    state["next_role"] = target_role
    state["updated_at"] = now_iso()
    await network_broker.save_state(run_id, state)
    await network_broker.append_event(
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

    task_entry_id = await network_broker.send_task(target_role, task)

    deleted = 0
    if req.delete_from_dlq:
        deleted = await network_broker.delete_dlq_entry(source_role, req.entry_id)

    return {
        "status": "requeued",
        "run_id": run_id,
        "source_role": source_role,
        "target_role": target_role,
        "task_entry_id": task_entry_id,
        "dlq_entry_id": req.entry_id,
        "dlq_deleted": bool(deleted),
    }


@app.websocket("/ws/network/{run_id}")
async def network_websocket_endpoint(websocket: WebSocket, run_id: str):
    await websocket.accept()
    try:
        state = await network_broker.load_state(run_id)
        if state is None:
            await websocket.send_json({"error": "Run not found", "run_id": run_id})
            return

        history = await network_broker.read_event_history(run_id, count=300)
        last_id = "$"
        for event_id, payload in history:
            last_id = event_id
            await websocket.send_json(payload)

        while True:
            events = await network_broker.read_events(run_id, last_id, block_ms=1000, count=100)
            for event_id, payload in events:
                last_id = event_id
                await websocket.send_json(payload)

            state = await network_broker.load_state(run_id)
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


@app.websocket("/ws/orchestration/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    # Attempting to retrieve client IP safely from headers
    client_ip = websocket.headers.get("X-Forwarded-For", websocket.client.host if websocket.client else "Unknown")
    print(f"[{client_ip}] Client connected.")

    config = {"configurable": {"thread_id": client_id}}
    resume_mode = websocket.query_params.get("resume", "").lower() in {"1", "true", "yes"}

    try:
        if resume_mode:
            snapshot = graph.get_state(config)
            if not snapshot.next:
                await websocket.send_json({
                    "status": "completed",
                    "message": "No paused workflow found for this client_id"
                })
                return

            graph_input = None
            current_full_state = dict(snapshot.values or {})
            current_full_state.setdefault("messages", [])
        else:
            data = await websocket.receive_text()
            prompt, requested_model = _parse_start_payload(data)

            if not prompt:
                await websocket.send_json({"error": "Prompt is required"})
                return

            try:
                selected_model = resolve_model_name(requested_model)
            except ValueError as model_error:
                await websocket.send_json(
                    {
                        "error": str(model_error),
                        "available_models": get_available_models(),
                        "default_model": get_default_model(),
                    }
                )
                return

            graph_input = _initial_state(prompt, selected_model)
            current_full_state = graph_input.copy()

        # Instead of generic streaming, LangGraph stream yields {node_name: StateUpdates} natively
        async for output in graph.astream(graph_input, config=config):
            # output may contain node names or be a dict of updates
            for node_name, node_state in output.items():
                if node_name == "__interrupt__":
                    continue

                print(f"Yielding from {node_name}")

                # Merge the partial update into our full state tracker
                current_full_state.update(node_state)

                # Extract deploy log if we are currently on github_deploy_node
                deploy_msg = ""
                if node_name == "github_deploy_node" and node_state.get("messages"):
                    last_msg = node_state["messages"][-1]
                    deploy_msg = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
                elif current_full_state.get("messages"):
                    last_msg = current_full_state["messages"][-1]
                    last_msg_content = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
                    if last_msg_content.startswith("[Deploy"):
                        deploy_msg = last_msg_content

                # We extract the latest values from the updated full state
                safe_event = {
                    "active_node": node_state.get("current_active_agent") or node_name,
                    "pm_spec": current_full_state.get("pm_document"),
                    "architecture_spec": current_full_state.get("architecture_document"),
                    "current_logs": "\n".join(current_full_state.get("execution_logs", [])) if current_full_state.get("execution_logs") else "",
                    "source_code": current_full_state.get("source_code"),
                    "qa_report": current_full_state.get("qa_report"),
                    "github_deploy_log": deploy_msg,
                    "total_tokens": current_full_state.get("total_tokens", 0),
                    "total_cost": current_full_state.get("total_cost", 0.0),
                    "selected_model": current_full_state.get("selected_model", get_default_model()),
                }

                await websocket.send_json(safe_event)
                await asyncio.sleep(0.5)

        # 인터럽트 상태 확인
        snapshot = graph.get_state(config)
        if snapshot.next:
            await websocket.send_json(
                {
                    "status": "WAITING_FOR_USER",
                    "selected_model": current_full_state.get("selected_model", get_default_model()),
                }
            )
        else:
            await websocket.send_json(
                {
                    "status": "completed",
                    "selected_model": current_full_state.get("selected_model", get_default_model()),
                }
            )

    except WebSocketDisconnect:
        print("Client disconnected")

    except Exception as e:
        await websocket.send_json({"error": str(e)})

class ResumeRequest(BaseModel):
    client_id: str
    pm_document: Optional[str] = None
    architecture_document: Optional[str] = None
    selected_model: Optional[str] = None

@app.post("/api/resume")
async def resume_workflow(req: ResumeRequest):
    config = {"configurable": {"thread_id": req.client_id}}

    updates: Dict[str, Any] = {}
    if req.pm_document is not None:
        updates["pm_document"] = req.pm_document
    if req.architecture_document is not None:
        updates["architecture_document"] = req.architecture_document
    if req.selected_model is not None:
        try:
            updates["selected_model"] = resolve_model_name(req.selected_model)
        except ValueError as model_error:
            raise HTTPException(status_code=400, detail=str(model_error)) from model_error

    if updates:
        graph.update_state(config, updates)

    snapshot = graph.get_state(config)
    snapshot_state = dict(snapshot.values or {})
    return {
        "status": "resumed",
        "client_id": req.client_id,
        "waiting_nodes": list(snapshot.next) if snapshot.next else [],
        "selected_model": snapshot_state.get("selected_model", get_default_model()),
    }
