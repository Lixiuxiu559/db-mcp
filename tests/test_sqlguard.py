"""sqlguard 测试：只读判定、写操作识别、绕过手段防护。

这是本服务最关键的安全模块，用例覆盖参考 TS 版"只靠前缀匹配"会漏掉的所有场景。
"""

from __future__ import annotations

import pytest

from db_mcp.sqlguard import analyze, tokenize


def test_plain_select_is_read():
    a = analyze("SELECT * FROM users")
    assert a.is_read and not a.is_write and not a.is_high_risk


@pytest.mark.parametrize(
    "sql",
    [
        "select 1",
        "  SELECT id FROM t WHERE a = 'x'  ",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT 1",
        "EXPLAIN ANALYZE SELECT 1",
        "SHOW search_path",
        "TABLE users",
        "VALUES (1), (2)",
        "SELECT $$ DROP $$",  # 美元引号里的内容不是关键字
        "SELECT 'DROP TABLE t'",  # 字符串字面量里的内容不是关键字
        'SELECT "select", "update" FROM t',  # 引号标识符里的内容不是关键字
        "/* DROP TABLE t */ SELECT 1",  # 注释里的内容不是关键字
        "-- DELETE FROM t\nSELECT 1",
        # AS 后跟的是别名，不是关键字（曾把 pg_describe_table 的 cast/alias 误判为 DDL）
        "SELECT x AS comment, y AS drop FROM t",
        "SELECT col::text AS create FROM t",
    ],
)
def test_read_statements(sql):
    assert analyze(sql).is_read, sql


@pytest.mark.parametrize(
    "sql,expect_write",
    [
        ("INSERT INTO t VALUES (1)", True),
        ("UPDATE t SET a = 1", True),
        ("DELETE FROM t", True),
        # data-modifying CTE：参考实现的 startswith("WITH") 会放过它
        ("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", True),
        ("WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x", True),
        ("SELECT * INTO new_table FROM t", True),  # 建表
        ("SELECT * FROM t FOR UPDATE", True),
        ("SELECT * FROM t FOR SHARE", True),  # 加锁
        ("SELECT pg_read_file(chr(47))", False),  # 危险函数：非写但被禁
        ("SELECT pg_sleep(100)", False),
        ("SET search_path TO evil", True),
        ("CREATE TABLE t (a int)", True),
        ("DROP TABLE t", True),
        ("TRUNCATE t", True),
        ("ALTER TABLE t ADD c int", True),
        ("GRANT ALL ON t TO public", True),
        ("COPY t FROM STDIN", True),
        ("VACUUM t", True),
        # EXPLAIN ANALYZE 会真的执行内层语句，必须按内层语句判定
        ("EXPLAIN ANALYZE DELETE FROM t", True),
        ("EXPLAIN ANALYZE INSERT INTO t VALUES (1)", True),
    ],
)
def test_write_and_dangerous_statements(sql, expect_write):
    a = analyze(sql)
    assert not a.is_read, sql
    assert a.is_write == expect_write, f"{sql}: reason={a.reason}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE t",
        "SELECT 1; SELECT 2",
        "SELECT 1;;",
        "INSERT INTO t VALUES (1); DROP TABLE t",
    ],
)
def test_multi_statement_rejected(sql):
    a = analyze(sql)
    assert not a.is_read and not a.is_write
    assert "多条语句" in a.reason


@pytest.mark.parametrize("sql", ["DROP TABLE t", "TRUNCATE t", "ALTER TABLE t ADD c int"])
def test_high_risk_flag(sql):
    a = analyze(sql)
    assert a.is_high_risk and a.is_write


def test_empty_and_comment_only():
    assert not analyze("").is_read
    assert "不能为空" in analyze("").reason
    assert "不能为空" in analyze("   /* nothing */  ").reason


def test_tokenize_strips_comments_and_literals():
    tokens = tokenize("SELECT /* c */ 'x', \"y\" -- tail")
    assert tokens[0] == "SELECT"
    assert "C" not in tokens
    assert tokens.count("<str>") == 1
    assert "<ident>" in tokens


def test_tokenize_nested_block_comment():
    tokens = tokenize("SELECT /* a /* b */ c */ 1")
    assert tokens == ["SELECT", "<num>"]
