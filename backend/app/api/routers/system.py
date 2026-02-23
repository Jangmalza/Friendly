from fastapi import APIRouter


router = APIRouter()


@router.get("/")
def read_root():
    return {"status": "ok", "message": "AI Orchestrator API is running"}
