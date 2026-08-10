#!/usr/bin/env python3
"""Generate 2 extra synthetic healthcare datasets with planted quality issues.

Creates lab_results and medication_orders tables in healthcare.db.
Each has different issue types from raw_patients to test agent adaptability.
"""

import random
import sqlite3
from datetime import datetime, timedelta

random.seed(42)

DB = "sample-data/healthcare.db"

FIRST_NAMES = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda",
               "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
               "Thomas", "Sarah", "Charles", "Karen", "Emma", "Oliver", "Ava", "Liam", "Sophia"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
              "Rodriguez", "Martinez", "Hernandez", "Lopez", "Wilson", "Anderson", "Thomas"]

LAB_TESTS = [
    ("hemoglobin", "g/dL", 7.0, 17.5),
    ("glucose", "mg/dL", 70, 200),
    ("creatinine", "mg/dL", 0.5, 1.5),
    ("potassium", "mEq/L", 3.5, 5.5),
    ("sodium", "mEq/L", 135, 148),
    ("white_blood_cells", "K/uL", 4.0, 11.0),
    ("platelet_count", "K/uL", 150, 400),
]

MEDICATIONS = [
    ("metformin", 500, 2000, "mg"),
    ("lisinopril", 5, 40, "mg"),
    ("atorvastatin", 10, 80, "mg"),
    ("amoxicillin", 250, 1500, "mg"),
    ("omeprazole", 20, 40, "mg"),
    ("metoprolol", 25, 200, "mg"),
    ("amlodipine", 2.5, 10, "mg"),
    ("warfarin", 1, 10, "mg"),
    ("insulin_glargine", 10, 80, "units"),
    ("levothyroxine", 25, 200, "mcg"),
]

ALLERGIES = ["penicillin", "sulfa", "aspirin", "ibuprofen", "codeine", "latex", "none"]


def generate_lab_results(conn, n=5000):
    conn.execute("""CREATE TABLE IF NOT EXISTS lab_results (
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

    rows = []
    for i in range(n):
        name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        pid = f"P{random.randint(10000, 99999)}"
        test = random.choice(LAB_TESTS)
        test_name, unit, ref_low, ref_high = test
        mid = (ref_low + ref_high) / 2
        spread = (ref_high - ref_low) / 2

        # Normal value
        value = round(random.gauss(mid, spread * 0.3), 1)
        collect_date = (datetime(2024, 1, 1) + timedelta(days=random.randint(0, 365))).strftime("%Y-%m-%d")
        report_date = (datetime.strptime(collect_date, "%Y-%m-%d") + timedelta(days=random.randint(0, 3))).strftime("%Y-%m-%d")
        physician = f"Dr. {random.choice(LAST_NAMES)}"
        status = "final"

        # --- PLANTED ISSUES ---

        # Issue 1: Impossible lab values (negative or wildly out of range) ~3%
        if random.random() < 0.03:
            value = round(random.choice([-5.2, -12.0, ref_high * 10, ref_high * 50]), 1)

        # Issue 2: Unit mismatch — mg/dL recorded as mmol/L ~2%
        if random.random() < 0.02 and unit == "mg/dL":
            unit = "mmol/L"  # wrong unit, value not converted

        # Issue 3: Report date BEFORE collection date ~1.5%
        if random.random() < 0.015:
            report_date = (datetime.strptime(collect_date, "%Y-%m-%d") - timedelta(days=random.randint(1, 10))).strftime("%Y-%m-%d")

        # Issue 4: Missing patient_id ~2%
        if random.random() < 0.02:
            pid = ""

        # Issue 5: Duplicate results (same patient, same test, same date) — inserted as separate rows
        # ~1% get a duplicate
        if random.random() < 0.01:
            rows.append((i + n, name, pid, test_name, value, unit, ref_low, ref_high,
                         collect_date, report_date, physician, status))

        # Issue 6: NULL physician ~1%
        if random.random() < 0.01:
            physician = None

        # Issue 7: Status = 'preliminary' but no follow-up ~2%
        if random.random() < 0.02:
            status = "preliminary"

        rows.append((i, name, pid, test_name, value, unit, ref_low, ref_high,
                     collect_date, report_date, physician, status))

    conn.executemany("INSERT INTO lab_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"  lab_results: {len(rows)} rows, 7 planted issue types")
    return len(rows)


def generate_medication_orders(conn, n=4000):
    conn.execute("""CREATE TABLE IF NOT EXISTS medication_orders (
        order_id INTEGER PRIMARY KEY,
        patient_name TEXT,
        patient_id TEXT,
        medication TEXT,
        dosage REAL,
        dosage_unit TEXT,
        frequency TEXT,
        route TEXT,
        start_date TEXT,
        end_date TEXT,
        prescriber TEXT,
        allergy_check TEXT,
        status TEXT
    )""")

    frequencies = ["once daily", "twice daily", "three times daily", "every 6 hours", "as needed"]
    routes = ["oral", "IV", "subcutaneous", "topical", "inhaled"]

    rows = []
    for i in range(n):
        name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        pid = f"P{random.randint(10000, 99999)}"
        med = random.choice(MEDICATIONS)
        med_name, dose_min, dose_max, dose_unit = med
        dosage = round(random.uniform(dose_min, dose_max), 1)
        freq = random.choice(frequencies)
        route = random.choice(routes)
        start = (datetime(2024, 1, 1) + timedelta(days=random.randint(0, 365))).strftime("%Y-%m-%d")
        end = (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=random.randint(7, 90))).strftime("%Y-%m-%d")
        prescriber = f"Dr. {random.choice(LAST_NAMES)}"
        allergy = random.choice(ALLERGIES)
        status = "active"

        # --- PLANTED ISSUES ---

        # Issue 1: Dosage exceeds max safe limit ~3%
        if random.random() < 0.03:
            dosage = round(dose_max * random.uniform(2, 5), 1)

        # Issue 2: End date before start date ~2%
        if random.random() < 0.02:
            end = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=random.randint(1, 30))).strftime("%Y-%m-%d")

        # Issue 3: Allergy conflict — patient has penicillin allergy but prescribed amoxicillin ~1.5%
        if random.random() < 0.015 and med_name == "amoxicillin":
            allergy = "penicillin"  # amoxicillin + penicillin allergy = contraindicated

        # Issue 4: Missing prescriber ~2%
        if random.random() < 0.02:
            prescriber = None

        # Issue 5: Negative dosage ~1%
        if random.random() < 0.01:
            dosage = round(-random.uniform(1, 100), 1)

        # Issue 6: Wrong route for medication ~1.5%
        if random.random() < 0.015:
            if med_name in ("metformin", "lisinopril", "atorvastatin"):
                route = "IV"  # these are oral-only medications

        # Issue 7: Future start date (not yet valid) ~1%
        if random.random() < 0.01:
            start = (datetime.now() + timedelta(days=random.randint(30, 365))).strftime("%Y-%m-%d")

        rows.append((i, name, pid, med_name, dosage, dose_unit, freq, route,
                     start, end, prescriber, allergy, status))

    conn.executemany("INSERT INTO medication_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"  medication_orders: {len(rows)} rows, 7 planted issue types")
    return len(rows)


def main():
    conn = sqlite3.connect(DB)

    # Drop if exists (re-runnable)
    conn.execute("DROP TABLE IF EXISTS lab_results")
    conn.execute("DROP TABLE IF EXISTS medication_orders")

    print("Generating extra healthcare datasets...")
    n1 = generate_lab_results(conn)
    n2 = generate_medication_orders(conn)
    conn.commit()
    conn.close()

    print(f"\nDone. {n1 + n2} total rows added to {DB}")
    print("Run `python sentinel.py --dry-run` to triage the new tables.")


if __name__ == "__main__":
    main()
