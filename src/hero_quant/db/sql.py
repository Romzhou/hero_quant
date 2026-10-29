"""db.sql — 迁移脚本语句切分（支持注释 / 引号 / dollar-quote）。

为什么需要：psycopg 扩展协议不允许一次执行多条语句，而迁移脚本必须能写
`DO $$ ... $$` 与 PL/pgSQL 函数体（体内含分号）。直接 `sql.split(";")` 会把
函数体切碎并破坏事务，所以这里做最小但正确的词法扫描：

- `--` 行注释：整行丢弃（保留空行不影响）
- `'...'` / `"..."`：引号内的 `;` 不切分，`''` / `""` 视为转义
- `$tag$...$tag$`：dollar-quote 内的 `;` 不切分（支持 `$$` 与 `$func$`）

返回的语句已去掉首尾空白与纯注释片段；空脚本返回空列表。
"""

from __future__ import annotations

import re

_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def split_sql_statements(sql: str) -> list[str]:
    """把一段 SQL 文本切成可逐条执行的语句列表（不改变语义）。"""
    if not isinstance(sql, str):
        raise TypeError("sql must be a str")

    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)

    def flush() -> None:
        text = "".join(buf).strip()
        if text and not _is_comment_only(text):
            statements.append(text)
        buf.clear()

    while i < n:
        ch = sql[i]

        # 行注释：整行跳过（换行保留，避免把下一行拼到当前 token 上）
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            if end == -1:
                i = n
            else:
                i = end + 1
            buf.append("\n")
            continue

        # 单引号字符串：'' 是转义单引号
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue

        # 双引号标识符："" 是转义
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue

        # dollar-quote：$tag$ ... $tag$
        if ch == "$":
            match = _DOLLAR_TAG_RE.match(sql, i)
            if match is not None:
                tag = match.group(0)
                end = sql.find(tag, match.end())
                if end == -1:
                    # 未闭合：交给数据库报语法错，不在这里静默吞掉
                    buf.append(sql[i:])
                    i = n
                    continue
                end += len(tag)
                buf.append(sql[i:end])
                i = end
                continue

        if ch == ";":
            flush()
            i += 1
            continue

        buf.append(ch)
        i += 1

    flush()
    return statements


def _is_comment_only(text: str) -> bool:
    """判断片段是否只由 `--` 注释组成（切分后可能残留注释块）。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return False
    return True


__all__ = ["split_sql_statements"]
