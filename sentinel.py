#!/usr/bin/env python3
"""Healthcare Sentinel — Clinical Data Triage Agent.

A true AI agent that autonomously discovers healthcare datasets in DataHub,
generates and runs quality checks, reasons about clinical severity, traces
lineage contamination, and writes findings back to DataHub.

Uses DataHub Agent Context Kit for metadata discovery and writeback.
Uses Claude for autonomous decision-making and clinical reasoning.
"""

import argparse
import getpass
import hashlib
import html as html_mod
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATAHUB_SERVER = os.getenv("DATAHUB_GMS_URL", "http://localhost:8080")
SQLITE_DB = os.getenv("HEALTHCARE_DB", "sample-data/healthcare.db")
REPORT_DIR = Path("report")
MODEL = os.getenv("SENTINEL_MODEL", "claude-haiku-4-5")
_CURRENT_RUN_MODE = "interactive"  # set in main() based on args

# ---------------------------------------------------------------------------
# Terminal formatting (ANSI colors)
# ---------------------------------------------------------------------------

_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

def _rgb(r, g, b, text):
    return f"\033[38;2;{r};{g};{b}m{text}\033[0m" if _USE_COLOR else str(text)

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else str(text)

_BOLD = lambda t: _c("1", t)
_DIM = lambda t: _rgb(122, 122, 130, t)
_RED = lambda t: _rgb(239, 68, 68, t)
_GREEN = lambda t: _rgb(40, 200, 64, t)
_YELLOW = lambda t: _rgb(212, 168, 67, t)
_BLUE = lambda t: _rgb(24, 144, 255, t)
_CYAN = lambda t: _rgb(24, 144, 255, t)
_GRAY = lambda t: _rgb(122, 122, 130, t)

_SQL_TOOLS = {"run_sql"}

def _tool_color(name):
    return _GREEN if name in _SQL_TOOLS else _BLUE


def _urn_table(urn):
    parts = urn.split(".")
    last = parts[-1] if len(parts) > 1 else urn
    return last.rstrip(")").split(",")[0] if "," in last else last


def _condense_tool_args(name, args):
    if name == "run_sql":
        q = args.get("query", "")
        return f'"{q[:70]}{"..." if len(q) > 70 else ""}"'
    if name in ("list_schema_fields", "get_lineage"):
        return _urn_table(str(args.get("urn", "")))
    if name == "report_finding":
        return f'{args.get("check_name", "?")} on {args.get("table", "?")}'
    if name == "add_tags":
        tags = args.get("tag_urns", args.get("tags", []))
        if isinstance(tags, list):
            return str([str(t).split(":")[-1] for t in tags])[:60]
        return str(tags)[:60]
    if name == "update_description":
        return _urn_table(str(args.get("entity_urn", "")))
    if name == "save_document":
        return f'"{args.get("title", "?")}"'
    if name == "search_documents":
        q = args.get("query", "*")
        return f'"{q}"'
    return json.dumps(args, default=str)[:60]


def _condense_tool_result(name, content):
    if name == "run_sql":
        m = re.search(r'\[\[(\d+)\]\]', content) or re.search(r'"rows"\s*:\s*\[\s*\[\s*(\d+)', content)
        if m:
            return m.group(1)
        rows = content.count('[["') + content.count("[[")
        if rows > 1:
            return f"{rows} rows"
        return content[:50]
    if name == "list_schema_fields":
        count = content.count('"fieldPath"')
        return f"{count} columns" if count else content[:40]
    if name == "search":
        m = re.search(r'"total"\s*:\s*(\d+)', content)
        return f"{m.group(1)} results" if m else content[:40]
    if name == "get_lineage":
        m = re.search(r'"total"\s*:\s*(\d+)', content)
        return f"{m.group(1)} downstream" if m else content[:40]
    if name in ("add_tags", "update_description"):
        return "done" if "urn:" in content else content[:40]
    if name == "save_document":
        return "saved" if ("urn:" in content or "success" in content.lower()) else content[:40]
    if name == "report_finding":
        return "recorded"
    if name == "search_documents":
        m = re.search(r'"total"\s*:\s*(\d+)', content)
        return f"{m.group(1)} docs" if m else content[:40]
    if name == "get_entities":
        return "loaded" if "urn:" in content else content[:40]
    return content[:50]


# ---------------------------------------------------------------------------
# Tool: report_finding — agent reports each discovered issue
# ---------------------------------------------------------------------------

def make_report_finding_tool():
    from langchain_core.tools import tool

    @tool
    def report_finding(check_name: str, table: str, column: str, severity: str,
                       affected_rows: int, description: str, clinical_impact: str,
                       recommendation: str) -> str:
        """Report a data quality finding. Call this for EVERY issue you discover,
        whether or not you can fix it. This is how findings enter the triage report.
        Severity: CRITICAL, HIGH, MEDIUM, or LOW."""
        return f"Finding recorded: {check_name} on {table}.{column} ({severity}, {affected_rows} rows)"

    return report_finding


# ---------------------------------------------------------------------------
# Tool: run_sql — the agent's window into actual row data
# ---------------------------------------------------------------------------

def make_run_sql_tool(db_path: str):
    from langchain_core.tools import tool

    @tool
    def run_sql(query: str) -> str:
        """Run a read-only SELECT query against the local healthcare SQLite database.
        Table names match the last segment of the DataHub URN (e.g. raw_patients).
        Returns up to 20 rows as JSON. Use COUNT(*) for issue counts, LIMIT 3 for samples.
        You may call this multiple times in parallel in a single turn."""
        q = (query or "").strip().rstrip(";")
        if not q.upper().startswith("SELECT") or ";" in q:
            return "ERROR: only single SELECT statements allowed"
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        except Exception as e:
            return f"SQL ERROR: cannot open database: {e}"
        try:
            cur = conn.execute(q)
            if cur.description is None:
                return "SQL ERROR: query produced no result set"
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(20)
            return json.dumps({"columns": cols, "rows": rows}, default=str)[:2000]
        except Exception as e:
            return f"SQL ERROR: {e}"
        finally:
            conn.close()

    return run_sql


# ---------------------------------------------------------------------------
# Tool: apply_fix — data remediation with snapshot + audit trail
# ---------------------------------------------------------------------------

_DENY_SQL = re.compile(r'\b(DROP\s+TABLE|ATTACH|DETACH|PRAGMA\s+(?!table_info))\b', re.I)
_ALLOW_STARTS = ('UPDATE', 'DELETE', 'INSERT', 'CREATE TABLE', 'ALTER TABLE')
_PROTECTED_SQL = re.compile(r'(_sentinel_audit|_sentinel_changelog|_sentinel_cdc_|sqlite_master|sqlite_sequence|sqlite_temp_master)', re.I)
_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

_CREATE_RE = re.compile(r'^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["\[`]?(\w+)', re.I)
_ALTER_RE = re.compile(r'^ALTER\s+TABLE\s+["\[`]?(\w+)["\]`]?\s+ADD\s+(?:COLUMN\s+)?["\[`]?(\w+)', re.I)
_DML_RE = re.compile(r'^(?:UPDATE\s+(?:OR\s+\w+\s+)?|DELETE\s+FROM\s+|INSERT\s+(?:OR\s+\w+\s+)?INTO\s+)["\[`]?(\w+)', re.I)

_AUDIT_DDL = """CREATE TABLE IF NOT EXISTS _sentinel_audit (
        op_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT, ts TEXT, op_type TEXT, target_table TEXT,
        sql_text TEXT, rows_affected INTEGER, rationale TEXT,
        approved_by TEXT, snapshot_table TEXT, undone_at TEXT,
        hostname TEXT, ip_address TEXT, run_mode TEXT
    )"""

_AUDIT_MIGRATE_COLS = [
    ("hostname", "TEXT"),
    ("ip_address", "TEXT"),
    ("run_mode", "TEXT"),
]

_CHANGELOG_DDL = """CREATE TABLE IF NOT EXISTS _sentinel_changelog (
        change_id INTEGER PRIMARY KEY AUTOINCREMENT,
        op_id INTEGER,
        target_table TEXT,
        change_type TEXT,
        row_id INTEGER,
        row_data TEXT,
        ts TEXT
    )"""


def _create_cdc_triggers(conn, tbl: str, op_id: int, ts: str):
    """Row-level CDC: capture before-images of exactly the rows each statement touches."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info([{tbl}])").fetchall()]
    pairs = ", ".join("'{}', OLD.[{}]".format(c.replace("'", "''"), c) for c in cols)
    ins = ("INSERT INTO _sentinel_changelog (op_id, target_table, change_type, row_id, row_data, ts) "
           f"VALUES ({int(op_id)}, '{tbl}', '{{ct}}', {{rid}}, {{data}}, '{ts}')")
    conn.execute(f"CREATE TRIGGER [_sentinel_cdc_u_{tbl}] BEFORE UPDATE ON [{tbl}] BEGIN "
                 + ins.format(ct='before_update', rid='OLD.rowid', data=f"json_object({pairs})") + "; END")
    conn.execute(f"CREATE TRIGGER [_sentinel_cdc_d_{tbl}] BEFORE DELETE ON [{tbl}] BEGIN "
                 + ins.format(ct='before_delete', rid='OLD.rowid', data=f"json_object({pairs})") + "; END")
    conn.execute(f"CREATE TRIGGER [_sentinel_cdc_i_{tbl}] AFTER INSERT ON [{tbl}] BEGIN "
                 + ins.format(ct='inserted_rowid', rid='NEW.rowid', data='NULL') + "; END")


def _drop_cdc_triggers(conn, tbl: str):
    for p in ('u', 'd', 'i'):
        conn.execute(f"DROP TRIGGER IF EXISTS [_sentinel_cdc_{p}_{tbl}]")


def _get_device_context() -> dict:
    """Collect forensic device info for audit trail."""
    hn = socket.gethostname()
    try:
        ip = socket.gethostbyname(hn)
    except socket.gaierror:
        ip = "unknown"
    return {"hostname": hn, "ip_address": ip, "reviewer": os.getenv("SENTINEL_REVIEWER", "reviewer")}


def init_audit_db(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(_AUDIT_DDL)
        conn.execute(_CHANGELOG_DDL)
        # Migrate existing tables — add new columns if missing
        existing = {r[1] for r in conn.execute("PRAGMA table_info(_sentinel_audit)").fetchall()}
        for col_name, col_type in _AUDIT_MIGRATE_COLS:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE _sentinel_audit ADD COLUMN {col_name} {col_type}")
        conn.commit()
    finally:
        conn.close()


def execute_fix(db_path: str, action_type: str, table: str, sql: str, reason: str) -> str:
    """Core remediation logic — no langchain dependency. Used by both the tool and dry-run."""
    if not _IDENT_RE.match((table or "").strip()):
        return "DENIED: invalid table name."
    table = table.strip()
    stmts = [s.strip() for s in (sql or "").split('|||') if s.strip()]
    if not stmts:
        return "DENIED: empty SQL."
    for stmt in stmts:
        if _DENY_SQL.search(stmt):
            return "DENIED: SQL contains forbidden operation."
        if _PROTECTED_SQL.search(stmt):
            return "DENIED: SQL targets a protected sentinel table."
        if not any(stmt.upper().startswith(s) for s in _ALLOW_STARTS):
            return "DENIED: SQL must start with UPDATE, DELETE, INSERT, or CREATE TABLE."

    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.isolation_level = None
    except Exception as e:
        return f"ERROR: cannot open database: {e}"

    try:
        conn.execute(_AUDIT_DDL)
        conn.execute(_CHANGELOG_DDL)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return f"ERROR: table {table} does not exist."
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        now = datetime.now().isoformat()

        instrumented = set()
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO _sentinel_audit (run_id,ts,op_type,target_table,sql_text,rows_affected,rationale,approved_by) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, now, action_type, table, sql, 0, reason, "pending"))
        op_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for stmt in stmts:
            m = _CREATE_RE.match(stmt)
            if m:
                new_tbl = m.group(1)
                conn.execute(stmt)
                conn.execute("INSERT INTO _sentinel_changelog (op_id,target_table,change_type,row_id,row_data,ts) VALUES (?,?,?,?,?,?)",
                             (op_id, new_tbl, 'created_table', None, None, now))
                continue
            if stmt.upper().startswith('ALTER'):
                m = _ALTER_RE.match(stmt)
                if not m:
                    raise ValueError("only ALTER TABLE ... ADD COLUMN is supported")
                alt_tbl, col = m.group(1), m.group(2)
                conn.execute(stmt)
                conn.execute("INSERT INTO _sentinel_changelog (op_id,target_table,change_type,row_id,row_data,ts) VALUES (?,?,?,?,?,?)",
                             (op_id, alt_tbl, 'added_column', None, json.dumps({"column": col}), now))
                if alt_tbl in instrumented:
                    _drop_cdc_triggers(conn, alt_tbl)
                    _create_cdc_triggers(conn, alt_tbl, op_id, now)
                continue
            m = _DML_RE.match(stmt)
            if not m or not _IDENT_RE.match(m.group(1)):
                raise ValueError(f"cannot determine target table of: {stmt[:60]}")
            dml_tbl = m.group(1)
            if dml_tbl not in instrumented:
                _create_cdc_triggers(conn, dml_tbl, op_id, now)
                instrumented.add(dml_tbl)
            conn.execute(stmt)

        for t in instrumented:
            _drop_cdc_triggers(conn, t)
        captured = conn.execute(
            "SELECT COUNT(*) FROM _sentinel_changelog WHERE op_id=? AND change_type IN ('before_update','before_delete','inserted_rowid')",
            (op_id,)).fetchone()[0]

        ctx = _get_device_context()
        conn.execute(
            "UPDATE _sentinel_audit SET rows_affected=?, approved_by=?, hostname=?, ip_address=?, run_mode=? WHERE op_id=?",
            (captured, ctx["reviewer"], ctx["hostname"], ctx["ip_address"], _CURRENT_RUN_MODE, op_id))
        conn.execute("COMMIT")
        return f"OK: {action_type} on {table} — {captured} rows affected. Changelog: {captured} row-level before-images captured. Undo: python sentinel.py --undo {op_id}"
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return f"ERROR: {e}. No changes made (rolled back)."
    finally:
        conn.close()


def make_apply_fix_tool(db_path: str):
    from langchain_core.tools import tool

    @tool
    def apply_fix(action_type: str, table: str, sql: str, reason: str) -> str:
        """Execute a data remediation on the healthcare database with row-level change capture.

        action_type: 'correct' | 'standardize' | 'quarantine' | 'flag' | 'deduplicate' | 'enrich'
        table: target table name
        sql: SQL statement(s) to execute, separated by ||| for multi-step operations
        reason: clinical justification for this fix

        Before-images of only the affected rows are captured to _sentinel_changelog for full reversibility.
        Every operation is logged to _sentinel_audit. Undo with: python sentinel.py --undo <op_id>"""
        return execute_fix(db_path, action_type, table, sql, reason)

    return apply_fix


def undo_operation(db_path: str, op_id: int):
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.isolation_level = None
    except Exception as e:
        print(f"  ERROR: cannot open database: {e}")
        return False
    try:
        try:
            row = conn.execute("SELECT op_type, target_table, undone_at FROM _sentinel_audit WHERE op_id=?", (op_id,)).fetchone()
        except sqlite3.OperationalError:
            print(f"  ERROR: no audit table found. No operations to undo.")
            return False
        if not row:
            print(f"  ERROR: operation {op_id} not found.")
            return False
        op_type, table, undone_at = row
        if undone_at:
            print(f"  ERROR: operation {op_id} was already undone at {undone_at}.")
            return False
        try:
            entries = conn.execute(
                "SELECT change_id, target_table, change_type, row_id, row_data FROM _sentinel_changelog WHERE op_id=? ORDER BY change_id DESC",
                (op_id,)).fetchall()
        except sqlite3.OperationalError:
            entries = []
        if not entries:
            print(f"  ERROR: no changelog entries found for operation {op_id}; cannot undo.")
            return False

        table_cols = {}

        def cols_of(t):
            if t not in table_cols:
                table_cols[t] = {r[1] for r in conn.execute(f"PRAGMA table_info([{t}])").fetchall()}
            return table_cols[t]

        conn.execute("BEGIN")
        restored = removed = dropped = 0
        skipped_cols = []
        for change_id, tgt, ctype, rid, rdata in entries:
            if not tgt or not _IDENT_RE.match(tgt):
                raise ValueError(f"invalid table name in changelog entry {change_id}")
            if ctype == 'created_table':
                conn.execute(f"DROP TABLE IF EXISTS [{tgt}]")
                table_cols.pop(tgt, None)
                dropped += 1
            elif ctype == 'added_column':
                col = (json.loads(rdata) if rdata else {}).get("column", "?")
                skipped_cols.append(f"{tgt}.{col}")
            elif ctype in ('inserted_rowid', 'before_update', 'before_delete'):
                if not cols_of(tgt):
                    raise ValueError(f"table {tgt} no longer exists; cannot undo")
                if ctype == 'inserted_rowid':
                    conn.execute(f"DELETE FROM [{tgt}] WHERE rowid=?", (rid,))
                    removed += 1
                else:
                    data = json.loads(rdata)
                    cols = [c for c in data if c in cols_of(tgt)]
                    if ctype == 'before_update':
                        if cols:
                            sets = ", ".join(f"[{c}]=?" for c in cols)
                            conn.execute(f"UPDATE [{tgt}] SET {sets} WHERE rowid=?", [data[c] for c in cols] + [rid])
                    else:  # before_delete — reinsert with original rowid
                        col_list = ", ".join(['rowid'] + [f"[{c}]" for c in cols])
                        ph = ", ".join(['?'] * (len(cols) + 1))
                        conn.execute(f"INSERT INTO [{tgt}] ({col_list}) VALUES ({ph})", [rid] + [data[c] for c in cols])
                    restored += 1
        conn.execute("UPDATE _sentinel_audit SET undone_at=? WHERE op_id=?", (datetime.now().isoformat(), op_id))
        conn.execute("COMMIT")
        parts = [f"{n} {label}" for n, label in
                 ((restored, "rows restored"), (removed, "inserted rows removed"), (dropped, "tables dropped")) if n]
        print(f"  UNDONE: operation {op_id} ({op_type} on {table}). {', '.join(parts) or 'no row changes'}.")
        if skipped_cols:
            print(f"  NOTE: SQLite cannot drop columns; left in place (values reset): {', '.join(skipped_cols)}")
        return True
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        print(f"  ERROR during undo: {e}")
        return False
    finally:
        conn.close()


def show_audit_history(db_path: str):
    try:
        conn = sqlite3.connect(db_path, timeout=30)
    except Exception as e:
        print(f"  ERROR: cannot open database: {e}")
        return
    try:
        try:
            rows = conn.execute("SELECT op_id, ts, op_type, target_table, rows_affected, rationale, undone_at, approved_by FROM _sentinel_audit ORDER BY op_id").fetchall()
        except sqlite3.OperationalError:
            print("  No audit history found.")
            return
        if not rows:
            print("  No operations recorded.")
            return
        print(f"\n  {'ID':>4}  {'Time':>19}  {'Type':<14}  {'Table':<20}  {'Rows':>6}  {'Reviewer':<14}  {'Status':<8}  Reason")
        print(f"  {'—'*4}  {'—'*19}  {'—'*14}  {'—'*20}  {'—'*6}  {'—'*14}  {'—'*8}  {'—'*26}")
        for r in rows:
            status = "UNDONE" if r[6] else "ACTIVE"
            reviewer = str(r[7] or "?")[:14]
            print(f"  {r[0] if r[0] is not None else '?':>4}  {str(r[1] or '')[:19]:>19}  {str(r[2] or ''):<14}  "
                  f"{str(r[3] or ''):<20}  {r[4] if r[4] is not None else 0:>6}  {reviewer:<14}  {status:<8}  {str(r[5] or '')[:30]}")
    finally:
        conn.close()


def show_operation_detail(db_path: str, op_id: int):
    try:
        conn = sqlite3.connect(db_path, timeout=30)
    except Exception as e:
        print(f"  ERROR: cannot open database: {e}")
        return
    try:
        try:
            row = conn.execute(
                "SELECT op_id, run_id, ts, op_type, target_table, sql_text, rows_affected, rationale, "
                "approved_by, undone_at, hostname, ip_address, run_mode "
                "FROM _sentinel_audit WHERE op_id=?", (op_id,)).fetchone()
        except sqlite3.OperationalError:
            print("  No audit history found.")
            return
        if not row:
            print(f"  Operation {op_id} not found.")
            return
        oid, run_id, ts, op_type, table, sql_text, rows_affected, rationale, approved_by, undone_at, hostname, ip_addr, run_mode = row
        status = f"UNDONE at {undone_at}" if undone_at else "ACTIVE"
        denied = str(approved_by or "").startswith("DENIED:")

        print(f"\n  {'='*60}")
        print(f"  Operation #{oid}")
        print(f"  {'='*60}")
        print(f"  Time:       {ts}")
        print(f"  Type:       {op_type}")
        print(f"  Table:      {table}")
        print(f"  Rows:       {rows_affected}")
        print(f"  Reviewer:   {approved_by}")
        print(f"  Hostname:   {hostname or 'unknown'}")
        print(f"  IP:         {ip_addr or 'unknown'}")
        print(f"  Run mode:   {run_mode or 'unknown'}")
        print(f"  Status:     {'DENIED (not executed)' if denied else status}")
        print(f"\n  SQL:")
        for stmt in (sql_text or "").split("|||"):
            s = stmt.strip()
            if s:
                print(f"    {s}")
        print(f"\n  Rationale:")
        print(f"    {rationale or 'none'}")

        if not denied:
            try:
                stats = conn.execute(
                    "SELECT change_type, COUNT(*) FROM _sentinel_changelog WHERE op_id=? GROUP BY change_type ORDER BY change_type",
                    (op_id,)).fetchall()
            except sqlite3.OperationalError:
                stats = []
            if stats:
                print(f"\n  Changelog:  {sum(c for _, c in stats):,} entries")
                for ctype, cnt in stats:
                    print(f"    {ctype:<16} {cnt:,}")
            else:
                print(f"\n  Changelog:  no entries")
            if table and _IDENT_RE.match(table) and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                curr_count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
                print(f"  Current:    {curr_count:,} rows in {table}")
        print(f"  {'='*60}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Watch mode — file watcher with change detection
# ---------------------------------------------------------------------------

def file_fingerprint(path: str) -> dict:
    p = Path(path)
    content = p.read_bytes()
    st = p.stat()
    lines = content.split(b'\n')
    # Schema sig = header + first 50 data rows (stable across appends)
    head_lines = min(51, max(1, len(lines) // 2))
    head = b'\n'.join(lines[:head_lines])
    return {
        "path": str(p),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "row_count": len(lines),
        "content_hash": hashlib.sha256(content).hexdigest(),
        "head_hash": hashlib.sha256(head).hexdigest(),
    }


def classify_change(old: dict | None, new: dict) -> str:
    if not old:
        return "NEW"
    if old.get("content_hash") == new.get("content_hash"):
        return "NO-OP"
    if old.get("head_hash") == new.get("head_hash") and new.get("row_count", 0) > old.get("row_count", 0):
        return "APPEND"
    return "UPDATE"


WATCH_EXTENSIONS = ("*.csv", "*.db", "*.sqlite", "*.xlsx", "*.xls")


def _scan_data_files(watch_dir: str) -> dict:
    files = {}
    for ext in WATCH_EXTENSIONS:
        for f in Path(watch_dir).glob(ext):
            try:
                files[str(f)] = file_fingerprint(str(f))
            except OSError:
                continue
    return files


def watch_loop(watch_dir: str, run_fn, interval: int = 10):
    known = {}
    exts = ", ".join(WATCH_EXTENSIONS)
    print(f"\n  [Watch] Monitoring {watch_dir} for ({exts}) every {interval}s. Ctrl+C to stop.\n")

    # Initial scan
    try:
        known = _scan_data_files(watch_dir)
    except Exception as e:
        print(f"  [Watch] Initial scan error: {e}")
    print(f"  [Watch] Tracking {len(known)} existing files.\n")

    while True:
        try:
            time.sleep(interval)
            try:
                current = _scan_data_files(watch_dir)
            except Exception as e:
                print(f"  [Watch] Scan error: {e}")
                continue
            changes = []
            for path, fp in current.items():
                change = classify_change(known.get(path), fp)
                if change != "NO-OP":
                    changes.append((path, change))
            # Detect deletions
            for path in set(known) - set(current):
                print(f"  [Watch] DELETED: {Path(path).name}")
            known = current
            if changes:
                for path, change_type in changes:
                    print(f"  [Watch] {change_type}: {Path(path).name}")
                try:
                    run_fn()
                except Exception as e:
                    print(f"  [Watch] Triage run failed: {e}")
            else:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"  [Heartbeat] {ts} — no changes detected.")
        except KeyboardInterrupt:
            print("\n  [Watch] Stopped.")
            break


# ---------------------------------------------------------------------------
# Agent system prompt — phased mission for reliable autonomous behavior
# ---------------------------------------------------------------------------

# Architecture decision: specialist agents with distinct expertise catch issues
# a single generalist misses. Each specialist's narrow focus produces deeper
# investigation — a clinical safety agent won't overlook patient harm indicators
# that a generalist might deprioritize in favor of more obvious schema issues.
# Adversarial verification eliminates false positives before remediation.

def _load_skill(name: str) -> str:
    path = Path(__file__).parent / "skills" / name
    return path.read_text() if path.exists() else ""

SPECIALISTS = [
    {"name": "Clinical Safety", "skill": "clinical_safety.md", "icon": "CS"},
    {"name": "Financial Integrity", "skill": "financial_integrity.md", "icon": "FI"},
    {"name": "Data Quality", "skill": "data_quality.md", "icon": "DQ"},
]

SYSTEM_PROMPT = """You are Healthcare Sentinel, an autonomous clinical data triage agent working
inside a DataHub metadata catalog connected to a hospital's SQLite warehouse.

MISSION: Find data quality issues that could harm patients, rank them by
clinical severity, record your findings in DataHub, and remediate what you can.

INVESTIGATION METHODOLOGY:
- Schema metadata is a hypothesis. typeof() and COUNT(*) are your sources.
  Never report a finding without SQL evidence confirming it.
- For each anomaly, actively check the counter-claim: could this value be
  legitimate (e.g. refund, sentinel value, timezone convention)? Report it
  only if the data is objectively malformed despite plausible explanations.
- Check every table independently. Absence of a defect in one table does not
  mean another table is clean — materialized copies diverge from their source.
- Log what you checked and found clean. Gaps in coverage are gaps in trust.

Work through these phases in order. Complete ALL phases before outputting your
final answer.

0. RECALL — call search_documents("Sentinel Learnings") to check for learnings
   from previous runs. If learnings exist, verify each one before opening new
   investigations: run its evidence_query.
   - Count = 0: the issue was already fixed at the source. Note it as resolved
     in your audit log — do NOT re-report it as a new finding and do NOT
     re-fix the source. Then run the same check on downstream materialized
     tables: a stale copy still carrying the defect is a pipeline-rebuild
     finding (recommendation: rebuild the table from its fixed source), not
     something to hand-edit with apply_fix. Rate these MEDIUM — the root
     cause is fixed and remediation is a routine rebuild.
   - Count > 0: the issue persists or regressed — treat it as live.
   Learnings are a head start, not a finish line. After verifying them,
   you MUST proceed to DISCOVER and investigate ALL tables — especially
   any new tables the learnings don't mention.

1. DISCOVER — use search to find all healthcare datasets. Collect their URNs
   from the results. Never construct URNs yourself. The catalog can lag the
   warehouse: after searching, run
   SELECT name FROM sqlite_master WHERE type='table'
   and compare against the search results. Investigate every warehouse table
   even if it is not yet cataloged — tables added since the last run are the
   least validated and the most likely to carry fresh defects.

2. UNDERSTAND — call list_schema_fields for each dataset. Note column names,
   types, and anything suspicious. Compare column types across related tables
   in the pipeline (e.g. if age is TEXT in the source but INT in the mart,
   that's a schema-level finding worth reporting — type mismatches block
   numeric validation and can silently corrupt casts).

3. INVESTIGATE — write SQL queries to find clinically dangerous data quality
   issues. Think like a clinician: what data errors could cause patient harm?
   Consider: wrong vitals/ages, missing identifiers, chronology violations,
   invalid coded values, negative amounts, type mismatches, and any other
   anomaly you notice. Check ALL tables independently — run the same
   validation checks on source, staging, and mart tables separately, because
   materialized tables carry their own copy of the data. Use typeof() to
   confirm actual storage types when schema says one thing but data might
   differ. Use COUNT(*) to quantify, then LIMIT 3 for samples on confirmed
   issues.

4. TRACE — for each confirmed issue, call get_lineage (upstream=false) to find
   downstream datasets. Then verify whether each downstream table actually
   inherited the defect by running the same SQL check there. Downstream
   contamination in materialized tables (not live views) is a separate finding
   — materialized tables require independent remediation because they won't
   auto-heal when the source is fixed. Report each as its own finding.

5. ASSESS — assign each finding a severity:
   CRITICAL = could directly cause patient harm (wrong dosing, transfusion
     mismatch, unidentifiable patients). Use for SOURCE tables only.
   HIGH = corrupts downstream clinical/financial decisions. Use for
     downstream/inherited issues in materialized tables, and for schema-level
     issues that block validation.
   MEDIUM = blocks validation, documentation, or reporting. Includes
     type mismatches that prevent proper sorting/filtering.
   LOW = cosmetic or minor inconsistency.
   Write 1-2 sentences of clinical impact and one recommendation per finding.

   IMPORTANT: Call report_finding for EVERY issue — fixable or not. Schema
   issues, downstream contamination in materialized tables, and any other
   anomaly all get a report_finding call. This is how findings enter the
   triage report.

6. RECORD — write findings back to DataHub:
   a. add_tags: tag affected datasets with severity and data-quality-issues tags.
      Tag contaminated downstream datasets with upstream-contamination.
   b. update_description: on CRITICAL/HIGH datasets, add a warning block wrapped
      in <!-- sentinel:start --> and <!-- sentinel:end --> markers. If markers
      already exist, replace the content between them.
   c. add_structured_properties (if available): write trust scores per dataset.
      Trust score: start at 100, subtract 30/CRITICAL, 15/HIGH, 5/MEDIUM, 1/LOW.

7. REMEDIATE — for CRITICAL and HIGH findings, propose data fixes using
   apply_fix. Available operations:
   - correct: UPDATE invalid values to NULL or a safe default
   - quarantine: move unfixable rows to a quarantine table
   - flag: add a _sentinel_flagged column to mark suspect rows
   - standardize: normalize formats
   - deduplicate: remove duplicate rows
   - enrich: fill missing values from related tables
   Use ||| to separate multi-statement operations.
   Fix the source table first, then fix any materialized downstream tables
   that still carry the defect (they won't auto-update).
   IMPORTANT: Call ALL apply_fix operations in ONE turn — do not split them
   across multiple turns. The human reviews one batch, not individual fixes.
   After remediation, verify your fixes worked.
   Not all findings require apply_fix — schema issues (e.g. wrong column
   type) are valid findings even without a fix. Include them in your final
   JSON with a recommendation.
   Save an audit log via save_document.

8. LEARN — call save_document with a "Sentinel Learnings" JSON array:
   {"type", "table", "column", "claim", "evidence_query", "confidence"}
   Save only actionable learnings a future agent can use. Drop anything
   without an evidence_query or not scoped to a specific table.column.
   Also save a "Sentinel Triage Report" markdown summary for catalog search.

FINAL ANSWER: After completing all phases, output your findings as a JSON array.
Each finding:
{"check_name", "table", "column", "description", "affected_rows",
 "sample_data", "severity", "clinical_impact", "recommendation",
 "downstream_contamination": [list of table names]}

OUTPUT FORMAT RULES (feeds a slide deck):
- "description": max 8 words (slide headline)
- "clinical_impact": max 25 words, plain language
- "recommendation": max 15 words
- "check_name": short snake_case identifier
- "sample_data": max 3 rows, max 4 columns each
- DEDUPLICATE: same root cause in the same table = one finding. List
  downstream VIEWS in "downstream_contamination". But a materialized table
  (not a view) carrying the same defect is a SEPARATE finding — it holds
  its own copy of the data and requires independent remediation."""

KICKOFF = "Run a full clinical data triage of the healthcare datasets registered in DataHub."

# ---------------------------------------------------------------------------
# DataHub document memory — recall, upsert, reset
# ---------------------------------------------------------------------------

LEARNINGS_TITLE = "Sentinel Learnings"
SENTINEL_DOC_PREFIX = "Sentinel"

# search_documents deliberately omits document content to keep agent context
# small, so recall has to read `contents` through GraphQL to see the text.
_DOCUMENTS_GQL = """
query sentinelDocuments($query: String!, $count: Int!) {
  searchAcrossEntities(input: {query: $query, count: $count, types: [DOCUMENT]}) {
    searchResults {
      entity {
        urn
        ... on Document {
          info { title lastModified { time } contents { text } }
        }
      }
    }
  }
}
"""


def fetch_documents(graph, query: str = "*", count: int = 50) -> list:
    """Read DataHub documents with their content. Returns [] on any failure."""
    try:
        resp = graph.execute_graphql(_DOCUMENTS_GQL, variables={"query": query, "count": count})
    except Exception:
        return []
    results = ((resp or {}).get("searchAcrossEntities") or {}).get("searchResults") or []
    docs = []
    for sr in results:
        entity = (sr or {}).get("entity") or {}
        urn = entity.get("urn")
        if not urn:
            continue
        info = entity.get("info") or {}
        docs.append({
            "urn": urn,
            "title": info.get("title") or "",
            "content": (info.get("contents") or {}).get("text") or "",
            "modified": (info.get("lastModified") or {}).get("time") or 0,
        })
    return docs


def find_sentinel_documents(graph) -> list:
    return [d for d in fetch_documents(graph) if d["title"].startswith(SENTINEL_DOC_PREFIX)]


def latest_learnings(graph) -> dict:
    """Most recently modified non-empty 'Sentinel Learnings' document, or None."""
    docs = [d for d in fetch_documents(graph, query=LEARNINGS_TITLE)
            if d["title"] == LEARNINGS_TITLE and d["content"].strip()]
    return max(docs, key=lambda d: d["modified"]) if docs else None


def wrap_save_document_upsert(tool, graph):
    """save_document mints a fresh UUID document per call, so repeated runs would
    orphan each previous version. Reuse the existing URN for the same title so
    learnings live in one document that later runs can find."""
    from langchain_core.tools import StructuredTool

    def invoke(**kwargs):
        if not kwargs.get("urn"):
            title = str(kwargs.get("title") or "")
            existing = [d for d in fetch_documents(graph, query=title) if d["title"] == title]
            if existing:
                kwargs["urn"] = max(existing, key=lambda d: d["modified"])["urn"]
        return tool.invoke(kwargs)

    return StructuredTool.from_function(
        func=invoke, name=tool.name, description=tool.description, args_schema=tool.args_schema)


# ---------------------------------------------------------------------------
# Agent runner — ReAct loop with streaming console output
# ---------------------------------------------------------------------------

MUTATION_TOOLS = {"add_tags", "update_description", "add_structured_properties", "save_document", "apply_fix"}  # add_structured_properties only active with SENTINEL_STRUCTURED_PROPS env


def _safe_wrap(t):
    """Wrap a tool so exceptions return error messages instead of crashing the agent."""
    from langchain_core.tools import StructuredTool

    def safe_invoke(**kwargs):
        try:
            return t.invoke(kwargs)
        except Exception as e:
            return f"ERROR executing {t.name}: {e}. Continue with remaining tasks."

    return StructuredTool.from_function(
        func=safe_invoke, name=t.name, description=t.description, args_schema=t.args_schema)


def _format_apply_fix_desc(tool_call, state, runtime):
    """Custom description for apply_fix approval display."""
    a = tool_call.get("args") or {}
    return (f"[{a.get('action_type', '?')}] {a.get('table', '?')}\n"
            f"  SQL: {str(a.get('sql', ''))[:200]}\n"
            f"  Reason: {a.get('reason', '')}")


def extract_action_requests(value) -> list[dict]:
    """Normalize a HITL interrupt payload into a list of action-request dicts."""
    val = value if isinstance(value, dict) else {}
    actions = val.get("action_requests", [val])
    if isinstance(actions, dict):
        actions = [actions]
    elif not isinstance(actions, list):
        actions = [val]
    return [a if isinstance(a, dict) else {} for a in actions]


def parse_approval_choice(choice: str, n: int) -> set:
    """Parse batch approval input ('all' / 'none' / '1,3,5') into 0-based approved indices.
    Unrecognized input denies by default."""
    c = (choice or "").strip().lower()
    if c in ("all", "y", "yes"):
        return set(range(n))
    if c in ("none", "n", "no", ""):
        return set()
    approved = set()
    for part in c.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < n:
                approved.add(idx)
    return approved


def run_agent(tools, db_path: str, auto_approve: bool = False, kickoff: str = None, system_prompt: str = None, read_only: bool = False, max_steps: int = 500):
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    if MODEL.startswith("claude"):
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=MODEL, max_tokens=4096)
    elif MODEL.startswith("gemini"):
        # Architecture decision: Vertex AI via GOOGLE_GENAI_USE_VERTEXAI=1
        # routes through GCP with ADC credentials — higher rate limits,
        # no data training, SLA-backed. Set GOOGLE_CLOUD_PROJECT and
        # GOOGLE_GENAI_USE_VERTEXAI=1 before running.
        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(model=MODEL, max_output_tokens=4096)
    else:
        from langchain_ollama import ChatOllama
        llm = ChatOllama(model=MODEL, num_ctx=8192)
    sql_tool = make_run_sql_tool(db_path)
    fix_tool = make_apply_fix_tool(db_path)
    finding_tool = make_report_finding_tool()
    init_audit_db(db_path)

    safe_tools = [_safe_wrap(t) for t in tools] + [sql_tool, finding_tool]
    if not read_only:
        safe_tools.append(fix_tool)
    all_tools = safe_tools

    # Only gate apply_fix (data modifications) — auto-approve metadata writes
    # Tags, descriptions, save_document are safe metadata operations
    if auto_approve:
        middleware = []
    else:
        middleware = [HumanInTheLoopMiddleware(interrupt_on={
            "apply_fix": {
                "allowed_decisions": ["approve", "reject"],
                "description": _format_apply_fix_desc,
            },
        })]

    prompt = system_prompt or SYSTEM_PROMPT
    agent = create_agent(llm, all_tools, system_prompt=prompt,
                         middleware=middleware, checkpointer=MemorySaver())

    tool_log = []
    final_text = ""
    import uuid
    config = {"configurable": {"thread_id": f"triage-{uuid.uuid4().hex[:8]}"}, "recursion_limit": max_steps}

    print(f"  {_GRAY('Agent model:')} {MODEL}")
    print(f"  {_GRAY('Tools:')} {', '.join(t.name for t in all_tools)}")
    if not auto_approve:
        print(f"  {_YELLOW('Human-in-the-loop: ON')} {_GRAY('(batch approval for mutation tools)')}")
    print()

    seen_call_ids = set()
    _pending_tool_ids = {}
    _seen_msg_count = 0

    def stream_once(inp):
        nonlocal final_text, _seen_msg_count
        try:
            for step in agent.stream(inp, config, stream_mode="values"):
                msgs = step.get("messages") if isinstance(step, dict) else None
                if not msgs:
                    continue
                new_msgs = msgs[_seen_msg_count:]
                _seen_msg_count = len(msgs)
                for msg in new_msgs:
                    if getattr(msg, "tool_calls", None):
                        for tc in msg.tool_calls:
                            tc_id = tc.get("id")
                            if tc_id in seen_call_ids:
                                continue
                            seen_call_ids.add(tc_id)
                            entry = {"tool": tc.get("name", "?"), "args": tc.get("args", {})}
                            tool_log.append(entry)
                            if tc_id:
                                _pending_tool_ids[tc_id] = entry
                    elif getattr(msg, "type", None) == "tool":
                        full_content = str(msg.content)
                        tid = getattr(msg, "tool_call_id", None)
                        tname, targs = "?", {}
                        if tid and tid in _pending_tool_ids:
                            entry = _pending_tool_ids[tid]
                            tname = entry.get("tool", "?")
                            targs = entry.get("args", {})
                            entry["result"] = full_content
                            del _pending_tool_ids[tid]
                        a_short = _condense_tool_args(tname, targs)
                        r_short = _condense_tool_result(tname, full_content[:2000])
                        tc = _tool_color(tname)
                        print(f"  {tc('→')} {tc(tname)}({_DIM(a_short)}) {_GRAY('←')} {_GRAY(r_short)}")
                    elif getattr(msg, "type", None) == "ai" and msg.content:
                        final_text = msg.content if isinstance(msg.content, str) else str(msg.content)
        except Exception as e:
            print(f"\n  {_RED('✗')} [Agent] Run stopped: {type(e).__name__}: {e}")
            return False
        return True

    # Initial run
    if not stream_once({"messages": [("user", kickoff or KICKOFF)]}):
        return final_text, tool_log

    # Handle batch approvals from HumanInTheLoopMiddleware
    while True:
        try:
            state = agent.get_state(config)
            pending = [i for task in state.tasks for i in task.interrupts]
        except Exception as e:
            print(f"  {_RED('✗')} Could not read pending approvals: {e}")
            break
        if not pending:
            break

        # Collect all action requests from the batch interrupt
        all_actions = []
        for p in pending:
            for ar in extract_action_requests(p.value):
                all_actions.append({"interrupt": p, "action": ar})

        # Display batch
        print(f"\n  {_YELLOW('BATCH APPROVAL')} {_GRAY('—')} {_BOLD(f'{len(all_actions)} operation(s)')}")
        print(f"  {_GRAY('─' * 56)}")
        for i, item in enumerate(all_actions, 1):
            ar = item["action"]
            name = ar.get("name", ar.get("tool", "?"))
            desc = ar.get("description", "")
            args = ar.get("arguments", ar.get("args", {}))
            if name == "apply_fix" and isinstance(args, dict):
                target = str(args.get("table", "") or "")
                affected = ""
                if _IDENT_RE.match(target):
                    try:
                        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
                        try:
                            for stmt in str(args.get("sql", "")).split("|||"):
                                stmt = stmt.strip()
                                if "WHERE" in stmt.upper():
                                    wi = stmt.upper().index("WHERE")
                                    cnt = conn.execute(f"SELECT COUNT(*) FROM [{target}] {stmt[wi:]}").fetchone()[0]
                                    affected = f" ({cnt:,} rows)"
                                    break
                        finally:
                            conn.close()
                    except Exception:
                        pass
                atype = args.get('action_type', '?')
                print(f"  {i}. {_YELLOW(f'[{atype}]')} {_BOLD(target)}{affected}")
                print(f"     {_GRAY('SQL:')} {str(args.get('sql',''))}")
                print(f"     {_GRAY('Reason:')} {str(args.get('reason',''))}")
            else:
                print(f"  {i}. {_YELLOW(f'[{name}]')} {json.dumps(args, default=str)[:100]}")
        print(f"  {_GRAY('─' * 56)}")

        # Ask once for the batch
        if auto_approve:
            choice = "all"
        else:
            try:
                choice = input(f"  {_YELLOW('Approve which?')} [all / 1,3,5 / none] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(f"\n  {_RED('No input available — denying all.')}")
                choice = "none"

        approved = parse_approval_choice(choice, len(all_actions))

        # Build decisions and log
        decisions = []
        for i, item in enumerate(all_actions):
            ok = i in approved
            ar = item["action"]
            name = ar.get("name", ar.get("tool", "?"))
            args = ar.get("arguments", ar.get("args", {}))
            tool_log.append({"tool": "HUMAN_APPROVAL", "args": {"action": name, "approved": ok}})
            if ok:
                decisions.append({"type": "approve"})
            else:
                decisions.append({"type": "reject", "message": "human declined"})
                # Log denied apply_fix to audit trail
                if name == "apply_fix" and isinstance(args, dict):
                    ctx = _get_device_context()
                    try:
                        audit_conn = sqlite3.connect(db_path, timeout=30)
                        audit_conn.execute(_AUDIT_DDL)
                        audit_conn.execute(
                            "INSERT INTO _sentinel_audit (run_id,ts,op_type,target_table,sql_text,rows_affected,rationale,approved_by,hostname,ip_address,run_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (datetime.now().strftime("%Y%m%d_%H%M%S"), datetime.now().isoformat(),
                             args.get("action_type", "?"), args.get("table", "?"),
                             args.get("sql", ""), 0, args.get("reason", ""),
                             f"DENIED:{ctx['reviewer']}",
                             ctx["hostname"], ctx["ip_address"], _CURRENT_RUN_MODE))
                        audit_conn.commit()
                        audit_conn.close()
                    except Exception:
                        pass

        n_ok = len(approved)
        n_deny = len(all_actions) - n_ok
        print(f"  {_GREEN(f'✓ {n_ok} approved')}, {_GRAY(f'{n_deny} denied') if n_deny else ''}")

        if not stream_once(Command(resume={"decisions": decisions})):
            break

    return final_text, tool_log


def parse_findings(text: str) -> list[dict]:
    if not isinstance(text, str):
        return []
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("\n", 1)
        text = (parts[1] if len(parts) > 1 else "").rsplit("```", 1)[0].strip()
    candidates = []
    try:
        candidates.append(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                candidates.append(json.loads(match.group()))
            except (json.JSONDecodeError, ValueError):
                pass
    for parsed in candidates:
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            return [f for f in parsed if isinstance(f, dict)]
    return []


# Architecture decision: dedup by (table, column) keeps the highest-severity
# version when multiple specialists flag the same root cause. This prevents
# double-counting while preserving the most conservative risk assessment.
def merge_findings(all_findings: list[dict]) -> list[dict]:
    seen = {}
    for f in all_findings:
        key = (f.get("table", ""), f.get("column", ""), f.get("check_name", ""))
        existing = seen.get(key)
        if not existing:
            seen[key] = f
        else:
            e_rank = _SEV_RANK.get(existing.get("severity", "LOW"), 3)
            f_rank = _SEV_RANK.get(f.get("severity", "LOW"), 3)
            if f_rank < e_rank:
                seen[key] = f
    return list(seen.values())


def extract_findings_from_tool_log(tool_log: list[dict]) -> list[dict]:
    """Extract findings from the agent's report_finding tool calls.
    Falls back to apply_fix parsing if report_finding wasn't used."""
    findings = []
    seen_keys = set()

    for entry in tool_log:
        if entry.get("tool") != "report_finding":
            continue
        args = entry.get("args", {})
        if not isinstance(args, dict):
            continue
        key = (args.get("table", ""), args.get("check_name", ""), args.get("column", ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        findings.append({
            "check_name": args.get("check_name", ""),
            "table": args.get("table", ""),
            "column": args.get("column", ""),
            "description": args.get("description", ""),
            "affected_rows": args.get("affected_rows", 0),
            "severity": args.get("severity", "MEDIUM"),
            "clinical_impact": args.get("clinical_impact", ""),
            "recommendation": args.get("recommendation", ""),
            "sample_data": [], "downstream_contamination": [],
        })

    if findings:
        return findings

    # Fallback: extract from apply_fix calls when report_finding not used
    for entry in tool_log:
        if entry.get("tool") != "apply_fix":
            continue
        args = entry.get("args", {})
        if not isinstance(args, dict):
            continue
        table = args.get("table", "")
        reason = args.get("reason", "")
        check = args.get("action_type", "fix")
        key = (table, check)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        findings.append({
            "check_name": check, "table": table, "column": "",
            "description": reason[:60] if reason else check,
            "affected_rows": 0, "severity": "HIGH",
            "clinical_impact": reason, "recommendation": "",
            "sample_data": [], "downstream_contamination": [],
        })
    return findings


_SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

SOURCE_TABLES = {"raw_patients"}


def collapse_downstream_findings(findings: list[dict]) -> list[dict]:
    """Collapse inherited downstream findings into the source finding's
    downstream_contamination list. Keeps findings on source tables and
    truly independent downstream issues (e.g. negative_length_of_stay).
    No hardcoded check names — uses table + column hierarchy to decide."""
    source_by_key = {}
    downstream = []

    for f in findings:
        table = f.get("table", "")
        check = f.get("check_name", "")
        col = f.get("column", "")
        if table in SOURCE_TABLES:
            source_by_key[(check, col)] = f
        else:
            downstream.append(f)

    unmatched = []
    for f in downstream:
        check = f.get("check_name", "")
        col = f.get("column", "")
        parent = None
        for (src_check, src_col), src_f in source_by_key.items():
            if col and col == src_col and src_check in check:
                parent = src_f
                break
        if parent:
            ds = parent.get("downstream_contamination", [])
            table = f.get("table", "")
            if table not in ds:
                ds.append(table)
            parent["downstream_contamination"] = ds
        else:
            unmatched.append(f)

    # Second pass: merge orphan findings that share the same column across
    # tables — pick highest severity as primary, rest become downstream.
    merged = []
    col_primary = {}
    for f in unmatched:
        col = f.get("column", "")
        if not col or "," in col or col == "*":
            merged.append(f)
            continue
        if col in col_primary:
            primary = col_primary[col]
            ds = primary.get("downstream_contamination", [])
            table = f.get("table", "")
            if table not in ds:
                ds.append(table)
            primary["downstream_contamination"] = ds
            p_sev = _SEV_RANK.get(primary.get("severity", ""), 99)
            f_sev = _SEV_RANK.get(f.get("severity", ""), 99)
            if f_sev < p_sev:
                primary["severity"] = f.get("severity")
            if (f.get("affected_rows") or 0) > (primary.get("affected_rows") or 0):
                primary["affected_rows"] = f["affected_rows"]
        else:
            col_primary[col] = f
            merged.append(f)

    return list(source_by_key.values()) + merged


_SEVERITY_KEYWORDS = {
    "age": "impossible_ages",
    "identifier": "missing_identifiers",
    "name": "missing_identifiers",
    "discharge": "discharge_before_admission",
    "chronolog": "discharge_before_admission",
    "admission": "discharge_before_admission",
    "billing": "negative_billing",
    "blood": "invalid_blood_types",
    "propagat": "propagated_bad_ages",
    "downstream": "propagated_bad_ages",
    "text": "age_stored_as_text",
    "stored_as": "age_stored_as_text",
    "future": "future_admission_dates",
    "null_medical": "null_medical_conditions",
}


def _match_clinical_rule(check_name: str, description: str = "") -> tuple | None:
    """Fuzzy match a finding to CLINICAL_RULES via check_name or description keywords."""
    rule = CLINICAL_RULES.get(check_name or "")
    if rule:
        return rule
    combined = f"{check_name or ''} {description or ''}".lower()
    for keyword, rule_name in _SEVERITY_KEYWORDS.items():
        if keyword in combined:
            return CLINICAL_RULES.get(rule_name)
    return None


def enforce_severity(findings: list[dict]) -> list[dict]:
    """Deterministic severity: CLINICAL_RULES override LLM severity entirely.
    Uses fuzzy matching on check_name + description to handle LLM naming variance.
    For downstream/inherited findings, cap at HIGH (never CRITICAL)."""
    findings = [f for f in (findings or []) if isinstance(f, dict)]
    for f in findings:
        rule = _match_clinical_rule(f.get("check_name", ""), f.get("description", ""))
        if rule:
            f["severity"] = rule[0]
        else:
            llm_sev = str(f.get("severity") or "MEDIUM").upper()
            if llm_sev not in _SEV_RANK:
                llm_sev = "MEDIUM"
            f["severity"] = llm_sev
        # Downstream contamination findings cap at HIGH
        table = str(f.get("table") or "")
        if table.startswith("mart_") or table.startswith("staging_") or table.startswith("v_"):
            if f["severity"] == "CRITICAL":
                f["severity"] = "HIGH"
    return findings


# ---------------------------------------------------------------------------
# Dry-run — offline mode with built-in rules (no API calls)
# ---------------------------------------------------------------------------

CLINICAL_RULES = {
    "impossible_ages": ("CRITICAL", "Ages like -88 or 262 cause fatal drug dosing errors.", "Block pipelines until ages validated."),
    "missing_identifiers": ("CRITICAL", "Unnamed patients are invisible in emergency triage.", "Quarantine and trace back to source."),
    "discharge_before_admission": ("CRITICAL", "Negative stay length triggers $10K fraud audits.", "Halt billing; fix dates manually."),
    "propagated_bad_ages": ("HIGH", "Bad ages spread to downstream analytics and ML models.", "Fix upstream first, rebuild mart."),
    "invalid_blood_types": ("HIGH", "Wrong blood type risks fatal transfusion reaction.", "Cross-reference with lab records."),
    "negative_billing": ("HIGH", "Negative amounts mask revenue leakage.", "Audit against adjustment records."),
    "future_admission_dates": ("MEDIUM", "Future dates indicate clock errors or fabrication.", "Add date constraint to pipeline."),
    "null_medical_conditions": ("MEDIUM", "Missing diagnosis blocks care pathway assignment.", "Flag for CDI review."),
    "age_stored_as_text": ("MEDIUM", "Text type prevents numeric validation and sorting.", "Alter column to INTEGER."),
}

FALLBACK_CHECKS = [
    {"name": "impossible_ages", "table": "raw_patients", "column": "age",
     "query": "SELECT COUNT(*) FROM raw_patients WHERE CAST(age AS INT) > 120 OR CAST(age AS INT) < 0",
     "sample_query": "SELECT name, age FROM raw_patients WHERE CAST(age AS INT) > 120 OR CAST(age AS INT) < 0 LIMIT 3",
     "description": "Patients with impossible age values (negative or > 120)"},
    {"name": "missing_identifiers", "table": "raw_patients", "column": "name",
     "query": "SELECT COUNT(*) FROM raw_patients WHERE name IS NULL OR TRIM(name) = ''",
     "sample_query": "SELECT rowid, age, gender FROM raw_patients WHERE name IS NULL OR TRIM(name) = '' LIMIT 3",
     "description": "Patient records with missing or empty name identifiers"},
    {"name": "discharge_before_admission", "table": "raw_patients", "column": "discharge_date",
     "query": "SELECT COUNT(*) FROM raw_patients WHERE discharge_date < date_of_admission AND discharge_date != '' AND date_of_admission != ''",
     "sample_query": "SELECT name, date_of_admission, discharge_date FROM raw_patients WHERE discharge_date < date_of_admission AND discharge_date != '' AND date_of_admission != '' LIMIT 3",
     "description": "Patients discharged before their admission date"},
    {"name": "negative_billing", "table": "raw_patients", "column": "billing_amount",
     "query": "SELECT COUNT(*) FROM raw_patients WHERE CAST(billing_amount AS REAL) < 0",
     "sample_query": "SELECT name, billing_amount FROM raw_patients WHERE CAST(billing_amount AS REAL) < 0 LIMIT 3",
     "description": "Negative billing amounts"},
    {"name": "age_stored_as_text", "table": "raw_patients", "column": "age",
     "query": "SELECT 1", "sample_query": "SELECT typeof(age) FROM raw_patients LIMIT 1",
     "description": "Age column stored as TEXT instead of INTEGER", "is_schema_issue": True},
    {"name": "propagated_bad_ages", "table": "mart_demographics", "column": "age",
     "query": "SELECT COUNT(*) FROM mart_demographics WHERE age > 120 OR age < 0",
     "sample_query": "SELECT name, age FROM mart_demographics WHERE age > 120 OR age < 0 LIMIT 3",
     "description": "Impossible age values propagated to downstream mart table"},
]

LINEAGE_MAP = {
    "raw_patients": ["staging_patients"],
    "staging_patients": ["mart_billing", "mart_demographics"],
}


def dry_run_pipeline(db_path: str) -> tuple[list[dict], list[dict], list[dict]]:
    datasets = []
    findings = []
    try:
        conn = sqlite3.connect(db_path, timeout=30)
    except Exception as e:
        print(f"  ERROR: cannot open database: {e}")
        return datasets, findings, []
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                  if not r[0].startswith('_') and not r[0].startswith('sqlite_')]
        for t in tables:
            try:
                cols = conn.execute(f"PRAGMA table_info([{t}])").fetchall()
                row_count = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            except Exception as e:
                print(f"  SKIP: {t} — {e}")
                continue
            datasets.append({"table": t, "columns": [{"name": c[1], "type": c[2] or "TEXT"} for c in cols], "row_count": row_count})
            print(f"  Found: {t} ({row_count:,} rows, {len(cols)} columns)")

        for check in FALLBACK_CHECKS:
            try:
                row = conn.execute(check["query"]).fetchone()
                count = row[0] if row else 0
                if check.get("is_schema_issue"):
                    count = 1
                if count and count > 0:
                    cur = conn.execute(check["sample_query"])
                    sample_cols = [d[0] for d in cur.description] if cur.description else []
                    sample_rows = [dict(zip(sample_cols, r)) for r in cur.fetchall()]
                    rule = CLINICAL_RULES.get(check["name"], ("MEDIUM", "Impact not assessed", "Review manually"))
                    findings.append({
                        "check_name": check["name"], "table": check["table"], "column": check["column"],
                        "description": check["description"], "affected_rows": count,
                        "sample_data": sample_rows[:3], "is_schema_issue": check.get("is_schema_issue", False),
                        "severity": rule[0], "clinical_impact": rule[1], "recommendation": rule[2],
                    })
                    label = "schema" if check.get("is_schema_issue") else f"{count} rows"
                    print(f"  FOUND: {check['name']} ({label}) -> {rule[0]}")
            except Exception as e:
                print(f"  SKIP: {check['name']} — {e}")
    finally:
        conn.close()

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: severity_order.get(f.get("severity", "LOW"), 3))

    for f in findings:
        source = f["table"]
        contaminated = []
        seen = {source}
        queue = LINEAGE_MAP.get(source, [])[:]
        while queue:
            ds = queue.pop(0)
            if ds in seen:
                continue
            seen.add(ds)
            contaminated.append(ds)
            queue.extend(LINEAGE_MAP.get(ds, []))
        f["downstream_contamination"] = contaminated
        if contaminated:
            print(f"  Lineage: {source} -> {' -> '.join(contaminated)}")

    return datasets, findings, []


# ---------------------------------------------------------------------------
# HTML Report Generator
# ---------------------------------------------------------------------------

def generate_report(datasets: list[dict], findings: list[dict], actions: list[dict], tool_log: list[dict] = None, db_path: str = None) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts_slug = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"triage-report_{ts_slug}.html"
    esc = html_mod.escape

    datasets = [d for d in (datasets or []) if isinstance(d, dict)]
    findings = [dict(f) for f in (findings or []) if isinstance(f, dict)]
    for f in findings:
        f["severity"] = str(f.get("severity") or "MEDIUM").strip().upper()

    sev_colors = {"CRITICAL": "#EF4444", "HIGH": "#F59E0B", "MEDIUM": "#E6C470", "LOW": "#22C55E"}
    sev_bg = {"CRITICAL": "rgba(239,68,68,.12)", "HIGH": "rgba(245,158,11,.12)", "MEDIUM": "rgba(230,196,112,.12)", "LOW": "rgba(34,197,94,.12)"}
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    total_rows = sum(d.get("row_count") or 0 for d in datasets if isinstance(d.get("row_count"), (int, float)))
    # Source rows = only raw/source tables (not staging/mart/view duplicates)
    source_rows = sum(d.get("row_count") or 0 for d in datasets
                      if isinstance(d.get("row_count"), (int, float))
                      and not str(d.get("table", "")).startswith(("staging_", "mart_", "v_")))
    scan_time = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    def trunc(text, max_chars):
        text = str(text)
        return (text[:max_chars-1] + "…") if len(text) > max_chars else text

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings = sorted(findings, key=lambda f: severity_order.get(f.get("severity", "LOW"), 3))

    finding_slides = ""
    for i, f in enumerate(findings):
        sev = f.get("severity", "MEDIUM")
        color = sev_colors.get(sev, "#888")
        bg = sev_bg.get(sev, "rgba(0,0,0,0)")
        rows = f.get("affected_rows", "?")
        rows_str = f"{rows:,}" if isinstance(rows, int) else str(rows)
        impact = trunc(esc(str(f.get("clinical_impact", ""))).split(".")[0] + ".", 120)
        desc = trunc(esc(str(f.get("description", ""))), 60)
        table_col = f"{esc(str(f.get('table','')))}.{esc(str(f.get('column','')))}"

        lineage_inner = ""
        downstream = f.get("downstream_contamination")
        if isinstance(downstream, (list, tuple)) and downstream:
            ns = [f"<span class='ln s'>{esc(str(f.get('table','')))}</span>"]
            for ds in downstream:
                ns += [f"<span class='la'>&rarr;</span>", f"<span class='ln d'>{esc(str(ds))}</span>"]
            lineage_inner = f"<div class='lineage' style='margin-top:auto'>{''.join(ns)}</div>"

        finding_slides += f"""
<div class="slide"><div class="pad mid">
  <span class="sev" style="background:{bg};color:{color};font-size:15px;padding:8px 20px;border-radius:8px;align-self:flex-start">{sev}</span>
  <h2 style="margin-top:28px;max-width:22ch">{desc}</h2>
  <p class="voice">{impact}</p>
  <div style="margin-top:48px;display:flex;gap:32px;align-items:center;font-size:20px;color:var(--t2)">
    <span style="font-family:var(--mono);color:var(--champagne)">{table_col}</span>
    <span style="color:var(--t1);font-weight:700">{rows_str} rows</span>
  </div>
  {lineage_inner}
</div><div class="foot"><span>Healthcare Sentinel</span><span>{i + 3}</span></div></div>"""

    # --- Actions ---
    action_items = ""
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        if a.get("type") == "tag":
            action_items += f"<div class='act'><span class='act-i' style='background:rgba(230,196,112,.1);color:var(--champagne)'>T</span>{esc(str(a.get('table','')))} &rarr; {esc(str(a.get('tag','')))}</div>"
        elif a.get("type") == "description":
            action_items += f"<div class='act'><span class='act-i' style='background:rgba(127,178,255,.1);color:var(--blue)'>D</span>{esc(str(a.get('text','')))}</div>"
    if not action_items:
        action_items = "<p class='voice'>No write-back in dry-run mode.</p>"

    # --- Trace ---
    trace_slide = ""
    if tool_log:
        def _trace_desc(e):
            tool = str(e.get("tool", ""))
            a = e.get("args", {}) if isinstance(e.get("args"), dict) else {}
            if tool == "search":
                return "Search DataHub catalog"
            if tool == "search_documents":
                return f"Recall: {a.get('query','')}"
            if tool == "list_schema_fields":
                urn = str(a.get("urn", ""))
                name = urn.split(".")[-1].split(",")[0] if "." in urn else urn
                return f"Read schema: {name}"
            if tool == "get_lineage":
                urn = str(a.get("urn", ""))
                name = urn.split(".")[-1].split(",")[0] if "." in urn else urn
                return f"Trace lineage: {name}"
            if tool == "get_entities":
                return "Read entity metadata"
            if tool == "run_sql":
                q = str(a.get("query", ""))
                return f"{q}"
            if tool == "apply_fix":
                return f"{a.get('action_type','?')} on {a.get('table','?')}"
            if tool == "add_tags":
                return f"Tag {len(a.get('entity_urns', []))} datasets"
            if tool == "update_description":
                urn = str(a.get("entity_urn", ""))
                name = urn.split(".")[-1].split(",")[0] if "." in urn else urn
                return f"Update description: {name}"
            if tool == "save_document":
                return f"Save: {a.get('title','')[:40]}"
            if tool == "HUMAN_APPROVAL":
                status = "approved" if a.get("approved") else "denied"
                return f"{a.get('action','?')} — {status}"
            return esc(json.dumps(a, default=str)[:60])

        trace_entries = [e for e in tool_log if isinstance(e, dict)]
        lines = "".join(
            f"<div class='tl'><span class='tl-t'>{esc(str(e.get('tool','')))}</span><span class='tl-a'>{esc(_trace_desc(e))}</span></div>"
            for e in trace_entries)
        trace_slide = f"""
<div class="slide" style="height:auto;min-height:854px"><div class="pad">
  <div class="kick">How I Worked</div>
  <h2>{len(trace_entries)} tool calls. Autonomous.</h2>
  <div class="trace" style="margin-top:24px">{lines}</div>
</div><div class="foot" style="position:relative"><span>Healthcare Sentinel</span></div></div>"""

    # --- Audit trail ---
    audit_slide = ""
    if db_path:
        try:
            aconn = sqlite3.connect(db_path, timeout=10)
            try:
                audit_rows = aconn.execute(
                    "SELECT op_id, ts, op_type, target_table, rows_affected, approved_by, hostname, run_mode "
                    "FROM _sentinel_audit ORDER BY op_id DESC LIMIT 10").fetchall()
            except sqlite3.OperationalError:
                audit_rows = []
            aconn.close()
            if audit_rows:
                n_approved = sum(1 for r in audit_rows if not str(r[5] or "").startswith("DENIED"))
                n_denied = sum(1 for r in audit_rows if str(r[5] or "").startswith("DENIED"))
                audit_items = ""
                for r in audit_rows:
                    oid, ts, op_type, table, rows, reviewer, hostname, run_mode = r
                    denied = str(reviewer or "").startswith("DENIED:")
                    status_color = "var(--red)" if denied else "var(--green)"
                    status_text = "DENIED" if denied else "APPROVED"
                    reviewer_name = str(reviewer or "?").replace("DENIED:", "")
                    ts_short = str(ts or "")[:16]
                    audit_items += (
                        f"<div class='dg' style='padding:10px 18px;margin-bottom:5px'>"
                        f"<span style='color:{status_color};font-weight:800;font-family:var(--mono);font-size:12px;min-width:64px'>{status_text}</span>"
                        f"<span class='dg-t' style='font-size:12px'>#{oid}</span>"
                        f"<span style='font-size:13px;color:var(--champagne);min-width:90px'>{esc(str(op_type or ''))}</span>"
                        f"<span class='dg-d' style='font-size:14px'>{esc(str(table or ''))}</span>"
                        f"<span class='dg-r' style='font-size:13px'>{rows if rows else 0:,} rows</span>"
                        f"<span style='font-size:12px;color:var(--t2);min-width:90px'>{esc(reviewer_name[:14])}</span>"
                        f"<span style='font-size:11px;color:var(--t3)'>{esc(ts_short)}</span>"
                        f"</div>")
                audit_slide = f"""
<div class="slide" style="height:auto;min-height:400px"><div class="pad">
  <div class="kick">Audit Trail</div>
  <h2>{len(audit_rows)} operations. <span style="color:var(--green)">{n_approved} approved</span>{f', <span style="color:var(--red)">{n_denied} denied</span>' if n_denied else ''}.</h2>
  <p class="voice" style="margin-bottom:24px">Every remediation logged with reviewer identity, device, and full SQL. Denied proposals tracked too.</p>
  <div style="max-width:960px">{audit_items}</div>
  <div style="margin-top:32px;font-size:14px;color:var(--t3);font-family:var(--mono)">python sentinel.py --history &nbsp;&bull;&nbsp; --show N &nbsp;&bull;&nbsp; --undo N</div>
</div></div>"""
        except Exception:
            pass

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Healthcare Sentinel &mdash; Triage Report</title>
<style>
:root{{--champagne:#E6C470;--green:#22C55E;--red:#EF4444;--amber:#F59E0B;--blue:#7FB2FF;
--bg:#08080D;--surface:#12131A;--surface2:#1A1B24;--bs:rgba(255,255,255,0.08);
--t1:#EDEDF0;--t2:#9a9a9a;--t3:#555;--mono:ui-monospace,'SF Mono',Menlo,monospace}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Avenir Next',Avenir,-apple-system,sans-serif;background:#000;color:var(--t1);-webkit-font-smoothing:antialiased}}
.slide{{width:1280px;height:854px;margin:24px auto;background:var(--bg);position:relative;overflow:hidden;display:flex;flex-direction:column;border:1px solid var(--bs)}}
.pad{{padding:74px 88px 80px;flex:1;display:flex;flex-direction:column}}
.mid{{flex:1;display:flex;flex-direction:column;justify-content:center}}
.foot{{position:absolute;left:0;right:0;bottom:0;display:flex;justify-content:space-between;padding:0 56px 24px;font-size:12px;color:var(--t3)}}
.kick{{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.14em;color:var(--champagne);margin-bottom:20px}}
h1{{font-size:56px;font-weight:800;letter-spacing:-.03em;line-height:1.08}} h1 span{{color:var(--champagne)}}
h2{{font-size:42px;font-weight:800;letter-spacing:-.025em;line-height:1.18}}
.voice{{font-size:24px;color:var(--t2);line-height:1.55;max-width:36ch;margin-top:20px}}
.voice b{{color:var(--t1)}}
.big{{font-size:160px;font-weight:800;line-height:.85;color:var(--champagne)}}
.sev{{font-size:10px;font-weight:800;padding:4px 12px;border-radius:6px;text-transform:uppercase;letter-spacing:.05em}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;margin-top:48px}}
.stat{{background:var(--surface);border:1px solid var(--bs);border-radius:14px;padding:32px;text-align:center}}
.stat .n{{font-size:56px;font-weight:800;line-height:1}}
.stat .l{{font-size:11px;color:var(--t3);text-transform:uppercase;letter-spacing:.06em;margin-top:10px}}
.stat.crit .n{{color:var(--red)}} .stat.high .n{{color:var(--amber)}} .stat.med .n{{color:var(--champagne)}} .stat.low .n{{color:var(--green)}}
.lineage{{display:flex;align-items:center;gap:10px;padding:16px 20px;background:var(--surface);border:1px solid var(--bs);border-radius:10px;font-family:var(--mono);font-size:16px}}
.ln{{padding:6px 14px;border-radius:6px;font-size:15px}} .ln.s{{background:rgba(127,178,255,.1);color:var(--blue)}} .ln.d{{background:rgba(239,68,68,.1);color:var(--red)}}
.la{{color:var(--t3);font-size:18px}}
.act{{display:flex;align-items:flex-start;gap:12px;padding:12px 20px;background:var(--surface);border:1px solid var(--bs);border-radius:10px;margin-bottom:8px;font-size:13px;color:var(--t2);font-family:var(--mono);word-break:break-all;line-height:1.4}}
.act-i{{width:28px;height:28px;border-radius:7px;display:grid;place-items:center;font-size:13px;font-weight:800;flex-shrink:0}}
.trace{{background:var(--surface);border:1px solid var(--bs);border-radius:14px;padding:20px;overflow-y:auto;flex:1}}
.tl{{display:flex;gap:14px;padding:5px 0;font-size:11px;font-family:var(--mono);border-bottom:1px solid var(--bs);align-items:flex-start}}
.tl-t{{color:var(--champagne);font-weight:700;min-width:170px;flex-shrink:0}} .tl-a{{color:var(--t3);word-break:break-all;white-space:normal;line-height:1.4}}
.dg{{display:flex;align-items:center;gap:16px;padding:13px 18px;background:var(--surface);border:1px solid var(--bs);border-radius:10px;margin-bottom:8px;font-size:15px}}
.dg .sev{{flex-shrink:0;min-width:64px;text-align:center}}
.dg-d{{color:var(--t1);font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.dg-t{{font-family:var(--mono);color:var(--champagne);font-size:13px;white-space:nowrap;flex-shrink:0}}
.dg-r{{color:var(--t2);font-weight:700;min-width:88px;text-align:right;white-space:nowrap;flex-shrink:0}}
</style></head><body>

<div class="slide"><div class="pad" style="justify-content:center">
  <div class="kick">Triage Report</div>
  <h1>Healthcare <span>Sentinel</span></h1>
  <p class="voice"><b>{source_rows:,} patient records</b> across <b>{len(datasets)} datasets</b> ({total_rows:,} rows scanned).<br>Here's what I found.</p>
  <div style="margin-top:auto;font-size:13px;color:var(--t3)">{scan_time} &bull; {MODEL}</div>
</div><div class="foot"><span>Healthcare Sentinel</span><span>1</span></div></div>

<div class="slide"><div class="pad mid">
  <div class="big">{len(findings)}</div>
  <h2 style="margin-top:12px">issues found.</h2>
  <div class="stats">
    <div class="stat crit"><div class="n">{counts['CRITICAL']}</div><div class="l">Critical</div></div>
    <div class="stat high"><div class="n">{counts['HIGH']}</div><div class="l">High</div></div>
    <div class="stat med"><div class="n">{counts['MEDIUM']}</div><div class="l">Medium</div></div>
    <div class="stat low"><div class="n">{counts['LOW']}</div><div class="l">Low</div></div>
  </div>
</div><div class="foot"><span>Healthcare Sentinel</span><span>2</span></div></div>

{finding_slides}

<div class="slide" style="height:auto;min-height:854px"><div class="pad">
  <div class="kick">Write-Back</div>
  <h2>I tagged these in DataHub.</h2>
  <p class="voice">The next person sees my warnings.</p>
  <div style="margin-top:32px">{action_items}</div>
</div><div class="foot" style="position:relative"><span>Healthcare Sentinel</span></div></div>

{trace_slide}

{audit_slide}

<div class="slide"><div class="pad" style="justify-content:center;align-items:center;text-align:center">
  <h1>Healthcare <span>Sentinel</span></h1>
  <p class="voice" style="text-align:center;max-width:none">Triage complete.</p>
</div><div class="foot"><span>Healthcare Sentinel</span></div></div>

</body></html>"""

    report_path.write_text(html, encoding="utf-8")
    # Symlink latest for easy access
    latest = REPORT_DIR / "triage-report.html"
    try:
        latest.unlink(missing_ok=True)
        latest.symlink_to(report_path.name)
    except OSError:
        pass
    return str(report_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Healthcare Sentinel — Clinical Data Triage Agent")
    parser.add_argument("--dry-run", action="store_true", help="Use built-in rules, no API calls (free)")
    parser.add_argument("--auto-approve", action="store_true", help="Skip human approval for DataHub writes")
    parser.add_argument("--undo", type=int, metavar="OP_ID", help="Undo a remediation operation by its ID")
    parser.add_argument("--history", action="store_true", help="Show audit trail of all remediation operations")
    parser.add_argument("--show", type=int, metavar="OP_ID", help="Show full detail of a remediation operation")
    parser.add_argument("--watch", metavar="DIR", help="Watch a directory for new/changed data files (.csv, .db, .xlsx, .xls)")
    parser.add_argument("--no-verify", action="store_true", help="Skip the verification pass (single-pass mode)")
    parser.add_argument("--reset", action="store_true", help="Reset to clean state: restore original DB and clear DataHub learnings")
    args = parser.parse_args()

    # Handle --reset before anything else
    if args.reset:
        print(f"\n  {_YELLOW('↻')} Resetting to clean state...")
        # 1. Restore original DB from earliest snapshot
        snap_dir = Path("snapshots")
        snaps = sorted(snap_dir.glob("healthcare_*.db")) if snap_dir.exists() else []
        if snaps:
            shutil.copy2(snaps[0], SQLITE_DB)
            print(f"  {_GREEN('✓')} DB restored from: {snaps[0]}")
        else:
            print(f"  Warning: no snapshots found in snapshots/")

        # 2. Clear DataHub learnings and audit docs
        try:
            from datahub.sdk.main_client import DataHubClient
            client = DataHubClient(server=DATAHUB_SERVER)
            docs = find_sentinel_documents(client._graph)
            deleted = 0
            for d in docs:
                try:
                    client._graph.hard_delete_entity(d["urn"])
                    deleted += 1
                except Exception as e:
                    print(f"  Warning: could not delete '{d['title']}': {e}")
            print(f"  {_GREEN('✓')} DataHub learnings cleared ({deleted} of {len(docs)} documents deleted)")
        except Exception as e:
            print(f"  Warning: could not clear DataHub learnings: {e}")
            print(f"  (DataHub may not be running — DB was still restored)")

        # 3. Clear report directory
        report_dir = Path("report")
        if report_dir.exists():
            for f in report_dir.glob("triage-report_*.html"):
                f.unlink()
            print(f"  {_GREEN('✓')} Reports cleared")

        print(f"\n  {_GREEN('✓')} Clean state ready. Run: {_BOLD('python sentinel.py')}")
        return

    # Set run mode for audit trail
    global _CURRENT_RUN_MODE
    if args.dry_run:
        _CURRENT_RUN_MODE = "dry-run"
    elif args.auto_approve:
        _CURRENT_RUN_MODE = "auto-approve"
    elif args.watch:
        _CURRENT_RUN_MODE = "watch"
    else:
        _CURRENT_RUN_MODE = "interactive"

    # Handle --undo, --history, --show before anything else
    if args.undo is not None or args.history or args.show is not None:
        if not Path(SQLITE_DB).exists():
            print(f"\nERROR: Database not found at {SQLITE_DB}")
            sys.exit(1)
        init_audit_db(SQLITE_DB)
        if args.undo is not None:
            print(f"\n  Undoing operation {args.undo}...")
            undo_operation(SQLITE_DB, args.undo)
        elif args.show is not None:
            show_operation_detail(SQLITE_DB, args.show)
        else:
            show_audit_history(SQLITE_DB)
        return

    print(_BOLD("=" * 60))
    print(_BOLD("  Healthcare Sentinel — Clinical Data Triage Agent"))
    if args.dry_run:
        print(f"  {_GRAY('[DRY RUN — no API calls, no DataHub writes]')}")
    elif not args.auto_approve:
        print(f"  {_YELLOW('[HUMAN-IN-THE-LOOP — you approve DataHub writes]')}")
    print(_BOLD("=" * 60))

    if not args.dry_run and not args.watch:
        if MODEL.startswith("claude") and not os.getenv("ANTHROPIC_API_KEY"):
            print("\nERROR: Set ANTHROPIC_API_KEY environment variable")
            print("  export ANTHROPIC_API_KEY=sk-ant-...")
            print("  Or use --dry-run to skip API calls")
            sys.exit(1)
        if MODEL.startswith("gemini") and not os.getenv("GOOGLE_API_KEY") and not os.getenv("GOOGLE_CLOUD_PROJECT"):
            print("\nERROR: Set up Gemini API access")
            print("  Option A — AI Studio (free):")
            print("    export GOOGLE_API_KEY=AIza...")
            print("    Get a key: https://aistudio.google.com/apikey")
            print("  Option B — Vertex AI:")
            print("    export GOOGLE_CLOUD_PROJECT=your-project-id")
            print("    export GOOGLE_GENAI_USE_VERTEXAI=1")
            print("    gcloud auth application-default login")
            sys.exit(1)

    if not Path(SQLITE_DB).exists():
        print(f"\nERROR: Database not found at {SQLITE_DB}")
        sys.exit(1)

    # Full DB snapshot before any remediation run (git-like reversibility)
    if not args.dry_run:
        try:
            snap_dir = Path("snapshots")
            snap_dir.mkdir(parents=True, exist_ok=True)
            snap_path = snap_dir / f"healthcare_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copy2(SQLITE_DB, snap_path)
            print(f"  {_GRAY('DB snapshot:')} {snap_path}")
        except OSError as e:
            print(f"\nERROR: could not write DB snapshot: {e}")
            sys.exit(1)

    try:
        init_audit_db(SQLITE_DB)
    except sqlite3.Error as e:
        print(f"\nERROR: could not initialize audit table: {e}")
        sys.exit(1)

    # Watch mode — monitor directory for file changes
    if args.watch:
        watch_dir = args.watch
        if not Path(watch_dir).is_dir():
            print(f"\nERROR: {watch_dir} is not a directory")
            sys.exit(1)

        def run_once():
            print(f"\n  [Watch] Change detected — running triage...")
            if args.dry_run:
                dry_run_pipeline(SQLITE_DB)
            else:
                try:
                    from datahub.sdk.main_client import DataHubClient
                    from datahub_agent_context.langchain_tools import build_langchain_tools
                    client = DataHubClient(server=DATAHUB_SERVER)
                    tools = build_langchain_tools(client, include_mutations=True)
                    KEEP = {"search", "search_documents", "get_entities", "list_schema_fields",
                            "get_lineage", "get_dataset_queries", "add_tags",
                            "update_description", "add_structured_properties", "save_document"}
                    tools = [wrap_save_document_upsert(t, client._graph) if t.name == "save_document" else t
                             for t in tools if t.name in KEEP]
                    run_agent(tools, SQLITE_DB, auto_approve=args.auto_approve)
                except Exception as e:
                    print(f"  ERROR: {e}")

        watch_loop(watch_dir, run_once)
        return

    if args.dry_run:
        print(f"\n[1/4] Discovering datasets in {SQLITE_DB}...")
        datasets, findings, actions = dry_run_pipeline(SQLITE_DB)
        tool_log = []
        print(f"\n  {len(findings)} findings across {len(datasets)} datasets")

        # Simulate batch approval UX with remediation proposals from findings
        remediation_proposals = []
        for f in findings:
            sev = f.get("severity", "")
            if sev not in ("CRITICAL", "HIGH"):
                continue
            tbl = f.get("table", "?")
            col = f.get("column", "?")
            check = f.get("check_name", "")
            rows = f.get("affected_rows", "?")
            if check == "impossible_ages":
                remediation_proposals.append({"action_type": "correct", "table": tbl,
                    "sql": f"UPDATE {tbl} SET {col}=NULL WHERE CAST({col} AS INT)<0 OR CAST({col} AS INT)>120",
                    "reason": f"{rows} impossible ages cause fatal drug dosing errors", "rows": rows})
            elif check == "missing_identifiers":
                remediation_proposals.append({"action_type": "quarantine", "table": tbl,
                    "sql": f"CREATE TABLE _quarantine_{tbl}_null_names AS SELECT * FROM {tbl} WHERE name IS NULL ||| DELETE FROM {tbl} WHERE name IS NULL",
                    "reason": f"{rows} unidentifiable patients — quarantine for review", "rows": rows})
            elif check == "discharge_before_admission":
                remediation_proposals.append({"action_type": "correct", "table": tbl,
                    "sql": f"UPDATE {tbl} SET discharge_date=NULL WHERE discharge_date < date_of_admission",
                    "reason": f"{rows} chronology violations — nulling discharge_date", "rows": rows})
            elif check == "negative_billing":
                remediation_proposals.append({"action_type": "correct", "table": tbl,
                    "sql": f"UPDATE {tbl} SET {col}=NULL WHERE CAST({col} AS REAL)<0",
                    "reason": f"{rows} negative amounts corrupt revenue accounting", "rows": rows})
            elif check == "propagated_bad_ages":
                remediation_proposals.append({"action_type": "correct", "table": tbl,
                    "sql": f"UPDATE {tbl} SET {col}=NULL WHERE {col}<0 OR {col}>120",
                    "reason": f"{rows} bad ages propagated from upstream — cascade fix", "rows": rows})

        if remediation_proposals:
            print(f"\n  {_YELLOW('BATCH APPROVAL')} {_GRAY('—')} {_BOLD(f'{len(remediation_proposals)} remediation(s) proposed')}")
            print(f"  {_GRAY('─' * 56)}")
            for i, p in enumerate(remediation_proposals, 1):
                atype = p['action_type']
                if isinstance(p['rows'], int):
                    print(f"  {i}. {_YELLOW(f'[{atype}]')} {_BOLD(p['table'])} ({p['rows']:,} rows)")
                else:
                    print(f"  {i}. {_YELLOW(f'[{atype}]')} {_BOLD(p['table'])}")
                print(f"     {_GRAY('SQL:')} {p['sql']}")
                print(f"     {_GRAY('Reason:')} {p['reason']}")
            print(f"  {_GRAY('─' * 56)}")
            try:
                choice = input(f"  {_YELLOW('Approve which?')} [all / 1,3 / none] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "none"
            approved = parse_approval_choice(choice, len(remediation_proposals))
            # Execute approved remediation proposals
            for i, p in enumerate(remediation_proposals):
                if i in approved:
                    result = execute_fix(SQLITE_DB, p["action_type"], p["table"], p["sql"], p["reason"])
                    print(f"  {_GREEN('✓')} {result}")
                else:
                    ctx = _get_device_context()
                    try:
                        audit_conn = sqlite3.connect(SQLITE_DB, timeout=30)
                        audit_conn.execute(_AUDIT_DDL)
                        audit_conn.execute(
                            "INSERT INTO _sentinel_audit (run_id,ts,op_type,target_table,sql_text,rows_affected,rationale,approved_by,hostname,ip_address,run_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (datetime.now().strftime("%Y%m%d_%H%M%S"), datetime.now().isoformat(),
                             p["action_type"], p["table"], p["sql"], 0, p["reason"],
                             f"DENIED:{ctx['reviewer']}", ctx["hostname"], ctx["ip_address"], _CURRENT_RUN_MODE))
                        audit_conn.commit()
                        audit_conn.close()
                    except Exception:
                        pass
                    print(f"  {_RED('✗')} DENIED: {p['action_type']} on {p['table']}")
    else:
        print(f"\n{_YELLOW('[Agent]')} Connecting to DataHub Agent Context Kit...")
        try:
            from datahub.sdk.main_client import DataHubClient
            from datahub_agent_context.langchain_tools import build_langchain_tools

            client = DataHubClient(server=DATAHUB_SERVER)
            tools = build_langchain_tools(client, include_mutations=True)

            KEEP = {"search", "search_documents", "get_entities", "list_schema_fields",
                    "get_lineage", "get_dataset_queries", "add_tags",
                    "update_description", "save_document"}
            if os.getenv("SENTINEL_STRUCTURED_PROPS"):
                KEEP.add("add_structured_properties")
            tools = [wrap_save_document_upsert(t, client._graph) if t.name == "save_document" else t
                     for t in tools if t.name in KEEP]
            print(f"  {_GREEN('✓')} Connected. {len(tools)} DataHub tools loaded.")
            print(f"  {_GRAY('Memory: search_documents (recall) + save_document (learn)')}")
        except Exception as e:
            print(f"  {_RED('✗')} DataHub connection failed: {e}")
            print(f"  {_GRAY('Tip: Start Docker Desktop, then run: datahub docker quickstart')}")
            sys.exit(1)

        # Pre-fetch learnings so the agent can't skip Recall
        prior_doc = latest_learnings(client._graph)
        prior_learnings = prior_doc["content"][:4000] if prior_doc else None
        if prior_learnings:
            print(f"  {_BLUE('↻')} Prior learnings found ({len(prior_learnings)} chars)")
        else:
            print(f"  {_GRAY('No prior learnings — first run since reset')}")

        learnings_prefix = ""
        if prior_learnings:
            learnings_prefix = (
                f"PRIOR LEARNINGS (for Phase 0 RECALL only — verify these first, then proceed):\n"
                f"{prior_learnings}\n\n"
                f"After verifying learnings, you MUST continue through ALL phases (1-8). "
                f"New tables may exist that learnings don't cover. Run sqlite_master to find them.\n\n"
            )

        # ── Two-pass flow: full triage → verification ─────────────────
        # Architecture decision: two-pass sequential outperformed parallel
        # multi-specialist orchestration (3 specialists + verifier) in
        # testing. The parallel approach produced 0 parsed findings across
        # all models tested — specialists investigated thoroughly but
        # exhausted recursion limits before outputting structured JSON.
        # Two-pass preserves the verification value (independent second
        # opinion) while keeping the reliable single-agent triage that
        # consistently produces 6 findings with proper batch approval.

        kickoff = f"{learnings_prefix}{KICKOFF}"
        print(f"\n{_BOLD(_BLUE('[Pass 1]'))} Full triage scan...\n")
        final_text, tool_log = run_agent(tools, SQLITE_DB, auto_approve=args.auto_approve, kickoff=kickoff)

        # Try to parse structured JSON from the agent's final output.
        # If the model doesn't produce JSON (common with Gemini), reconstruct
        # findings from the agent's actual tool calls — what it investigated
        # via run_sql and remediated via apply_fix. No hardcoded hints.
        findings = enforce_severity(parse_findings(final_text))
        tool_findings = enforce_severity(extract_findings_from_tool_log(tool_log))
        if not findings:
            findings = tool_findings
        elif tool_findings:
            existing_keys = {(f.get("table"), f.get("check_name"), f.get("column")) for f in findings}
            for tf in tool_findings:
                key = (tf.get("table"), tf.get("check_name"), tf.get("column"))
                if key not in existing_keys:
                    findings.append(tf)
                    existing_keys.add(key)
                else:
                    for f in findings:
                        if (f.get("table"), f.get("check_name"), f.get("column")) == key:
                            if not f.get("affected_rows") and tf.get("affected_rows"):
                                f["affected_rows"] = tf["affected_rows"]
                            break

        findings = collapse_downstream_findings(findings)

        # ── Pass 2: Verification scan ──────────────────────────────────
        # Architecture decision: a second pass with the verifier prompt
        # re-checks each finding and looks for gaps. Skipped when findings
        # came from deterministic fallback (nothing to verify — they're
        # hardcoded). Only runs on AI-generated findings where confirmation
        # bias could produce false positives.
        if findings and not args.no_verify:
            print(f"\n{_BOLD(_BLUE('[Pass 2]'))} Review — verifying {len(findings)} findings + independent investigation...\n")
            verifier_prompt = _load_skill("verifier.md")
            if verifier_prompt:
                READ_ONLY = {"search", "search_documents", "get_entities",
                             "list_schema_fields", "get_lineage", "run_sql",
                             "report_finding"}
                verify_tools = [t for t in tools if t.name in READ_ONLY]
                findings_json = json.dumps(findings, indent=2, default=str)
                verify_kickoff = (
                    f"PHASE 1: Verify these {len(findings)} findings from the triage scan:\n\n"
                    f"{findings_json}\n\n"
                    f"Re-run SQL for each finding to confirm or refute it. "
                    f"For schema claims, use typeof() to verify actual storage type. "
                    f"Call report_finding for each CONFIRMED finding. "
                    f"Do NOT call report_finding for refuted findings.\n\n"
                    f"PHASE 2: After verifying, run your own independent investigation "
                    f"for issues the triage agent missed. Check typeof() on numeric "
                    f"columns, check materialized tables independently, look for "
                    f"cross-table type mismatches. Call report_finding for any NEW "
                    f"issues you discover."
                )
                verify_db = str(snap_path) if not args.dry_run else SQLITE_DB
                verify_text, verify_log = run_agent(
                    verify_tools, verify_db, auto_approve=True,
                    kickoff=verify_kickoff, system_prompt=verifier_prompt,
                    read_only=True)
                tool_log.extend(verify_log)

                # Additive verification: Pass 1 findings are the baseline.
                # Verifier can only REMOVE findings it explicitly refuted
                # (count=0 or typeof contradicts), and ADD new ones.
                verified = enforce_severity(parse_findings(verify_text))
                if not verified:
                    verified = enforce_severity(extract_findings_from_tool_log(verify_log))
                verified = collapse_downstream_findings(verified) if verified else []

                # Build set of what verifier confirmed
                confirmed_keys = {(f.get("table"), f.get("check_name"), f.get("column")) for f in verified}

                # Check verifier's SQL results for explicit refutations (count=0)
                refuted_keys = set()
                for entry in verify_log:
                    if entry.get("tool") != "run_sql":
                        continue
                    sql = (entry.get("args", {}) or {}).get("query", "")
                    result = str(entry.get("result", ""))
                    if "COUNT(*)" not in sql.upper():
                        continue
                    import re
                    m = re.search(r'\[\[(\d+)\]\]', result) or re.search(r'"rows"\s*:\s*\[\s*\[\s*(\d+)', result)
                    if m and int(m.group(1)) == 0:
                        sql_lower = sql.lower()
                        for f in findings:
                            t = f.get("table", "")
                            c = f.get("column", "")
                            if t in sql_lower and c and c in sql_lower:
                                refuted_keys.add((t, f.get("check_name"), c))

                # Start from Pass 1, remove only explicit refutations, add verifier extras
                kept = [f for f in findings
                        if (f.get("table"), f.get("check_name"), f.get("column")) not in refuted_keys]
                kept_keys = {(f.get("table"), f.get("check_name"), f.get("column")) for f in kept}
                kept_table_col = {(f.get("table"), f.get("column")) for f in kept if f.get("column")}
                for f in verified:
                    key = (f.get("table"), f.get("check_name"), f.get("column"))
                    if key in kept_keys:
                        continue
                    col = f.get("column", "")
                    if col and "," not in col and (f.get("table"), col) in kept_table_col:
                        continue
                    kept.append(f)
                    kept_keys.add(key)
                    if col and "," not in col:
                        kept_table_col.add((f.get("table"), col))

                # Re-collapse: reviewer may re-add downstream findings that
                # Pass 1 already collapsed into source downstream_contamination
                kept = collapse_downstream_findings(kept)

                n_refuted = len(findings) - len([f for f in findings if (f.get("table"), f.get("check_name"), f.get("column")) not in refuted_keys])
                n_added = len(kept) - len(findings) + n_refuted
                print(f"\n  {_GREEN('✓')} Reviewer: {_GRAY(f'{n_refuted} refuted')}, {_GREEN(f'{n_added} new')}, {_BOLD(f'{len(kept)} total')}")
                findings = kept

        # Shared post-processing for both single-agent and multi-agent paths
        datasets = []
        try:
            conn = sqlite3.connect(SQLITE_DB, timeout=30)
            try:
                for t in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                          if not r[0].startswith('_') and not r[0].startswith('sqlite_')]:
                    cols = conn.execute(f"PRAGMA table_info([{t}])").fetchall()
                    rc = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                    datasets.append({"table": t, "columns": [{"name": c[1], "type": c[2] or "TEXT"} for c in cols], "row_count": rc})
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"  Warning: could not read dataset inventory: {e}")

        actions = []
        for e in tool_log:
            a = e.get("args") if isinstance(e.get("args"), dict) else {}
            if e.get("tool") == "add_tags":
                urns = a.get("entity_urns")
                name = str(urns[0]).split(".")[-1] if isinstance(urns, list) and urns else "?"
                actions.append({"type": "tag", "table": name, "tag": str(a.get("tag_urns", [""]))})
            elif e.get("tool") == "update_description":
                actions.append({"type": "description",
                                "table": str(a.get("entity_urn", "")).split(".")[-1],
                                "text": str(a.get("description", ""))[:80]})

    print(f"\n{_BOLD('[Report]')} Generating triage report...")
    try:
        report_path = generate_report(datasets, findings, actions, tool_log, db_path=SQLITE_DB)
        print(f"  {_GREEN('✓')} Saved to: {report_path}")
        try:
            webbrowser.open(f"file://{Path(report_path).resolve()}")
        except Exception:
            pass
    except Exception as e:
        print(f"  ERROR: could not generate report: {e}")

    print(f"\n{_BOLD('=' * 60)}")
    print(f"  {_GREEN('✓')} Triage complete. {_BOLD(f'{len(findings)} findings')} across {len(datasets)} datasets.")
    sev_counts = {}
    for f in findings:
        s = f.get("severity", "?")
        sev_counts[s] = sev_counts.get(s, 0) + 1
    sev_parts = []
    for k, v in sorted(sev_counts.items()):
        color = _RED if k == "CRITICAL" else (_YELLOW if k == "HIGH" else _GRAY)
        sev_parts.append(f"{color(f'{v} {k}')}")
    print(f"  Severity: {', '.join(sev_parts)}")
    if tool_log:
        print(f"  {_GRAY(f'Agent made {len(tool_log)} tool calls')}")
    print(_BOLD("=" * 60))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        sys.exit(130)
