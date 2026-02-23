#!/usr/bin/env python3
import asyncio
import copy
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Must be set before worker initialization.
os.environ.setdefault("NETWORK_MOCK_MODE", "true")
os.environ.setdefault("NETWORK_ENABLE_PARALLEL_DEVELOPERS", "true")
os.environ.setdefault("NETWORK_MOCK_QA_FAIL", "false")

from app.distributed.common import initial_network_state  # noqa: E402
from app.distributed.worker import DistributedWorker  # noqa: E402


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


async def _run_parallel_flow_check() -> Dict[str, Any]:
    run_id = "ci-mock-parallel-run"
    init_state = initial_network_state(
        run_id=run_id,
        prompt="CI mock parallel validation",
        selected_model="gpt-4o-mini",
    )
    broker = InMemoryBroker({run_id: init_state})

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
    workers = {
        role: DistributedWorker(role=role, broker=broker) for role in worker_roles
    }

    await broker.send_task(
        "pm",
        {
            "run_id": run_id,
            "from_role": "ci",
            "_attempt": 1,
        },
    )

    max_steps = 64
    for _ in range(max_steps):
        if not broker.sent_tasks:
            break
        role, task = broker.sent_tasks.pop(0)
        if role not in workers:
            raise RuntimeError(f"No worker configured for role '{role}'")
        await workers[role]._handle_task(task)

        snapshot = await broker.load_state(run_id)
        if snapshot and snapshot.get("status") in {"completed", "failed"} and not broker.sent_tasks:
            break

    final_state = await broker.load_state(run_id)
    if final_state is None:
        raise RuntimeError("Run state was lost during mock flow check.")

    events = broker.events.get(run_id, [])
    roles_seen = {str(item.get("from_role")) for item in events if item.get("from_role")}
    event_messages = [str(item.get("message", "")) for item in events]

    required_files = {"backend/app/main.py", "frontend/src/App.tsx"}
    source_code = final_state.get("source_code")
    missing_files: List[str] = []
    if not isinstance(source_code, dict):
        missing_files = sorted(required_files)
    else:
        missing_files = sorted(path for path in required_files if path not in source_code)

    return {
        "run_id": run_id,
        "final_state": final_state,
        "roles_seen": sorted(roles_seen),
        "event_count": len(events),
        "has_fanout": any("parallel fan-out started" in message for message in event_messages),
        "has_merge": any("all developer branches completed" in message for message in event_messages),
        "missing_files": missing_files,
    }


def main() -> None:
    result = asyncio.run(_run_parallel_flow_check())
    final_state = result["final_state"]

    status = str(final_state.get("status"))
    if status != "completed":
        raise SystemExit(f"[FAIL] final workflow status is '{status}', expected 'completed'")

    roles_seen = set(result["roles_seen"])
    required_roles = {"developer_backend", "developer_frontend"}
    if not required_roles.issubset(roles_seen):
        missing_roles = sorted(required_roles - roles_seen)
        raise SystemExit(f"[FAIL] parallel roles missing from events: {', '.join(missing_roles)}")

    if not result["has_fanout"]:
        raise SystemExit("[FAIL] fan-out event not found in workflow events")
    if not result["has_merge"]:
        raise SystemExit("[FAIL] merge-complete event not found in workflow events")
    if result["missing_files"]:
        raise SystemExit("[FAIL] merged source_code missing files: " + ", ".join(result["missing_files"]))

    print("[PASS] mock parallel flow validation succeeded")
    print(f"run_id={result['run_id']}")
    print(f"event_count={result['event_count']}")
    print(f"roles_seen={','.join(result['roles_seen'])}")
    print(f"final_status={status}")


if __name__ == "__main__":
    main()
