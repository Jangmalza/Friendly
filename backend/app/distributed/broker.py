import json
import os
from typing import Any, Dict, List, Optional, Tuple

from redis.asyncio import Redis, from_url
from redis.exceptions import ResponseError

from .common import to_jsonable


class RedisStreamBroker:
    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or os.environ.get("REDIS_URL", "redis://redis:6379/0")
        self._client: Optional[Redis] = None

    async def client(self) -> Redis:
        if self._client is None:
            self._client = from_url(self.redis_url, decode_responses=True)
        return self._client

    async def ping(self) -> None:
        client = await self.client()
        await client.ping()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def task_stream(role: str) -> str:
        return f"agent:{role}:tasks"

    @staticmethod
    def event_stream(run_id: str) -> str:
        return f"run:{run_id}:events"

    @staticmethod
    def state_key(run_id: str) -> str:
        return f"run:{run_id}:state"

    @staticmethod
    def dlq_stream(role: str) -> str:
        return f"agent:{role}:dlq"

    async def save_state(self, run_id: str, state: Dict[str, Any]) -> None:
        client = await self.client()
        payload = json.dumps(to_jsonable(state), ensure_ascii=False)
        await client.set(self.state_key(run_id), payload)

    async def load_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        client = await self.client()
        raw = await client.get(self.state_key(run_id))
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    async def send_task(self, role: str, payload: Dict[str, Any]) -> str:
        client = await self.client()
        stream = self.task_stream(role)
        body = {"payload": json.dumps(to_jsonable(payload), ensure_ascii=False)}
        return await client.xadd(stream, body)

    async def append_dlq(self, role: str, payload: Dict[str, Any]) -> str:
        client = await self.client()
        stream = self.dlq_stream(role)
        body = {"payload": json.dumps(to_jsonable(payload), ensure_ascii=False)}
        return await client.xadd(stream, body)

    async def read_dlq_entries(
        self,
        role: str,
        *,
        count: int = 100,
        reverse: bool = True,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        client = await self.client()
        stream = self.dlq_stream(role)
        if reverse:
            entries = await client.xrevrange(stream, max="+", min="-", count=count)
        else:
            entries = await client.xrange(stream, min="-", max="+", count=count)
        return self._decode_entries(entries)

    async def get_dlq_entry(self, role: str, entry_id: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        client = await self.client()
        stream = self.dlq_stream(role)
        entries = await client.xrange(stream, min=entry_id, max=entry_id, count=1)
        decoded = self._decode_entries(entries)
        if not decoded:
            return None
        return decoded[0]

    async def delete_dlq_entry(self, role: str, entry_id: str) -> int:
        client = await self.client()
        stream = self.dlq_stream(role)
        return await client.xdel(stream, entry_id)

    async def append_event(self, run_id: str, payload: Dict[str, Any]) -> str:
        client = await self.client()
        stream = self.event_stream(run_id)
        body = {"payload": json.dumps(to_jsonable(payload), ensure_ascii=False)}
        return await client.xadd(stream, body)

    async def read_tasks(
        self,
        role: str,
        last_id: str,
        *,
        block_ms: int = 1000,
        count: int = 10,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        return await self._read_stream(self.task_stream(role), last_id, block_ms=block_ms, count=count)

    async def ensure_task_group(self, role: str, group_name: str, *, start_id: str = "0-0") -> None:
        client = await self.client()
        stream = self.task_stream(role)
        try:
            await client.xgroup_create(stream, group_name, id=start_id, mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_group_tasks(
        self,
        role: str,
        group_name: str,
        consumer_name: str,
        *,
        block_ms: int = 1000,
        count: int = 10,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        client = await self.client()
        stream = self.task_stream(role)
        response = await client.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={stream: ">"},
            count=count,
            block=block_ms,
        )
        return self._decode_xread_response(response)

    async def read_group_pending_tasks(
        self,
        role: str,
        group_name: str,
        consumer_name: str,
        *,
        count: int = 10,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        client = await self.client()
        stream = self.task_stream(role)
        response = await client.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={stream: "0"},
            count=count,
        )
        return self._decode_xread_response(response)

    async def claim_stale_group_tasks(
        self,
        role: str,
        group_name: str,
        consumer_name: str,
        *,
        min_idle_ms: int = 30000,
        count: int = 10,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        client = await self.client()
        stream = self.task_stream(role)

        pending_entries = await client.xpending_range(
            stream,
            group_name,
            min="-",
            max="+",
            count=count,
        )
        if not pending_entries:
            return []

        stale_ids: List[str] = []
        for item in pending_entries:
            message_id = item.get("message_id")
            idle_ms = int(item.get("time_since_delivered", 0))
            if isinstance(message_id, str) and idle_ms >= min_idle_ms:
                stale_ids.append(message_id)

        if not stale_ids:
            return []

        claimed_entries = await client.xclaim(
            stream,
            group_name,
            consumer_name,
            min_idle_time=min_idle_ms,
            message_ids=stale_ids,
        )
        return self._decode_entries(claimed_entries)

    async def ack_task(self, role: str, group_name: str, entry_id: str) -> int:
        client = await self.client()
        stream = self.task_stream(role)
        return await client.xack(stream, group_name, entry_id)

    async def get_task_group_stats(self, role: str, group_name: str) -> Dict[str, Any]:
        client = await self.client()
        stream = self.task_stream(role)
        dlq_stream = self.dlq_stream(role)

        stream_length = int(await client.xlen(stream))
        dlq_length = int(await client.xlen(dlq_stream))

        groups: List[Dict[str, Any]]
        try:
            groups = await client.xinfo_groups(stream)
        except ResponseError:
            groups = []

        raw_group = next(
            (
                item
                for item in groups
                if str(self._pick_info(item, "name", default="")) == group_name
            ),
            None,
        )

        consumers: List[Dict[str, Any]] = []
        if raw_group is not None:
            try:
                consumers = await client.xinfo_consumers(stream, group_name)
            except ResponseError:
                consumers = []

        return {
            "role": role,
            "stream": stream,
            "stream_length": stream_length,
            "dlq_stream": dlq_stream,
            "dlq_length": dlq_length,
            "group": self._normalize_group_info(raw_group, fallback_name=group_name),
            "consumers": [self._normalize_consumer_info(item) for item in consumers],
        }

    async def read_events(
        self,
        run_id: str,
        last_id: str,
        *,
        block_ms: int = 1000,
        count: int = 100,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        return await self._read_stream(self.event_stream(run_id), last_id, block_ms=block_ms, count=count)

    async def read_event_history(self, run_id: str, *, count: int = 200) -> List[Tuple[str, Dict[str, Any]]]:
        client = await self.client()
        stream = self.event_stream(run_id)
        entries = await client.xrange(stream, min="-", max="+", count=count)
        history: List[Tuple[str, Dict[str, Any]]] = []
        for entry_id, fields in entries:
            payload_raw = fields.get("payload", "{}")
            payload = self._decode_payload(payload_raw)
            history.append((entry_id, payload))
        return history

    async def _read_stream(
        self,
        stream: str,
        last_id: str,
        *,
        block_ms: int,
        count: int,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        client = await self.client()
        response = await client.xread({stream: last_id}, count=count, block=block_ms)
        if not response:
            return []
        return self._decode_xread_response(response)

    def _decode_xread_response(
        self,
        response: List[Tuple[str, List[Tuple[str, Dict[str, str]]]]],
    ) -> List[Tuple[str, Dict[str, Any]]]:
        items: List[Tuple[str, Dict[str, Any]]] = []
        for _, entries in response:
            items.extend(self._decode_entries(entries))
        return items

    def _decode_entries(
        self,
        entries: List[Tuple[str, Dict[str, str]]],
    ) -> List[Tuple[str, Dict[str, Any]]]:
        items: List[Tuple[str, Dict[str, Any]]] = []
        for entry_id, fields in entries:
            payload_raw = fields.get("payload", "{}")
            payload = self._decode_payload(payload_raw)
            items.append((entry_id, payload))
        return items

    @staticmethod
    def _decode_payload(payload_raw: str) -> Dict[str, Any]:
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = {"raw": payload_raw}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        return payload

    @staticmethod
    def _pick_info(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in data:
                return data[key]
        return default

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _normalize_group_info(self, data: Optional[Dict[str, Any]], *, fallback_name: str) -> Dict[str, Any]:
        if data is None:
            return {
                "exists": False,
                "name": fallback_name,
                "consumers": 0,
                "pending": 0,
                "lag": 0,
                "entries_read": 0,
                "last_delivered_id": "0-0",
            }

        return {
            "exists": True,
            "name": str(self._pick_info(data, "name", default=fallback_name)),
            "consumers": self._safe_int(self._pick_info(data, "consumers", default=0)),
            "pending": self._safe_int(self._pick_info(data, "pending", default=0)),
            "lag": self._safe_int(self._pick_info(data, "lag", default=0)),
            "entries_read": self._safe_int(
                self._pick_info(data, "entries-read", "entries_read", default=0)
            ),
            "last_delivered_id": str(
                self._pick_info(data, "last-delivered-id", "last_delivered_id", default="0-0")
            ),
        }

    def _normalize_consumer_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": str(self._pick_info(data, "name", default="")),
            "pending": self._safe_int(self._pick_info(data, "pending", default=0)),
            "idle_ms": self._safe_int(self._pick_info(data, "idle", "idle_ms", default=0)),
        }
