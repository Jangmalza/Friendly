import argparse
import asyncio
import os
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from app.workflow.nodes import (
    architect_node,
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
    "developer": developer_node,
    "tool_execution": tool_execution_node,
    "qa": qa_node,
    "github_deploy": github_deploy_node,
}

ROLE_ACTIVE_NODE = {
    "pm": "pm_node",
    "architect": "architect_node",
    "developer": "developer_node",
    "tool_execution": "tool_execution_node",
    "qa": "qa_node",
    "github_deploy": "github_deploy_node",
}

LINEAR_ROUTE = {
    "pm": "architect",
    "architect": "developer",
    "developer": "tool_execution",
    "tool_execution": "qa",
}


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

        if state.get("status") in {"completed", "failed"}:
            return

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

        if state.get("status") in {"completed", "failed"}:
            return

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

        if state.get("status") in {"completed", "failed"}:
            return

        expected_role = state.get("next_role")
        if expected_role and expected_role != self.role:
            return

        try:
            state["selected_model"] = resolve_model_name(state.get("selected_model")) if state.get("selected_model") else get_default_model()
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

        next_role, terminal_status, terminal_note = self._route_after_node(merged)

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

    def _route_after_node(self, state: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        if self.role in LINEAR_ROUTE:
            return LINEAR_ROUTE[self.role], None, None

        if self.role == "qa":
            qa_report = str(state.get("qa_report", ""))
            revision_count = int(state.get("revision_count", 0))

            if "STATUS: PASS" in qa_report:
                return "github_deploy", None, None

            if "STATUS: FAIL" in qa_report:
                if revision_count >= self.qa_max_retries:
                    return None, "failed", f"QA failed after {revision_count} revisions."
                return "developer", None, None

            return None, "failed", "QA report status is ambiguous."

        if self.role == "github_deploy":
            return None, "completed", "Distributed workflow completed."

        return None, "failed", f"No route configured for role '{self.role}'."


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
