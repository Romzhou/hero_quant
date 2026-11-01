# 密钥托管与轮换（PR-151 · Phase 1）

> 状态：**抽象 + env 后端已落地；真 Vault / KMS 后端未实现**。
> 配套代码：`src/hero_quant/secrets/`（base / env_provider / manager / rotation）。
> env 入口：`HERO_SECRETS_BACKEND`（默认 `env`），见 `src/hero_quant/config/settings.py`。

## 1. 组件

| 模块 | 职责 |
|---|---|
| `secrets/base.py` | `SecretsProvider` ABC + `SecretValue`（repr/日志只出指纹与长度，真值只经 `reveal()`） |
| `secrets/env_provider.py` | `EnvSecretsProvider`：逻辑名 → env 变量映射集中登记；`rotation: manual`、`persistence: env_file` |
| `secrets/manager.py` | `get_secrets_provider()`：`env` 可用；`vault`/`kms` 抛 `UnsupportedSecretsBackendError`；未知值 `ValueError` |
| `secrets/rotation.py` | 轮换记账：`begin_rotation` / `complete_rotation` / `rotation_status`，只记版本/时间/指纹 |
| `secrets/resolve.py` | **业务侧唯一取密钥入口**：`resolve_llm_api_key()` / `resolve_secret()` / `secret_value()`；非 env 后端返回 `(None, reason)`，不回落 env |

逻辑名 → 环境变量（唯一登记处，新增密钥先改 `SECRET_ENV_VARS` 再改本文）：

| 逻辑名 | 变量 | 双读槽位变量 |
|---|---|---|
| `llm_api_key` | `HERO_API_KEY` | `HERO_API_KEY_NEXT` |
| `cohere_api_key` | `COHERE_API_KEY` | `COHERE_API_KEY_NEXT` |
| `openai_api_key` | `OPENAI_API_KEY` | `OPENAI_API_KEY_NEXT` |
| `deepseek_api_key` | `DEEPSEEK_API_KEY` | `DEEPSEEK_API_KEY_NEXT` |
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY_NEXT` |
| `claude_api_key` | `CLAUDE_API_KEY` | `CLAUDE_API_KEY_NEXT` |
| `pii_encryption_key` | `PII_ENCRYPTION_KEY` | `PII_ENCRYPTION_KEY_NEXT` |
| `pg_password` | `HERO_PG_PASSWORD` | `HERO_PG_PASSWORD_NEXT` |
| `redis_password` | `HERO_REDIS_PASSWORD` | `HERO_REDIS_PASSWORD_NEXT` |

## 2. 轮换状态机（三态）

```
unknown ──begin_rotation──> rotating ──complete_rotation──> stable
  (无记录)                  (双读窗口·新旧并存)              (新版本生效·旧版本待撤)
```

- `begin_rotation(name)`：要求 `next` 槽已由运维配好新值，且指纹 ≠ 当前值；只开窗口，**不动版本**。
  缺 `_NEXT` 或指纹相同 → `SecretRotationError`（拒绝记账，防止"假轮换"）。
- `complete_rotation(name)`：要求主槽当前值的指纹 == 窗口期 `pending_fingerprint`（即新值**真的**被切到主变量），
  才 `version+1`、写 `rotated_at` / `previous_version`。没切就报 `SecretRotationError`，不许口头报完成。
- `rotation_status(name)` → `{name, state, version, rotated_at, previous_version, persistence, fingerprint, pending_fingerprint}`。

## 3. env 后端轮换 runbook（双读窗口）

以 `HERO_API_KEY` 为例（其它密钥同构）：

1. **备新值**：在 LLM 供应商侧创建新 key，**不要**立刻删旧 key。
2. **开窗口**：把新值写进 `HERO_API_KEY_NEXT`（旧值仍在 `HERO_API_KEY` 生效），重启/重载进程。
   ```bash
   python -c "from hero_quant.secrets import begin_rotation; print(begin_rotation('llm_api_key'))"
   # -> state=rotating, pending_fingerprint=<新值指纹>, version 不变
   ```
3. **验证新值**：在窗口期内用新 key 打一次真实调用（见第 4 节）。失败 → 撤 `_NEXT`、回到第 1 步，
   状态停在 `rotating`，**不要**调 `complete_rotation`。
4. **切主槽**：把 `HERO_API_KEY` 的值换成新值，重载进程（此时 `HERO_API_KEY` 与 `HERO_API_KEY_NEXT` 同值）。
5. **记账**：
   ```bash
   python -c "from hero_quant.secrets import complete_rotation; print(complete_rotation('llm_api_key'))"
   # -> state=stable, version+1, previous_version=<旧版本>, rotated_at=<UTC>
   ```
   若第 4 步没做，此步抛 `SecretRotationError`（指纹不匹配）。
6. **撤旧**：确认新值稳定后，移除 `HERO_API_KEY_NEXT`，并在供应商侧吊销旧 key。

## 4. 验证步骤

```bash
# 4.1 后端与能力声明（不输出任何密钥值）
python -c "from hero_quant.secrets import get_secrets_provider as g; p=g(); print(p.describe())"
# 期望：backend=env, degraded=False, capabilities.rotation=manual, persistence=env_file

# 4.2 取到/取不到
python -c "from hero_quant.secrets import get_secrets_provider as g; print(g().get_secret('llm_api_key'))"
# 期望：SecretValue(name='llm_api_key', ..., length=N, fingerprint='xxxxxxxxxxxx')  ← 不含真值

# 4.3 fail-closed：vault 未实现
HERO_SECRETS_BACKEND=vault python -c "from hero_quant.secrets import get_secrets_provider as g; g()"
# 期望：UnsupportedSecretsBackendError（不会静默拿到 .env 明文值）

# 4.4 状态落盘（var/secrets_state.json 只含版本/时间/指纹）
python -c "from hero_quant.secrets import rotation_status as r; print(r('llm_api_key'))"
grep -c 'sk-' var/secrets_state.json   # 期望 0 —— 状态文件里不该出现任何密钥片段
```

单测：`pytest -q tests/test_pr151_secrets.py`（脱敏、fail-closed、三处联动、轮换记账全覆盖）。

## 5. 未覆盖范围（必读，不许说"已托管"）

1. **生产 `.env` 仍是明文密钥**。本批只加了抽象层与记账，**没有**把任何密钥搬出 `.env` / 进程环境。
   `EnvSecretsProvider.capabilities()` 里 `persistence: "env_file"` 就是这个事实的显式声明。
   `.env` 的文件权限、备份、容器镜像层、CI 日志残留等风险**全部照旧**。
2. **真 Vault / KMS 后端不存在**。`vault` / `kms` 只在 `manager.py` 里登记为保留字，命中即抛
   `UnsupportedSecretsBackendError`；没有 client、没有鉴权、没有租约/自动续期。
   在它们真正接入前，任何"密钥已托管/已自动轮换"的说法都是假的。
3. **无自动轮换**。env 后端 `rotation: "manual"`：轮换是上面第 3 节的人工 runbook；
   没有定时触发、没有到期告警、没有"密钥年龄"度量。
4. **无删除/吊销能力**。代码只记账，不调供应商 API；旧 key 的吊销在 runbook 第 6 步由人执行。
5. **未接审计链**。轮换事件目前只走 `logging` / 可选 `structlog`（字段只有 name/state/version/fingerprint），
   **没有**写入 `hero_quant/audit` 的既有审计存储。
   代价说明：接审计要动 audit 的 schema 与写入路径（跨线），且审计存储本身可能含 PII 通道，
   需要一并定脱敏口径与失败语义（写审计失败算不算轮换失败）；本批不做，留后续批次。
6. **双读窗口只是"能并存"，不是"自动双读"**。窗口期内新旧值谁生效、业务是否真的同时接受两者，
   取决于调用方接线——业务侧目前只读主槽（`resolve_secret` 走 `slot="current"`），
   `*_NEXT` 仍需人工切换。
7. **仍未接线的读取点**（这两条本批故意不动，别当成已完成）：
   - `api/middleware/pii.py` 仍直接读 `Settings().pii_encryption_key`（PII 加密口令走同一条 env 路径，
     但改它要连带定义"取不到 key 时是否拒绝加密/明文放行"的语义，属另一个 PR）；
   - PG / Redis 口令仍由 DSN 组装（`settings.py` 的 env 网关），`pg_password`/`redis_password`
     两个逻辑名暂无业务调用方；
   - `security/credentials.py` 是另一套「凭据引用」解析（`${VAR}` / 文件凭据），未接 provider。

### 5.1 已接线的读取点（本批）

| 位置 | 说明 |
|---|---|
| `llm/factory.py::LLMFactory.create` | 显式入参 > provider(`HERO_API_KEY`) > `Settings.api_key` > provider(`OPENAI_API_KEY`) > `Settings.openai_api_key` > provider 专属（deepseek/anthropic）；strict 缺 key 抛 `ValueError` 且带 reason |
| `llm/gateway.py::LLMGateway.from_settings` / `_env_key` | primary 与回退链的 key 都经 provider；不再 `os.environ.get` |
| `agent/embed.py::_try_openai` | `openai_api_key` 经 provider（env 后端下回退 Settings 同源字段） |
| `memory/rerank.py::CohereReranker` | `cohere_api_key` 同上 |

行为差异只有一条：`HERO_SECRETS_BACKEND=vault|kms` 时上面每一处都拿不到 key（即使 `.env` 里有），
表现为既有降级路径（`_FakeLLM` / 空 key 跳过）**而不是**偷偷用明文——这是刻意的 fail-closed。

## 6. 铁律

- 密钥值永不进日志、永不进 git、永不进状态文件；日志只能是指纹（sha256 前 12 位）+ 长度。
- 拿不到就说拿不到：provider 缺失返回 `None`，不抛、不造值、不用占位符冒充。
- 降级/不可用必须显式：`vault`/`kms` 抛明确异常，不静默回落 env。
- 轮换状态文件的 `persistence` 字段必须如实：写不进去就是 `memory`，不假装已落盘。
