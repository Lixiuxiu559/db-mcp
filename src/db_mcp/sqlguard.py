"""SQL 语句安全分析。

**这是本服务最关键的安全点**，不要只靠 `sql.upper().startswith("SELECT")` 判定
只读——那种写法会被下面这些绕过：

    WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x   -- data-modifying CTE
    SELECT 1; DROP TABLE t                                   -- 多语句
    SELECT ... INTO new_table                                -- 建表
    SELECT ... FOR UPDATE                                    -- 加锁
    SELECT pg_read_file('/etc/passwd')                       -- 读服务器文件
    /* DROP TABLE t */ SELECT 1                              -- 注释里的迷惑项

因此这里的做法是**先做词法扫描**（正确剥离注释、单引号字符串、双引号标识符、
PostgreSQL 的美元引号字符串），把 SQL 归一成 token 序列后再分类。

需要说明的边界：词法分析无法判定运行期行为（如存储过程内部做了什么），
所以它只是**快速失败 + 给 LLM 可读报错**的第一道防线。真正的兜底是
PostgreSQL 层面——read 实例的查询一律跑在**只读事务**里（见 db/pool.py），
数据库自己会拒绝任何写操作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 允许作为"只读语句"起头的关键字
READ_STARTERS = frozenset({"SELECT", "WITH", "SHOW", "EXPLAIN", "DESCRIBE", "DESC", "TABLE", "VALUES"})

# 会修改数据的 DML
_WRITE_KEYWORDS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "COPY"})

# 会修改结构/权限/统计信息的 DDL 与维护命令
_DDL_KEYWORDS = frozenset(
    {
        "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "COMMENT",
        "VACUUM", "REINDEX", "CLUSTER", "REFRESH", "LOCK", "ANALYZE",
    }
)

# 其他有副作用或会改会话状态的语句
_OTHER_MUTATING = frozenset(
    {
        "SET", "RESET", "CALL", "DO", "DISCARD", "NOTIFY", "LISTEN", "UNLISTEN",
        "IMPORT", "SECURITY", "PREPARE", "EXECUTE", "DEALLOCATE", "DECLARE", "FETCH",
    }
)

# 需要 confirm=true 二次确认的高危结构变更
HIGH_RISK_STARTERS = frozenset({"DROP", "TRUNCATE", "ALTER"})

# 只读实例同样要拒绝的危险函数：能读服务器文件、能搞垮实例
_FUNCTION_DENYLIST = frozenset(
    {
        "PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_LS_DIR", "PG_LS_LOGDIR",
        "PG_LS_WALDIR", "PG_STAT_FILE", "LO_IMPORT", "LO_EXPORT",
        "PG_SLEEP", "PG_SLEEP_FOR", "PG_SLEEP_UNTIL",
        "DBLINK", "PG_TERMINATE_BACKEND", "PG_CANCEL_BACKEND",
    }
)

_MUTATING = _WRITE_KEYWORDS | _DDL_KEYWORDS | _OTHER_MUTATING

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUM_RE = re.compile(r"\d+(?:\.\d*)?")
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")

# 归一化占位符：字符串 / 双引号标识符 / 数字，都不是关键字，不可能撞上关键字集合
_STR = "<str>"
_IDENT = "<ident>"
_NUM = "<num>"


@dataclass(frozen=True)
class SqlAnalysis:
    """一条 SQL 的静态分析结果。"""

    sql: str
    first: str
    is_read: bool
    is_write: bool  # 明确的写语句（DML/DDL/会话变更），用于区分"这是写"与"被拒的只读"
    is_high_risk: bool
    reason: str  # 拒绝/需确认的原因（已可读），正常语句为空串


def tokenize(sql: str) -> list[str]:
    """把 SQL 归一成显著 token 序列（关键字大写，标点保留原字符）。

    丢弃空白与注释；字符串/美元引号内容归一为 ``<str>``，双引号标识符归一为
    ``<ident>``，数字归一为 ``<num>`` —— 这样"关键字"只可能来自真实的语法位置，
    `SELECT 'DROP TABLE t'` 不会被误判成写操作。
    """
    tokens: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

        if ch.isspace():
            i += 1
            continue

        # 行注释
        if sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue

        # 块注释（PostgreSQL 支持嵌套）
        if sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth > 0:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            continue

        # 美元引号字符串 $tag$ ... $tag$（未闭合则吞到结尾）
        match = _DOLLAR_TAG_RE.match(sql, i)
        if match:
            tag = match.group(0)
            end = sql.find(tag, match.end())
            i = n if end < 0 else end + len(tag)
            tokens.append(_STR)
            continue

        # 单引号字符串（'' 表示一个引号）
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            tokens.append(_STR)
            continue

        # 双引号标识符（"" 表示一个引号）
        if ch == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            tokens.append(_IDENT)
            continue

        match = _IDENT_RE.match(sql, i)
        if match:
            tokens.append(match.group(0).upper())
            i = match.end()
            continue

        match = _NUM_RE.match(sql, i)
        if match:
            tokens.append(_NUM)
            i = match.end()
            continue

        tokens.append(ch)
        i += 1

    return tokens


def _has_multiple_statements(tokens: list[str]) -> bool:
    """除末尾分号外还存在分号，即多语句（含 `SELECT 1;;` 这类空语句）。"""
    for idx, token in enumerate(tokens):
        if token == ";" and idx != len(tokens) - 1:
            return True
    return False


def _has_for_share(tokens: list[str]) -> bool:
    """`SELECT ... FOR SHARE` 会加锁，只读实例不应执行。

    `FOR UPDATE` 无需单独判断——UPDATE 已在写关键字集合中。
    """
    return "SHARE" in tokens and "FOR" in tokens


# EXPLAIN 的可选参数（去掉这些后，剩下的才是被 EXPLAIN 的语句本身）
_EXPLAIN_OPTIONS = frozenset({"ANALYZE", "VERBOSE", "COSTS", "SETTINGS", "BUFFERS",
                              "WAL", "TIMING", "SUMMARY", "FORMAT", "TRUE", "FALSE",
                              "ON", "OFF", "TEXT", "JSON", "YAML", "XML"})


def _strip_explain(tokens: list[str]) -> list[str]:
    """剥离 EXPLAIN 前缀，返回被解释的语句 token。

    `EXPLAIN ANALYZE DELETE FROM t` 会**真的执行删除**，所以不能因为看到
    EXPLAIN 就放行——必须看它后面跟的是什么语句。这里剥掉 EXPLAIN 与其选项，
    把内层语句交给同一套写关键字检查。
    """
    idx = 1
    while idx < len(tokens) and tokens[idx] in _EXPLAIN_OPTIONS:
        idx += 1
    return tokens[idx:]


def _scan_keywords(tokens: list[str]) -> set[str]:
    """扫描 token 序列里的关键字，**跳过 `AS` 之后的别名**。

    这一步不可省：`SELECT x AS comment` 里的 COMMENT 是列别名而非 DDL 关键字，
    若不排除，一条正常的只读查询会被误判成写操作（实测踩到）。
    `AS` 后面只可能是别名/类型名，而真正的写语句（UPDATE/DELETE/INSERT）
    不会紧跟在 AS 之后，所以这个排除不会放过任何真实写入。
    """
    keywords: set[str] = set()
    prev = ""
    for token in tokens:
        if prev != "AS":
            keywords.add(token)
        prev = token
    return keywords


def analyze(sql: str) -> SqlAnalysis:
    """分析一条 SQL：是否只读、是否写、是否高危、拒绝原因。

    只做静态判定，不执行。调用方依据 is_read / is_write / is_high_risk 决定放行或报错。
    """
    stripped = sql.strip()
    if not stripped:
        return _reject(sql, "", "SQL 语句不能为空")

    tokens = tokenize(stripped)
    if not tokens:
        # 整条语句都是注释
        return _reject(sql, "", "SQL 语句不能为空（只有注释）")

    if _has_multiple_statements(tokens):
        return _reject(
            sql, tokens[0],
            "不允许多条语句一次执行（检测到语句间分号）。请拆成多次调用。",
        )

    first = tokens[0]
    # EXPLAIN 的检查对象是它后面那条语句，不是 EXPLAIN 本身
    inner = _strip_explain(tokens) if first == "EXPLAIN" else tokens
    if not inner:
        return _reject(sql, first, "EXPLAIN 后面缺少要分析的语句")
    inner_first = inner[0]

    token_set = _scan_keywords(inner)

    denied = sorted(token_set & _FUNCTION_DENYLIST)
    if denied:
        return _reject(
            sql, inner_first,
            f"语句包含被禁用的函数: {', '.join(denied)}"
            "（可读服务器文件或影响实例稳定性）。",
        )

    mutating = sorted(token_set & _MUTATING)
    # SELECT ... INTO new_table 会建表，token 里没有写关键字，需单独判
    select_into = inner_first == "SELECT" and "INTO" in token_set
    for_share = _has_for_share(inner)

    is_write = bool(mutating) or select_into or for_share
    is_read = inner_first in READ_STARTERS and not is_write

    reason = ""
    if not is_read:
        if mutating:
            reason = f"语句包含写操作关键字: {', '.join(mutating)}"
        elif select_into:
            reason = "SELECT ... INTO 会创建新表，属于写操作"
        elif for_share:
            reason = "SELECT ... FOR SHARE 会对行加锁，属于写操作"
        elif inner_first not in READ_STARTERS:
            reason = f"语句以 {inner_first} 开头，不在允许的只读语句范围内"
        else:
            reason = "语句不是只读语句"

    return SqlAnalysis(
        sql=sql,
        first=first,
        is_read=is_read,
        is_write=is_write,
        is_high_risk=inner_first in HIGH_RISK_STARTERS,
        reason=reason,
    )


def _reject(sql: str, first: str, reason: str) -> SqlAnalysis:
    """构造一条"被拒绝"的分析结果（既非只读也非合法写）。"""
    return SqlAnalysis(
        sql=sql, first=first, is_read=False, is_write=False,
        is_high_risk=False, reason=reason,
    )
