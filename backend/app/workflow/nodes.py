import os
import json
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Type, TypeVar
from dotenv import load_dotenv

load_dotenv()

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field, ValidationError

from .state import AgentState

api_key = os.environ.get("OPENAI_API_KEY")
DEFAULT_TEMPERATURE = 0.7

try:
    DEFAULT_TEMPERATURE = float(os.environ.get("OPENAI_TEMPERATURE", "0.7"))
except ValueError:
    DEFAULT_TEMPERATURE = 0.7

DEFAULT_MODEL = (os.environ.get("OPENAI_MODEL_DEFAULT") or "gpt-4o-mini").strip()
_raw_available_models = os.environ.get(
    "OPENAI_AVAILABLE_MODELS",
    "gpt-4o-mini,gpt-4o,gpt-4.1,gpt-5-mini,gpt-5",
)
_model_candidates = [item.strip() for item in _raw_available_models.split(",") if item.strip()]
AVAILABLE_MODELS = tuple(dict.fromkeys(_model_candidates))
if not AVAILABLE_MODELS:
    AVAILABLE_MODELS = (DEFAULT_MODEL,)
if DEFAULT_MODEL not in AVAILABLE_MODELS:
    AVAILABLE_MODELS = (DEFAULT_MODEL, *AVAILABLE_MODELS)


def _parse_model_list(raw_models: str) -> List[str]:
    parsed = [item.strip() for item in raw_models.split(",") if item.strip()]
    return list(dict.fromkeys(parsed))


_raw_fallback_models = os.environ.get("OPENAI_FALLBACK_MODELS", "gpt-4.1,gpt-4o-mini")
FALLBACK_MODELS = tuple(
    model_name
    for model_name in _parse_model_list(_raw_fallback_models)
    if model_name in AVAILABLE_MODELS and model_name != DEFAULT_MODEL
)


def _read_int_env(key: str, default: int) -> int:
    raw_value = os.environ.get(key)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _read_float_env(key: str, default: float) -> float:
    raw_value = os.environ.get(key)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


LLM_MAX_ATTEMPTS = _read_int_env("LLM_MAX_ATTEMPTS", 2)
LLM_RETRY_BACKOFF_SEC = _read_float_env("LLM_RETRY_BACKOFF_SEC", 1.0)
PROMPT_MAX_DOC_CHARS = _read_int_env("PROMPT_MAX_DOC_CHARS", 12000)
PROMPT_MAX_LOG_CHARS = _read_int_env("PROMPT_MAX_LOG_CHARS", 6000)
PROMPT_MAX_SOURCE_FILES = _read_int_env("PROMPT_MAX_SOURCE_FILES", 20)
PROMPT_MAX_CHARS_PER_FILE = _read_int_env("PROMPT_MAX_CHARS_PER_FILE", 3000)
PROMPT_MAX_TOTAL_SOURCE_CHARS = _read_int_env("PROMPT_MAX_TOTAL_SOURCE_CHARS", 24000)


def get_available_models() -> List[str]:
    return list(AVAILABLE_MODELS)


def get_default_model() -> str:
    return DEFAULT_MODEL


def get_fallback_models() -> List[str]:
    return list(FALLBACK_MODELS)


def resolve_model_name(requested_model: Optional[str]) -> str:
    if requested_model is None:
        return DEFAULT_MODEL

    normalized = requested_model.strip()
    if not normalized:
        return DEFAULT_MODEL

    if normalized not in AVAILABLE_MODELS:
        allowed = ", ".join(AVAILABLE_MODELS)
        raise ValueError(f"Unsupported model '{normalized}'. Allowed models: {allowed}")

    return normalized


def _iter_candidate_models(preferred_model: str) -> List[str]:
    ordered = [preferred_model]
    for candidate in FALLBACK_MODELS:
        if candidate not in ordered:
            ordered.append(candidate)
    if DEFAULT_MODEL not in ordered:
        ordered.append(DEFAULT_MODEL)
    return ordered


def get_llm_for_state(state: AgentState, *, override_model: Optional[str] = None) -> ChatOpenAI:
    model_name = resolve_model_name(override_model or state.get("selected_model"))

    kwargs = {"model": model_name}
    # GPT-5 family currently accepts only default temperature(1).
    if model_name.startswith("gpt-5"):
        kwargs["temperature"] = 1
    else:
        kwargs["temperature"] = DEFAULT_TEMPERATURE

    if api_key:
        kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)


class _NoopCallback:
    """Fallback callback when langchain_community is unavailable."""

    total_tokens = 0
    total_cost = 0.0


@contextmanager
def get_openai_callback_safe() -> Iterator[object]:
    """Use OpenAI callback if available; otherwise keep workflow running."""

    try:
        from langchain_community.callbacks.manager import get_openai_callback
    except Exception:
        yield _NoopCallback()
        return

    with get_openai_callback() as cb:
        yield cb


TModel = TypeVar("TModel", bound=BaseModel)


def _strip_code_fence(text: str) -> str:
    value = text.strip()
    if not value.startswith("```"):
        return value
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        return text

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == "\"":
                in_string = False
            continue

        if char == "\"":
            in_string = True
            continue

        if char == "{":
            depth += 1
            continue

        if char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]

    return text[start:]


def _parse_json_object(text: str) -> Dict[str, Any]:
    cleaned = _strip_code_fence(text)
    candidate = _extract_json_object(cleaned).strip()
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("Parsed JSON is not an object")
    return parsed


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return text
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n...(truncated {len(text) - limit} chars)"


def _stringify_source_code_for_prompt(source_code: Dict[str, Any]) -> str:
    if not isinstance(source_code, dict):
        return str(source_code)

    lines: List[str] = []
    total_chars = 0
    sorted_items = sorted(source_code.items(), key=lambda item: str(item[0]))

    for index, (file_path, content) in enumerate(sorted_items):
        if index >= PROMPT_MAX_SOURCE_FILES:
            lines.append("... additional files omitted")
            break

        path = str(file_path)
        content_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
        trimmed = _truncate_text(content_text, PROMPT_MAX_CHARS_PER_FILE)
        entry = f"### {path}\n{trimmed}"

        projected = total_chars + len(entry)
        if projected > PROMPT_MAX_TOTAL_SOURCE_CHARS:
            remaining = max(0, PROMPT_MAX_TOTAL_SOURCE_CHARS - total_chars)
            if remaining > 0:
                lines.append(_truncate_text(entry, remaining))
            lines.append("... source summary truncated due to prompt budget")
            break

        lines.append(entry)
        total_chars = projected

    if not lines:
        return "{}"
    return "\n\n".join(lines)


def _build_invoke_config(*, node_name: str, model_name: str, state: AgentState) -> Dict[str, Any]:
    metadata = {
        "node": node_name,
        "model": model_name,
    }
    run_id = state.get("run_id")
    if isinstance(run_id, str) and run_id:
        metadata["run_id"] = run_id
    return {
        "run_name": f"{node_name}:{model_name}",
        "tags": ["ai-orchestrator", "network-workflow", node_name, model_name],
        "metadata": metadata,
    }


def invoke_with_retry(
    chain: Any,
    payload: Dict[str, Any],
    *,
    max_attempts: Optional[int] = None,
    invoke_config: Optional[Dict[str, Any]] = None,
) -> Any:
    attempts = max(1, max_attempts or LLM_MAX_ATTEMPTS)
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            if invoke_config:
                return chain.invoke(payload, config=invoke_config)
            return chain.invoke(payload)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            backoff = max(0.0, LLM_RETRY_BACKOFF_SEC) * attempt
            if backoff > 0:
                time.sleep(backoff)

    if last_error is not None:
        raise last_error
    raise RuntimeError("invoke_with_retry failed without exception")


def invoke_with_model_fallback(
    *,
    state: AgentState,
    node_name: str,
    payload: Dict[str, Any],
    chain_builder: Any,
) -> tuple[Any, str]:
    preferred_model = resolve_model_name(state.get("selected_model"))
    candidate_models = _iter_candidate_models(preferred_model)
    errors: List[str] = []

    for model_name in candidate_models:
        llm = get_llm_for_state(state, override_model=model_name)
        chain = chain_builder(llm)
        invoke_config = _build_invoke_config(node_name=node_name, model_name=model_name, state=state)
        try:
            response = invoke_with_retry(chain, payload, invoke_config=invoke_config)
            return response, model_name
        except Exception as exc:
            errors.append(f"{model_name}: {exc}")

    raise RuntimeError("Model fallback exhausted: " + " | ".join(errors))


def invoke_structured_with_fallback(
    *,
    state: AgentState,
    node_name: str,
    system_prompt: str,
    human_prompt: str,
    payload: Dict[str, Any],
    schema_cls: Type[TModel],
) -> tuple[TModel, str]:
    preferred_model = resolve_model_name(state.get("selected_model"))
    candidate_models = _iter_candidate_models(preferred_model)
    schema_json = json.dumps(schema_cls.model_json_schema(), ensure_ascii=False)
    errors: List[str] = []

    for model_name in candidate_models:
        llm = get_llm_for_state(state, override_model=model_name)
        invoke_config = _build_invoke_config(node_name=node_name, model_name=model_name, state=state)

        primary_prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", human_prompt),
        ])
        primary_chain = primary_prompt | llm.with_structured_output(schema_cls)

        try:
            response = invoke_with_retry(primary_chain, payload, invoke_config=invoke_config)
            if isinstance(response, schema_cls):
                return response, model_name
            return schema_cls.model_validate(response), model_name
        except Exception as primary_error:
            fallback_prompt = ChatPromptTemplate.from_messages([
                (
                    "system",
                    (
                        f"{system_prompt}\n\n"
                        "반드시 JSON 객체 하나만 출력하세요. 설명/마크다운/코드블록을 절대 포함하지 마세요."
                    ),
                ),
                (
                    "human",
                    (
                        f"{human_prompt}\n\n"
                        "출력은 아래 JSON 스키마를 만족해야 합니다.\n{json_schema}"
                    ),
                ),
            ])
            fallback_chain = fallback_prompt | llm
            fallback_payload = dict(payload)
            fallback_payload["json_schema"] = schema_json
            try:
                raw_response = invoke_with_retry(
                    fallback_chain,
                    fallback_payload,
                    max_attempts=1,
                    invoke_config=invoke_config,
                )
                data = _parse_json_object(str(getattr(raw_response, "content", raw_response)))
                return schema_cls.model_validate(data), model_name
            except (json.JSONDecodeError, ValidationError, ValueError) as fallback_parse_error:
                errors.append(
                    f"{model_name}: primary={primary_error}; fallback_parse={fallback_parse_error}"
                )
            except Exception as fallback_error:
                errors.append(
                    f"{model_name}: primary={primary_error}; fallback={fallback_error}"
                )

    raise RuntimeError(
        "Structured output failed across candidate models: " + " | ".join(errors)
    )

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
    user_req = _truncate_text(state.get("user_requirement", ""), PROMPT_MAX_DOC_CHARS)
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", PM_SYSTEM_PROMPT),
        ("human", "사용자 요구사항: {user_req}")
    ])

    with get_openai_callback_safe() as cb:
        response, used_model = invoke_with_model_fallback(
            state=state,
            node_name="pm_node",
            payload={"user_req": user_req},
            chain_builder=lambda llm: prompt | llm,
        )
        
    current_tokens = state.get("total_tokens", 0) + cb.total_tokens
    current_cost = state.get("total_cost", 0.0) + cb.total_cost
        
    return {
        "pm_document": response.content,
        "selected_model": used_model,
        "total_tokens": current_tokens,
        "total_cost": current_cost,
        "current_active_agent": "pm_node",
        "messages": [AIMessage(content=f"[PM 기획서 산출물]\n{response.content}")]
    }

def architect_node(state: AgentState) -> AgentState:
    """Tech Stack & Directory Structure by Architect Agent"""
    print("--- ARCHITECT NODE ---")
    pm_doc = _truncate_text(state.get("pm_document", ""), PROMPT_MAX_DOC_CHARS)
    
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
    
    with get_openai_callback_safe() as cb:
        response, used_model = invoke_with_model_fallback(
            state=state,
            node_name="architect_node",
            payload={"pm_doc": pm_doc},
            chain_builder=lambda llm: prompt | llm,
        )
        
    return {
        "architecture_document": response.content,
        "selected_model": used_model,
        "total_tokens": state.get("total_tokens", 0) + cb.total_tokens,
        "total_cost": state.get("total_cost", 0.0) + cb.total_cost,
        "current_active_agent": "architect_node",
        "messages": [AIMessage(content=f"[아키텍트 설계도 산출물]\n{response.content}")]
    }

class CodeOutput(BaseModel):
    """A dictionary of file paths to their complete source code content."""
    files: Dict[str, str] = Field(
        description="Dictionary where keys are file paths (e.g., 'main.py', 'src/utils.py') and values are the full source code."
    )


def _normalize_generated_files(files: Dict[str, Any]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for file_path, content in files.items():
        path = str(file_path).strip()
        if not path:
            continue
        if isinstance(content, str):
            normalized[path] = content
        else:
            normalized[path] = json.dumps(content, ensure_ascii=False, indent=2)
    return normalized

def developer_node(state: AgentState) -> AgentState:
    """Code Generation by Developer Agent"""
    print("--- DEVELOPER NODE ---")
    
    pm_doc = _truncate_text(state.get("pm_document", ""), PROMPT_MAX_DOC_CHARS)
    arch_doc = _truncate_text(state.get("architecture_document", ""), PROMPT_MAX_DOC_CHARS)
    
    system_prompt = """당신은 최고 수준의 시니어 소프트웨어 엔지니어입니다.
    제공된 기획서와 아키텍처 설계도를 바탕으로 실제 작동하는 완벽한 소스 코드를 작성하세요.
    반드시 제공된 도구 인터페이스(CodeOutput) 구조에 맞게 응답하십시오.
    """
    human_prompt = "기획서:\n{pm_doc}\n\n아키텍처 설계도:\n{arch_doc}"
    
    with get_openai_callback_safe() as cb:
        try:
            response, used_model = invoke_structured_with_fallback(
                state=state,
                node_name="developer_node",
                system_prompt=system_prompt,
                human_prompt=human_prompt,
                payload={"pm_doc": pm_doc, "arch_doc": arch_doc},
                schema_cls=CodeOutput,
            )
            source_code_dict = _normalize_generated_files(response.files)
            if not source_code_dict:
                source_code_dict = {"error.log": "코드 생성 결과가 비어 있습니다."}
        except Exception as e:
            try:
                used_model = resolve_model_name(state.get("selected_model"))
            except Exception:
                used_model = DEFAULT_MODEL
            source_code_dict = {"error.log": f"코드 생성(Structured Output) 실패:\n{str(e)}"}
    
    return {
        "source_code": source_code_dict,
        "selected_model": used_model,
        "total_tokens": state.get("total_tokens", 0) + cb.total_tokens,
        "total_cost": state.get("total_cost", 0.0) + cb.total_cost,
        "current_active_agent": "tool_execution_node",
        "messages": [AIMessage(content=f"[개발자 코드 작성 완료] {len(source_code_dict)}개의 파일 생성됨.")]
    }

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
        try:
            is_outside_workspace = os.path.commonpath([workspace_dir, full_path]) != workspace_dir
        except ValueError:
            is_outside_workspace = True

        if is_outside_workspace:
            logs.append(f"[Security Error] Path traversal blocked for {file_path}")
            continue
        
        # 하위 디렉토리가 필요한 경우 생성
        parent_dir = os.path.dirname(full_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(code_content)

    # Docker SDK를 이용한 안전한 샌드박스 파이썬 실행
    try:
        import docker
        from docker.errors import DockerException, ContainerError, ImageNotFound
    except Exception as e:
        logs.append(f"[Docker Error] Docker SDK import failed: {str(e)}")
    else:
        try:
            client = docker.from_env()

            for file_path in source_code.keys():
                if file_path.endswith(".py"):
                    # 샌드박스 환경(Docker Container) 내에서 스크립트 문법 검사 실행
                    try:
                        # 마운트 및 실행 속도를 위해 python:3.11-slim 사용
                        client.containers.run(
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
                        logs.append("[Docker Error] 'python:3.11-slim' Image not found. Is it pulled?")

        except DockerException as e:
            logs.append(
                f"[Docker Error] Failed to connect to Docker daemon: {str(e)}\n\n"
                "(Fallback: Docker가 실행 중인지 확인하세요.)"
            )

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
    pm_doc = _truncate_text(state.get("pm_document", ""), PROMPT_MAX_DOC_CHARS)
    source_code = _stringify_source_code_for_prompt(state.get("source_code", {}))
    execution_logs = _truncate_text("\n".join(state.get("execution_logs", [])), PROMPT_MAX_LOG_CHARS)
    current_revision = state.get("revision_count", 0)

    system_prompt = """당신은 꼼꼼한 수석 QA 엔지니어이자 코드 리뷰어입니다.
    기획서, 작성된 소스 코드, 그리고 코드 실행 로그를 분석하여 버그, 보안 취약점, 혹은 기획 누락이 있는지 엄격하게 검토하세요.
    반드시 제공된 도구 인터페이스(QAReport) 구조에 맞게 응답하십시오.
    """

    human_prompt = "기획서:\n{pm_doc}\n\n소스코드:\n{source_code}\n\n실행로그:\n{execution_logs}"

    with get_openai_callback_safe() as cb:
        try:
            response, used_model = invoke_structured_with_fallback(
                state=state,
                node_name="qa_node",
                system_prompt=system_prompt,
                human_prompt=human_prompt,
                payload={
                    "pm_doc": pm_doc,
                    "source_code": source_code,
                    "execution_logs": execution_logs,
                },
                schema_cls=QAReport,
            )
            qa_report = response.review_markdown
            is_pass = response.is_pass
        except Exception as e:
            try:
                used_model = resolve_model_name(state.get("selected_model"))
            except Exception:
                used_model = DEFAULT_MODEL
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
        "selected_model": used_model,
        "revision_count": next_revision,
        "total_tokens": state.get("total_tokens", 0) + cb.total_tokens,
        "total_cost": state.get("total_cost", 0.0) + cb.total_cost,
        "current_active_agent": "END" if is_pass else "developer_node", 
        "messages": [AIMessage(content=f"[QA 검토 완료]\n{qa_report}")]
    }

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
        from github import Github
        from github import Auth

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
