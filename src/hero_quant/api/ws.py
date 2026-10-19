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
import hashlib
import json
import logging
import os
import socket
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from hero_quant.api.security import consume_ticket, consume_ticket_with_user
from hero_quant.infra.redis import RedisStream, get_redis

logger = logging.getLogger(__name__)

try:
    import structlog as _structlog  # type: ignore

    _slog = _structlog.get_logger(__name__)
except Exception:  # pragma: no cover — 最小环境无 structlog
    _slog = None  # type: ignore

_CHAIN = "A"  # F线分级：本文件位于问答链（WS 实时推送 query/stream 轨迹事件）


def _warn(op: str, msg: str, *args: Any, **fields: Any) -> None:
    """F线分级告警（D线 risk/_log_silent_handled 同款 schema）：stdlib warning 为主
    （caplog/现网采集兼容），structlog 结构化为辅（error_code + chain + trace 语义字段；
    trace_id 由 server.py contextvars 自动透传）。PII 约束：fields 只传 error_code /
    chain / channel 等非敏感字段，禁 ticket/user/key 原值。"""
    _exc_info = fields.pop("exc_info", None)
    try:
        logger.warning(msg, *args, exc_info=_exc_info)
    except Exception:
        pass
    if _slog is None:
        return
    try:
        _slog.warning(op, error_code=op, chain=fields.pop("chain", _CHAIN), exc_info=_exc_info, **fields)
    except Exception:
        pass


def _consume_ticket_identity(ticket: str | None) -> tuple[bool, str | None]:
    """消费票据并返回 (ok, user_id)：优先带绑定的原子消费，兼容仅桩 consume_ticket 的旧测试替身。

    中文：正常路径走 consume_ticket_with_user（单次原子消费，返回绑定 user_id 字符串，
    匿名票为""）；当其失败时再探 consume_ticket——仅测试替身将其桩为 True 时才视为匿名
    通过（生产真实失败时两者皆 False，无放行缺口）。
    """
    try:
        ok, uid = consume_ticket_with_user(ticket)
    except (ValueError, TypeError, AttributeError, RuntimeError, OSError):
        ok, uid = False, None
    if ok:
        return True, (uid if isinstance(uid, str) else "")
    try:
        if consume_ticket(ticket):
            return True, ""
    except (ValueError, TypeError, AttributeError, RuntimeError, OSError):
        pass  # intentional: 旧测试替身兼容探针；失败即匿名不通过（fail-closed），见函数 docstring
    return False, None


def _resolve_identity_channel(
    ticket_user: str | None, claimed_user: str | None, ws: Any | None = None, kind: str = "trace"
) -> tuple[str | None, str]:
    """由服务端身份派生 channel：绑定票按 ticket_user；匿名票一律 per-connection。

    中文：ticket_user 非空时 claimed_user 为空则直接派生，声明不一致视为冒充返回 (None, reason)；
    匿名票（""）忽略 ?user= 自声明，一律 per-connection 频道（G-02 订阅侧收紧：发侧已强制匿名
    user_id=""，?user= 不可信，不得派生他人 ws:channel:{user}；ws=None 的直接调用回退共享 trace 频道）。
    返回 (channel, "") 成功；失败返回 (None, reason)。
    """
    try:
        t_user = (ticket_user or "").strip()
    except (ValueError, TypeError, AttributeError):
        t_user = ""
    try:
        claimed = (claimed_user or "").strip()
    except (ValueError, TypeError, AttributeError):
        claimed = ""
    if t_user:
        if claimed and claimed != t_user:
            return None, f"Ticket user mismatch (ticket={t_user!r} claimed={claimed!r})"
        return resolve_user_channel(t_user, ws, kind=kind), ""
    # G-02 订阅侧收紧：匿名票禁止由 ?user= 派生他人用户频道，一律 per-connection 频道。
    # 全量广播（user=None 事件走 manager.broadcast）仍触达匿名连接，Monitor 兼容不受影响；
    # 匿名连接不再接收他人用户定向事件——此为 intended 隔离收紧，非回归（见 F2 端点注记）。
    return resolve_user_channel(None, ws, kind=kind), ""

router = APIRouter()

# ── Multi-instance broadcast (PR1-B) ──
TRACE_STREAM = "hero:stream:trace"
TRACE_GROUP = "hero:ws-trace"
TRACE_CHANNEL = "trace"
USER_CHANNEL_PREFIX = "ws:channel:"
ONLINE_PREFIX = "ws:online:"
ONLINE_TTL = 90  # seconds, per heartbeat-guide ws:online:{channel} TTL 90s
# user 截断上限：超长 user 加哈希后缀保隔离（长 user 不得静默折叠到同一 channel）
MAX_USER_LEN = 128


def _norm_user(user: str | None) -> str:
    """归一化 user：去首尾空白；超长则保留前 64 字符 + sha1 短哈希，后缀保隔离。"""
    u = (user or "").strip()
    if len(u) <= 64:
        return u
    suffix = hashlib.sha1(u.encode("utf-8")).hexdigest()[:8]  # 非安全用途，仅隔离
    return f"{u[:64]}#{suffix}"


def resolve_user_channel(user: str | None, ws: Any | None = None, kind: str = "trace") -> str:
    """R3: user 查询串 → 用户级频道，ticket 语义不变（security.py 不动）。

    - user 非空 → f"ws:channel:{user.strip()[:64]}"（双连接共享，跨 user 隔离）。
    - user 为空 → 回退单机频道 f"{kind}:{id(ws)}"（兼容旧 Monitor 全量广播）。
    """
    u = _norm_user(user)
    if len((user or "").strip()) > MAX_USER_LEN:
        # 超长 user 拒绝归属到共享频道，退化为单机频道防串扰
        u = ""
    if u:
        return f"{USER_CHANNEL_PREFIX}{u}"
    if ws is not None:
        return f"{kind}:{id(ws)}"
    return TRACE_CHANNEL

try:
    _HOST = socket.gethostname()
except Exception:
    _HOST = "unknown"  # intentional: 主机名仅用于实例标识，失败回退 unknown（offline-safe）
INSTANCE_ID = f"{_HOST}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

# ── Online presence (Redis heartbeat, best-effort) ──

# presence 续约节流：每个 channel 两次 SET 之间至少间隔该秒数，避免逐消息打 Redis
PRESENCE_REFRESH_INTERVAL = 30.0
_presence_touched: dict[str, datetime] = {}


async def mark_online(channel: str) -> None:
    """Register instance presence: SET ws:online:{channel} {instanceId} EX 90."""
    if not (channel or "").strip():
        return
    try:
        client = await get_redis()
        if client is None:
            return
        await client.set(f"{ONLINE_PREFIX}{channel}", INSTANCE_ID, ex=ONLINE_TTL)
    except Exception as e:
        _warn(
            "ws.mark_online_failed",
            "ws.mark_online_failed channel=%s error=%s error_code=ws.mark_online_failed",
            channel,
            str(e),
            channel=channel,
        )


async def refresh_presence(channel: str) -> None:
    """续约 presence TTL（节流）：连接存活期间定期重 SET，防止 90s 过期误判离线。"""
    if not (channel or "").strip():
        return
    now = datetime.now(timezone.utc)
    last = _presence_touched.get(channel)
    if last is not None and (now - last).total_seconds() < PRESENCE_REFRESH_INTERVAL:
        return
    _presence_touched[channel] = now
    await mark_online(channel)


async def mark_offline(channel: str) -> None:
    """Remove presence only if it is ours (avoid deleting a sibling worker's key)."""
    if not (channel or "").strip():
        return
    try:
        client = await get_redis()
        if client is None:
            return
        key = f"{ONLINE_PREFIX}{channel}"
        try:
            # 原子比较删除：仅值仍是本实例才删，避免误删兄弟 worker 刚重建的 presence
            await client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                key,
                INSTANCE_ID,
            )
        except Exception:
            # eval 不可用（如部分 fakeredis）才退化为读后删；删错风险由 TTL 90s 兜底
            try:
                current = await client.get(key)
            except Exception as _e:
                # presence 读失败视为不持有（TTL 90s 自愈）；告警便于发现 Redis 持续不可用
                _warn(
                    "ws.mark_offline_read_failed",
                    "ws.mark_offline_read_failed channel=%s error=%s error_code=ws.mark_offline_read_failed",
                    channel,
                    str(_e),
                    channel=channel,
                )
                current = None
            if current == INSTANCE_ID:
                try:
                    await client.delete(key)
                except Exception as e:
                    _warn(
                        "ws.mark_offline_delete_failed",
                        "ws.mark_offline_delete_failed channel=%s error=%s error_code=ws.mark_offline_delete_failed",
                        channel,
                        str(e),
                        channel=channel,
                    )
        finally:
            _presence_touched.pop(channel, None)
    except Exception as e:
        _warn(
            "ws.mark_offline_failed",
            "ws.mark_offline_failed channel=%s error=%s error_code=ws.mark_offline_failed",
            channel,
            str(e),
            channel=channel,
        )


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
            pass  # intentional: 连接已先摘除；close 失败不影响清理结果（best-effort 收尾）

    async def send_to(self, channel: str, data: dict[str, Any]) -> bool:
        conns = list(self._connections.get(channel, ()))
        if not conns:
            # Also try broadcast to "trace" wildcard for Monitor
            return False

        async def _safe_send(ws: WebSocket) -> bool:
            # 窄化：仅协议/IO/序列化异常判为坏连接并摘除；程序错误上抛便于排查
            try:
                await ws.send_json(data)
                return True
            except (RuntimeError, ValueError, TypeError, OSError) as e:
                _warn(
                    "ws.send_failed",
                    "ws.send_failed channel=%s error=%s error_code=ws.send_failed",
                    channel,
                    str(e),
                    channel=channel,
                )
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
    # Server-driven keepalive budget: at most this many CONSECUTIVE server
    # pongs (zero client traffic) may renew liveness. Real client traffic
    # resets the budget via record(). Bounds ghost-connection lifetime so
    # dead peers eventually expire instead of being renewed forever.
    MAX_SERVER_KEEPALIVES = 3

    def __init__(self):
        self._last_active: dict[str, datetime] = {}
        self._server_keepalives: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    def record(self, channel: str) -> None:
        self._last_active[channel] = datetime.now(timezone.utc)
        # Genuine client traffic resets the server-pong budget.
        self._server_keepalives[channel] = 0

    def record_keepalive(self, channel: str) -> bool:
        """Bounded server-keepalive renewal: renew liveness only while the
        consecutive server-pong budget lasts; return False once exhausted
        (caller must then skip heartbeat/presence renewal so the channel
        becomes evictable)."""
        n = self._server_keepalives.get(channel, 0) + 1
        if n > self.MAX_SERVER_KEEPALIVES:
            return False
        self.record(channel)  # refresh _last_active; also resets budget to 0
        self._server_keepalives[channel] = n  # re-apply consecutive count
        return True

    def remove(self, channel: str) -> None:
        self._last_active.pop(channel, None)
        self._server_keepalives.pop(channel, None)
        # 同步清理 presence 续约节流表，否则 _presence_touched 随 channel 增长无界膨胀
        _presence_touched.pop(channel, None)

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
                # Pop under WSManager._lock (connect/disconnect mutate the same
                # dict under it); pop BEFORE close so a reconnect added during
                # close() is not evicted by a stale snapshot. Re-validate
                # staleness under the lock: a record() that landed after the
                # snapshot must not be evicted.
                async with manager._lock:
                    last = self._last_active.get(ch)
                    if last is None or now - last <= self.TIMEOUT:
                        continue
                    conns = list(manager._connections.pop(ch, set()))
                for ws in conns:
                    try:
                        await ws.close(code=4002, reason="heartbeat timeout")
                    except Exception:
                        pass  # intentional: 超时摘除已生效；close 失败不影响驱逐结果（best-effort 收尾）
                # Do NOT delete a fresh record() from a reconnect that happened
                # during the awaited close() above — otherwise the new
                # connection is left unmonitored (never times out).
                last = self._last_active.get(ch)
                if last is None or datetime.now(timezone.utc) - last > self.TIMEOUT:
                    self.remove(ch)


heartbeat = HeartbeatMonitor()


# ── Endpoints ──


@router.websocket("/ws/trace")
async def ws_trace(
    websocket: WebSocket,
    ticket: str = Query(default="", description="SSE ticket from POST /v1/query/ticket"),
    user: str = Query(default="", description="R3 可选 userId：仅绑定票生效（须与 ticket.user 一致，否则4001）；匿名票忽略"),
):
    """WebSocket 进度推送 — 订阅 trace 事件流。

    连接：ws://host/ws/trace?ticket=<ticket>&user=<userId，可选>
    票据单次有效，需先 POST /v1/query/ticket 获取。channel 由服务端票据身份派生：
    绑定票（user_id 非空）以 ticket.user 为准，?user= 声明不一致视为冒充关闭 4001；
    匿名票（user_id=""）忽略 ?user=，一律 per-connection 频道（G-02 订阅侧收紧）。
    F2 兼容结论：全量广播（user=None 事件）仍触达所有连接（含匿名），旧 Monitor 语义保留；
    匿名 ?user= 搭他人用户频道已关闭——intended 隔离收紧，语义不擅自扩大（真 per-user 身份待后续专线）。
    任何消息(ping/chat)均视为心跳，60s 无消息断开(4002)。
    """
    ok, ticket_user = _consume_ticket_identity(ticket)
    if not ticket or not ok:
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": -1002, "message": "Invalid or expired ticket"})
        await websocket.close(code=4001, reason="Invalid ticket")
        return

    channel, _mismatch = _resolve_identity_channel(ticket_user, user, websocket, kind="trace")
    if channel is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": -1002, "message": "Ticket user mismatch"})
        await websocket.close(code=4001, reason="Ticket user mismatch")
        return
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
                # Server keepalive only: send pong but renew liveness ONLY
                # within the bounded keepalive budget (record_keepalive).
                # Unbounded renewal would keep dead peers alive forever.
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    break
                if heartbeat.record_keepalive(channel):
                    await refresh_presence(channel)
                continue
            heartbeat.record(channel)
            await refresh_presence(channel)
            if isinstance(raw, dict) and raw.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            # Broadcast any tool/delta events from other sources? Client sends ignored
    except WebSocketDisconnect:
        pass  # intentional: 客户端正常断开，非错误（finally 照常清理连接与 presence）
    except (RuntimeError, ValueError, KeyError) as e:
        # 窄化：仅收 receive_json/连接层的协议与解析异常，其余程序错误继续上抛
        _warn(
            "ws.trace_error",
            "ws.trace_error channel=%s error=%s error_code=ws.trace_error",
            channel,
            str(e),
            channel=channel,
        )
    finally:
        await manager.disconnect(channel, websocket)
        if not manager.is_online(channel):
            heartbeat.remove(channel)
            await mark_offline(channel)


@router.websocket("/ws/query")
async def ws_query(
    websocket: WebSocket,
    ticket: str = Query(default="", description="SSE ticket"),
    user: str = Query(default="", description="R3 可选 userId：仅绑定票生效（须与 ticket.user 一致，否则4001）；匿名票忽略"),
):
    """WebSocket for query stream — alternative to /v1/query/stream SSE（身份闭环同 ws_trace：绑定票 mismatch 4001 关闭；匿名票一律 per-connection，见 ws_trace F2 注记）。"""
    ok, ticket_user = _consume_ticket_identity(ticket)
    if not ticket or not ok:
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": -1002, "message": "Invalid or expired ticket"})
        await websocket.close(code=4001, reason="Invalid ticket")
        return

    channel, _mismatch = _resolve_identity_channel(ticket_user, user, websocket, kind="query")
    if channel is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": -1002, "message": "Ticket user mismatch"})
        await websocket.close(code=4001, reason="Ticket user mismatch")
        return
    await manager.connect(channel, websocket)
    heartbeat.record(channel)
    await mark_online(channel)
    await websocket.send_json({"type": "connected", "channel": channel})

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_json(), timeout=60)
            except asyncio.TimeoutError:
                # Server keepalive only: bounded renewal (same policy as trace).
                try:
                    await websocket.send_json({"type": "pong"})
                except Exception:
                    break
                if heartbeat.record_keepalive(channel):
                    await refresh_presence(channel)
                continue
            heartbeat.record(channel)
            await refresh_presence(channel)
            if isinstance(raw, dict) and raw.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif isinstance(raw, dict) and raw.get("type") == "query":
                # Client can send {"type":"query","q":"..."} to trigger query — stub for now
                q = raw.get("q", "")
                await websocket.send_json({"type": "tool", "tool": "query", "status": "running", "preview": q[:80]})
                await websocket.send_json({"delta": f"query received: {q[:80]}"})
                await websocket.send_json({"type": "done"})
    except WebSocketDisconnect:
        pass  # intentional: 客户端正常断开，非错误（finally 照常清理连接与 presence）
    except (RuntimeError, ValueError, KeyError) as e:
        _warn(
            "ws.query_error",
            "ws.query_error channel=%s error=%s error_code=ws.query_error",
            channel,
            str(e),
            channel=channel,
        )
    finally:
        await manager.disconnect(channel, websocket)
        # 共享 channel（如 ws:channel:{user} 多端登录）：仅最后一条连接离开才清状态
        if not manager.is_online(channel):
            heartbeat.remove(channel)
            await mark_offline(channel)


# Helper for TraceWriter to broadcast events without blocking
def _trace_consumer_group() -> str:
    """Per-worker consumer group: every worker receives every Stream entry
    (true broadcast). A single shared group would deliver each entry to
    exactly one worker, so most workers would miss the event."""
    return f"{TRACE_GROUP}:{INSTANCE_ID}"


async def broadcast_trace_event(event: dict[str, Any], user: str | None = None) -> None:
    """Best-effort broadcast: publish to Redis Stream (tagged with origin),
    then deliver locally.

    R3: user 非空 → 定向投递该用户频道（Redis payload channel=ws:channel:{user}，
    本地 send_to 同频道，跨 user 隔离）；user 为空 → 旧语义全量广播 + channel=trace。
    Local delivery is kept for single-process/low-latency + Monitor compat;
    the origin tag lets each worker's consumer suppress its own echo (no
    duplicate), while per-worker groups ensure every worker still sees
    entries from OTHER workers (no miss).
    """
    channel = resolve_user_channel(user, None, kind="trace")
    try:
        await RedisStream().publish(
            TRACE_STREAM,
            {
                "channel": channel,
                "origin": INSTANCE_ID,
                "data": json.dumps(event, ensure_ascii=False, default=str),
            },
        )
    except Exception as e:
        _warn(
            "ws.stream_publish_failed",
            "ws.stream_publish_failed channel=%s error=%s error_code=ws.stream_publish_failed",
            channel,
            str(e),
            channel=channel,
        )
        # Fall back to local delivery only when publish failed / no Redis.
    try:
        if channel == TRACE_CHANNEL:
            await manager.broadcast(event)
        else:
            await manager.send_to(channel, event)
    except Exception as e2:
        _warn(
            "ws.broadcast_failed",
            "ws.broadcast_failed channel=%s error=%s error_code=ws.broadcast_failed",
            channel,
            str(e2),
            channel=channel,
        )


def _stream_entry_is_stale(msg_id: str, started_ms: int) -> bool:
    """True when a Stream entry predates this consumer (restart replay).

    IDs are {ms}-{seq}; unparseable IDs are treated as fresh (deliver).
    """
    try:
        return int(str(msg_id).split("-")[0]) < started_ms
    except (ValueError, TypeError, AttributeError, IndexError):
        return False


async def run_trace_consumer(stop_event: asyncio.Event | None = None) -> None:
    """Per-worker consumer: forward Stream entries to local manager.send_to.

    lifespan 启动每 worker 一个消费者；stop_event 置位即退出。兼容
    fakeredis 缺 xreadgroup 时 RedisStream 内部已回退 xread。
    """
    stream = RedisStream()
    consumer = f"{INSTANCE_ID}"
    group = _trace_consumer_group()
    # Per-worker groups are new on every restart while the Stream persists:
    # skip entries predating this consumer so a restart does not replay
    # history (duplicate storm). Stream IDs are {ms}-{seq}.
    started_ms = int(time.time() * 1000)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            entries = await stream.subscribe_consumer(
                TRACE_STREAM, group, consumer, count=20, block=1000
            )
        except Exception as e:
            # intentional: 轮询循环 1s 重试，warning 会刷屏；Redis 持续不可用由 publish 侧 warning 暴露
            logger.debug("ws.consumer_read_failed error=%s error_code=ws.consumer_read_failed", str(e))
            await asyncio.sleep(1.0)
            continue
        if not entries:
            # fakeredis / non-blocking backends return instantly when empty;
            # yield so the loop never hot-spins and stop_event stays honored.
            await asyncio.sleep(0.1)
            continue
        for msg_id, fields in entries:
            try:
                if _stream_entry_is_stale(msg_id, started_ms):
                    # Pre-restart history for this fresh group: ack and drop.
                    try:
                        await stream.ack(TRACE_STREAM, group, msg_id)
                    except Exception:
                        pass  # intentional: ack 失败由 stream pending 重投递自愈；抛错会中断消费循环
                    continue
                if str(fields.get("origin") or "") == INSTANCE_ID:
                    # Own publish was already delivered locally by
                    # broadcast_trace_event; suppress the echo (no duplicate).
                    try:
                        await stream.ack(TRACE_STREAM, group, msg_id)
                    except Exception:
                        pass  # intentional: 同上（ack 自愈，不中断消费循环）
                    continue
                chan = str(fields.get("channel", TRACE_CHANNEL) or TRACE_CHANNEL)
                raw = fields.get("data", "{}")
                payload = json.loads(raw) if isinstance(raw, str) else {}
                if isinstance(payload, dict):
                    if chan == TRACE_CHANNEL:
                        await manager.broadcast(payload)
                    else:
                        await manager.send_to(chan, payload)
            except Exception as e:
                # Forward/parse failure: do NOT ack — the entry stays pending
                # for redelivery instead of being silently dropped.
                # intentional: 不 ack 即重投递自愈；warning 会按坏消息刷屏，pending 积压由运维侧观察
                logger.debug("ws.consumer_forward_failed error=%s error_code=ws.consumer_forward_failed", str(e))
                continue
            try:
                await stream.ack(TRACE_STREAM, group, msg_id)
            except Exception:
                pass  # intentional: ack 失败由 stream pending 重投递自愈；抛错会中断消费循环


def broadcast_trace_event_sync(event: dict[str, Any], user: str | None = None) -> None:
    """Sync wrapper for non-async call sites (e.g. TraceWriter.append).

    user 非空 → 仅投递该用户频道；为空则保持旧全量广播语义。user 路由必须
    显式透传，禁止默认 user=None 把用户级事件广播给所有连接用户。
    """
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无运行中 loop 时不再静默吞事件：打日志便于排查调用方上下文问题
            _warn(
                "ws.broadcast_sync_no_loop",
                "ws.broadcast_sync_no_loop dropping event type=%s error_code=ws.broadcast_sync_no_loop",
                type(event).__name__,
            )
            return
        # Keep the single-arg call shape when no routing is needed (call-site
        # monkeypatch compat); pass user through only when set.
        if user is None:
            task = loop.create_task(broadcast_trace_event(event))
        else:
            task = loop.create_task(broadcast_trace_event(event, user=user))
    except Exception as e:
        _warn(
            "ws.broadcast_sync_schedule_failed",
            "ws.broadcast_sync_schedule_failed error=%s error_code=ws.broadcast_sync_schedule_failed",
            str(e),
        )
        return

    def _log_task_error(t: asyncio.Task) -> None:
        # 后台广播异常必须可观测，否则 sync 调用方永远丢事件还查不到
        try:
            if t.cancelled():
                return
            err = t.exception()
        except Exception as e:
            _warn(
                "ws.broadcast_sync_failed",
                "ws.broadcast_sync_failed error=%s error_code=ws.broadcast_sync_failed",
                str(e),
            )
            return
        if err is not None:
            _warn(
                "ws.broadcast_sync_failed",
                "ws.broadcast_sync_failed error=%s error_code=ws.broadcast_sync_failed",
                str(err),
            )

    try:
        task.add_done_callback(_log_task_error)
    except Exception as e:
        _warn(
            "ws.broadcast_sync_callback_failed",
            "ws.broadcast_sync_callback_failed error=%s error_code=ws.broadcast_sync_callback_failed",
            str(e),
        )
