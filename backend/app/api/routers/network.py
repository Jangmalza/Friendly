from fastapi import APIRouter, HTTPException, WebSocket

from app.api.deps import get_network_broker
from app.api.schemas import ApprovalDecisionRequest, NetworkStartRequest
from app.api.services.network_service import (
    decide_network_approval,
    get_network_events,
    get_network_run_summary,
    start_network_run,
    stream_network_events,
)
from app.workflow.nodes import (
    get_available_models,
    get_default_model,
    get_fallback_models,
    resolve_model_name,
)


router = APIRouter()


@router.get("/api/models")
def list_supported_models():
    return {
        "default_model": get_default_model(),
        "available_models": get_available_models(),
        "fallback_models": get_fallback_models(),
    }


@router.post("/api/network/start")
async def start_network_workflow(req: NetworkStartRequest):
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    try:
        selected_model = resolve_model_name(req.selected_model)
    except ValueError as model_error:
        raise HTTPException(status_code=400, detail=str(model_error)) from model_error

    broker = get_network_broker()
    return await start_network_run(broker=broker, prompt=prompt, selected_model=selected_model)


@router.get("/api/network/runs/{run_id}")
async def get_network_run(run_id: str):
    broker = get_network_broker()
    return await get_network_run_summary(broker=broker, run_id=run_id)


@router.post("/api/network/runs/{run_id}/approval")
async def decide_network_run_approval(run_id: str, req: ApprovalDecisionRequest):
    broker = get_network_broker()
    return await decide_network_approval(broker=broker, run_id=run_id, req=req)


@router.get("/api/network/runs/{run_id}/events")
async def get_network_run_events(run_id: str, limit: int = 100):
    broker = get_network_broker()
    return await get_network_events(broker=broker, run_id=run_id, limit=limit)


@router.websocket("/ws/network/{run_id}")
async def network_websocket_endpoint(websocket: WebSocket, run_id: str):
    broker = get_network_broker()
    await stream_network_events(broker=broker, websocket=websocket, run_id=run_id)
