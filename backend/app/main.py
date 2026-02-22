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

        # Maintain the full state throughout the stream
        current_full_state = initial_state.copy()

        # Instead of generic streaming, LangGraph stream yields {node_name: StateUpdates} natively
        async for output in graph.astream(initial_state):
            for node_name, node_state in output.items():
                print(f"Yielding from {node_name}")
                
                # Merge the partial update into our full state tracker
                current_full_state.update(node_state)
                
                # We extract the latest values from the updated full state
                safe_event = {
                    "active_node": node_state.get("current_active_agent") or node_name,
                    "pm_spec": current_full_state.get("pm_document"),
                    "architecture_spec": current_full_state.get("architecture_document"),
                    "current_logs": "\\n".join(current_full_state.get("execution_logs", [])) if current_full_state.get("execution_logs") else "",
                    "source_code": current_full_state.get("source_code"),
                    "qa_report": current_full_state.get("qa_report"),
                    "total_tokens": current_full_state.get("total_tokens", 0),
                    "total_cost": current_full_state.get("total_cost", 0.0)
                }
                
                await websocket.send_json(safe_event)
                await asyncio.sleep(0.5)
            
        await websocket.send_json({"status": "completed"})
            
    except Exception as e:
        await websocket.send_json({"error": str(e)})

    except WebSocketDisconnect:
        print("Client disconnected")
