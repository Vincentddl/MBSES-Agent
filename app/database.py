"""Database is the durable task and model authority."""
from sqlalchemy import create_engine, event
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
    Base.metadata.create_all(engine)
