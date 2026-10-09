
import os
import sys
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Use the same Atlas connection string as your existing dev tools.
# Example PowerShell:
# $env:MONGODB_URI = "mongodb+srv://..."

uri = os.environ.get("MONGODB_URI")

if not uri:
    sys.exit("ERROR: Set MONGODB_URI to your Atlas connection string.")

client = MongoClient(uri, serverSelectionTimeoutMS=10000)

try:
    for database_name in client.list_database_names():
        if database_name in ("admin", "local", "config"):
            continue

        database = client[database_name]
        print(f"\nDATABASE: {database_name}")
        print("-" * 90)

        rows = []

        for collection_name in database.list_collection_names():
            try:
                stats = database.command("collStats", collection_name)

                rows.append({
                    "name": collection_name,
                    "count": stats.get("count", 0),
                    "data_mb": stats.get("size", 0) / 1048576,
                    "storage_mb": stats.get("storageSize", 0) / 1048576,
                    "index_mb": stats.get("totalIndexSize", 0) / 1048576,
                })
            except PyMongoError as exc:
                print(f"Cannot inspect {collection_name}: {exc}")

        rows.sort(
            key=lambda r: r["storage_mb"] + r["index_mb"],
            reverse=True
        )

        print(
            f'{"Collection":40} {"Documents":>12} '
            f'{"Data MB":>10} {"Storage MB":>12} {"Index MB":>10}'
        )

        for row in rows:
            print(
                f'{row["name"][:40]:40} '
                f'{row["count"]:12,} '
                f'{row["data_mb"]:10.2f} '
                f'{row["storage_mb"]:12.2f} '
                f'{row["index_mb"]:10.2f}'
            )

except PyMongoError as exc:
    print(f"Atlas connection/report failed: {exc}")
    sys.exit(1)
finally:
    client.close()
