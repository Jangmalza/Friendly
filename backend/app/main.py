import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any

from app.workflow.graph import agent_graph as graph

app = FastAPI(title="AI Orchestrator API")

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


@app.websocket("/ws/orchestration/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    # Attempting to retrieve client IP safely from headers
    client_ip = websocket.headers.get("X-Forwarded-For", websocket.client.host if websocket.client else "Unknown")
    print(f"[{client_ip}] Client connected.")
    
    try:
        data = await websocket.receive_text()
        
        # UI sends raw text: ws.send(input);
        prompt = data.strip()
        
        if not prompt:
            await websocket.send_json({"error": "Prompt is required"})
            return

        initial_state = {
            "user_requirement": prompt,
            "revision_count": 0,
            "current_active_agent": "start",
            "pm_document": "",
            "architecture_document": "",
            "source_code": {},
            "qa_report": "",
            "execution_logs": [],
            "messages": []
        }

        # Human-in-the-Loop: 스레드 ID 컨피그 주입
        config = {"configurable": {"thread_id": client_id}}

        # Maintain the full state throughout the stream
        current_full_state = initial_state.copy()

        # Instead of generic streaming, LangGraph stream yields {node_name: StateUpdates} natively
        async for output in graph.astream(initial_state, config=config):
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
                    deploy_msg = node_state["messages"][-1].content
                elif current_full_state.get("messages") and current_full_state["messages"][-1].content.startswith("[Deploy"):
                    deploy_msg = current_full_state["messages"][-1].content
                
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
                    "total_cost": current_full_state.get("total_cost", 0.0)
                }
                
                await websocket.send_json(safe_event)
                await asyncio.sleep(0.5)
                
        # 인터럽트 상태 확인
        snapshot = graph.get_state(config)
        if snapshot.next:
            await websocket.send_json({"status": "WAITING_FOR_USER"})
        else:
            await websocket.send_json({"status": "completed"})
            
    except Exception as e:
        await websocket.send_json({"error": str(e)})

    except WebSocketDisconnect:
        print("Client disconnected")

from pydantic import BaseModel

class ResumeRequest(BaseModel):
    client_id: str
    pm_document: str = None
    architecture_document: str = None

@app.post("/api/resume")
async def resume_workflow(req: ResumeRequest):
    config = {"configurable": {"thread_id": req.client_id}}
    
    # Update state with user edits
    graph.update_state(
        config,
        {
            "pm_document": req.pm_document,
            "architecture_document": req.architecture_document
        }
    )
    
    # We don't stream here for simplicity; the WS might be disconnected.
    # In a full app, we'd notify the active WS session to resume streaming.
    # For now, we just resume the graph in the background (or block until done).
    return {"status": "resumed", "client_id": req.client_id}
