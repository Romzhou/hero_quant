// ============================================================================
// hero-quant 单机容量压测（PR-302）——k6 脚本
//
// 用法：
//   k6 run scripts/perf/loadtest.js                          # 默认打本机 http://127.0.0.1:8899
//   BASE_URL=https://quant.example.com k6 run scripts/perf/loadtest.js
//   SCENARIOS=backtest VUS=5 DURATION=2m k6 run scripts/perf/loadtest.js
//
// 环境变量：
//   BASE_URL    目标地址（默认 http://127.0.0.1:8899）
//   INSECURE=1  自签证书/本地 CA 时开启（默认校验，避免把 TLS 问题测成性能问题）
//   AUTH_TOKEN  开启身份后有值时注入 Authorization: Bearer
//   SCENARIOS   逗号分隔，只跑其中几个：health,query_sync,query_stream,backtest,risk
//   TICKER / QUERY  回测标的与问答文本
//   VUS / DURATION  仅 exploration 用的粗调钩子
//
// 诚实边界：
//   1) **不要拿本脚本在无 LLM key 的环境出的数字当容量基线**——Hero 会走 _FakeLLM
//      降级路径，响应带 degraded:true，延迟不代表真实 LLM 链路。脚本会统计 degraded
//      比例（`degraded_ratio`），压测报告里必须先看它。
//   2) k6 会缓冲响应体，SSE 场景只能验证**连接建立与首字节**，不能度量流式帧延迟；
//      真实 SSE 长连接行为要用浏览器/e2e 观测，别在这里编数据。
// ============================================================================

import http from "k6/http";
import { check, group, sleep } from "k6";
import { Rate, Trend } from "k6/metrics";

const BASE = (__ENV.BASE_URL || "http://127.0.0.1:8899").replace(/\/$/, "");
const INSECURE = (__ENV.INSECURE || "0") === "1";
const AUTH_TOKEN = __ENV.AUTH_TOKEN || "";
const TICKER = __ENV.TICKER || "600519.SH";
const QUERY = __ENV.QUERY || "回测 600519.SH 近一月等权";
const WANTED = (__ENV.SCENARIOS || "health,query_sync,query_stream,backtest,risk").split(",");
const VUS = parseInt(__ENV.VUS || "10", 10);

const degradedRatio = new Rate("degraded_ratio"); // 降级响应占比：先看它再谈性能
const healthTrend = new Trend("health_ready_seconds");

const headers = {
  Accept: "application/json",
  "User-Agent": "hero-quant-loadtest/0.1.0 (k6)",
};
if (AUTH_TOKEN) {
  headers["Authorization"] = `Bearer ${AUTH_TOKEN}`;
}

function enabled(name) {
  return WANTED.map((s) => s.trim()).includes(name);
}

function request(label, path, params = {}) {
  const res = http.get(`${BASE}${path}`, { headers, timeout: "120s", ...params });
  // 降级是**显式**的：body 里的 degraded:true 必须被计数，不能只看 HTTP 200 就说成功。
  let degraded = false;
  try {
    const body = typeof res.body === "string" ? res.body : "";
    if (body.includes('"degraded":true') || body.includes('"degraded": true')) {
      degraded = true;
    }
  } catch (e) {
    // body 非 JSON（HTML/占位页）时不做降级判定，交由 status check 暴露
  }
  degradedRatio.add(degraded);
  check(res, {
    [`${label} status 200/206`]: (r) => r.status === 200 || r.status === 206,
    [`${label} not 503-front-missing`]: (r) => r.status !== 503,
  });
  return res;
}

export const options = {
  insecureSkipTLSVerify: INSECURE,
  noConnectionReuse: false,
  scenarios: {
    // 健康检查：给容量基线一个"纯净"参考，也证明 /ready 探针在负载下不假绿
    health: {
      executor: "constant-vus",
      vus: 2,
      duration: __ENV.DURATION || "3m",
      tags: { scenario: "health" },
    },
    // 同步问答：主链路（LLM），预期是最容易被限流/超时的场景
    query_sync: {
      executor: "ramping-vus",
      startTime: "30s",
      startVUs: 0,
      stages: [
        { duration: "1m", target: Math.max(1, Math.floor(VUS / 2)) },
        { duration: "3m", target: VUS },
        { duration: "1m", target: 0 },
      ],
      tags: { scenario: "query_sync" },
    },
    // SSE：验证连接建立能力，不做帧级延迟度量（见文件头说明）
    query_stream: {
      executor: "ramping-vus",
      startTime: "60s",
      startVUs: 0,
      stages: [
        { duration: "1m", target: Math.max(1, Math.floor(VUS / 4)) },
        { duration: "3m", target: Math.max(1, Math.floor(VUS / 2)) },
        { duration: "1m", target: 0 },
      ],
      tags: { scenario: "query_stream" },
    },
    // 回测：重计算，关注 CPU/内存与 PG 连接
    backtest: {
      executor: "ramping-vus",
      startTime: "90s",
      startVUs: 0,
      stages: [
        { duration: "1m", target: Math.max(1, Math.floor(VUS / 4)) },
        { duration: "3m", target: Math.max(1, Math.floor(VUS / 2)) },
        { duration: "1m", target: 0 },
      ],
      tags: { scenario: "backtest" },
    },
    risk: {
      executor: "constant-vus",
      startTime: "90s",
      vus: 2,
      duration: "2m",
      tags: { scenario: "risk" },
    },
  },
  thresholds: {
    // 目标是"单机不雪崩"，不是绝对快；数值待首次基线后回填到 docs/perf-baseline.md
    "http_req_duration{scenario:health}": ["p(95)<1000"],
    "http_req_failed{scenario:query_sync}": ["rate<0.10"],
    "http_req_failed{scenario:backtest}": ["rate<0.10"],
    degraded_ratio: ["rate<0.50"], // 无 key 时全场 degraded，此时应看 historical 说明
  },
};

export function setup() {
  console.log(`target=${BASE} insecure=${INSECURE} auth=${AUTH_TOKEN ? "yes" : "no"}`);
  console.log("提醒：无 LLM key 时 _FakeLLM 会接管，延迟不具参考性，先看 degraded_ratio");
  return { started: Date.now() };
}

export default function () {
  const scenario = __ENV.K6_SCENARIO || "default";

  if (scenario === "health") {
    const res = http.get(`${BASE}/ready`, { headers, timeout: "10s" });
    healthTrend.add(res.timings.duration / 1000);
    // 这里不断言 200：/ready 在压测当下降级是**待观测事实**，由 http_req_failed 计数呈现。
    return;
  }

  if (scenario === "query_sync" && enabled("query_sync")) {
    group("sync query", () => {
      request("query", `/v1/query?q=${encodeURIComponent(QUERY)}`);
    });
    sleep(1);
    return;
  }

  if (scenario === "query_stream" && enabled("query_stream")) {
    group("stream query", () => {
      // SSE：只验证握手与首字节，k6 不流式消费，别拿这里的数字当帧延迟
      const res = http.get(`${BASE}/v1/query/stream?q=${encodeURIComponent(QUERY)}`, {
        headers,
        timeout: "60s",
      });
      check(res, {
        "stream handshake ok": (r) => r.status === 200,
        "stream content-type": (r) =>
          (r.headers["Content-Type"] || "").toLowerCase().includes("text/event-stream"),
      });
    });
    sleep(1);
    return;
  }

  if (scenario === "backtest" && enabled("backtest")) {
    group("backtest metrics", () => {
      request("backtest", `/v1/backtest/metrics.json?ticker=${encodeURIComponent(TICKER)}`);
    });
    sleep(1);
    return;
  }

  if (scenario === "risk" && enabled("risk")) {
    group("risk summary", () => {
      request("risk", "/v1/risk/summary");
    });
    sleep(2);
    return;
  }

  // 兜底：脚本被直接 `k6 run` 且无 scenario 标签时跑一遍主链路
  request("query", `/v1/query?q=${encodeURIComponent(QUERY)}`);
  sleep(1);
}

export function teardown(data) {
  console.log(`done in ${Math.round((Date.now() - data.started) / 1000)}s`);
}
