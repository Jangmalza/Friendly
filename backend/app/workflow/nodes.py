import os
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from .state import WorkflowState

# You will need to set OPENAI_API_KEY environment variable
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7)

def pm_node(state: WorkflowState) -> WorkflowState:
    """Requirement Analysis by PM Agent"""
    print("--- PM NODE ---")
    sys_msg = SystemMessage(content="You are an expert Product Manager. Analyze the user requirements and output a clear, actionable Project Specification in Markdown format.")
    human_msg = HumanMessage(content=state["user_requirements"])
    
    response = llm.invoke([sys_msg, human_msg])
    return {"pm_spec": response.content}

def architect_node(state: WorkflowState) -> WorkflowState:
    """Tech Stack & Directory Structure by Architect Agent"""
    print("--- ARCHITECT NODE ---")
    sys_msg = SystemMessage(content="You are an expert Software Architect. Based on the PM specification, design the technical stack and directory structure. Output in Markdown format.")
    human_msg = HumanMessage(content=f"Here is the PM Specification:\n\n{state.get('pm_spec', '')}")
    
    response = llm.invoke([sys_msg, human_msg])
    return {"architecture_spec": response.content}

def developer_node(state: WorkflowState) -> WorkflowState:
    """Code Generation by Developer Agent"""
    print("--- DEVELOPER NODE ---")
    sys_msg = SystemMessage(content="You are an expert Software Developer. Based on the Architecture specification and PM requirements, write the actual source code. Output only the code and filepath for now.")
    human_msg = HumanMessage(content=f"Architecture:\n{state.get('architecture_spec', '')}\n\nPM Spec:\n{state.get('pm_spec', '')}")
    
    response = llm.invoke([sys_msg, human_msg])
    
    # Very basic placeholder logic for code mapping
    source_code = state.get("source_code", {})
    source_code["main.py"] = response.content
    
    return {"source_code": source_code}

def tool_node(state: WorkflowState) -> WorkflowState:
    """Saving Files & Sandbox Test"""
    print("--- TOOL NODE ---")
    # Simulate saving files and running a test
    import time
    time.sleep(2)
    logs = "Simulated Execution Log:\n"
    for filename, code in state.get("source_code", {}).items():
        logs += f"Successfully wrote {filename} to disk.\n"
    logs += "Running tests... All tests passed."
    
    return {"execution_logs": logs}

def qa_node(state: WorkflowState) -> WorkflowState:
    """Log Analysis & Feedback by QA Agent"""
    print("--- QA NODE ---")
    sys_msg = SystemMessage(content="You are an expert QA Engineer. Analyze the execution logs and the source code. Output whether the code is VALID or INVALID, and provide feedback.")
    human_msg = HumanMessage(content=f"Logs:\n{state.get('execution_logs', '')}")
    
    response = llm.invoke([sys_msg, human_msg])
    
    # Very basic logic to determine validation
    is_valid = "INVALID" not in response.content.upper()
    
    # We increment the error counter if it fails
    current_errors = state.get("error_counter", 0)
    if not is_valid:
        current_errors += 1
        
    return {
        "execution_logs": state.get("execution_logs", "") + f"\n\nQA Feedback:\n{response.content}",
        "error_counter": current_errors,
        "is_valid": is_valid # Set a key to be used by the conditional edge router
    }
