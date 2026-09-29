# 测试门现状报告（大白话版）

结论：现在 5 道硬门都开着，能拦低级错误，但 coverage 50 太松、源码断言和 Fake 池是最大坑。

## 现在有哪些门

1. ruff：只查 `src`，规则只有 E/F/W，还放过长行（E501）。
2. coverage：`--cov-fail-under=50`，不到 50% 就挂。
3. 前端三件套：typecheck + build + vitest，阻塞式，缺一个都不行。
4. pip-audit：依赖漏洞，阻塞式。
5. gitleaks：密钥扫描，阻塞式。另外 Playwright e2e 是非阻塞，仅参考。

## 现状评价

- ruff：太松。够拦语法错，拦不住 bug（比如空 except、醒目并发坑）。
- coverage 50：太松。刚及格线，新代码不写测试也能混过去。
- 前端三件套：够用。有坑：本地没装依赖会全挂，本次就是例子。
- pip-audit/gitleaks：够用，继续保持。
- e2e 非阻塞：有坑，只能看不能拦，上线前容易漏。

## 升级排序（先升哪个）

1. 先改源码断言测试（如 test_api_server_harden.py 读 server.py 找字符串）。它只查“字在不在”，改个变量名就误报/漏报，最骗人。
2. 再补 Fake 池的真 PG 路径。现在 Fake 池只测“假水管通不通”，真 SQL 语法错、字段错全测不出。
3. 再升 coverage 50→70。分三步：50→55（先把新代码覆盖）→60（补核心 billing/checkpoint）→70（补 agent loop）。每步修一次 CI 数字，别一次跳。
4. 最后加 ruff 规则（B bugbear、I 排序、N 命名），坑最小。

## 三个重点怎么改

1. 源码断言改行为断言：别 `read_text` 查 `"resolve()" in src`，改成真实发请求：用 TestClient 请求 `/../etc/passwd` 看是否 404/400；prometheus 指标不开时调接口看不崩。测“行为”，不测“字”。
2. Fake 池补真 PG：保留 Fake 做快测，CI 里已有真 PG（test.yml 起了 postgres:16）。加一条标记 `pg_real` 的用例：连 `HERO_CHECKPOINT_DSN` 真建表、真 insert、真 select 断言，别 mock。本地没库就 skip，CI 必跑。
3. coverage 分步走：每次只涨 5%，对应补一批用例，CI 数字同步改，避免一次涨太多全红。
