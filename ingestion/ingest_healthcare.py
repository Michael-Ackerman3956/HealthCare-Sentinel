"""Register healthcare SQLite datasets in DataHub with schemas + lineage."""
import sqlite3
from pathlib import Path
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.metadata.schema_classes import (
    DatasetSnapshotClass,
    SchemaMetadataClass,
    SchemaFieldClass,
    OtherSchemaClass,
    UpstreamClass,
    UpstreamLineageClass,
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    TagAssociationClass,
    GlobalTagsClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
)
from datahub.metadata.com.linkedin.pegasus2avro.schema import (
    StringType,
    NumberType,
)

EMITTER = DatahubRestEmitter("http://localhost:8080")
PLATFORM = "sqlite"
DB = str(Path(__file__).resolve().parent.parent / "sample-data" / "healthcare.db")

SQLITE_TO_DH = {
    "TEXT": StringType,
    "INT": NumberType,
    "REAL": NumberType,
    "INTEGER": NumberType,
    "": StringType,
}


def dataset_urn(table_name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{PLATFORM},healthcare.{table_name},PROD)"


def get_columns(db_path: str, table_name: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    cols = []
    for row in cursor:
        _, name, col_type, *_ = row
        cols.append({"name": name, "type": col_type or "TEXT"})
    conn.close()
    return cols


def emit_dataset(table_name: str, description: str, is_view: bool = False):
    urn = dataset_urn(table_name)
    columns = get_columns(DB, table_name if not is_view else table_name)

    props = MetadataChangeProposalWrapper(
        entityUrn=urn,
        aspect=DatasetPropertiesClass(
            name=table_name,
            description=description,
            customProperties={
                "database": "healthcare.db",
                "platform_instance": "healthcare",
                "is_view": str(is_view),
            },
        ),
    )
    EMITTER.emit(props)

    if columns:
        fields = []
        for col in columns:
            dh_type = SQLITE_TO_DH.get(col["type"].upper(), StringType)
            fields.append(
                SchemaFieldClass(
                    fieldPath=col["name"],
                    type={"type": dh_type()},
                    nativeDataType=col["type"] or "TEXT",
                    description="",
                )
            )

        schema = MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=SchemaMetadataClass(
                schemaName=table_name,
                platform=f"urn:li:dataPlatform:{PLATFORM}",
                version=0,
                hash="",
                platformSchema=OtherSchemaClass(rawSchema=""),
                fields=fields,
            ),
        )
        EMITTER.emit(schema)

    print(f"  registered: {table_name} ({len(columns)} columns)")


def emit_lineage(downstream: str, upstreams: list[str]):
    urn = dataset_urn(downstream)
    upstream_refs = [
        UpstreamClass(
            dataset=dataset_urn(u),
            type=DatasetLineageTypeClass.TRANSFORMED,
        )
        for u in upstreams
    ]
    lineage = MetadataChangeProposalWrapper(
        entityUrn=urn,
        aspect=UpstreamLineageClass(upstreams=upstream_refs),
    )
    EMITTER.emit(lineage)
    print(f"  lineage: {' + '.join(upstreams)} -> {downstream}")


def emit_tags(table_name: str, tags: list[str]):
    urn = dataset_urn(table_name)
    tag_assocs = [TagAssociationClass(tag=f"urn:li:tag:{t}") for t in tags]
    tag_aspect = MetadataChangeProposalWrapper(
        entityUrn=urn,
        aspect=GlobalTagsClass(tags=tag_assocs),
    )
    EMITTER.emit(tag_aspect)
    print(f"  tags on {table_name}: {tags}")


def main():
    print("Registering healthcare datasets...")

    emit_dataset("raw_patients",
        "Raw patient records — 55,500 rows. Contains planted data quality issues: "
        "negative billing amounts, null names, invalid ages, swapped dates.")

    emit_dataset("staging_patients",
        "Staged patient data with cleaned columns (lowercase, trimmed). "
        "Zero filtering — all defects from raw flow through.")

    emit_dataset("mart_billing",
        "Billing mart: billing amounts, length of stay, insurance. "
        "Inherits swapped-date bug as negative length_of_stay_days.")

    emit_dataset("mart_demographics",
        "Demographics mart: age, gender, blood type, conditions. "
        "Inherits invalid ages (-88 to 285) from raw.")

    emit_dataset("v_staging_from_raw",
        "View: transforms raw_patients with LOWER/TRIM on gender, blood_type, "
        "condition, admission_type, test_results. Adds pipeline_status='staged'.",
        is_view=True)

    emit_dataset("v_billing_from_staging",
        "View: extracts billing fields from staging, calculates length_of_stay_days "
        "via JULIANDAY(discharge_date) - JULIANDAY(date_of_admission).",
        is_view=True)

    emit_dataset("v_demographics_from_staging",
        "View: extracts demographic fields from staging, casts age to INTEGER.",
        is_view=True)

    print("\nSetting up lineage...")

    # Pipeline: raw -> staging -> marts (via views)
    emit_lineage("v_staging_from_raw", ["raw_patients"])
    emit_lineage("staging_patients", ["v_staging_from_raw"])
    emit_lineage("v_billing_from_staging", ["staging_patients"])
    emit_lineage("v_demographics_from_staging", ["staging_patients"])
    emit_lineage("mart_billing", ["v_billing_from_staging"])
    emit_lineage("mart_demographics", ["v_demographics_from_staging"])

    print("\nApplying tags...")

    emit_tags("raw_patients", ["pii", "raw-layer", "quality-issues"])
    emit_tags("staging_patients", ["pii", "staging-layer"])
    emit_tags("mart_billing", ["billing", "mart-layer", "quality-issues"])
    emit_tags("mart_demographics", ["demographics", "mart-layer", "quality-issues"])

    print("\nDone! Visit http://localhost:9002 to see the datasets.")


if __name__ == "__main__":
    main()
