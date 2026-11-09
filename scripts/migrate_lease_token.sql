-- Legacy MySQL column helper. Prefer scripts/migrate_runtime.py --apply,
-- which also snapshots data and creates additive tables. Stop the app and
-- back up the database before using this helper. Repeated execution is safe.
SET @mbses_ddl = IF(EXISTS(SELECT 1 FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'generation_task' AND column_name = 'lease_token'),
    'SELECT 1', 'ALTER TABLE generation_task ADD COLUMN lease_token VARCHAR(32) NULL');
PREPARE mbses_migration FROM @mbses_ddl;
EXECUTE mbses_migration;
DEALLOCATE PREPARE mbses_migration;
SET @mbses_ddl = IF(EXISTS(SELECT 1 FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'generation_task' AND column_name = 'execution_deadline_at'),
    'SELECT 1', 'ALTER TABLE generation_task ADD COLUMN execution_deadline_at DATETIME NULL');
PREPARE mbses_migration FROM @mbses_ddl;
EXECUTE mbses_migration;
DEALLOCATE PREPARE mbses_migration;
