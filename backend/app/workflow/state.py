from typing import Annotated, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class WorkflowState(TypedDict):
    """
    State representing the multi-agent software development lifecycle.
    """
    messages: Annotated[list[AnyMessage], add_messages]
    user_requirements: str
    pm_spec: str
    architecture_spec: str
    source_code: dict[str, str]  # file path -> file content
    execution_logs: str
    error_counter: int
