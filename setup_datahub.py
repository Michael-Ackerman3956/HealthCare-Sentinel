#!/usr/bin/env python3
"""Register Sentinel structured properties in DataHub.

Run once after `datahub docker quickstart` to enable Phase 6c structured
property writeback. Without this, add_structured_properties calls will fail
gracefully and the agent continues without them.

Usage:
    python setup_datahub.py
"""

import os

DATAHUB_SERVER = os.getenv("DATAHUB_GMS_URL", "http://localhost:8080")

PROPERTIES = [
    {
        "id": "sentinel.trust_score",
        "qualified_name": "sentinel.trust_score",
        "display_name": "Sentinel Trust Score",
        "description": "0-100 data quality score from Healthcare Sentinel (100=clean, 0=critically unsafe)",
        "value_type": "string",
        "entity_types": ["dataset"],
    },
    {
        "id": "sentinel.worst_severity",
        "qualified_name": "sentinel.worst_severity",
        "display_name": "Sentinel Worst Severity",
        "description": "Worst finding severity: CRITICAL, HIGH, MEDIUM, LOW, or CLEAN",
        "value_type": "string",
        "entity_types": ["dataset"],
    },
    {
        "id": "sentinel.finding_count",
        "qualified_name": "sentinel.finding_count",
        "display_name": "Sentinel Finding Count",
        "description": "Total data quality issues found on this dataset",
        "value_type": "string",
        "entity_types": ["dataset"],
    },
    {
        "id": "sentinel.top_issue",
        "qualified_name": "sentinel.top_issue",
        "display_name": "Sentinel Top Issue",
        "description": "One-sentence description of the worst finding",
        "value_type": "string",
        "entity_types": ["dataset"],
    },
    {
        "id": "sentinel.last_triage",
        "qualified_name": "sentinel.last_triage",
        "display_name": "Sentinel Last Triage",
        "description": "Date of last triage run (YYYY-MM-DD)",
        "value_type": "string",
        "entity_types": ["dataset"],
    },
    {
        "id": "sentinel.remediation_status",
        "qualified_name": "sentinel.remediation_status",
        "display_name": "Sentinel Remediation Status",
        "description": "Summary of remediation actions taken (e.g. '2 fixed, 1 pending')",
        "value_type": "string",
        "entity_types": ["dataset"],
    },
]


def main():
    try:
        from datahub.sdk.main_client import DataHubClient
    except ImportError:
        print("ERROR: datahub SDK not installed. Run: pip install -r requirements.txt")
        return

    print(f"Connecting to DataHub at {DATAHUB_SERVER}...")
    try:
        client = DataHubClient(server=DATAHUB_SERVER)
        graph = client._graph
    except Exception as e:
        print(f"ERROR: cannot connect to DataHub: {e}")
        print("Tip: Start DataHub first with: datahub docker quickstart")
        return

    registered = 0
    for prop in PROPERTIES:
        urn = f"urn:li:structuredProperty:{prop['id']}"
        try:
            graph.execute_graphql(
                """mutation createStructuredProperty($input: CreateStructuredPropertyInput!) {
                    createStructuredProperty(input: $input) { urn }
                }""",
                variables={
                    "input": {
                        "id": prop["id"],
                        "qualifiedName": prop["qualified_name"],
                        "displayName": prop["display_name"],
                        "description": prop["description"],
                        "valueType": "urn:li:dataType:datahub.string",
                        "entityTypes": [f"urn:li:entityType:datahub.{t}" for t in prop["entity_types"]],
                        "cardinality": "SINGLE",
                    }
                },
            )
            print(f"  OK: {prop['id']} — {prop['display_name']}")
            registered += 1
        except Exception as e:
            err = str(e)
            if "already exists" in err.lower() or "duplicate" in err.lower():
                print(f"  SKIP: {prop['id']} — already registered")
                registered += 1
            else:
                print(f"  FAIL: {prop['id']} — {err[:120]}")

    print(f"\n{registered}/{len(PROPERTIES)} structured properties registered.")
    if registered == len(PROPERTIES):
        print("Phase 6c (add_structured_properties) will now work in sentinel.py.")
    else:
        print("Some properties failed. The agent will skip Phase 6c gracefully.")


if __name__ == "__main__":
    main()
