# Database notes

The database was isolated. Initialization was repeated to test idempotency, and a pre-lifecycle `devices` schema was migrated. Handoff text is generated on demand and has no persistence table.
