from langgraph.graph import StateGraph, END
from typing import Literal

from .state import AgentState
from .nodes import pm_node, architect_node, developer_node, tool_execution_node, qa_node, github_deploy_node

def qa_router(state: AgentState):
    qa_report = state.get("qa_report", "")
    revision_count = state.get("revision_count", 0)
    max_retries = 3

    # 최대 수정 횟수 초과 시 강제 종료
    if revision_count >= max_retries:
        return "end"

    # QA 리포트에서 상태 코드 분석
    if "STATUS: PASS" in qa_report:
        return "github_deploy_node"
    elif "STATUS: FAIL" in qa_report:
        return "developer_node"
    else:
        # 상태 코드가 명확하지 않은 경우 안전을 위해 종료
        return "end"

# 1. 그래프 초기화 (상태 객체 스키마 주입)
workflow = StateGraph(AgentState)

# 2. 노드 등록
workflow.add_node("pm_node", pm_node)
workflow.add_node("architect_node", architect_node)
workflow.add_node("developer_node", developer_node)
workflow.add_node("tool_execution_node", tool_execution_node)
workflow.add_node("qa_node", qa_node)
workflow.add_node("github_deploy_node", github_deploy_node)

# 3. 진입점 설정
workflow.set_entry_point("pm_node")

# 4. 순차적 실행 엣지 연결
workflow.add_edge("pm_node", "architect_node")
workflow.add_edge("architect_node", "developer_node")
workflow.add_edge("developer_node", "tool_execution_node")
workflow.add_edge("tool_execution_node", "qa_node")
workflow.add_edge("github_deploy_node", END)

# 5. 조건부 엣지 연결 (QA 결과에 따른 분기)
workflow.add_conditional_edges(
    "qa_node",
    qa_router,
    {
        # 라우터 함수의 반환값에 따라 이동할 실제 노드 매핑
        "developer_node": "developer_node",
        "github_deploy_node": "github_deploy_node",
        "end": END
    }
)

# 6. checkpointer 추가하여 Human-in-the-Loop 적용 (개발 요정 전 일시정지)
from langgraph.checkpoint.memory import MemorySaver

memory = MemorySaver()
agent_graph = workflow.compile(
    checkpointer=memory,
    interrupt_before=["developer_node"]
)
