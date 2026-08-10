#!/usr/bin/env python3
"""Add lab_results table to simulate new data arriving between triage runs.

Use between Run 1 and Run 2 to demonstrate compound learning:
  Run 1: python sentinel.py              # triages raw_patients
  Run 2: python sample-data/add_lab_results.py  # new data arrives
  Run 3: python sentinel.py              # agent recalls learnings, triages new table
"""

import random
import sqlite3
from datetime import datetime, timedelta

random.seed(42)

DB = "sample-data/healthcare.db"

FIRST_NAMES = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda",
               "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]

LAB_TESTS = [
    ("hemoglobin", "g/dL", 7.0, 17.5),
    ("glucose", "mg/dL", 70, 200),
    ("creatinine", "mg/dL", 0.5, 1.5),
    ("potassium", "mEq/L", 3.5, 5.5),
    ("sodium", "mEq/L", 135, 148),
    ("white_blood_cells", "K/uL", 4.0, 11.0),
    ("platelet_count", "K/uL", 150, 400),
]


def main():
    conn = sqlite3.connect(DB)

    if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='lab_results'").fetchone()[0]:
        print("lab_results already exists — dropping and recreating")
        conn.execute("DROP TABLE lab_results")

    conn.execute("""CREATE TABLE lab_results (
        result_id INTEGER PRIMARY KEY,
        patient_name TEXT,
        patient_id TEXT,
        test_name TEXT,
        result_value REAL,
        unit TEXT,
        reference_low REAL,
        reference_high REAL,
        collection_date TEXT,
        report_date TEXT,
        ordering_physician TEXT,
        status TEXT
    )""")

    n = 5000
    rows = []
    for i in range(n):
        name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        pid = f"P{random.randint(10000, 99999)}"
        test = random.choice(LAB_TESTS)
        test_name, unit, ref_low, ref_high = test
        mid = (ref_low + ref_high) / 2
        spread = (ref_high - ref_low) / 2
        value = round(random.gauss(mid, spread * 0.3), 1)
        collect_date = (datetime(2024, 1, 1) + timedelta(days=random.randint(0, 365))).strftime("%Y-%m-%d")
        report_date = (datetime.strptime(collect_date, "%Y-%m-%d") + timedelta(days=random.randint(0, 3))).strftime("%Y-%m-%d")
        physician = f"Dr. {random.choice(LAST_NAMES)}"
        status = "final"

        # Planted issues (similar PATTERNS to raw_patients but different domain)

        # Impossible values (like impossible ages — same pattern, different data)
        if random.random() < 0.03:
            value = round(random.choice([-5.2, -12.0, ref_high * 10, ref_high * 50]), 1)

        # Unit mismatch (novel issue — agent must discover this independently)
        if random.random() < 0.02:
            if unit == "mg/dL":
                unit = "mmol/L"

        # Report before collection (like discharge before admission — same pattern)
        if random.random() < 0.015:
            report_date = (datetime.strptime(collect_date, "%Y-%m-%d") - timedelta(days=random.randint(1, 10))).strftime("%Y-%m-%d")

        # Missing patient_id (like missing names — same pattern)
        if random.random() < 0.02:
            pid = ""

        # NULL physician (like missing identifiers)
        if random.random() < 0.01:
            physician = None

        rows.append((i, name, pid, test_name, value, unit, ref_low, ref_high,
                     collect_date, report_date, physician, status))

    conn.executemany("INSERT INTO lab_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()

    # Summary
    neg = conn.execute("SELECT COUNT(*) FROM lab_results WHERE result_value < 0").fetchone()[0]
    unit_mm = conn.execute("SELECT COUNT(*) FROM lab_results WHERE unit = 'mmol/L'").fetchone()[0]
    report_before = conn.execute("SELECT COUNT(*) FROM lab_results WHERE report_date < collection_date").fetchone()[0]
    empty_pid = conn.execute("SELECT COUNT(*) FROM lab_results WHERE patient_id = ''").fetchone()[0]
    null_doc = conn.execute("SELECT COUNT(*) FROM lab_results WHERE ordering_physician IS NULL").fetchone()[0]
    conn.close()

    print(f"\n  New dataset added: lab_results ({n} rows)")
    print(f"  Planted issues:")
    print(f"    Impossible values (negative/extreme): {neg}")
    print(f"    Unit mismatches (mg/dL → mmol/L):     {unit_mm}")
    print(f"    Report before collection date:        {report_before}")
    print(f"    Missing patient_id:                   {empty_pid}")
    print(f"    NULL physician:                       {null_doc}")
    print(f"\n  The agent should detect these using patterns learned from raw_patients:")
    print(f"    'impossible values' pattern  → catches negative lab values")
    print(f"    'missing identifiers' pattern → catches empty patient_id")
    print(f"    'chronology violation' pattern → catches report before collection")
    print(f"    'unit mismatch' → novel discovery (not in prior learnings)")


if __name__ == "__main__":
    main()
