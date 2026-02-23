from typing import Optional

from fastapi import APIRouter

from app.api.deps import get_network_broker
from app.api.schemas import DLQRequeueRequest
from app.api.services.network_service import (
    get_admin_dlq,
    get_admin_queue_stats,
    requeue_admin_dlq_entry,
)


router = APIRouter()


@router.get("/api/network/admin/queues")
async def get_network_queue_stats(role: Optional[str] = None):
    broker = get_network_broker()
    return await get_admin_queue_stats(broker=broker, role=role)


@router.get("/api/network/admin/dlq/{role}")
async def get_network_dlq(role: str, limit: int = 100):
    broker = get_network_broker()
    return await get_admin_dlq(broker=broker, role=role, limit=limit)


@router.post("/api/network/admin/dlq/{role}/requeue")
async def requeue_network_dlq_entry(role: str, req: DLQRequeueRequest):
    broker = get_network_broker()
    return await requeue_admin_dlq_entry(broker=broker, role=role, req=req)
