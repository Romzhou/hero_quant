"""hero_quant.stream — 实时流式能力入口。

职责：暴露增量因子与流式服务，供 WS/Redpanda 链路复用。
架构位置：实时链路门面，聚合 factor（增量计算）与 service（服务编排）。
关键设计：按标的隔离的滑动窗口增量计算；同步/异步双入口与有界缓冲，保证低延迟与可用性。
"""

from hero_quant.stream.factor import IncrementalFactor
from hero_quant.stream.service import StreamService, Tick

__all__ = ["IncrementalFactor", "StreamService", "Tick"]
