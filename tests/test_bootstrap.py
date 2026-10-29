import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_package_version_single_source():
    import hero_quant

    # PR-181：pyproject.toml 是唯一版本来源；__version__ 不得再硬编码。
    assert hero_quant.__version__ == _pyproject_version()


def test_dockerfile_version_not_hardcoded():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG HERO_QUANT_VERSION" in dockerfile
    assert 'org.opencontainers.image.version="${HERO_QUANT_VERSION}"' in dockerfile
    # 防止把字面版本写回 OCI label（0.2.0 曾与 pyproject 0.3.0 漂移）。
    assert not re.search(r'org\.opencontainers\.image\.version="\d', dockerfile)
