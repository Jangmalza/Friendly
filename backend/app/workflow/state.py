from typing import TypedDict, Annotated, List, Dict
import operator
from langchain_core.messages import BaseMessage

class AgentState(TypedDict):
    # 1. 기본 요구사항 및 메타데이터
    user_requirement: str
    selected_model: str
    revision_count: int
    current_active_agent: str

    # 2. 에이전트 산출물 (새로운 작업 완료 시 기존 내용 덮어쓰기)
    pm_document: str
    architecture_document: str
    source_code: Dict[str, str]
    qa_report: str
    
    # 3. 비용 및 토큰 추적
    total_tokens: int
    total_cost: float

    # 3. 누적되는 데이터 (Annotated와 operator.add를 사용하여 기존 데이터에 추가)
    execution_logs: Annotated[List[str], operator.add]
    messages: Annotated[List[BaseMessage], operator.add]
