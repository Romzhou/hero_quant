# SBOM（CycloneDX 1.5 JSON）

- 归属：PR-153 供应链可交付物；生成器 `scripts/generate_sbom.py`，CI 步骤 `.github/workflows/test.yml`，用例 `tests/test_generate_sbom.py`
- 定位：**物料清单**，回答「这个 commit 的镜像/环境里装了哪些 Python 发行版、各是什么版本」
- 非定位：**不是漏洞门**。已知漏洞靠 `pip-audit`（`.github/workflows/test.yml`，阻塞），密钥靠 gitleaks（`.github/workflows/test.yml`，阻塞）

## 1. 覆盖范围（重要，别拿它当全量清单）

| 纳入 | 不纳入 |
| --- | --- |
| 当前 Python 解释器里 `importlib.metadata.distributions()` 能枚举到的**全部已安装发行版**（含传递依赖） | OS 包（apt/apk/dpkg）、容器基础镜像层、系统库（glibc/openssl 等） |
| 组件字段：`type=library`、PEP 503 规范化的 `name`、`version`、`purl=pkg:pypi/<name>@<version>`、`bom-ref`（= purl） | `frontend/` 的 npm 依赖（package-lock.json 才是其权威清单）、`crates/` 的 Rust 依赖 |
| BOM 主体 `metadata.component` = `hero-quant`（`pkg:pypi/hero-quant@<version>`） | 构建工具链（编译器、node、npm 版本） |

口径说明：

- 运行环境不同 → 清单不同。CI 装的是 `requirements-lock.txt`（hash 校验）+ `pip install -e ".[dev,ashare,us]"` + 少量工具包，本地 venv 与之不完全一致，**组件数不可直接对比**。
- `hero-quant` 自身由 `metadata.component` 描述，故不出现在 `components` 里（避免同一 `bom-ref` 出现两次，见 `scripts/generate_sbom.py`）。
- `metadata.component.version` 是脚本内常量 `COMPONENT_VERSION`（`scripts/generate_sbom.py`），与 `pyproject.toml` 的 `project.version` 同步维护——脚本刻意不解析文件/环境，升版本时两处一起改。

## 2. 本地生成（纯标准库，无需装任何工具）

```bash
# 写文件
python scripts/generate_sbom.py --output sbom.json

# 只想要 stdout（默认 --output -）
python scripts/generate_sbom.py > sbom.json

# 钉住时间戳 → 同环境两次生成字节完全一致（可 diff、可进产物比对）
SOURCE_DATE_EPOCH=1700000000 python scripts/generate_sbom.py --output sbom.json
python scripts/generate_sbom.py --timestamp 2026-01-01T00:00:00Z --output sbom.json
```

参数只有两个（`scripts/generate_sbom.py`）：

- `--output PATH`：默认 `-`（stdout）。
- `--timestamp ISO8601`：默认取环境变量 `SOURCE_DATE_EPOCH`（秒级 epoch，唯一允许读取的环境变量），未设或脏值则用当前 UTC。

退出码：`0` 成功；`1` 写文件失败（stderr 只输出通用原因，如 `failed to write SBOM: No such file or directory`，不回显路径）；`2` `--timestamp` 不是合法 ISO-8601。

确定性来自两处设计：

- `components` 按 `(name, version)` 排序去重（`scripts/generate_sbom.py`），不随 `importlib.metadata` 的枚举顺序抖动；
- `serialNumber = urn:uuid:` + `uuid.uuid5(固定命名空间, 排序后组件 + timestamp)`（`scripts/generate_sbom.py`），**不给随机 uuid4**；同输入必得同一 serial 与同一份字节。

脚本不读 `.env`、配置、DSN、API key，输出不含路径与密钥（`tests/test_generate_sbom.py` 把 `HERO_API_KEY` / `ak-*` / `postgresql://` 等形态塞进 env 再断言零命中）。

## 3. 怎么读这份 JSON

关键字段（CycloneDX 1.5 顶层）：

| 字段 | 含义 |
| --- | --- |
| `bomFormat` / `specVersion` | 恒为 `CycloneDX` / `1.5`；升版本前先确认下游消费方 |
| `serialNumber` | 本 BOM 的稳定标识（`urn:uuid:<v5>`）；组件集合或时间戳变了 serial 就变，可当“清单指纹”用 |
| `metadata.timestamp` | 生成时刻（UTC RFC3339，秒精度） |
| `metadata.tools.components` | 生成工具 `hero-quant-sbom`（自证来源） |
| `metadata.component` | BOM 主体：`hero-quant` 应用及其版本/purl |
| `components[]` | 现存 Python 发行版；每项含 `type/name/version/purl/bom-ref` |

常用查法：

```bash
python - <<'PY'
import json
d = json.load(open("sbom.json", encoding="utf-8"))
print(d["serialNumber"], len(d["components"]))
for c in d["components"]:
    print(c["name"], c["version"])
PY

jq -r '.components[] | "\(.name)==\(.version)"' sbom.json | sort   # 有 jq 时
grep -c '"purl"' sbom.json                                        # 组件数粗算
```

对比两份 BOM（例如合入前后）：

```bash
diff <(jq -r '.components[].purl' before.json | sort) <(jq -r '.components[].purl' after.json | sort)
```

## 4. SBOM ≠ 漏洞扫描

- 本 BOM 只声明**有什么**，不声明**有没有洞**。CVE 判定、可达性、修复建议一律不在本文件里。
- 依赖漏洞门是 `pip-audit`（`.github/workflows/test.yml`，**阻塞**，自 W1-D 起去掉 `|| true`）；对 `setuptools` 的误报处理见 `.github/workflows/test.yml`。
- 密钥泄漏门是 gitleaks（`.github/workflows/test.yml`，**阻塞**）。
- SBOM 步骤本身**不阻塞漏洞**：`generate_sbom.py` 只要枚举成功就 `0`；它红了通常说明脚本/环境坏了，而不是发现了 CVE。
- 需要把 BOM 喂给扫描器时（例如 `grype sbom:./sbom.json`、Dependency-Track），那是**消费方**的事，本仓不引入这类依赖（新依赖一律走 ADR）。

## 5. CI 里的位置与产物

- 顺序：hash 安装 → ruff → 前端 → pytest(cov50) → PG 守卫断言 → setuptools 升级 → `pip-audit` → **SBOM 生成 + 上传**（`.github/workflows/test.yml`）→ gitleaks → Playwright/e2e。
- 放在 `pip-audit` 之后：审计通过才产 BOM，避免为一次注定失败的 run 留垃圾 artifact；放在 gitleaks 之前：`sbom.json` 里本就不该有密钥，gitleaks 顺带覆盖不到时也不影响（它扫的是仓库内容，不是 run 产物）。
- 产物名 `sbom-cyclonedx`，文件 `sbom.json`，`if-no-files-found: error`（没生成成功就红，不给空 artifact）。
- **保留策略由 GitHub 决定**：本仓不写 `retention-days`，走仓库/组织默认（公开仓默认 90 天；具体以 GitHub 文档与仓库设置为准）。需要长期归档时自行下载后存外部制品库，本仓不入库 `sbom.json`。
- 上传只需默认 `permissions: contents: read`（`.github/workflows/test.yml`），故未改权限段。

## 6. 不做什么（边界）

- 不引入 CycloneDX 官方 CLI、syft、trivy、grype 等第三方工具，也不新增 Python 依赖——只用标准库（`argparse`/`importlib.metadata`/`json`/`uuid`/`datetime`），CI 不为此额外联网。
- 不做 OS/容器镜像级 SBOM（那是 `docker build` 侧的产物，需另开专线，且要新增工具依赖）。
- 不把 `sbom.json` 提交进仓库：它是 run 级产物，提交会让每次依赖变动都产生 diff 噪音（`.gitignore` 未加，但请勿入库）。
