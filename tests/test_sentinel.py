"""Tests for Healthcare Sentinel — pure functions, SQL safety, audit trail, CDC, approval flow."""

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import sentinel

HAS_LC = importlib.util.find_spec("langchain_core") is not None
needs_lc = pytest.mark.skipif(not HAS_LC, reason="langchain_core not installed")


# ---------------------------------------------------------------------------
# SQL safety guards
# ---------------------------------------------------------------------------

def test_deny_sql_blocks_drop():
    assert sentinel._DENY_SQL.search("DROP TABLE patients")
    assert sentinel._DENY_SQL.search("drop table patients")

def test_deny_sql_blocks_attach():
    assert sentinel._DENY_SQL.search("ATTACH DATABASE 'x.db'")

def test_deny_sql_blocks_all_drop():
    assert sentinel._DENY_SQL.search("DROP TABLE IF EXISTS _snap_1")
    assert sentinel._DENY_SQL.search("DROP TABLE anything")

def test_allow_starts_accepts_valid():
    for stmt in ["UPDATE t SET x=1", "DELETE FROM t", "INSERT INTO t VALUES(1)",
                 "CREATE TABLE t(x INT)", "ALTER TABLE t ADD COLUMN y INT"]:
        assert any(stmt.upper().startswith(s) for s in sentinel._ALLOW_STARTS), f"rejected: {stmt}"

def test_allow_starts_rejects_select():
    assert not any("SELECT 1".upper().startswith(s) for s in sentinel._ALLOW_STARTS)

def test_protected_sql_blocks_audit():
    assert sentinel._PROTECTED_SQL.search("UPDATE _sentinel_audit SET x=1")
    assert sentinel._PROTECTED_SQL.search("DELETE FROM _sentinel_changelog")
    assert sentinel._PROTECTED_SQL.search("INSERT INTO _sentinel_cdc_u_t")
    assert sentinel._PROTECTED_SQL.search("CREATE TABLE x AS SELECT * FROM sqlite_master")

def test_ident_re_validates():
    assert sentinel._IDENT_RE.match("raw_patients")
    assert sentinel._IDENT_RE.match("mart_demographics")
    assert not sentinel._IDENT_RE.match("'; DROP TABLE --")
    assert not sentinel._IDENT_RE.match("")
    assert not sentinel._IDENT_RE.match("table name with spaces")
    assert not sentinel._IDENT_RE.match("123start")


# ---------------------------------------------------------------------------
# Severity floor
# ---------------------------------------------------------------------------

def test_enforce_severity_raises():
    findings = [{"check_name": "impossible_ages", "severity": "MEDIUM"}]
    result = sentinel.enforce_severity(findings)
    assert result[0]["severity"] == "CRITICAL"

def test_enforce_severity_overrides_to_rule():
    # CLINICAL_RULES says negative_billing = HIGH, so even if LLM says CRITICAL, rule wins
    findings = [{"check_name": "negative_billing", "severity": "CRITICAL"}]
    result = sentinel.enforce_severity(findings)
    assert result[0]["severity"] == "HIGH"

def test_enforce_severity_unknown_check():
    findings = [{"check_name": "custom_check", "severity": "LOW"}]
    result = sentinel.enforce_severity(findings)
    assert result[0]["severity"] == "LOW"

def test_enforce_severity_none_severity():
    result = sentinel.enforce_severity([{"check_name": "impossible_ages", "severity": None}])
    assert result[0]["severity"] == "CRITICAL"  # "NONE" ranks as unknown -> floor raises

def test_enforce_severity_unknown_severity_string():
    result = sentinel.enforce_severity([{"check_name": "negative_billing", "severity": "BANANAS"}])
    assert result[0]["severity"] == "HIGH"

def test_enforce_severity_missing_keys():
    result = sentinel.enforce_severity([{}])
    assert result[0]["severity"] == "MEDIUM"  # defaults unknown to MEDIUM

def test_enforce_severity_empty_and_none():
    assert sentinel.enforce_severity([]) == []
    assert sentinel.enforce_severity(None) == []

def test_enforce_severity_caps_downstream_at_high():
    findings = [
        {"check_name": "impossible_ages", "severity": "CRITICAL", "table": "mart_demographics"},
        {"check_name": "impossible_ages", "severity": "CRITICAL", "table": "staging_patients"},
        {"check_name": "impossible_ages", "severity": "CRITICAL", "table": "raw_patients"},
    ]
    result = sentinel.enforce_severity(findings)
    assert result[0]["severity"] == "HIGH"   # mart_ = downstream, capped
    assert result[1]["severity"] == "HIGH"   # staging_ = downstream, capped
    assert result[2]["severity"] == "CRITICAL"  # raw = source, keeps CRITICAL


def test_enforce_severity_filters_non_dicts():
    result = sentinel.enforce_severity([None, "junk", {"check_name": "impossible_ages", "severity": "LOW"}])
    assert len(result) == 1
    assert result[0]["severity"] == "CRITICAL"


# ---------------------------------------------------------------------------
# File change classification
# ---------------------------------------------------------------------------

def test_classify_new():
    assert sentinel.classify_change(None, {"content_hash": "a"}) == "NEW"

def test_classify_noop():
    fp = {"content_hash": "abc", "head_hash": "h1", "row_count": 10}
    assert sentinel.classify_change(fp, fp) == "NO-OP"

def test_classify_append():
    old = {"content_hash": "a", "head_hash": "h1", "row_count": 10}
    new = {"content_hash": "b", "head_hash": "h1", "row_count": 15}
    assert sentinel.classify_change(old, new) == "APPEND"

def test_classify_update():
    old = {"content_hash": "a", "head_hash": "h1", "row_count": 10}
    new = {"content_hash": "b", "head_hash": "h2", "row_count": 10}
    assert sentinel.classify_change(old, new) == "UPDATE"

def test_classify_same_head_same_rowcount_is_update():
    old = {"content_hash": "a", "head_hash": "h1", "row_count": 10}
    new = {"content_hash": "b", "head_hash": "h1", "row_count": 10}
    assert sentinel.classify_change(old, new) == "UPDATE"

def test_classify_missing_keys_does_not_crash():
    assert sentinel.classify_change({"content_hash": "a"},
                                    {"content_hash": "b", "head_hash": "h", "row_count": 5}) == "UPDATE"

def test_file_fingerprint_empty_file(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_bytes(b"")
    fp = sentinel.file_fingerprint(str(p))
    assert fp["size"] == 0
    assert fp["row_count"] == 1  # b"".split -> [b""]

def test_file_fingerprint_unicode(tmp_path):
    p = tmp_path / "u.csv"
    p.write_text("name,note\nJosé,café ☕\n", encoding="utf-8")
    fp = sentinel.file_fingerprint(str(p))
    assert len(fp["content_hash"]) == 64

def test_file_fingerprint_append_detected_on_large_file(tmp_path):
    p = tmp_path / "big.csv"
    lines = ["col1,col2"] + [f"{i},{i}" for i in range(120)]
    p.write_text("\n".join(lines) + "\n")
    old = sentinel.file_fingerprint(str(p))
    with open(p, "a") as fh:
        fh.write("append1,x\nappend2,y\n")
    new = sentinel.file_fingerprint(str(p))
    assert sentinel.classify_change(old, new) == "APPEND"


# ---------------------------------------------------------------------------
# Findings parser
# ---------------------------------------------------------------------------

def test_parse_findings_json_array():
    text = json.dumps([{"check_name": "test", "severity": "HIGH"}])
    result = sentinel.parse_findings(text)
    assert len(result) == 1
    assert result[0]["check_name"] == "test"

def test_parse_findings_code_block():
    text = "```json\n[{\"check_name\": \"x\"}]\n```"
    result = sentinel.parse_findings(text)
    assert len(result) == 1

def test_parse_findings_empty():
    assert sentinel.parse_findings("") == []
    assert sentinel.parse_findings("no json here") == []

def test_parse_findings_single_dict():
    text = json.dumps({"check_name": "solo"})
    result = sentinel.parse_findings(text)
    assert len(result) == 1

def test_parse_findings_embedded_in_prose():
    text = 'Here are my findings: [{"check_name": "x", "severity": "LOW"}] — done.'
    result = sentinel.parse_findings(text)
    assert len(result) == 1
    assert result[0]["check_name"] == "x"

def test_parse_findings_non_string_input():
    assert sentinel.parse_findings(None) == []
    assert sentinel.parse_findings({"not": "a string"}) == []
    assert sentinel.parse_findings(42) == []

def test_parse_findings_filters_non_dicts():
    result = sentinel.parse_findings('[{"a": 1}, "junk", 3, null]')
    assert result == [{"a": 1}]

def test_parse_findings_bare_code_fence():
    assert sentinel.parse_findings("```") == []


# ---------------------------------------------------------------------------
# Batch approval — choice parsing and interrupt payload parsing
# ---------------------------------------------------------------------------

def test_parse_approval_all_variants():
    for c in ("all", "ALL", " y ", "yes"):
        assert sentinel.parse_approval_choice(c, 3) == {0, 1, 2}

def test_parse_approval_none_variants():
    for c in ("none", "n", "no", "", "   "):
        assert sentinel.parse_approval_choice(c, 3) == set()

def test_parse_approval_indices():
    assert sentinel.parse_approval_choice("1,3", 5) == {0, 2}
    assert sentinel.parse_approval_choice(" 1 , 2 ", 5) == {0, 1}

def test_parse_approval_out_of_range_and_zero():
    assert sentinel.parse_approval_choice("99", 3) == set()
    assert sentinel.parse_approval_choice("0", 3) == set()  # 1-based; 0 invalid

def test_parse_approval_garbage_denies():
    assert sentinel.parse_approval_choice("approve everything", 3) == set()
    assert sentinel.parse_approval_choice("-1", 3) == set()
    assert sentinel.parse_approval_choice("1.5", 3) == set()

def test_parse_approval_mixed_valid_invalid():
    assert sentinel.parse_approval_choice("1,abc,2", 3) == {0, 1}

def test_parse_approval_none_input():
    assert sentinel.parse_approval_choice(None, 3) == set()

def test_extract_actions_normal_batch():
    val = {"action_requests": [{"name": "apply_fix", "arguments": {"table": "t"}},
                               {"name": "add_tags", "arguments": {}}]}
    actions = sentinel.extract_action_requests(val)
    assert len(actions) == 2
    assert actions[0]["name"] == "apply_fix"

def test_extract_actions_empty_list():
    assert sentinel.extract_action_requests({"action_requests": []}) == []

def test_extract_actions_missing_key_falls_back_to_value():
    val = {"name": "apply_fix", "arguments": {}}
    assert sentinel.extract_action_requests(val) == [val]

def test_extract_actions_single_dict_value():
    val = {"action_requests": {"name": "x"}}
    assert sentinel.extract_action_requests(val) == [{"name": "x"}]

def test_extract_actions_malformed_inputs():
    assert sentinel.extract_action_requests(None) == [{}]
    assert sentinel.extract_action_requests("garbage") == [{}]
    val = {"action_requests": None}
    assert sentinel.extract_action_requests(val) == [val]
    # non-dict items coerced so downstream .get() never crashes
    actions = sentinel.extract_action_requests({"action_requests": ["junk", {"name": "ok"}]})
    assert actions == [{}, {"name": "ok"}]
    assert all(isinstance(a, dict) for a in actions)


# ---------------------------------------------------------------------------
# Audit trail — init, apply_fix, undo, history, show
# ---------------------------------------------------------------------------

def _make_test_db():
    """Create a temp DB with a test table."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE test_patients (id INT, name TEXT, age INT)")
    conn.executemany("INSERT INTO test_patients VALUES (?,?,?)",
                     [(1, "Alice", 30), (2, "Bob", -5), (3, "Carol", 200), (4, None, 45)])
    conn.commit()
    conn.close()
    return path

def test_init_audit_db():
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert "_sentinel_audit" in tables
        assert "_sentinel_changelog" in tables
    finally:
        os.unlink(path)

def test_init_audit_db_idempotent():
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        sentinel.init_audit_db(path)  # second call must not raise
        conn = sqlite3.connect(path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(_sentinel_audit)").fetchall()]
        conn.close()
        for c in ("hostname", "ip_address", "run_mode"):
            assert cols.count(c) == 1
    finally:
        os.unlink(path)

def _make_old_schema_audit(path):
    """Create the pre-migration audit table (no hostname/ip_address/run_mode)."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE _sentinel_audit (
        op_id INTEGER PRIMARY KEY,
        run_id TEXT, ts TEXT, op_type TEXT, target_table TEXT,
        sql_text TEXT, rows_affected INTEGER, rationale TEXT,
        approved_by TEXT, snapshot_table TEXT, undone_at TEXT)""")
    conn.execute(
        "INSERT INTO _sentinel_audit (op_id,run_id,ts,op_type,target_table,sql_text,rows_affected,rationale,approved_by) "
        "VALUES (1,'old_run','2025-01-01T00:00:00','correct','test_patients','UPDATE test_patients SET age=NULL',2,'old row','olduser')")
    conn.commit()
    conn.close()

def test_init_audit_db_migrates_old_schema():
    path = _make_test_db()
    try:
        _make_old_schema_audit(path)
        sentinel.init_audit_db(path)
        conn = sqlite3.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(_sentinel_audit)").fetchall()}
        row = conn.execute("SELECT hostname, ip_address, run_mode FROM _sentinel_audit WHERE op_id=1").fetchone()
        conn.close()
        assert {"hostname", "ip_address", "run_mode"} <= cols
        assert row == (None, None, None)  # old row survives with NULL new columns
    finally:
        os.unlink(path)

def test_show_operation_detail_old_row_after_migration(capsys):
    path = _make_test_db()
    try:
        _make_old_schema_audit(path)
        sentinel.init_audit_db(path)
        sentinel.show_operation_detail(path, 1)
        out = capsys.readouterr().out
        assert "Operation #1" in out
        assert "unknown" in out  # NULL hostname/ip/run_mode render as 'unknown'
        assert "olduser" in out
    finally:
        os.unlink(path)

def test_show_operation_detail_denied_row(capsys):
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO _sentinel_audit (run_id,ts,op_type,target_table,sql_text,rows_affected,rationale,approved_by) "
            "VALUES ('r','2025-01-01','correct','test_patients','UPDATE x',0,'r','DENIED:reviewer')")
        conn.commit()
        conn.close()
        sentinel.show_operation_detail(path, 1)
        out = capsys.readouterr().out
        assert "DENIED (not executed)" in out
    finally:
        os.unlink(path)

def _simulate_fix(path, op_id=1, action_type="correct", table="test_patients",
                   sql="UPDATE test_patients SET age=NULL WHERE age<0 OR age>120",
                   reason="Impossible ages", approved_by=None):
    """Simulate apply_fix with CDC triggers (no langchain dependency)."""
    import getpass
    if approved_by is None:
        approved_by = os.getenv("SENTINEL_REVIEWER", getpass.getuser())
    conn = sqlite3.connect(path, timeout=30)
    conn.isolation_level = None
    conn.execute(sentinel._AUDIT_DDL)
    conn.execute(sentinel._CHANGELOG_DDL)
    now = datetime.now().isoformat()
    conn.execute("BEGIN")
    sentinel._create_cdc_triggers(conn, table, op_id, now)
    for stmt in sql.split("|||"):
        conn.execute(stmt.strip())
    sentinel._drop_cdc_triggers(conn, table)
    captured = conn.execute(
        "SELECT COUNT(*) FROM _sentinel_changelog WHERE op_id=? AND change_type IN ('before_update','before_delete','inserted_rowid')",
        (op_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO _sentinel_audit (op_id,run_id,ts,op_type,target_table,sql_text,rows_affected,rationale,approved_by) VALUES (?,?,?,?,?,?,?,?,?)",
        (op_id, "test_run", now, action_type, table, sql, captured, reason, approved_by))
    conn.execute("COMMIT")
    conn.close()
    return captured, 0


def test_apply_fix_and_undo():
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        total, _ = _simulate_fix(path)
        assert total == 2

        conn = sqlite3.connect(path)
        nulls = conn.execute("SELECT COUNT(*) FROM test_patients WHERE age IS NULL").fetchone()[0]
        conn.close()
        assert nulls == 2

        sentinel.undo_operation(path, 1)
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT age FROM test_patients WHERE id=2").fetchone()
        conn.close()
        assert row[0] == -5
    finally:
        os.unlink(path)

def test_undo_nonexistent_op():
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        assert sentinel.undo_operation(path, 999) is False
    finally:
        os.unlink(path)

def test_undo_twice_fails_second_time():
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        _simulate_fix(path)
        assert sentinel.undo_operation(path, 1) is True
        assert sentinel.undo_operation(path, 1) is False  # already undone
    finally:
        os.unlink(path)

def test_undo_without_audit_table():
    path = _make_test_db()
    try:
        assert sentinel.undo_operation(path, 1) is False
    finally:
        os.unlink(path)

def test_undo_no_changelog_entries():
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO _sentinel_audit (run_id,ts,op_type,target_table,sql_text,rows_affected,rationale,approved_by) "
            "VALUES ('r','2025-01-01','correct','test_patients','UPDATE x',0,'r','user')")
        conn.commit()
        conn.close()
        assert sentinel.undo_operation(path, 1) is False
    finally:
        os.unlink(path)

def test_show_operation_detail(capsys):
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        _simulate_fix(path, reason="Negative ages are impossible")
        sentinel.show_operation_detail(path, 1)
        out = capsys.readouterr().out
        assert "Operation #1" in out
        assert "correct" in out
        assert "test_patients" in out
        assert "Negative ages" in out
    finally:
        os.unlink(path)

def test_show_audit_history_with_reviewer(capsys):
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        _simulate_fix(path, action_type="flag",
                      sql="UPDATE test_patients SET age=-1 WHERE age<0",
                      reason="Flag bad ages", approved_by="testuser")
        sentinel.show_audit_history(path)
        out = capsys.readouterr().out
        assert "Reviewer" in out
        assert "ACTIVE" in out
        assert "testuser" in out
    finally:
        os.unlink(path)

def test_show_operation_not_found(capsys):
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        sentinel.show_operation_detail(path, 999)
        out = capsys.readouterr().out
        assert "not found" in out
    finally:
        os.unlink(path)

def test_snapshot_restore_returns_db_to_pre_remediation_state():
    """Known issue 3: after remediation, restoring the snapshot must restore findings."""
    path = _make_test_db()
    snap = path + ".snap"
    try:
        sentinel.init_audit_db(path)
        shutil.copy2(path, snap)
        _simulate_fix(path)  # nulls the 2 impossible ages
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM test_patients WHERE age<0 OR age>120").fetchone()[0] == 0
        conn.close()
        shutil.copy2(snap, path)
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM test_patients WHERE age<0 OR age>120").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM _sentinel_audit").fetchone()[0] == 0
        conn.close()
    finally:
        os.unlink(path)
        if os.path.exists(snap):
            os.unlink(snap)


# ---------------------------------------------------------------------------
# apply_fix — real tool (langchain), CDC, multi-statement, op_id allocation
# ---------------------------------------------------------------------------

def _op_id_from(result: str) -> int:
    m = re.search(r"--undo (\d+)", result)
    assert m, f"no op_id in: {result}"
    return int(m.group(1))

@needs_lc
def test_apply_fix_tool_update_and_undo():
    path = _make_test_db()
    try:
        fix = sentinel.make_apply_fix_tool(path)
        result = fix.invoke({"action_type": "correct", "table": "test_patients",
                             "sql": "UPDATE test_patients SET age=NULL WHERE age<0 OR age>120",
                             "reason": "impossible ages"})
        assert result.startswith("OK:"), result
        assert "2 rows affected" in result
        op_id = _op_id_from(result)
        assert sentinel.undo_operation(path, op_id) is True
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT age FROM test_patients WHERE id=3").fetchone()[0] == 200
        conn.close()
    finally:
        os.unlink(path)

@needs_lc
def test_apply_fix_multi_statement_quarantine_and_undo():
    """Known issue 5: CREATE TABLE + DELETE in one apply_fix call."""
    path = _make_test_db()
    try:
        fix = sentinel.make_apply_fix_tool(path)
        result = fix.invoke({"action_type": "quarantine", "table": "test_patients",
                             "sql": "CREATE TABLE quarantine_null_names AS SELECT * FROM test_patients WHERE name IS NULL "
                                    "||| DELETE FROM test_patients WHERE name IS NULL",
                             "reason": "unidentifiable patients"})
        assert result.startswith("OK:"), result
        op_id = _op_id_from(result)

        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM quarantine_null_names").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM test_patients WHERE name IS NULL").fetchone()[0] == 0
        ctypes = {r[0] for r in conn.execute(
            "SELECT DISTINCT change_type FROM _sentinel_changelog WHERE op_id=?", (op_id,)).fetchall()}
        conn.close()
        assert "created_table" in ctypes
        assert "before_delete" in ctypes

        assert sentinel.undo_operation(path, op_id) is True
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM test_patients WHERE name IS NULL").fetchone()[0] == 1
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quarantine_null_names'").fetchone()
        conn.close()
    finally:
        os.unlink(path)

@needs_lc
def test_apply_fix_alter_plus_update_flag():
    path = _make_test_db()
    try:
        fix = sentinel.make_apply_fix_tool(path)
        result = fix.invoke({"action_type": "flag", "table": "test_patients",
                             "sql": "ALTER TABLE test_patients ADD COLUMN flagged INT DEFAULT 0 "
                                    "||| UPDATE test_patients SET flagged=1 WHERE age<0",
                             "reason": "flag suspect rows"})
        assert result.startswith("OK:"), result
        op_id = _op_id_from(result)
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM test_patients WHERE flagged=1").fetchone()[0] == 1
        ctypes = {r[0] for r in conn.execute(
            "SELECT DISTINCT change_type FROM _sentinel_changelog WHERE op_id=?", (op_id,)).fetchall()}
        conn.close()
        assert "added_column" in ctypes
        assert "before_update" in ctypes
    finally:
        os.unlink(path)

@needs_lc
def test_apply_fix_insert_captured_and_undone():
    path = _make_test_db()
    try:
        fix = sentinel.make_apply_fix_tool(path)
        result = fix.invoke({"action_type": "enrich", "table": "test_patients",
                             "sql": "INSERT INTO test_patients VALUES (5, 'Eve', 28)",
                             "reason": "backfill"})
        assert result.startswith("OK:"), result
        op_id = _op_id_from(result)
        assert sentinel.undo_operation(path, op_id) is True
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM test_patients WHERE id=5").fetchone()[0] == 0
        conn.close()
    finally:
        os.unlink(path)

@needs_lc
def test_apply_fix_denials():
    path = _make_test_db()
    try:
        fix = sentinel.make_apply_fix_tool(path)
        cases = [
            ({"action_type": "correct", "table": "test_patients",
              "sql": "DROP TABLE test_patients", "reason": "r"}, "forbidden"),
            ({"action_type": "correct", "table": "test_patients",
              "sql": "DELETE FROM _sentinel_audit", "reason": "r"}, "protected"),
            ({"action_type": "correct", "table": "test_patients",
              "sql": "SELECT * FROM test_patients", "reason": "r"}, "must start with"),
            ({"action_type": "correct", "table": "bad table; --",
              "sql": "UPDATE t SET x=1", "reason": "r"}, "invalid table name"),
            ({"action_type": "correct", "table": "test_patients",
              "sql": "   ", "reason": "r"}, "empty SQL"),
        ]
        for args, expect in cases:
            result = fix.invoke(args)
            assert result.startswith("DENIED:"), f"{args['sql']} -> {result}"
            assert expect in result, f"{args['sql']} -> {result}"
        # denied ops must leave no audit rows
        conn = sqlite3.connect(path)
        sentinel.init_audit_db(path)
        assert conn.execute("SELECT COUNT(*) FROM _sentinel_audit").fetchone()[0] == 0
        conn.close()
    finally:
        os.unlink(path)

@needs_lc
def test_apply_fix_missing_table_error():
    path = _make_test_db()
    try:
        fix = sentinel.make_apply_fix_tool(path)
        result = fix.invoke({"action_type": "correct", "table": "no_such_table",
                             "sql": "UPDATE no_such_table SET x=1", "reason": "r"})
        assert result.startswith("ERROR:")
        assert "does not exist" in result
    finally:
        os.unlink(path)

@needs_lc
def test_apply_fix_rolls_back_multi_statement_failure():
    path = _make_test_db()
    try:
        fix = sentinel.make_apply_fix_tool(path)
        result = fix.invoke({"action_type": "correct", "table": "test_patients",
                             "sql": "UPDATE test_patients SET age=1 WHERE id=1 "
                                    "||| UPDATE nonexistent_tbl SET x=1",
                             "reason": "partial failure"})
        assert result.startswith("ERROR:"), result
        assert "rolled back" in result
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT age FROM test_patients WHERE id=1").fetchone()[0] == 30
        assert conn.execute("SELECT COUNT(*) FROM _sentinel_audit").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM _sentinel_changelog").fetchone()[0] == 0
        conn.close()
    finally:
        os.unlink(path)

@needs_lc
def test_apply_fix_op_ids_never_collide():
    """Known issue 2: AUTOINCREMENT must hand out distinct op_ids even with gaps."""
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO _sentinel_audit (op_id, run_id) VALUES (40, 'manual')")
        conn.commit()
        conn.close()
        fix = sentinel.make_apply_fix_tool(path)
        r1 = fix.invoke({"action_type": "correct", "table": "test_patients",
                         "sql": "UPDATE test_patients SET name=name WHERE id=1", "reason": "a"})
        r2 = fix.invoke({"action_type": "correct", "table": "test_patients",
                         "sql": "UPDATE test_patients SET name=name WHERE id=1", "reason": "b"})
        id1, id2 = _op_id_from(r1), _op_id_from(r2)
        assert id1 == 41 and id2 == 42
    finally:
        os.unlink(path)

@needs_lc
def test_apply_fix_concurrent_threads_distinct_op_ids():
    """Known issue 2: parallel tool calls must not hit UNIQUE constraint on op_id."""
    path = _make_test_db()
    try:
        sentinel.init_audit_db(path)
        fix = sentinel.make_apply_fix_tool(path)
        results = [None] * 4

        def work(i):
            results[i] = fix.invoke({"action_type": "correct", "table": "test_patients",
                                     "sql": "UPDATE test_patients SET name=name WHERE id=1",
                                     "reason": f"thread {i}"})

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(r.startswith("OK:") for r in results), results
        op_ids = {_op_id_from(r) for r in results}
        assert len(op_ids) == 4
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# _safe_wrap — error containment without breaking tool schemas
# ---------------------------------------------------------------------------

@needs_lc
def test_safe_wrap_catches_runtime_error():
    from langchain_core.tools import tool as lc_tool

    @lc_tool
    def boom(x: int) -> str:
        """Always fails like unregistered structured properties."""
        raise RuntimeError("structured properties not registered")

    wrapped = sentinel._safe_wrap(boom)
    out = wrapped.invoke({"x": 1})
    assert "ERROR executing boom" in out
    assert "structured properties not registered" in out

@needs_lc
def test_safe_wrap_preserves_schema_and_behavior():
    """Known issue 4: wrapped tool must accept the same arguments as the original."""
    from langchain_core.tools import tool as lc_tool

    @lc_tool
    def add_nums(a: int, b: int = 10) -> str:
        """Adds two numbers."""
        return str(a + b)

    wrapped = sentinel._safe_wrap(add_nums)
    assert wrapped.name == "add_nums"
    assert wrapped.description == add_nums.description
    assert wrapped.args == add_nums.args  # schema identical for LLM function-calling
    assert wrapped.invoke({"a": 2, "b": 3}) == "5"
    assert wrapped.invoke({"a": 2}) == "12"  # defaults still work


# ---------------------------------------------------------------------------
# DataHub document memory — recall, upsert, reset
# ---------------------------------------------------------------------------

class _FakeGraph:
    """Stands in for DataHubGraph, returning a documentSearch-shaped payload."""

    def __init__(self, docs=(), error=None):
        self.docs = list(docs)
        self.error = error
        self.deleted = []

    def execute_graphql(self, query, variables=None, **kwargs):
        if self.error:
            raise self.error
        return {"searchAcrossEntities": {"searchResults": [
            {"entity": {"urn": d.get("urn"), "info": {
                "title": d.get("title"),
                "lastModified": {"time": d.get("modified")},
                "contents": {"text": d.get("content")}}}}
            for d in self.docs]}}

    def hard_delete_entity(self, urn):
        self.deleted.append(urn)
        return (1, 0)


def _doc(urn, title, content="x", modified=1):
    return {"urn": urn, "title": title, "content": content, "modified": modified}


def test_fetch_documents_parses_urn_title_content_and_time():
    g = _FakeGraph([_doc("urn:li:document:a", "Sentinel Learnings", "learned things", 42)])
    assert sentinel.fetch_documents(g) == [
        {"urn": "urn:li:document:a", "title": "Sentinel Learnings",
         "content": "learned things", "modified": 42}]

def test_fetch_documents_returns_empty_on_graph_error():
    assert sentinel.fetch_documents(_FakeGraph(error=RuntimeError("gms down"))) == []

def test_fetch_documents_tolerates_null_fields_and_skips_urnless():
    g = _FakeGraph([{"urn": None, "title": "orphan"},
                    {"urn": "urn:li:document:b", "title": None,
                     "content": None, "modified": None}])
    assert sentinel.fetch_documents(g) == [
        {"urn": "urn:li:document:b", "title": "", "content": "", "modified": 0}]

def test_latest_learnings_none_when_no_documents():
    """Regression: an empty search result must not read as 'learnings found'.
    The old code stringified the empty envelope and reported 345 chars."""
    assert sentinel.latest_learnings(_FakeGraph([])) is None

def test_latest_learnings_ignores_other_titles_and_blank_content():
    g = _FakeGraph([_doc("urn:li:document:a", "Sentinel Triage Report", "report body"),
                    _doc("urn:li:document:b", sentinel.LEARNINGS_TITLE, "   ")])
    assert sentinel.latest_learnings(g) is None

def test_latest_learnings_picks_most_recently_modified():
    g = _FakeGraph([_doc("urn:li:document:old", sentinel.LEARNINGS_TITLE, "run 1", 100),
                    _doc("urn:li:document:new", sentinel.LEARNINGS_TITLE, "run 1 + run 2", 200)])
    assert sentinel.latest_learnings(g)["content"] == "run 1 + run 2"

def test_find_sentinel_documents_filters_by_title_prefix():
    g = _FakeGraph([_doc("urn:li:document:a", "Sentinel Learnings"),
                    _doc("urn:li:document:b", "Sentinel Triage Report"),
                    _doc("urn:li:document:c", "Shared"),
                    _doc("urn:li:document:d", "New Document")])
    assert [d["urn"] for d in sentinel.find_sentinel_documents(g)] == [
        "urn:li:document:a", "urn:li:document:b"]

@needs_lc
def test_save_document_upsert_reuses_existing_urn_for_same_title():
    from langchain_core.tools import tool as lc_tool
    seen = {}

    @lc_tool
    def save_document(title: str, content: str, urn: Optional[str] = None) -> str:
        """Saves a document."""
        seen.update(title=title, content=content, urn=urn)
        return "ok"

    g = _FakeGraph([_doc("urn:li:document:old", sentinel.LEARNINGS_TITLE, "run 1", 100),
                    _doc("urn:li:document:new", sentinel.LEARNINGS_TITLE, "run 2", 200)])
    wrapped = sentinel.wrap_save_document_upsert(save_document, g)
    assert wrapped.name == "save_document"
    wrapped.invoke({"title": sentinel.LEARNINGS_TITLE, "content": "run 3"})
    assert seen["urn"] == "urn:li:document:new"  # newest wins, no duplicate created

@needs_lc
def test_save_document_upsert_leaves_new_titles_and_explicit_urns_alone():
    from langchain_core.tools import tool as lc_tool
    seen = {}

    @lc_tool
    def save_document(title: str, content: str, urn: Optional[str] = None) -> str:
        """Saves a document."""
        seen.update(title=title, urn=urn)
        return "ok"

    g = _FakeGraph([_doc("urn:li:document:a", sentinel.LEARNINGS_TITLE, "run 1", 100)])
    wrapped = sentinel.wrap_save_document_upsert(save_document, g)

    wrapped.invoke({"title": "Sentinel Triage Report", "content": "c"})
    assert seen["urn"] is None  # no title match -> create new

    wrapped.invoke({"title": sentinel.LEARNINGS_TITLE, "content": "c", "urn": "urn:li:document:z"})
    assert seen["urn"] == "urn:li:document:z"  # caller's urn is respected

@needs_lc
def test_save_document_upsert_survives_lookup_failure():
    from langchain_core.tools import tool as lc_tool

    @lc_tool
    def save_document(title: str, content: str, urn: Optional[str] = None) -> str:
        """Saves a document."""
        return f"saved urn={urn}"

    wrapped = sentinel.wrap_save_document_upsert(
        save_document, _FakeGraph(error=RuntimeError("gms down")))
    assert wrapped.invoke({"title": sentinel.LEARNINGS_TITLE, "content": "c"}) == "saved urn=None"


# ---------------------------------------------------------------------------
# run_sql tool
# ---------------------------------------------------------------------------

@needs_lc
def test_run_sql_select_and_guards():
    path = _make_test_db()
    try:
        run_sql = sentinel.make_run_sql_tool(path)
        out = json.loads(run_sql.invoke({"query": "SELECT COUNT(*) AS n FROM test_patients"}))
        assert out["rows"][0][0] == 4
        assert run_sql.invoke({"query": "UPDATE test_patients SET age=0"}).startswith("ERROR")
        assert run_sql.invoke({"query": "SELECT 1; DROP TABLE test_patients"}).startswith("ERROR")
        assert run_sql.invoke({"query": ""}).startswith("ERROR")
        assert json.loads(run_sql.invoke({"query": "SELECT 1;"}))["rows"] == [[1]]  # trailing ; OK
    finally:
        os.unlink(path)

@needs_lc
def test_run_sql_caps_rows_at_20():
    path = _make_test_db()
    try:
        conn = sqlite3.connect(path)
        conn.executemany("INSERT INTO test_patients VALUES (?,?,?)",
                         [(100 + i, f"p{i}", 40) for i in range(30)])
        conn.commit()
        conn.close()
        run_sql = sentinel.make_run_sql_tool(path)
        out = json.loads(run_sql.invoke({"query": "SELECT id FROM test_patients"}))
        assert len(out["rows"]) == 20
    finally:
        os.unlink(path)

@needs_lc
def test_run_sql_missing_db_returns_error_string():
    run_sql = sentinel.make_run_sql_tool("/nonexistent/dir/nope.db")
    out = run_sql.invoke({"query": "SELECT 1"})
    assert "ERROR" in out


# ---------------------------------------------------------------------------
# Dry-run pipeline
# ---------------------------------------------------------------------------

def _make_clinical_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE raw_patients (
        name TEXT, age TEXT, gender TEXT,
        date_of_admission TEXT, discharge_date TEXT, billing_amount TEXT)""")
    conn.executemany("INSERT INTO raw_patients VALUES (?,?,?,?,?,?)", [
        ("Alice", "30", "F", "2024-01-01", "2024-01-05", "100.0"),
        ("Bob", "-5", "M", "2024-01-01", "2024-01-02", "50"),
        (None, "40", "M", "2024-01-01", "2024-01-03", "70"),
        ("Dan", "50", "M", "2024-02-01", "2024-01-01", "-20"),
    ])
    conn.execute("CREATE TABLE mart_demographics (name TEXT, age INT)")
    conn.executemany("INSERT INTO mart_demographics VALUES (?,?)", [("Alice", 30), ("Bob", -5)])
    conn.commit()
    conn.close()
    return path

def test_dry_run_pipeline_on_synthetic_db():
    path = _make_clinical_db()
    try:
        datasets, findings, _ = sentinel.dry_run_pipeline(path)
        assert {d["table"] for d in datasets} == {"raw_patients", "mart_demographics"}
        names = {f["check_name"] for f in findings}
        assert {"impossible_ages", "missing_identifiers", "discharge_before_admission",
                "negative_billing", "age_stored_as_text", "propagated_bad_ages"} <= names
        # sorted by severity: CRITICAL first
        assert findings[0]["severity"] == "CRITICAL"
        # lineage contamination fan-out from raw_patients
        ages = next(f for f in findings if f["check_name"] == "impossible_ages")
        assert ages["downstream_contamination"] == ["staging_patients", "mart_billing", "mart_demographics"]
        for f in findings:
            assert f["severity"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
            assert "clinical_impact" in f
    finally:
        os.unlink(path)

def test_dry_run_pipeline_missing_db():
    datasets, findings, actions = sentinel.dry_run_pipeline("/nonexistent/nope.db")
    assert datasets == [] and findings == [] and actions == []

def test_dry_run_pipeline():
    db = "sample-data/healthcare.db"
    if not Path(db).exists():
        pytest.skip("no sample data")
    datasets, findings, _ = sentinel.dry_run_pipeline(db)
    assert len(datasets) > 0
    assert len(findings) > 0
    for f in findings:
        assert f["severity"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        assert "clinical_impact" in f


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def test_generate_report_zero_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "REPORT_DIR", tmp_path)
    p = sentinel.generate_report([], [], [])
    html = Path(p).read_text()
    assert "issues found." in html
    assert "0 rows" in html or ">0<" in html

def test_generate_report_none_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "REPORT_DIR", tmp_path)
    p = sentinel.generate_report(None, None, None, None)
    assert Path(p).exists()

def test_generate_report_malformed_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "REPORT_DIR", tmp_path)
    findings = [
        {},  # all keys missing
        {"severity": None, "affected_rows": None, "description": None},
        {"severity": "bogus", "check_name": "x", "affected_rows": "many"},
        {"severity": "critical", "description": "lower case severity", "affected_rows": 3,
         "downstream_contamination": "not-a-list"},
        "not a dict",
        None,
    ]
    p = sentinel.generate_report([{"table": "t", "row_count": None}, "junk"], findings, [{"bad": 1}, None])
    html = Path(p).read_text()
    assert "CRITICAL" in html  # lowercase severity normalized

def test_generate_report_escapes_html(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "REPORT_DIR", tmp_path)
    findings = [{"severity": "HIGH", "description": "<script>alert(1)</script>",
                 "table": "t", "column": "c", "affected_rows": 1,
                 "clinical_impact": "José's café — unicode ✓"}]
    p = sentinel.generate_report([], findings, [])
    html = Path(p).read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html

def test_generate_report_trace_and_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "REPORT_DIR", tmp_path)
    actions = [{"type": "tag", "table": "raw_patients", "tag": "clinical-severity-CRITICAL"},
               {"type": "description", "text": "warning added"}]
    tool_log = [{"tool": "run_sql", "args": {"query": "SELECT 1"}}, "junk"]
    p = sentinel.generate_report([], [], actions, tool_log)
    html = Path(p).read_text()
    assert "How I Worked" in html
    assert "clinical-severity-CRITICAL" in html


# ---------------------------------------------------------------------------
# Clinical rules coverage
# ---------------------------------------------------------------------------

def test_clinical_rules_complete():
    for check in sentinel.FALLBACK_CHECKS:
        assert check["name"] in sentinel.CLINICAL_RULES, f"missing rule: {check['name']}"

def test_sev_rank_complete():
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        assert sev in sentinel._SEV_RANK
