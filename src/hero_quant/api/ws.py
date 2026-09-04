"""api.ws — WebSocket 进度推送（Phase 2）。

职责：为 Live/Monitor 提供多智能体与 trace 事件的实时推送，替代轮询。
架构位置：api 层 WS 边界，依赖 security 票据与 infra.redis 分布式状态。
关键设计：
- 移植 skills/fastapi-ws-module-skill 的 WSManager(多端登录) + HeartbeatMonitor(30s ping/60s超时)
- 鉴权复用 SSE 票据 consume_ticket（query ?ticket= 防代理日志）
- Redis Stream 广播便于多 worker；本地无 Redis 时内存广播（fakeredis 已有则走它）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from hero_quant.api.security import consume_ticket
from hero_quant.infra.redis import RedisStream, get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Multi-instance broadcast (PR1-B) ──
TRACE_STREAM = "hero:stream:trace"
TRACE_GROUP = "hero:ws-trace"
TRACE_CHANNEL = "trace"
USER_CHANNEL_PREFIX = "ws:channel:"
ONLINE_PREFIX = "ws:online:"
ONLINE_TTL = 90  # seconds, per heartbeat-guide ws:online:{channel} TTL 90s


def resolve_user_channel(user: str | None, ws: Any | None = None, kind: str = "trace") -> str:
    """R3: user 查询串 → 用户级频道，ticket 语义不变（security.py 不动）。

    - user 非空 → f"ws:channel:{user.strip()[:64]}"（双连接共享，跨 user 隔离）。
    - user 为空 → 回退单机频道 f"{kind}:{id(ws)}"（兼容旧 Monitor 全量广播）。
    """
    u = (user or "").strip()[:64]
    if u:
        return f"{USER_CHANNEL_PREFIX}{u}"
    if ws is not None:
        return f"{kind}:{id(ws)}"
    return TRACE_CHANNEL

try:
    _HOST = socket.gethostname()
except Exception:
    _HOST = "unknown"
INSTANCE_ID = f"{_HOST}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

# ── Online presence (Redis heartbeat, best-effort) ──


async def mark_online(channel: str) -> None:
    """Register instance presence: SET ws:online:{channel} {instanceId} EX 90."""
    try:
        client = await get_redis()
        if client is None:
            return
        await client.set(f"{ONLINE_PREFIX}{channel}", INSTANCE_ID, ex=ONLINE_TTL)
    except Exception as e:
        logger.debug("ws.mark_online_failed error=%s", str(e))


async def mark_offline(channel: str) -> None:
    """Remove presence only if it is ours (avoid deleting a sibling worker's key)."""
    try:
        client = await get_redis()
        if client is None:
            return
        key = f"{ONLINE_PREFIX}{channel}"
        try:
            current = await client.get(key)
        except Exception:
            current = None
        if current is None or current == INSTANCE_ID:
            try:
                await client.delete(key)
            except Exception as e:
                logger.debug("ws.mark_offline_delete_failed error=%s", str(e))
    except Exception as e:
        logger.debug("ws.mark_offline_failed error=%s", str(e))


# ── Manager (多端登录) ──


class WSManager:
    """WebSocket 连接管理器 — 单机多端登录，广播到同用户所有连接。"""

    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, channel: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections[channel].add(ws)

    async def disconnect(self, channel: str, ws: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(channel)
            if conns and ws in conns:
                conns.discard(ws)
                if not conns:
                    self._connections.pop(channel, None)
        try:
            await ws.close()
        except Exception:
            pass

    async def send_to(self, channel: str, data: dict[str, Any]) -> bool:
        conns = list(self._connections.get(channel, ()))
        if not conns:
            # Also try broadcast to "trace" wildcard for Monitor
            return False

        async def _safe_send(ws: WebSocket) -> bool:
            try:
                await ws.send_json(data)
                return True
            except Exception:
                await self.disconnect(channel, ws)
                return False

        results = await asyncio.gather(*[_safe_send(ws) for ws in conns])
        return any(results)

    async def broadcast(self, data: dict[str, Any]) -> int:
        """Broadcast to all connected channels."""
        count = 0
        for channel in list(self._connections.keys()):
            if await self.send_to(channel, data):
                count += 1
        return count

    @property
    def channel_count(self) -> int:
        return len(self._connections)

    def is_online(self, channel: str) -> bool:
        return bool(self._connections.get(channel))


manager = WSManager()


# ── Heartbeat ──


class HeartbeatMonitor:
    TIMEOUT = timedelta(seconds=60)
    CHECK_INTERVAL = 10

    def __init__(self):
        self._last_active: dict[str, datetime] = {}
        self._task: asyncio.Task | None = None

    def record(self, channel: str) -> None:
        self._last_active[channel] = datetime.now(timezone.utc)

    def remove(self, channel: str) -> None:
        self._last_active.pop(channel, None)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._check_loop())

    async def _check_loop(self) -> None:
        while True:
            await asyncio.sleep(self.CHECK_INTERVAL)
            now = datetime.now(timezone.utc)
            expired = [ch for ch, last in self._last_active.items() if now - last > self.TIMEOUT]
            for ch in expired:
                conns = list(manager._connections.get(ch, ()))
                for ws in conns:
                    try:
                        await ws.close(code=4002, reason="heartbeat timeout")
                    except Exception:
                        pass
                manager._connections.pop(ch, None)
                self.remove(ch)


heartbeat = HeartbeatMonitor()


# ── Endpoints ──


@router.websocket("/ws/trace")
async def ws_trace(
    websocket: WebSocket,
    ticket: str = Query(default="", description="SSE ticket from POST /v1/query/ticket"),
    user: str = Query(default="", description="R3 可选 userId：非空则频道为 ws:channel:{user}"),
):
    """WebSocket 进度推送 — 订阅 trace 事件流。

    连接：ws://host/ws/trace?ticket=<ticket>&user=<userId，可选>
    票据单次有效，需先 POST /v1/query/ticket 获取。ticket 保持纯随机语义，
    user 仅决定频道归属（同 user 双连接共享 ws:channel:{user}，跨 user 隔离）。
    任何消息(ping/chat)均视为心跳，60s 无消息断开(4002)。
    """
    if not ticket or not consume_ticket(ticket):
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": -1002, "message": "Invalid or expired ticket"})
        await websocket.close(code=4001, reason="Invalid ticket")
        return

    channel = resolve_user_channel(user, websocket, kind="trace")
    await manager.connect(channel, websocket)
    heartbeat.record(channel)
    await mark_online(channel)
    await websocket.send_json({"type": "connected", "channel": channel})

    # Optionally push buffered? For now just stream live events
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_json(), timeout=30)
            except asyncio.TimeoutError:
                # Send ping to keep alive; client should pong, but we record anyway
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    break
                continue
            heartbeat.record(channel)
            if isinstance(raw, dict) and raw.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            # Broadcast any tool/delta events from other sources? Client sends ignored
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("ws.trace_error error=%s", str(e))
    finally:
        await manager.disconnect(channel, websocket)
        if not manager.is_online(channel):
            heartbeat.remove(channel)
            await mark_offline(channel)


@router.websocket("/ws/query")
async def ws_query(
    websocket: WebSocket,
    ticket: str = Query(default="", description="SSE ticket"),
    user: str = Query(default="", description="R3 可选 userId：非空则频道为 ws:channel:{user}"),
):
    """WebSocket for query stream — alternative to /v1/query/stream SSE."""
    if not ticket or not consume_ticket(ticket):
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": -1002, "message": "Invalid or expired ticket"})
        await websocket.close(code=4001, reason="Invalid ticket")
        return

    channel = resolve_user_channel(user, websocket, kind="query")
    await manager.connect(channel, websocket)
    heartbeat.record(channel)
    await mark_online(channel)
    await websocket.send_json({"type": "connected", "channel": channel})

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_json(), timeout=60)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    break
                continue
            heartbeat.record(channel)
            if isinstance(raw, dict) and raw.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif isinstance(raw, dict) and raw.get("type") == "query":
                # Client can send {"type":"query","q":"..."} to trigger query — stub for now
                q = raw.get("q", "")
                await websocket.send_json({"type": "tool", "tool": "query", "status": "running", "preview": q[:80]})
                await websocket.send_json({"delta": f"query received: {q[:80]}"})
                await websocket.send_json({"type": "done"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("ws.query_error error=%s", str(e))
    finally:
        await manager.disconnect(channel, websocket)
        heartbeat.remove(channel)
        await mark_offline(channel)


# Helper for TraceWriter to broadcast events without blocking
async def broadcast_trace_event(event: dict[str, Any], user: str | None = None) -> None:
    """Best-effort broadcast: publish to Redis Stream, then deliver locally.

    R3: user 非空 → 定向投递该用户频道（Redis payload channel=ws:channel:{user}，
    本地 send_to 同频道，跨 user 隔离）；user 为空 → 旧语义全量广播 + channel=trace。
    """
    channel = (f"{USER_CHANNEL_PREFIX}{(user or '').strip()[:64]}") if (user or "").strip() else TRACE_CHANNEL
    try:
        await RedisStream().publish(
            TRACE_STREAM,
            {"channel": channel, "data": json.dumps(event, ensure_ascii=False, default=str)},
        )
    except Exception as e:
        logger.debug("ws.stream_publish_failed error=%s", str(e))
    try:
        if channel == TRACE_CHANNEL:
            await manager.broadcast(event)
        else:
            await manager.send_to(channel, event)
    except Exception as e:
        logger.debug("ws.broadcast_failed error=%s", str(e))


async def run_trace_consumer(stop_event: asyncio.Event | None = None) -> None:
    """Per-worker consumer: forward Stream entries to local manager.send_to.

    lifespan 启动每 worker 一个消费者；stop_event 置位即退出。兼容
    fakeredis 缺 xreadgroup 时 RedisStream 内部已回退 xread。
    """
    stream = RedisStream()
    consumer = f"{INSTANCE_ID}"
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            entries = await stream.subscribe_consumer(
                TRACE_STREAM, TRACE_GROUP, consumer, count=20, block=1000
            )
        except Exception as e:
            logger.debug("ws.consumer_read_failed error=%s", str(e))
            await asyncio.sleep(1.0)
            continue
        if not entries:
            continue
        for msg_id, fields in entries:
            try:
                chan = str(fields.get("channel", TRACE_CHANNEL) or TRACE_CHANNEL)
                raw = fields.get("data", "{}")
                payload = json.loads(raw) if isinstance(raw, str) else {}
                if isinstance(payload, dict):
                    if chan == TRACE_CHANNEL:
                        await manager.broadcast(payload)
                    else:
                        await manager.send_to(chan, payload)
            except Exception as e:
                logger.debug("ws.consumer_forward_failed error=%s", str(e))
            finally:
                try:
                    await stream.ack(TRACE_STREAM, TRACE_GROUP, msg_id)
                except Exception:
                    pass


def broadcast_trace_event_sync(event: dict[str, Any]) -> None:
    """Sync wrapper for non-async call sites (e.g. TraceWriter.append)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(broadcast_trace_event(event))
        else:
            # No running loop — skip
            pass
    except Exception:
        pass
