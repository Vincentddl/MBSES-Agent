"""Database is the durable task and model authority."""
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings

options = {"pool_pre_ping": True}
if settings.DATABASE_URL.startswith("mysql+pymysql"):
    options.update(pool_recycle=1800, connect_args={"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30})
if settings.DATABASE_URL.startswith("sqlite"):
    options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    if settings.DATABASE_URL in {"sqlite://", "sqlite:///:memory:"}:
        options["poolclass"] = StaticPool
engine = create_engine(settings.DATABASE_URL, **options)
if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def sqlite_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def get_db():
    with SessionLocal() as session:
        yield session


def create_all():
    from app import models  # noqa: F401
    if not settings.DATABASE_URL.startswith("sqlite"):
        # Validate existing application schemas before any startup DDL. Empty
        # databases may bootstrap; upgrades go through the backup-first script.
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        expected = set(Base.metadata.tables)
        if tables & expected:
            cols = {column["name"] for column in inspector.get_columns("generation_task")} if "generation_task" in tables else set()
            if expected - tables or not {"lease_token", "execution_deadline_at"} <= cols:
                raise RuntimeError("Database upgrade required: stop app and run scripts/migrate_runtime.py --apply")
    Base.metadata.create_all(engine)
    # SQLite development upgrades; MySQL uses the backed-up migrate_runtime.py workflow.
    if settings.DATABASE_URL.startswith("sqlite"):
        with engine.begin() as conn:
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(generation_task)")}
            if cols and "lease_token" not in cols:
                conn.exec_driver_sql("ALTER TABLE generation_task ADD COLUMN lease_token VARCHAR(32)")
            if cols and "execution_deadline_at" not in cols:
                conn.exec_driver_sql("ALTER TABLE generation_task ADD COLUMN execution_deadline_at DATETIME")
