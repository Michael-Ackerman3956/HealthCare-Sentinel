#!/usr/bin/env python3
"""
SYNTHETIC DATA GENERATOR — Clinic 2 Patient Records

Simulates a second clinic sending patient data with DIFFERENT quality
issues than the original raw_patients. Same schema, different problems.

Use this to demonstrate compound learning:
  1. python sentinel.py                           # triage original data, agent learns
  2. python sample-data/add_clinic2_patients.py   # new synthetic data arrives
  3. python sentinel.py                           # agent recalls learnings, triages new data

Planted issues (different from raw_patients):
  - Future admission dates (not in original data)
  - Zero billing amounts (original had negative)
  - Duplicate patient names with conflicting ages
  - Invalid blood type strings (original had none)
  - Extreme billing >$500K (data entry errors)
  - Same-day discharge (0-day stays)
"""

import random
import sqlite3
from datetime import datetime, timedelta

random.seed(99)

DB = "sample-data/healthcare.db"

FIRST = ["Aisha", "Carlos", "Wei", "Fatima", "Dmitri", "Priya", "Hassan", "Yuki",
         "Olga", "Kwame", "Mei", "Abdul", "Sita", "Boris", "Amara", "Raj", "Lena"]
LAST = ["Patel", "Kim", "Santos", "Ahmed", "Nakamura", "Singh", "Okafor", "Chen",
        "Petrov", "Diaz", "Tanaka", "Ibrahim", "Nguyen", "Gupta", "Park"]
HOSPITALS = ["Metro General", "St. Mary's", "Regional Medical", "University Hospital",
             "County Health", "Pacific Medical"]
CONDITIONS = ["diabetes", "hypertension", "asthma", "pneumonia", "fracture",
              "appendicitis", "migraine", "anemia", "infection", "cardiac arrest"]
BLOOD = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
ADMISSION = ["Emergency", "Urgent", "Routine", "Elective"]
INSURANCE = ["Medicare", "Medicaid", "Blue Cross", "Aetna", "UnitedHealth", "Self-Pay"]


def main():
    conn = sqlite3.connect(DB)
    conn.execute("DROP TABLE IF EXISTS raw_patients_clinic2")

    # Get raw_patients schema
    cols = [r[1] for r in conn.execute("PRAGMA table_info(raw_patients)").fetchall()
            if r[1] != '_sentinel_flagged']  # skip sentinel columns
    print(f"Using schema from raw_patients: {len(cols)} columns")

    conn.execute(f"""CREATE TABLE raw_patients_clinic2 (
        {', '.join(f'[{c}] TEXT' for c in cols)}
    )""")

    n = 8000
    rows = []
    for i in range(n):
        name = f"{random.choice(FIRST)} {random.choice(LAST)}"
        age = str(random.randint(1, 95))
        gender = random.choice(["Male", "Female"])
        blood = random.choice(BLOOD)
        condition = random.choice(CONDITIONS)
        admit = (datetime(2023, 1, 1) + timedelta(days=random.randint(0, 700))).strftime("%Y-%m-%d")
        discharge = (datetime.strptime(admit, "%Y-%m-%d") + timedelta(days=random.randint(1, 30))).strftime("%Y-%m-%d")
        hospital = random.choice(HOSPITALS)
        doctor = f"Dr. {random.choice(LAST)}"
        admit_type = random.choice(ADMISSION)
        billing = round(random.uniform(500, 50000), 2)
        room = str(random.randint(100, 999))
        insurance = random.choice(INSURANCE)
        medication = random.choice(["Metformin", "Lisinopril", "Amoxicillin", "Ibuprofen", "Omeprazole"])

        # --- DIFFERENT PLANTED ISSUES (not the same as raw_patients) ---

        # Issue 1: Duplicate names with conflicting ages (~3%)
        if random.random() < 0.03:
            name = "John Smith"  # repeated name
            age = str(random.choice([25, 67, -3, 150]))  # conflicting ages

        # Issue 2: Future admission dates (~2%)
        if random.random() < 0.02:
            admit = (datetime.now() + timedelta(days=random.randint(30, 365))).strftime("%Y-%m-%d")
            discharge = (datetime.strptime(admit, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")

        # Issue 3: Billing amount = exactly 0 (suspicious, different from negative) ~2%
        if random.random() < 0.02:
            billing = 0.00

        # Issue 4: Mismatched gender/name patterns (e.g. male name with female gender) ~1.5%
        # Not catchable by simple rules but the agent might notice patterns

        # Issue 5: Invalid blood type strings (~1.5%)
        if random.random() < 0.015:
            blood = random.choice(["AB", "O", "B", "A", "unknown", "N/A", ""])

        # Issue 6: Room number as text with letters (~1%)
        if random.random() < 0.01:
            room = random.choice(["ICU-3", "ER-12", "N/A", ""])

        # Issue 7: Extremely high billing (>$500K — likely data entry error) ~0.5%
        if random.random() < 0.005:
            billing = round(random.uniform(500000, 5000000), 2)

        # Issue 8: Same admission and discharge date (0-day stay) ~2%
        if random.random() < 0.02:
            discharge = admit

        # Build row matching raw_patients column order
        row = {
            "name": name, "age": age, "gender": gender, "blood_type": blood,
            "medical_condition": condition, "date_of_admission": admit,
            "discharge_date": discharge, "doctor": doctor, "hospital": hospital,
            "insurance_provider": insurance, "billing_amount": str(billing),
            "room_number": room, "admission_type": admit_type,
            "discharge_disposition": "Home", "medication": medication,
        }
        row_vals = [row.get(c, "") for c in cols]
        rows.append(row_vals)

    placeholders = ", ".join(["?"] * len(cols))
    conn.executemany(f"INSERT INTO raw_patients_clinic2 VALUES ({placeholders})", rows)
    conn.commit()

    # Count planted issues
    print(f"\n  [Synthetic Data] Created raw_patients_clinic2 — {n} patient records from Clinic 2")
    print(f"  Same schema as raw_patients, different quality issues planted.")
    print(f"  Duplicate 'John Smith': {sum(1 for r in rows if r[0] == 'John Smith')}")
    future = conn.execute("SELECT COUNT(*) FROM raw_patients_clinic2 WHERE DATE(date_of_admission) > DATE('now')").fetchone()[0]
    print(f"  Future admissions: {future}")
    zero_bill = conn.execute("SELECT COUNT(*) FROM raw_patients_clinic2 WHERE CAST(billing_amount AS REAL) = 0").fetchone()[0]
    print(f"  Zero billing: {zero_bill}")
    bad_blood = conn.execute("SELECT COUNT(*) FROM raw_patients_clinic2 WHERE blood_type NOT IN ('A+','A-','B+','B-','AB+','AB-','O+','O-')").fetchone()[0]
    print(f"  Invalid blood types: {bad_blood}")
    same_day = conn.execute("SELECT COUNT(*) FROM raw_patients_clinic2 WHERE date_of_admission = discharge_date").fetchone()[0]
    print(f"  Same-day discharge: {same_day}")
    high_bill = conn.execute("SELECT COUNT(*) FROM raw_patients_clinic2 WHERE CAST(billing_amount AS REAL) > 500000").fetchone()[0]
    print(f"  Extreme billing (>$500K): {high_bill}")
    conn.close()
    print(f"\nDone. Run `python sentinel.py --dry-run` to see it discovered.")


if __name__ == "__main__":
    main()
