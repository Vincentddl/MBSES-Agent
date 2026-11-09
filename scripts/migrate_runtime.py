"""Add runtime tables/lease column after a private data snapshot; stop the app first."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import MetaData, Table, inspect, select, text
from app.config import settings
from app.database import Base, engine
from app import models  # noqa: F401


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing = sorted(set(Base.metadata.tables) - tables)
    lease_missing = 'generation_task' in tables and 'lease_token' not in {
        column['name'] for column in inspector.get_columns('generation_task')}
    deadline_missing = 'generation_task' in tables and 'execution_deadline_at' not in {
        column['name'] for column in inspector.get_columns('generation_task')}
    print(json.dumps({'missing_tables': missing, 'lease_column_missing': lease_missing,
                      'deadline_column_missing': deadline_missing, 'apply': args.apply}))
    if not args.apply:
        return
    with engine.connect() as conn:
        if 'generation_task' in tables:
            active = conn.execute(text("SELECT COUNT(*) FROM generation_task WHERE status IN ('PENDING','RUNNING','COMMITTING')")).scalar()
            if active:
                raise RuntimeError('Stop/drain active tasks before migrating; no data changed')
        snapshot = {}
        metadata = MetaData()
        for name in sorted(tables & set(Base.metadata.tables)):
            table = Table(name, metadata, autoload_with=conn)
            snapshot[name] = [dict(row) for row in conn.execute(select(table)).mappings()]
    destination = settings.DATA_DIR / 'backups' / ('runtime-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(snapshot, ensure_ascii=False, default=str), encoding='utf-8')
    if lease_missing:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE generation_task ADD COLUMN lease_token VARCHAR(32) NULL'))
    if deadline_missing:
        with engine.begin() as conn:
            conn.execute(text('ALTER TABLE generation_task ADD COLUMN execution_deadline_at DATETIME NULL'))
    Base.metadata.create_all(engine)
    fresh = inspect(engine)
    assert set(Base.metadata.tables) <= set(fresh.get_table_names())
    assert 'lease_token' in {c['name'] for c in fresh.get_columns('generation_task')}
    print(json.dumps({'migrated': True, 'snapshot_tables': len(snapshot), 'snapshot_file': str(destination)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
