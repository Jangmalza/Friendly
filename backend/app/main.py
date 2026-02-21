import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Any

from app.workflow.graph import graph

app = FastAPI(title="AI Orchestrator API")

origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

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


@app.websocket("/ws/orchestrate")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_text()
        request_data = json.loads(data)
        prompt = request_data.get("prompt", "")

        if not prompt:
            await websocket.send_json({"error": "Prompt is required"})
            return

        initial_state = {
            "messages": [("user", prompt)],
            "user_requirements": prompt,
            "pm_spec": "",
            "architecture_spec": "",
            "source_code": {},
            "execution_logs": "",
            "error_counter": 0
        }

        # Use LangGraph astream_events to stream execution progress in real-time
        # For LangGraph SDK or standard `astream`, yielding events helps power the UI
        try:
            async for event in graph.astream(initial_state, stream_mode="values"):
                # `astream` yields the whole state object
                 # Clean up any non-serializable elements before sending
                safe_event = {
                  "pm_spec": event.get("pm_spec"),
                  "architecture_spec": event.get("architecture_spec"),
                  "current_logs": event.get("execution_logs")
                }
                # Optional: Send typed node names if utilizing stream_mode="updates"
                await websocket.send_json(safe_event)
            
            await websocket.send_json({"status": "completed"})
            
        except Exception as e:
            await websocket.send_json({"error": str(e)})

    except WebSocketDisconnect:
        print("Client disconnected")
