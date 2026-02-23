from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers.admin import (
    get_network_dlq,
    get_network_queue_stats,
    requeue_network_dlq_entry,
    router as admin_router,
)
from app.api.routers.legacy import (
    resume_workflow,
    router as legacy_router,
    websocket_endpoint,
)
from app.api.routers.network import (
    decide_network_run_approval,
    get_network_run,
    get_network_run_events,
    list_supported_models,
    network_websocket_endpoint,
    router as network_router,
    start_network_workflow,
)
from app.api.routers.system import read_root, router as system_router
from app.api.schemas import (
    ApprovalDecisionRequest,
    DLQRequeueRequest,
    NetworkStartRequest,
    ResumeRequest,
)
from app.distributed.broker import RedisStreamBroker


app = FastAPI(title="AI Orchestrator API")
network_broker = RedisStreamBroker()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system_router)
app.include_router(network_router)
app.include_router(admin_router)
app.include_router(legacy_router)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await network_broker.close()


__all__ = [
    "app",
    "network_broker",
    "read_root",
    "list_supported_models",
    "start_network_workflow",
    "get_network_run",
    "decide_network_run_approval",
    "get_network_run_events",
    "get_network_queue_stats",
    "get_network_dlq",
    "requeue_network_dlq_entry",
    "network_websocket_endpoint",
    "websocket_endpoint",
    "resume_workflow",
    "NetworkStartRequest",
    "DLQRequeueRequest",
    "ApprovalDecisionRequest",
    "ResumeRequest",
]
