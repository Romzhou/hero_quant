"""hero_quant.api — HTTP 边界与安全能力。

职责：暴露 FastAPI 入口（server）与安全辅助（security），统一处理请求追踪、安全头与指标。
架构位置：系统对外网关层，位于 Agent/工具之上、前端 SPA 之前。
关键设计：最小 CSP 与回环 Host 白名单防护；X-Request-ID 透传与 OTel 占位；Prometheus 指标与 wall-time 预算观测。
"""
