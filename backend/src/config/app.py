"""FastAPI 装配：中间件顺序、异常处理、路由挂载、启动钩子。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config.database import engine, wait_for_db
from src.config.settings import settings
from src.middlewares.audit_logger import AuditMiddleware
from src.middlewares.auth import AuthMiddleware
from src.middlewares.error_handler import register_exception_handlers
from src.middlewares.rate_limit import RateLimitMiddleware
from src.routes import api_router

logger = logging.getLogger(f"{settings.app_name}.app")


def _bootstrap_database() -> None:
    from src.models import Base  # noqa: WPS433 - 延迟导入避免循环

    wait_for_db()
    Base.metadata.create_all(bind=engine)
    _ensure_columns()


def _ensure_columns() -> None:
    """轻量列迁移：create_all 只建新表不补列，这里为既有库补齐新增列（幂等）。"""
    from sqlalchemy import inspect, text

    additions = {
        "policies": {"group_id": "INTEGER"},
        "match_runs": {"batch_key": "VARCHAR(60)"},
    }
    with engine.begin() as conn:
        inspector = inspect(conn)
        existing_tables = set(inspector.get_table_names())
        for table, columns in additions.items():
            if table not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table)}
            for column, ddl in columns.items():
                if column in existing_columns:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        if "match_runs" in existing_tables:
            unique_constraints = inspector.get_unique_constraints("match_runs")
            unique_indexes = [
                index for index in inspector.get_indexes("match_runs") if index.get("unique")
            ]
            covered = any(
                "batch_key" in (item.get("column_names") or [])
                for item in [*unique_constraints, *unique_indexes]
            )
            if not covered:
                conn.execute(
                    text("CREATE UNIQUE INDEX uq_match_runs_batch_key ON match_runs (batch_key)")
                )
        if "policies" in existing_tables:
            indexes = {index["name"] for index in inspector.get_indexes("policies")}
            if "ix_policies_group_id" not in indexes:
                conn.execute(text("CREATE INDEX ix_policies_group_id ON policies (group_id)"))


def _run_seed_once() -> None:
    if not settings.seed_demo:
        return
    try:
        from scripts.seed_demo import seed_all

        seed_all()
    except Exception as exc:  # noqa: BLE001 - 启动阶段不阻塞
        logger.warning("[SEED_DEMO] seed failed (ignored): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_database()
    _run_seed_once()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_title, version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AuthMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RateLimitMiddleware)
    register_exception_handlers(app)

    app.include_router(api_router, prefix="/api")
    app.include_router(api_router, include_in_schema=False)
    return app
