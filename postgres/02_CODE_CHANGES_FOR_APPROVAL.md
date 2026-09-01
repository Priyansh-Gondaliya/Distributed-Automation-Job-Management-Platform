# PostgreSQL only — SQLite removed

**Status:** App uses **PostgreSQL exclusively** (`tbl_dfms_*` tables).  
No SQLite engine or `automation.db` fallback in runtime.

**Schema DDL:** `postgres/01_create_schema_REVIEW_ONLY.sql`  
(Verified against live DB + `app/db_compat.py` — **24 tables**.)

---

## Runtime files (current layout)

| File | Role |
|------|------|
| `app/config.py` | `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSCHEMA` |
| `app/db_compat.py` | psycopg2 + SQL adapter (`?`→`%s`, logical→`tbl_dfms_*`) |
| `app/database.py` | Business SQL + `_ensure_*` column/table helpers |
| `.env` / `.env.example` | Secrets (do not commit `.env`) |
| `postgres/01_create_schema_REVIEW_ONLY.sql` | One-time DDL + safe upgrade patches |
| `postgres/smoke_test.py` | Connection + claim smoke test |
| `postgres/smoke_dashboard.py` | Dashboard query smoke test |
| `scripts/init_db.py` | `init_schema()` seed / readiness |

### Smoke tests

```text
python postgres/smoke_test.py
python postgres/smoke_dashboard.py
```

Login after empty DB seed: `admin` / `admin123` (created by app seed if missing).
