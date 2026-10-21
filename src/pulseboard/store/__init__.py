"""Snapshot store: snapshots, baselines, plans and export records (DESIGN §11.2).

SQLAlchemy Core with Alembic migrations; SQLite locally, Postgres when hosted. Every row is keyed
by (site ID, Jira account ID). Credentials are never stored.
"""
