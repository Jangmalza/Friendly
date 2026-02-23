import argparse
import asyncio
import os
import socket
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from app.workflow.nodes import (
    architect_node,
    developer_backend_node,
    developer_frontend_node,
    developer_node,
    get_default_model,
    github_deploy_node,
    pm_node,
    qa_node,
    resolve_model_name,
    tool_execution_node,
)

from .broker import RedisStreamBroker
from .common import (
    MAX_RETRIES_DEFAULT,
    build_safe_event,
    merge_state,
    now_iso,
    role_chat_content,
    role_message_content,
    truncate_text,
)

ROLE_TO_NODE = {
    "pm": pm_node,
    "architect": architect_node,
    "developer_backend": developer_backend_node,
    "developer_frontend": developer_frontend_node,
    "developer": developer_node,
    "tool_execution": tool_execution_node,
    "qa": qa_node,
    "github_deploy": github_deploy_node,
}

ROLE_ACTIVE_NODE = {
    "pm": "pm_node",
    "architect": "architect_node",
    "developer_backend": "developer_backend_node",
    "developer_frontend": "developer_frontend_node",
    "developer": "developer_node",
    "tool_execution": "tool_execution_node",
    "qa": "qa_node",
    "github_deploy": "github_deploy_node",
}

LINEAR_ROUTE = {
    "pm": "architect",
    "developer": "tool_execution",
    "tool_execution": "qa",
}

PARALLEL_DEVELOPMENT_PHASE = "parallel_developer"
PARALLEL_DEVELOPER_ROLES: Tuple[str, ...] = ("developer_backend", "developer_frontend")


def _parse_int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_positive_int_env(key: str, default: int) -> int:
    parsed = _parse_int_env(key, default)
    if parsed <= 0:
        return default
    return parsed


def _parse_bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default

    normalized = str(raw).strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_role_set_env(key: str) -> Set[str]:
    raw = os.environ.get(key)
    if raw is None:
        return set()

    allowed_roles = set(ROLE_TO_NODE.keys())
    parsed: Set[str] = set()
    for token in str(raw).split(","):
        role = token.strip()
        if not role:
            continue
        if role in allowed_roles:
            parsed.add(role)
    return parsed


def _build_consumer_name(role: str) -> str:
    override = os.environ.get("NETWORK_TASK_CONSUMER_NAME")
    if override and override.strip():
        return override.strip()

    host = socket.gethostname() or "worker"
    pid = os.getpid()
    suffix = uuid4().hex[:6]
    return f"{role}-{host}-{pid}-{suffix}"


class DistributedWorker:
    def __init__(self, role: str, broker: RedisStreamBroker):
        self.role = role
        self.broker = broker

        group_name = (os.environ.get("NETWORK_TASK_GROUP") or "network-workers").strip()
        self.task_group_name = group_name or "network-workers"
        self.consumer_name = _build_consumer_name(role)

        self.task_max_attempts = _parse_positive_int_env("NETWORK_TASK_MAX_ATTEMPTS", MAX_RETRIES_DEFAULT)
        self.read_block_ms = _parse_positive_int_env("NETWORK_TASK_READ_BLOCK_MS", 2000)
        self.read_count = _parse_positive_int_env("NETWORK_TASK_READ_COUNT", 10)
        self.claim_min_idle_ms = _parse_positive_int_env("NETWORK_TASK_CLAIM_MIN_IDLE_MS", 30000)
        self.claim_interval_ms = _parse_positive_int_env("NETWORK_TASK_CLAIM_INTERVAL_MS", 10000)

        self.qa_max_retries = _parse_int_env("NETWORK_QA_MAX_RETRIES", MAX_RETRIES_DEFAULT)
        self.node_timeout_sec = _parse_int_env("NETWORK_NODE_TIMEOUT_SEC", 420)
        self.enable_parallel_developers = _parse_bool_env("NETWORK_ENABLE_PARALLEL_DEVELOPERS", True)
        self.approval_gate_roles = _parse_role_set_env("NETWORK_APPROVAL_GATES")

    async def run_forever(self) -> None:
        await self.broker.ensure_task_group(self.role, self.task_group_name, start_id="0-0")
        print(
            (
                f"[worker:{self.role}] started. group={self.task_group_name}, "
                f"consumer={self.consumer_name}. waiting for tasks..."
            ),
            flush=True,
        )

        last_claim_ms = 0.0
        while True:
            fresh_tasks = await self.broker.read_group_tasks(
                self.role,
                self.task_group_name,
                self.consumer_name,
                block_ms=self.read_block_ms,
                count=self.read_count,
            )
            if fresh_tasks:
                await self._process_entries(fresh_tasks, source="new")
                continue

            pending_tasks = await self.broker.read_group_pending_tasks(
                self.role,
                self.task_group_name,
                self.consumer_name,
                count=self.read_count,
            )
            if pending_tasks:
                await self._process_entries(pending_tasks, source="pending")
                continue

            now_ms = time.monotonic() * 1000
            if now_ms - last_claim_ms < self.claim_interval_ms:
                continue
            last_claim_ms = now_ms

            claimed_tasks = await self.broker.claim_stale_group_tasks(
                self.role,
                self.task_group_name,
                self.consumer_name,
                min_idle_ms=self.claim_min_idle_ms,
                count=self.read_count,
            )
            if claimed_tasks:
                await self._process_entries(claimed_tasks, source="claimed")

    async def _process_entries(
        self,
        tasks: List[Tuple[str, Dict[str, Any]]],
        *,
        source: str,
    ) -> None:
        for entry_id, task in tasks:
            await self._process_entry(entry_id, task, source=source)

    async def _process_entry(
        self,
        entry_id: str,
        task: Dict[str, Any],
        *,
        source: str,
    ) -> None:
        attempt = self._task_attempt(task)

        try:
            await self._handle_task(task)
        except Exception as exc:
            await self._handle_task_failure(entry_id, task, attempt=attempt, source=source, error=exc)
            return

        await self.broker.ack_task(self.role, self.task_group_name, entry_id)

    @staticmethod
    def _task_attempt(task: Dict[str, Any]) -> int:
        raw = task.get("_attempt", 1)
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return 1
        return max(1, parsed)

    @staticmethod
    def _error_message(exc: Exception, limit: int = 500) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        if len(text) <= limit:
            return text
        return f"{text[:limit]}...(truncated)"

    @staticmethod
    def _clear_approval_context(state: Dict[str, Any]) -> None:
        state["approval_stage"] = None
        state["approval_requested_at"] = None
        state["approval_pending_next_roles"] = []

    def _requires_approval_gate(
        self,
        *,
        next_roles: List[str],
        terminal_status: Optional[str],
    ) -> bool:
        if terminal_status is not None:
            return False
        if not next_roles:
            return False
        return self.role in self.approval_gate_roles

    async def _mark_waiting_for_approval(
        self,
        run_id: str,
        state: Dict[str, Any],
        *,
        next_roles: List[str],
    ) -> None:
        normalized_roles = [role for role in next_roles if role in ROLE_TO_NODE]
        if not normalized_roles:
            raise RuntimeError("Approval gate is configured, but no valid next roles were found.")

        self._clear_approval_context(state)
        state["status"] = "waiting_approval"
        state["next_role"] = None
        state["approval_stage"] = self.role
        state["approval_requested_at"] = now_iso()
        state["approval_pending_next_roles"] = normalized_roles
        state["updated_at"] = now_iso()
        await self.broker.save_state(run_id, state)

        next_roles_text = ", ".join(normalized_roles)
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="waiting_approval",
                from_role=self.role,
                to_role="director",
                event_type="workflow_status",
                note=(
                    f"[{self.role}] director approval required before routing to: {next_roles_text}. "
                    "Use /api/network/runs/{run_id}/approval to approve or reject."
                ),
            ),
        )
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="waiting_approval",
                from_role=self.role,
                to_role="director",
                event_type="agent_chat",
                note=(
                    f"@director {self.role} 단계 결과를 검토해주세요. "
                    f"승인 시 다음 단계: {next_roles_text}"
                ),
            ),
        )

    async def _handle_task_failure(
        self,
        entry_id: str,
        task: Dict[str, Any],
        *,
        attempt: int,
        source: str,
        error: Exception,
    ) -> None:
        error_message = self._error_message(error)
        run_id = str(task.get("run_id", "")).strip()

        if attempt < self.task_max_attempts:
            next_attempt = attempt + 1
            retry_payload = dict(task)
            retry_payload["_attempt"] = next_attempt
            retry_payload["from_role"] = self.role
            retry_payload["queued_at"] = now_iso()
            retry_payload["last_error"] = error_message
            retry_payload["last_failed_at"] = now_iso()
            await self.broker.send_task(self.role, retry_payload)

            if run_id:
                await self._mark_retry(run_id, failed_attempt=attempt, next_attempt=next_attempt, error_message=error_message)

            await self.broker.ack_task(self.role, self.task_group_name, entry_id)
            print(
                (
                    f"[worker:{self.role}] retry scheduled. run_id={run_id or '-'} "
                    f"source={source} attempt={attempt}/{self.task_max_attempts}"
                ),
                flush=True,
            )
            return

        dlq_payload = {
            "run_id": run_id or None,
            "role": self.role,
            "entry_id": entry_id,
            "consumer": self.consumer_name,
            "attempt": attempt,
            "max_attempts": self.task_max_attempts,
            "error": error_message,
            "failed_at": now_iso(),
            "task": task,
        }
        await self.broker.append_dlq(self.role, dlq_payload)

        if run_id:
            await self._mark_terminal_failure(run_id, attempt=attempt, error_message=error_message)

        await self.broker.ack_task(self.role, self.task_group_name, entry_id)
        print(
            (
                f"[worker:{self.role}] moved task to DLQ. run_id={run_id or '-'} "
                f"source={source} attempts={attempt}/{self.task_max_attempts}"
            ),
            flush=True,
        )

    async def _mark_retry(
        self,
        run_id: str,
        *,
        failed_attempt: int,
        next_attempt: int,
        error_message: str,
    ) -> None:
        state = await self.broker.load_state(run_id)
        if state is None:
            return

        if state.get("status") in {"completed", "failed", "waiting_approval"}:
            return

        self._clear_approval_context(state)
        state["status"] = "queued"
        state["next_role"] = self.role
        state["updated_at"] = now_iso()
        await self.broker.save_state(run_id, state)

        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="queued",
                from_role=self.role,
                to_role=self.role,
                event_type="workflow_status",
                note=(
                    f"[{self.role}] attempt {failed_attempt}/{self.task_max_attempts} failed; "
                    f"requeued as attempt {next_attempt}/{self.task_max_attempts}. "
                    f"reason: {error_message}"
                ),
            ),
        )
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="queued",
                from_role=self.role,
                to_role=self.role,
                event_type="agent_chat",
                note=(
                    f"@{self.role} 처리 중 오류로 재시도합니다 "
                    f"({next_attempt}/{self.task_max_attempts}). "
                    f"원인: {truncate_text(error_message, limit=220)}"
                ),
            ),
        )

    async def _mark_terminal_failure(
        self,
        run_id: str,
        *,
        attempt: int,
        error_message: str,
    ) -> None:
        state = await self.broker.load_state(run_id)
        if state is None:
            return

        if state.get("status") in {"completed", "failed", "waiting_approval"}:
            return

        self._clear_approval_context(state)
        state["status"] = "failed"
        state["next_role"] = None
        state["updated_at"] = now_iso()
        await self.broker.save_state(run_id, state)

        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="failed",
                from_role=self.role,
                event_type="workflow_status",
                note=(
                    f"[{self.role}] failed after {attempt}/{self.task_max_attempts} attempts; "
                    f"sent to DLQ. reason: {error_message}"
                ),
            ),
        )
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="failed",
                from_role=self.role,
                to_role="supervisor",
                event_type="agent_chat",
                note=(
                    f"@supervisor {self.role} 단계가 실패하여 DLQ로 이동했습니다. "
                    f"원인: {truncate_text(error_message, limit=220)}"
                ),
            ),
        )

    async def _handle_task(self, task: Dict[str, Any]) -> None:
        run_id = str(task.get("run_id", "")).strip()
        if not run_id:
            return

        state = await self.broker.load_state(run_id)
        if state is None:
            return

        if state.get("status") in {"completed", "failed", "waiting_approval"}:
            return

        expected_role = state.get("next_role")
        if expected_role:
            normalized_expected = str(expected_role).strip()
            if normalized_expected == "parallel_wait":
                if self.role not in PARALLEL_DEVELOPER_ROLES:
                    return
            elif normalized_expected != self.role:
                return

        try:
            state["selected_model"] = (
                resolve_model_name(state.get("selected_model"))
                if state.get("selected_model")
                else get_default_model()
            )
        except ValueError:
            state["selected_model"] = get_default_model()

        state["status"] = "running"
        state["current_active_agent"] = ROLE_ACTIVE_NODE.get(self.role, state.get("current_active_agent"))
        state["updated_at"] = now_iso()
        await self.broker.save_state(run_id, state)
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="running",
                from_role=self.role,
                to_role=self.role,
                event_type="workflow_status",
                note=f"[{self.role}] processing started.",
            ),
        )
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                state,
                status="running",
                from_role=self.role,
                to_role=self.role,
                event_type="agent_chat",
                note=f"@{self.role} 작업을 시작합니다. 입력 상태를 분석 중입니다.",
            ),
        )

        node_fn = ROLE_TO_NODE[self.role]

        try:
            update = await asyncio.wait_for(asyncio.to_thread(node_fn, state), timeout=self.node_timeout_sec)
            merged = merge_state(state, update)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"[{self.role}] timed out after {self.node_timeout_sec} seconds.") from exc
        except Exception as exc:
            raise RuntimeError(f"[{self.role}] failed: {exc}") from exc

        self._clear_approval_context(merged)
        next_roles, terminal_status, terminal_note = self._route_after_node(merged)

        if self._requires_approval_gate(next_roles=next_roles, terminal_status=terminal_status):
            await self._mark_waiting_for_approval(run_id, merged, next_roles=next_roles)
            return

        if len(next_roles) > 1:
            await self._start_parallel_developer_phase(run_id, merged, next_roles)
            return

        if self.role in PARALLEL_DEVELOPER_ROLES:
            await self._finalize_parallel_developer_branch(run_id, merged)
            return

        next_role = next_roles[0] if next_roles else None
        merged["next_role"] = next_role
        merged["updated_at"] = now_iso()
        merged["status"] = terminal_status or "running"
        await self.broker.save_state(run_id, merged)

        note = role_message_content(self.role, merged)
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                merged,
                status=merged.get("status", "running"),
                from_role=self.role,
                to_role=next_role,
                event_type="agent_message",
                note=note,
            ),
        )

        chat_note = role_chat_content(self.role, merged, next_role)
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                merged,
                status=merged.get("status", "running"),
                from_role=self.role,
                to_role=next_role,
                event_type="agent_chat",
                note=chat_note,
            ),
        )

        if next_role:
            await self.broker.send_task(
                next_role,
                {
                    "run_id": run_id,
                    "from_role": self.role,
                    "queued_at": now_iso(),
                    "_attempt": 1,
                },
            )
            return

        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                merged,
                status=merged.get("status", "completed"),
                from_role=self.role,
                event_type="workflow_status",
                note=terminal_note or "",
            ),
        )

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_role_list(raw_roles: Any) -> List[str]:
        if not isinstance(raw_roles, list):
            return []
        seen = set()
        normalized: List[str] = []
        for item in raw_roles:
            role_name = str(item).strip()
            if not role_name or role_name in seen:
                continue
            seen.add(role_name)
            normalized.append(role_name)
        return normalized

    @staticmethod
    def _merge_parallel_source_code_candidates(
        candidates: Dict[str, Dict[str, Any]],
        expected_roles: List[str],
    ) -> Dict[str, str]:
        merged_source: Dict[str, str] = {}
        conflict_paths: List[str] = []

        for role in expected_roles:
            payload = candidates.get(role) or {}
            source_code = payload.get("source_code")
            if not isinstance(source_code, dict):
                continue

            for raw_path, raw_content in source_code.items():
                path = str(raw_path).strip()
                if not path:
                    continue
                content = raw_content if isinstance(raw_content, str) else str(raw_content)

                if path not in merged_source:
                    merged_source[path] = content
                    continue

                if merged_source[path] == content:
                    continue

                # Prefer richer candidate content when the same path conflicts.
                if len(content) > len(merged_source[path]):
                    merged_source[path] = content
                conflict_paths.append(path)

        if conflict_paths:
            unique_conflicts = sorted(set(conflict_paths))
            merged_source["parallel_merge_conflicts.log"] = (
                "Conflicting files resolved by longest-content preference:\n"
                + "\n".join(unique_conflicts)
            )

        if not merged_source:
            merged_source["error.log"] = "병렬 개발 결과가 비어 있습니다."

        return merged_source

    async def _start_parallel_developer_phase(
        self,
        run_id: str,
        merged: Dict[str, Any],
        next_roles: List[str],
    ) -> None:
        phase = PARALLEL_DEVELOPMENT_PHASE
        normalized_roles = [role for role in next_roles if role in ROLE_TO_NODE]
        if not normalized_roles:
            raise RuntimeError("Parallel developer fan-out requested without valid roles.")

        self._clear_approval_context(merged)
        merged["parallel_phase"] = phase
        merged["parallel_expected_roles"] = normalized_roles
        merged["parallel_completed_roles"] = []
        merged["next_role"] = "parallel_wait"
        merged["updated_at"] = now_iso()
        merged["status"] = "running"
        await self.broker.save_state(run_id, merged)
        await self.broker.reset_parallel_phase(run_id, phase, roles=normalized_roles, clear_candidates=True)

        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                merged,
                status="running",
                from_role=self.role,
                to_role="parallel_wait",
                event_type="workflow_status",
                note=f"[{self.role}] parallel fan-out started: {', '.join(normalized_roles)}",
            ),
        )
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                merged,
                status="running",
                from_role=self.role,
                to_role="parallel_wait",
                event_type="agent_chat",
                note=(
                    f"@parallel-team {self.role} 단계에서 병렬 구현을 시작합니다. "
                    f"할당 역할: {', '.join(normalized_roles)}"
                ),
            ),
        )

        for role_name in normalized_roles:
            await self.broker.send_task(
                role_name,
                {
                    "run_id": run_id,
                    "from_role": self.role,
                    "queued_at": now_iso(),
                    "_attempt": 1,
                },
            )

    async def _finalize_parallel_developer_branch(
        self,
        run_id: str,
        merged: Dict[str, Any],
    ) -> None:
        phase = str(merged.get("parallel_phase") or PARALLEL_DEVELOPMENT_PHASE)
        expected_roles = self._normalize_role_list(merged.get("parallel_expected_roles"))
        if not expected_roles:
            expected_roles = list(PARALLEL_DEVELOPER_ROLES)
        if self.role not in expected_roles:
            expected_roles.append(self.role)

        branch_source = merged.get("branch_source_code")
        if not isinstance(branch_source, dict):
            branch_source = {}

        await self.broker.save_parallel_candidate(
            run_id,
            self.role,
            {
                "role": self.role,
                "source_code": branch_source,
                "selected_model": merged.get("selected_model"),
                "updated_at": now_iso(),
            },
        )
        is_new_completion, done_count = await self.broker.mark_parallel_branch_done(run_id, phase, self.role)
        done_roles = await self.broker.get_parallel_done_roles(run_id, phase)

        latest_state = await self.broker.load_state(run_id)
        if latest_state is None or latest_state.get("status") in {"completed", "failed"}:
            return

        self._clear_approval_context(latest_state)
        latest_state["status"] = "running"
        latest_state["next_role"] = "parallel_wait"
        latest_state["parallel_phase"] = phase
        latest_state["parallel_expected_roles"] = expected_roles
        latest_state["parallel_completed_roles"] = done_roles
        latest_state["current_active_agent"] = ROLE_ACTIVE_NODE.get(self.role, latest_state.get("current_active_agent"))
        latest_state["selected_model"] = merged.get("selected_model", latest_state.get("selected_model"))
        if is_new_completion:
            latest_state["total_tokens"] = self._as_int(latest_state.get("total_tokens")) + self._as_int(
                merged.get("token_usage_delta")
            )
            latest_state["total_cost"] = self._as_float(latest_state.get("total_cost")) + self._as_float(
                merged.get("cost_usage_delta")
            )
        latest_state["updated_at"] = now_iso()
        await self.broker.save_state(run_id, latest_state)

        branch_state_for_event = dict(latest_state)
        branch_state_for_event["branch_source_code"] = branch_source

        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                branch_state_for_event,
                status="running",
                from_role=self.role,
                to_role="parallel_supervisor",
                event_type="agent_message",
                note=(
                    f"{role_message_content(self.role, branch_state_for_event)} "
                    f"(parallel {len(done_roles)}/{len(expected_roles)})"
                ).strip(),
            ),
        )
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                branch_state_for_event,
                status="running",
                from_role=self.role,
                to_role="parallel_supervisor",
                event_type="agent_chat",
                note=role_chat_content(self.role, branch_state_for_event, "parallel_supervisor"),
            ),
        )

        if done_count < len(expected_roles):
            return

        acquired = await self.broker.try_acquire_parallel_merge(run_id, phase)
        if not acquired:
            return

        candidates = await self.broker.load_parallel_candidates(run_id, expected_roles)
        merged_source = self._merge_parallel_source_code_candidates(candidates, expected_roles)

        final_state = await self.broker.load_state(run_id)
        if final_state is None or final_state.get("status") in {"completed", "failed"}:
            return

        self._clear_approval_context(final_state)
        final_state["source_code"] = merged_source
        final_state["next_role"] = "tool_execution"
        final_state["parallel_phase"] = None
        final_state["parallel_expected_roles"] = []
        final_state["parallel_completed_roles"] = expected_roles
        final_state["updated_at"] = now_iso()
        final_state["status"] = "running"
        await self.broker.save_state(run_id, final_state)

        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                final_state,
                status="running",
                from_role="parallel_supervisor",
                to_role="tool_execution",
                event_type="workflow_status",
                note=(
                    "[parallel] all developer branches completed; "
                    f"merged files={len(merged_source)} and routed to tool_execution."
                ),
            ),
        )
        await self.broker.append_event(
            run_id,
            build_safe_event(
                run_id,
                final_state,
                status="running",
                from_role="parallel_supervisor",
                to_role="tool_execution",
                event_type="agent_chat",
                note=(
                    "@tool_execution 병렬 개발 산출물을 병합했습니다. "
                    "샌드박스 검증을 시작해주세요."
                ),
            ),
        )

        await self.broker.send_task(
            "tool_execution",
            {
                "run_id": run_id,
                "from_role": "parallel_supervisor",
                "queued_at": now_iso(),
                "_attempt": 1,
            },
        )
        await self.broker.reset_parallel_phase(run_id, phase, roles=expected_roles, clear_candidates=True)

    def _route_after_node(self, state: Dict[str, Any]) -> Tuple[List[str], Optional[str], Optional[str]]:
        if self.role == "architect":
            if self.enable_parallel_developers:
                return list(PARALLEL_DEVELOPER_ROLES), None, None
            return ["developer"], None, None

        if self.role in LINEAR_ROUTE:
            return [LINEAR_ROUTE[self.role]], None, None

        if self.role in PARALLEL_DEVELOPER_ROLES:
            return [], None, None

        if self.role == "qa":
            qa_report = str(state.get("qa_report", ""))
            revision_count = int(state.get("revision_count", 0))

            if "STATUS: PASS" in qa_report:
                return ["github_deploy"], None, None

            if "STATUS: FAIL" in qa_report:
                if revision_count >= self.qa_max_retries:
                    return [], "failed", f"QA failed after {revision_count} revisions."
                if self.enable_parallel_developers:
                    return list(PARALLEL_DEVELOPER_ROLES), None, None
                return ["developer"], None, None

            return [], "failed", "QA report status is ambiguous."

        if self.role == "github_deploy":
            return [], "completed", "Distributed workflow completed."

        return [], "failed", f"No route configured for role '{self.role}'."


async def _async_main(role: str) -> None:
    broker = RedisStreamBroker()
    await broker.ping()

    worker = DistributedWorker(role=role, broker=broker)
    try:
        await worker.run_forever()
    finally:
        await broker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Distributed worker for AI orchestrator.")
    parser.add_argument(
        "role",
        choices=sorted(ROLE_TO_NODE.keys()),
        help="Role handled by this worker process.",
    )
    args = parser.parse_args()

    asyncio.run(_async_main(args.role))


if __name__ == "__main__":
    main()
