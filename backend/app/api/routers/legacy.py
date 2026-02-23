import asyncio
import json
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.api.schemas import ResumeRequest
from app.workflow.graph import agent_graph as graph
from app.workflow.nodes import get_available_models, get_default_model, resolve_model_name


router = APIRouter()


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
        "messages": [],
    }


@router.websocket("/ws/orchestration/{client_id}")
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
                await websocket.send_json(
                    {
                        "status": "completed",
                        "message": "No paused workflow found for this client_id",
                    }
                )
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
                    "current_logs": (
                        "\n".join(current_full_state.get("execution_logs", []))
                        if current_full_state.get("execution_logs")
                        else ""
                    ),
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

    except Exception as exc:
        await websocket.send_json({"error": str(exc)})


@router.post("/api/resume")
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
