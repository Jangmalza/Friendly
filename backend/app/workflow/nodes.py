import os
from dotenv import load_dotenv

load_dotenv()

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage

from .state import AgentState

api_key = os.environ.get("OPENAI_API_KEY")
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7)

PM_SYSTEM_PROMPT = """당신은 10년 차 수석 프로덕트 매니저(PM)입니다.
사용자의 추상적인 요구사항을 분석하여, 다음 단계의 아키텍트와 개발자가 즉시 시스템을 설계하고 코드를 작성할 수 있는 수준의 명확한 요구사항 정의서(PRD)를 작성해야 합니다.

다음 항목을 반드시 포함하여 구조화된 마크다운 형식으로 작성하세요:
1. 프로젝트 개요 및 핵심 목표
2. 주요 기능 명세 (우선순위 포함)
3. 사용자 스토리 및 예상 시나리오
4. 엣지 케이스 및 예외 처리 고려사항

불필요한 인사말은 생략하고 오직 기획서 내용만 출력하세요.
"""

def pm_node(state: AgentState) -> AgentState:
    """Requirement Analysis by PM Agent"""
    print("--- PM NODE ---")
    user_req = state.get("user_requirement", "")
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", PM_SYSTEM_PROMPT),
        ("human", "사용자 요구사항: {user_req}")
    ])
    
    from langchain_community.callbacks.manager import get_openai_callback
    
    with get_openai_callback() as cb:
        response = chain.invoke({"user_req": user_req})
        
    current_tokens = state.get("total_tokens", 0) + cb.total_tokens
    current_cost = state.get("total_cost", 0.0) + cb.total_cost
        
    return {
        "pm_document": response.content,
        "total_tokens": current_tokens,
        "total_cost": current_cost,
        "current_active_agent": "pm_node",
        "messages": [AIMessage(content=f"[PM 기획서 산출물]\n{response.content}")]
    }

def architect_node(state: AgentState) -> AgentState:
    """Tech Stack & Directory Structure by Architect Agent"""
    print("--- ARCHITECT NODE ---")
    pm_doc = state.get("pm_document", "")
    
    system_prompt = """당신은 15년 차 수석 소프트웨어 아키텍트입니다.
    PM이 작성한 기획서를 바탕으로 최적의 기술 스택, 시스템 아키텍처, 디렉토리 구조, 그리고 API 엔드포인트 명세서를 작성해야 합니다.
    
    다음 항목을 반드시 포함하여 마크다운 형식으로 작성하세요:
    1. 시스템 아키텍처 개요 및 데이터 흐름
    2. 확정된 기술 스택 (프레임워크, 라이브러리 버전 포함)
    3. 상세 디렉토리 구조 및 파일별 역할 설명
    4. 핵심 데이터베이스 스키마 또는 인터페이스 명세
    
    개발자가 이 문서만 보고도 즉시 구조를 잡고 코딩을 시작할 수 있어야 합니다.
    """
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "기획 명세서:\n{pm_doc}")
    ])
    
    from langchain_community.callbacks.manager import get_openai_callback
    chain = prompt | llm
    with get_openai_callback() as cb:
        response = chain.invoke({"pm_doc": pm_doc})
        
    return {
        "architecture_document": response.content,
        "total_tokens": state.get("total_tokens", 0) + cb.total_tokens,
        "total_cost": state.get("total_cost", 0.0) + cb.total_cost,
        "current_active_agent": "developer_node",  # Moving to next active agent for UI
        "messages": [AIMessage(content=f"[아키텍트 설계도 산출물]\n{response.content}")]
    }

from pydantic import BaseModel, Field
from typing import Dict

class CodeOutput(BaseModel):
    """A dictionary of file paths to their complete source code content."""
    files: Dict[str, str] = Field(
        description="Dictionary where keys are file paths (e.g., 'main.py', 'src/utils.py') and values are the full source code."
    )

def developer_node(state: AgentState) -> AgentState:
    """Code Generation by Developer Agent"""
    print("--- DEVELOPER NODE ---")
    
    pm_doc = state.get("pm_document", "")
    arch_doc = state.get("architecture_document", "")
    
    system_prompt = """당신은 최고 수준의 시니어 소프트웨어 엔지니어입니다.
    제공된 기획서와 아키텍처 설계도를 바탕으로 실제 작동하는 완벽한 소스 코드를 작성하세요.
    반드시 제공된 도구 인터페이스(CodeOutput) 구조에 맞게 응답하십시오.
    """
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "기획서:\n{pm_doc}\n\n아키텍처 설계도:\n{arch_doc}")
    ])
    
    # Use Pydantic to enforce the JSON schema
    structured_llm = llm.with_structured_output(CodeOutput)
    chain = prompt | structured_llm
    
    from langchain_community.callbacks.manager import get_openai_callback
    with get_openai_callback() as cb:
        try:
            response: CodeOutput = chain.invoke({"pm_doc": pm_doc, "arch_doc": arch_doc})
            source_code_dict = response.files
        except Exception as e:
            source_code_dict = {"error.log": f"코드 생성(Structured Output) 실패:\n{str(e)}"}
    
    return {
        "source_code": source_code_dict,
        "total_tokens": state.get("total_tokens", 0) + cb.total_tokens,
        "total_cost": state.get("total_cost", 0.0) + cb.total_cost,
        "current_active_agent": "tool_execution_node",
        "messages": [AIMessage(content=f"[개발자 코드 작성 완료] {len(source_code_dict)}개의 파일 생성됨.")]
    }

import os
import docker
from docker.errors import DockerException, ContainerError, ImageNotFound

def tool_execution_node(state: AgentState) -> AgentState:
    """Saving Files & Sandbox Test"""
    print("--- TOOL NODE (DOCKER SANDBOX) ---")
    source_code = state.get("source_code", {})
    workspace_dir = os.path.abspath("./workspace")
    os.makedirs(workspace_dir, exist_ok=True)

    logs = []
    # 딕셔너리를 순회하며 파일 시스템에 소스 코드 저장 (Path Traversal 방지)
    for file_path, code_content in source_code.items():
        if not isinstance(code_content, str):
            logs.append(f"[Error] Invalid content for {file_path}")
            continue
            
        # 보안 검증: workspace 디렉토리를 벗어나는 경로 조작 공격 방지
        full_path = os.path.abspath(os.path.join(workspace_dir, file_path))
        if os.path.commonprefix([workspace_dir, full_path]) != workspace_dir:
            logs.append(f"[Security Error] Path traversal blocked for {file_path}")
            continue
        
        # 하위 디렉토리가 필요한 경우 생성
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(code_content)

    # Docker SDK를 이용한 안전한 샌드박스 파이썬 실행
    try:
        client = docker.from_env()
        
        for file_path in source_code.keys():
            if file_path.endswith(".py"):
                # 샌드박스 환경(Docker Container) 내에서 스크립트 문법 검사 실행
                try:
                    # 마운트 및 실행 속도를 위해 python:3.11-slim 사용
                    container_logs = client.containers.run(
                        image="python:3.11-slim",
                        command=f"python3 -m py_compile /app/{file_path}",
                        volumes={workspace_dir: {'bind': '/app', 'mode': 'ro'}},  # 읽기 전용 마운트
                        working_dir="/app",
                        remove=True,        # 실행 완료 후 즉시 컨테이너 삭제
                        stdout=True,
                        stderr=True,
                        network_disabled=True # 보안 강화: 네트워크 격리
                    )
                    
                    logs.append(f"[성공 - {file_path}]\n문법 검사 통과 (Docker Sandbox).")
                    
                except ContainerError as e:
                    # py_compile 실패 시 컨테이너가 에러 코드를 반환하므로 여기서 캐치됨
                    logs.append(f"[구문 에러 발생 - {file_path}]\n{e.stderr.decode('utf-8')}")
                except ImageNotFound:
                     logs.append(f"[Docker Error] 'python:3.11-slim' Image not found. Is it pulled?")
                    
    except DockerException as e:
        logs.append(f"[Docker Error] Failed to connect to Docker daemon: {str(e)}\n\n(Fallback: Docker가 실행 중인지 확인하세요.)")

    log_output = "\n".join(logs)
    if not log_output:
        log_output = "검사할 파이썬 파일이 없거나 실행이 생략되었습니다."

    return {
        "execution_logs": [log_output],
        "current_active_agent": "tool_execution_node",  # Transition to QA in UI
        "messages": [AIMessage(content=f"[도구 실행 완료] 시스템 로그:\n{log_output}")]
    }

class QAReport(BaseModel):
    """A detailed QA review and a final pass/fail status."""
    review_markdown: str = Field(
        description="Detailed review of the code in markdown format, explaining bugs, vulnerabilities, or missing features."
    )
    is_pass: bool = Field(
        description="True if the code is perfect, runs without errors, and fully meets the spec. False if it needs revision."
    )

def qa_node(state: AgentState) -> AgentState:
    """Log Analysis & Feedback by QA Agent"""
    print("--- QA NODE ---")
    pm_doc = state.get("pm_document", "")
    source_code = str(state.get("source_code", {}))
    execution_logs = "\n".join(state.get("execution_logs", []))
    current_revision = state.get("revision_count", 0)

    system_prompt = """당신은 꼼꼼한 수석 QA 엔지니어이자 코드 리뷰어입니다.
    기획서, 작성된 소스 코드, 그리고 코드 실행 로그를 분석하여 버그, 보안 취약점, 혹은 기획 누락이 있는지 엄격하게 검토하세요.
    반드시 제공된 도구 인터페이스(QAReport) 구조에 맞게 응답하십시오.
    """

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "기획서:\n{pm_doc}\n\n소스코드:\n{source_code}\n\n실행로그:\n{execution_logs}")
    ])

    structured_llm = llm.with_structured_output(QAReport)
    chain = prompt | structured_llm

    from langchain_community.callbacks.manager import get_openai_callback
    with get_openai_callback() as cb:
        try:
            response: QAReport = chain.invoke({
                "pm_doc": pm_doc, 
                "source_code": source_code, 
                "execution_logs": execution_logs
            })
            qa_report = response.review_markdown
            is_pass = response.is_pass
        except Exception as e:
            qa_report = f"QA 분석 실패:\n{str(e)}"
            is_pass = False

    if not is_pass:
        next_revision = current_revision + 1
        qa_report += "\n\n**STATUS: FAIL**"
    else:
        next_revision = current_revision
        qa_report += "\n\n**STATUS: PASS**"

    return {
        "qa_report": qa_report,
        "revision_count": next_revision,
        "total_tokens": state.get("total_tokens", 0) + cb.total_tokens,
        "total_cost": state.get("total_cost", 0.0) + cb.total_cost,
        "current_active_agent": "END" if is_pass else "developer_node", 
        "messages": [AIMessage(content=f"[QA 검토 완료]\n{qa_report}")]
    }

from github import Github
from github import Auth
import datetime

def github_deploy_node(state: AgentState) -> AgentState:
    """Auto-Deploy to GitHub"""
    print("--- GITHUB DEPLOY NODE ---")
    
    github_token = os.environ.get("GITHUB_ACCESS_TOKEN")
    if not github_token:
        deploy_log = "[Deploy Skipped] GITHUB_ACCESS_TOKEN이 설정되지 않아 배포 과정을 건너뜁니다."
        return {
            "current_active_agent": "END",
            "messages": [AIMessage(content=deploy_log)]
        }
        
    try:
        auth = Auth.Token(github_token)
        g = Github(auth=auth)
        user = g.get_user()
        
        # Create a unique repository name
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        repo_name = f"ai-generated-app-{timestamp}"
        
        repo = user.create_repo(
            repo_name,
            description="Auto-generated by AI Orchestrator",
            private=False
        )
        
        workspace_dir = os.path.abspath("./workspace")
        source_code = state.get("source_code", {})
        
        # Commit all generated files
        for file_path, code_content in source_code.items():
            if isinstance(code_content, str):
                repo.create_file(file_path, "Initial commit by AI", code_content)
                
        deploy_log = f"[Deploy Success] 성공적으로 GitHub에 배포되었습니다: {repo.html_url}"
        
    except Exception as e:
        deploy_log = f"[Deploy Failed] GitHub 배포 중 오류 발생: {str(e)}"

    return {
        "current_active_agent": "END",
        "messages": [AIMessage(content=deploy_log)]
    }
