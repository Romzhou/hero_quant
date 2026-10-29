"""真英雄量化 (hero-quant) —— 极简投研 Agent。

职责：顶层包入口，提供版本号与项目标识；架构位置：根包，被所有子包依赖。
设计决策：版本号唯一来源是 `pyproject.toml`（PR-181）。源码树内（editable /
`PYTHONPATH=src`）直接读 pyproject，保证本地与 CI 永远同源；只有装在
site-packages 的 wheel 才回退到 dist 元数据（构建产物的版本快照）。
"""

from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"

if _pyproject.is_file():
    import tomllib

    __version__ = tomllib.loads(_pyproject.read_text(encoding="utf-8"))["project"]["version"]
else:
    try:
        __version__ = _dist_version("hero-quant")
    except PackageNotFoundError:  # 既不在源码树也无安装元数据：显式未知，不猜版本
        __version__ = "0.0.0+unknown"
