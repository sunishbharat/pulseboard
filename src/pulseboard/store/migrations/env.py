"""Alembic environment: runs on the connection passed in by ``pulseboard.store.migrate``."""

from alembic import context

from pulseboard.store.schema import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
