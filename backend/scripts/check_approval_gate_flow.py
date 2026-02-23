#!/usr/bin/env python3
import asyncio
import copy
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Must be set before worker/app imports.
os.environ.setdefault("NETWORK_MOCK_MODE", "true")
os.environ.setdefault("NETWORK_ENABLE_PARALLEL_DEVELOPERS", "true")
os.environ.setdefault("NETWORK_MOCK_QA_FAIL", "false")
os.environ.setdefault("NETWORK_APPROVAL_GATES", "architect")

from app.distributed.common import initial_network_state  # noqa: E402
from app.distributed.worker import DistributedWorker  # noqa: E402
import app.main as network_api  # noqa: E402


class InMemoryBroker:
    def __init__(self, initial_states: Optional[Dict[str, Dict[str, Any]]] = None):
        self.states: Dict[str, Dict[str, Any]] = {
            run_id: copy.deepcopy(state) for run_id, state in (initial_states or {}).items()
        }
        self.events: Dict[str, List[Dict[str, Any]]] = {}
        self.sent_tasks: List[Tuple[str, Dict[str, Any]]] = []

        self.parallel_candidates: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.parallel_done_sets: Dict[Tuple[str, str], set[str]] = {}
        self.parallel_merge_gates: set[Tuple[str, str]] = set()
        self.approval_locks: set[str] = set()
        self._id_seq = 0

    def _next_id(self) -> str:
        self._id_seq += 1
        return str(self._id_seq)

    async def save_state(self, run_id: str, state: Dict[str, Any]) -> None:
        self.states[run_id] = copy.deepcopy(state)

    async def load_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        value = self.states.get(run_id)
        if value is None:
            return None
        return copy.deepcopy(value)

    async def append_event(self, run_id: str, payload: Dict[str, Any]) -> str:
        self.events.setdefault(run_id, []).append(copy.deepcopy(payload))
        return self._next_id()

    async def send_task(self, role: str, payload: Dict[str, Any]) -> str:
        self.sent_tasks.append((role, copy.deepcopy(payload)))
        return self._next_id()

    async def save_parallel_candidate(self, run_id: str, role: str, payload: Dict[str, Any]) -> None:
        self.parallel_candidates.setdefault(run_id, {})[role] = copy.deepcopy(payload)

    async def load_parallel_candidate(self, run_id: str, role: str) -> Optional[Dict[str, Any]]:
        item = self.parallel_candidates.get(run_id, {}).get(role)
        if item is None:
            return None
        return copy.deepcopy(item)

    async def load_parallel_candidates(self, run_id: str, roles: List[str]) -> Dict[str, Dict[str, Any]]:
        run_candidates = self.parallel_candidates.get(run_id, {})
        items: Dict[str, Dict[str, Any]] = {}
        for role in roles:
            candidate = run_candidates.get(role)
            if candidate is not None:
                items[role] = copy.deepcopy(candidate)
        return items

    async def mark_parallel_branch_done(self, run_id: str, phase: str, role: str) -> Tuple[bool, int]:
        key = (run_id, phase)
        done_set = self.parallel_done_sets.setdefault(key, set())
        before = len(done_set)
        done_set.add(role)
        after = len(done_set)
        return after > before, after

    async def get_parallel_done_roles(self, run_id: str, phase: str) -> List[str]:
        return sorted(self.parallel_done_sets.get((run_id, phase), set()))

    async def try_acquire_parallel_merge(self, run_id: str, phase: str, *, ttl_sec: int = 120) -> bool:
        _ = ttl_sec
        key = (run_id, phase)
        if key in self.parallel_merge_gates:
            return False
        self.parallel_merge_gates.add(key)
        return True

    async def reset_parallel_phase(
        self,
        run_id: str,
        phase: str,
        *,
        roles: Optional[List[str]] = None,
        clear_candidates: bool = False,
    ) -> int:
        deleted = 0
        key = (run_id, phase)
        if key in self.parallel_done_sets:
            del self.parallel_done_sets[key]
            deleted += 1
        if key in self.parallel_merge_gates:
            self.parallel_merge_gates.remove(key)
            deleted += 1

        if clear_candidates and roles:
            run_candidates = self.parallel_candidates.get(run_id, {})
            for role in roles:
                if role in run_candidates:
                    del run_candidates[role]
                    deleted += 1

        return deleted

    async def try_acquire_approval_decision(self, run_id: str, *, ttl_sec: int = 30) -> bool:
        _ = ttl_sec
        if run_id in self.approval_locks:
            return False
        self.approval_locks.add(run_id)
        return True

    async def release_approval_decision(self, run_id: str) -> int:
        if run_id in self.approval_locks:
            self.approval_locks.remove(run_id)
            return 1
        return 0


def _build_workers(broker: InMemoryBroker) -> Dict[str, DistributedWorker]:
    worker_roles = [
        "pm",
        "architect",
        "developer_backend",
        "developer_frontend",
        "developer",
        "tool_execution",
        "qa",
        "github_deploy",
    ]
    return {role: DistributedWorker(role=role, broker=broker) for role in worker_roles}


async def _run_until_waiting_approval(
    run_id: str,
    *,
    broker: InMemoryBroker,
    workers: Dict[str, DistributedWorker],
) -> Dict[str, Any]:
    await broker.send_task(
        "pm",
        {
            "run_id": run_id,
            "from_role": "ci",
            "_attempt": 1,
        },
    )

    max_steps = 32
    for _ in range(max_steps):
        state = await broker.load_state(run_id)
        if state is not None and state.get("status") == "waiting_approval":
            return state

        if not broker.sent_tasks:
            break

        role, task = broker.sent_tasks.pop(0)
        worker = workers.get(role)
        if worker is None:
            raise RuntimeError(f"No worker configured for role '{role}'")
        await worker._handle_task(task)

    raise RuntimeError("Run did not reach waiting_approval state")


async def _scenario_approve() -> None:
    run_id = "approval-gate-approve"
    state = initial_network_state(run_id=run_id, prompt="approval approve case", selected_model="gpt-4o-mini")
    broker = InMemoryBroker({run_id: state})
    workers = _build_workers(broker)

    waiting_state = await _run_until_waiting_approval(run_id, broker=broker, workers=workers)
    if waiting_state.get("approval_stage") != "architect":
        raise RuntimeError("approval_stage mismatch for approve scenario")

    network_api.network_broker = broker
    request = network_api.ApprovalDecisionRequest(action="approve", actor="ci", note="approved")
    response = await network_api.decide_network_run_approval(run_id, request)

    final_state = await broker.load_state(run_id)
    if final_state is None:
        raise RuntimeError("Run state missing after approve decision")

    if response.get("status") != "running":
        raise RuntimeError(f"Expected running status after approve, got: {response}")
    if final_state.get("status") != "running" or final_state.get("next_role") != "parallel_wait":
        raise RuntimeError(f"Approve did not route to parallel_wait: {final_state}")

    queued_roles = [role for role, _ in broker.sent_tasks]
    if queued_roles.count("developer_backend") != 1 or queued_roles.count("developer_frontend") != 1:
        raise RuntimeError(f"Approve did not queue both parallel developers exactly once: {queued_roles}")

    print("[PASS] approval approve scenario")


async def _scenario_reject_constraints() -> None:
    run_id = "approval-gate-reject"
    state = initial_network_state(run_id=run_id, prompt="approval reject case", selected_model="gpt-4o-mini")
    broker = InMemoryBroker({run_id: state})
    workers = _build_workers(broker)

    await _run_until_waiting_approval(run_id, broker=broker, workers=workers)
    network_api.network_broker = broker

    invalid_request = network_api.ApprovalDecisionRequest(
        action="reject",
        actor="ci",
        note="invalid route",
        reject_to_role="github_deploy",
    )
    try:
        await network_api.decide_network_run_approval(run_id, invalid_request)
    except HTTPException as exc:
        if exc.status_code != 400:
            raise RuntimeError(f"Expected 400 for invalid reject role, got {exc.status_code}")
    else:
        raise RuntimeError("Invalid reject_to_role was accepted unexpectedly")

    mid_state = await broker.load_state(run_id)
    if mid_state is None or mid_state.get("status") != "waiting_approval":
        raise RuntimeError("State should remain waiting_approval after invalid reject")

    valid_request = network_api.ApprovalDecisionRequest(action="reject", actor="ci", note="needs revision")
    response = await network_api.decide_network_run_approval(run_id, valid_request)

    final_state = await broker.load_state(run_id)
    if final_state is None:
        raise RuntimeError("Run state missing after valid reject")

    if response.get("status") != "queued" or response.get("next_role") != "architect":
        raise RuntimeError(f"Reject response mismatch: {response}")
    if final_state.get("status") != "queued" or final_state.get("next_role") != "architect":
        raise RuntimeError(f"Reject state mismatch: {final_state}")

    queued_roles = [role for role, _ in broker.sent_tasks]
    if queued_roles.count("architect") != 1:
        raise RuntimeError(f"Reject should queue architect exactly once: {queued_roles}")

    print("[PASS] approval reject scenario")


async def _scenario_waiting_bypass_block() -> None:
    run_id = "approval-gate-bypass"
    state = initial_network_state(run_id=run_id, prompt="approval bypass case", selected_model="gpt-4o-mini")
    broker = InMemoryBroker({run_id: state})
    workers = _build_workers(broker)

    await _run_until_waiting_approval(run_id, broker=broker, workers=workers)
    event_count_before = len(broker.events.get(run_id, []))
    queue_count_before = len(broker.sent_tasks)

    # Simulate stale/stray developer task that should be ignored while waiting approval.
    await workers["developer"]._handle_task({"run_id": run_id, "from_role": "ci", "_attempt": 1})

    final_state = await broker.load_state(run_id)
    if final_state is None:
        raise RuntimeError("Run state missing in bypass scenario")
    if final_state.get("status") != "waiting_approval":
        raise RuntimeError(f"waiting_approval bypass detected: {final_state}")
    if final_state.get("next_role") is not None:
        raise RuntimeError(f"next_role changed unexpectedly during waiting_approval: {final_state}")

    event_count_after = len(broker.events.get(run_id, []))
    queue_count_after = len(broker.sent_tasks)
    if event_count_after != event_count_before or queue_count_after != queue_count_before:
        raise RuntimeError(
            "Stray task should not emit events or enqueue tasks while waiting_approval"
        )

    print("[PASS] waiting_approval bypass block scenario")


async def _run_all() -> None:
    await _scenario_approve()
    await _scenario_reject_constraints()
    await _scenario_waiting_bypass_block()


def main() -> None:
    asyncio.run(_run_all())
    print("[PASS] approval gate validation succeeded")


if __name__ == "__main__":
    main()
