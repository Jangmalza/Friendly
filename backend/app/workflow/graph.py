from langgraph.graph import StateGraph, END
from typing import Literal

from .state import WorkflowState
from .nodes import pm_node, architect_node, developer_node, tool_node, qa_node

def should_continue(state: WorkflowState) -> Literal["developer_node", "END"]:
    # Simplistic conditional logic for now
    if state.get("error_counter", 0) > 3:
        return "END"
    
    # Needs actual logic based on QA analysis result
    # Assuming valid or invalid state flag attached to execution_logs later
    if getattr(state, "is_valid", True): 
        return "END"
    else:
        return "developer_node"

builder = StateGraph(WorkflowState)

builder.add_node("pm_node", pm_node)
builder.add_node("architect_node", architect_node)
builder.add_node("developer_node", developer_node)
builder.add_node("tool_node", tool_node)
builder.add_node("qa_node", qa_node)


builder.set_entry_point("pm_node")

builder.add_edge("pm_node", "architect_node")
builder.add_edge("architect_node", "developer_node")
builder.add_edge("developer_node", "tool_node")
builder.add_edge("tool_node", "qa_node")

# Conditional edge based on QA review
builder.add_conditional_edges(
    "qa_node", 
    should_continue,
    {
        "developer_node": "developer_node",
        "END": END
    }
)

graph = builder.compile()

