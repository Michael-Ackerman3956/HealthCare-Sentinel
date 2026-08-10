"""Register fiction-retail SQLite datasets in DataHub with schemas + lineage."""
import sqlite3
from pathlib import Path
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.metadata.schema_classes import (
    SchemaMetadataClass,
    SchemaFieldClass,
    OtherSchemaClass,
    UpstreamClass,
    UpstreamLineageClass,
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    TagAssociationClass,
    GlobalTagsClass,
)
from datahub.metadata.com.linkedin.pegasus2avro.schema import (
    StringType,
    NumberType,
)

EMITTER = DatahubRestEmitter("http://localhost:8080")
PLATFORM = "sqlite"
DB = str(Path(__file__).resolve().parent.parent / "sample-data" / "fiction-retail.db")

SQLITE_TO_DH = {"TEXT": StringType, "INT": NumberType, "INTEGER": NumberType, "REAL": NumberType, "": StringType}

TABLES = {
    "customers": "50K customers with segments (VIP, Active, New, Dormant, Churned). Core entity.",
    "orders": "150K orders linked to customers. Statuses: pending, shipped, delivered, canceled, backordered.",
    "order_items": "346K line items linking orders to products with quantities and prices.",
    "products": "5K products across 12 categories (apparel, electronics, home goods, etc.).",
    "suppliers": "500 suppliers providing products.",
    "shipments": "120K shipments via UPS/FedEx/USPS/DHL/Amazon. Avg 2 days to ship, 5.3 days transit.",
    "returns": "11.9K returns (7.96% rate). Reasons: buyer remorse, fit issue, not as described.",
    "promotions": "200 promotional campaigns linked to orders.",
    "inventory": "11.5K inventory records linking products to warehouses.",
    "warehouses": "15 warehouses across US, Canada, UK, Germany.",
}

LINEAGE = {
    "orders": ["customers"],
    "order_items": ["orders", "products"],
    "shipments": ["orders", "warehouses"],
    "returns": ["orders", "products"],
    "inventory": ["products", "warehouses"],
}


def dataset_urn(table: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{PLATFORM},fiction_retail.{table},PROD)"


def get_columns(table: str) -> list[dict]:
    conn = sqlite3.connect(DB)
    cursor = conn.execute(f"PRAGMA table_info({table})")
    cols = [{"name": r[1], "type": r[2] or "TEXT"} for r in cursor]
    conn.close()
    return cols


def main():
    print("Registering fiction-retail datasets...")

    for table, desc in TABLES.items():
        urn = dataset_urn(table)
        columns = get_columns(table)

        EMITTER.emit(MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=DatasetPropertiesClass(
                name=table, description=desc,
                customProperties={"database": "fiction-retail.db", "platform_instance": "fiction-retail"},
            ),
        ))

        fields = [
            SchemaFieldClass(
                fieldPath=c["name"],
                type={"type": SQLITE_TO_DH.get(c["type"].upper(), StringType)()},
                nativeDataType=c["type"] or "TEXT",
                description="",
            )
            for c in columns
        ]
        EMITTER.emit(MetadataChangeProposalWrapper(
            entityUrn=urn,
            aspect=SchemaMetadataClass(
                schemaName=table, platform=f"urn:li:dataPlatform:{PLATFORM}",
                version=0, hash="", platformSchema=OtherSchemaClass(rawSchema=""),
                fields=fields,
            ),
        ))
        print(f"  registered: {table} ({len(columns)} columns)")

    print("\nSetting up lineage...")
    for downstream, upstreams in LINEAGE.items():
        EMITTER.emit(MetadataChangeProposalWrapper(
            entityUrn=dataset_urn(downstream),
            aspect=UpstreamLineageClass(upstreams=[
                UpstreamClass(dataset=dataset_urn(u), type=DatasetLineageTypeClass.TRANSFORMED)
                for u in upstreams
            ]),
        ))
        print(f"  lineage: {' + '.join(upstreams)} -> {downstream}")

    print("\nApplying tags...")
    for table, tags in [
        ("customers", ["core-entity", "retail"]),
        ("orders", ["transactional", "retail"]),
        ("products", ["core-entity", "retail"]),
        ("returns", ["quality-metric", "retail"]),
    ]:
        EMITTER.emit(MetadataChangeProposalWrapper(
            entityUrn=dataset_urn(table),
            aspect=GlobalTagsClass(tags=[TagAssociationClass(tag=f"urn:li:tag:{t}") for t in tags]),
        ))
        print(f"  tags on {table}: {tags}")

    print("\nDone! Search 'fiction_retail' in DataHub.")


if __name__ == "__main__":
    main()
