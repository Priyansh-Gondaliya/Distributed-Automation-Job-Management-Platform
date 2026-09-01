"""
Initialize / seed the PostgreSQL schema defaults.

Tables must already exist (run postgres/01_create_schema_REVIEW_ONLY.sql once).

Usage:
    python init_db.py
"""
from app import config
from app import database


def main() -> None:
    database.init_schema()
    print(
        f"Postgres ready: {config.PGUSER}@{config.PGHOST}:{config.PGPORT}/"
        f"{config.PGDATABASE} (schema={config.PGSCHEMA})"
    )


if __name__ == "__main__":
    main()
