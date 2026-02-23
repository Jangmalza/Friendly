from typing import Optional

from pydantic import BaseModel


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


class ResumeRequest(BaseModel):
    client_id: str
    pm_document: Optional[str] = None
    architecture_document: Optional[str] = None
    selected_model: Optional[str] = None
