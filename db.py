"""
Database layer for SimBench backend.

DB is OPTIONAL. If DATABASE_URL is not set or PostgreSQL is unreachable,
every function in this module returns None/False and the app continues
running in filesystem-only mode — existing behaviour is fully preserved.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, JSON, String,
    create_engine, text,
)
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

logger = logging.getLogger(__name__)

_engine = None
_Session = None


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class NetworkRecord(Base):
    """[DB-BACKED] Mirrors the network metadata in data/networks.json."""
    __tablename__ = "networks"

    id           = Column(String,  primary_key=True)
    name         = Column(String,  nullable=False)
    voltage      = Column(String)
    type         = Column(String)
    status       = Column(String)
    created      = Column(String)
    version      = Column(String)
    buses        = Column(Integer)
    lines        = Column(Integer)
    transformers = Column(Integer)
    loads        = Column(Integer)
    plot_url     = Column(String)
    extra        = Column(JSON, nullable=True)   # forward-compat catch-all


class SimulationRun(Base):
    """
    [DB-BACKED] One row per completed simulation.
    CSV result files are still written to the filesystem (unchanged);
    this table stores only the run metadata / index.
    """
    __tablename__ = "simulation_runs"

    run_id     = Column(String,   primary_key=True)
    network_id = Column(String,   nullable=False, index=True)
    horizon    = Column(String,   nullable=False)   # day | week | month
    year       = Column(Integer,  nullable=False)
    month      = Column(Integer,  nullable=False)
    day        = Column(Integer,  nullable=True)    # None for month horizon
    mode             = Column(String,   nullable=False)
    status           = Column(String,   nullable=False, default="completed")
    has_trafo        = Column(Boolean,  nullable=False, default=False)
    started_at                = Column(DateTime, nullable=True)
    duration_seconds          = Column(Float,    nullable=True)
    violations_under_voltage  = Column(Integer,  nullable=True)
    violations_over_voltage   = Column(Integer,  nullable=True)
    violations_line_overload  = Column(Integer,  nullable=True)
    violations_trafo_overload = Column(Integer,  nullable=True)
    violations_total          = Column(Integer,  nullable=True)
    created_at                = Column(DateTime, nullable=False, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Connection bootstrap
# ---------------------------------------------------------------------------

def init_db() -> bool:
    """
    Connect to PostgreSQL and create tables if they don't exist.
    Returns True on success, False if DB is not configured or unreachable.
    Safe to call at startup — failure is logged and swallowed.
    """
    global _engine, _Session

    url = os.getenv("DATABASE_URL")
    if not url:
        logger.info("DATABASE_URL not set — running in filesystem-only mode")
        return False

    try:
        _engine = create_engine(url, pool_pre_ping=True, echo=False)
        with _engine.connect() as conn:
            conn.execute(text("SELECT 1"))   # verify reachability
        Base.metadata.create_all(_engine)
        _Session = sessionmaker(bind=_engine)
        logger.info("PostgreSQL connected and schema ready")
        return True
    except (OperationalError, SQLAlchemyError, Exception) as exc:
        logger.warning("PostgreSQL unavailable — filesystem fallback active. Reason: %s", exc)
        _engine = None
        _Session = None
        return False


def db_available() -> bool:
    return _Session is not None


# ---------------------------------------------------------------------------
# Run helpers (used by /runs and /networks/{id}/run)
# ---------------------------------------------------------------------------

def _run_to_api(run: SimulationRun) -> dict:
    """Convert an ORM row to the same JSON shape /runs has always returned."""
    nid, rid = run.network_id, run.run_id
    return {
        "network_id":       nid,
        "run_id":           rid,
        "started_at":       run.started_at.isoformat() if run.started_at else None,
        "duration_seconds": run.duration_seconds,
        "violations": {
            "under_voltage":  run.violations_under_voltage,
            "over_voltage":   run.violations_over_voltage,
            "line_overload":  run.violations_line_overload,
            "trafo_overload": run.violations_trafo_overload,
            "total":          run.violations_total,
        },
        "results": {
            "vm_pu":        f"/networks/{nid}/results/{rid}/vm-pu",
            "line_loading": f"/networks/{nid}/results/{rid}/line-loading",
            "trafo_loading": (
                f"/networks/{nid}/results/{rid}/trafo-loading"
                if run.has_trafo else None
            ),
        },
    }


def get_db_runs() -> list[dict] | None:
    """
    Return all runs from PostgreSQL ordered by creation time.
    Returns None if DB is unavailable — caller should then fall back to
    the filesystem scan. An empty list [] is a valid DB result and is
    returned as-is; the caller decides whether to also fall back in that case.
    """
    if not db_available():
        return None
    try:
        with _Session() as session:
            rows = (
                session.query(SimulationRun)
                .order_by(SimulationRun.created_at)
                .all()
            )
            return [_run_to_api(r) for r in rows]
    except SQLAlchemyError as exc:
        logger.warning("DB read failed, filesystem fallback active: %s", exc)
        return None


def save_run(
    *,
    run_id: str,
    network_id: str,
    horizon: str,
    year: int,
    month: int,
    day: int | None,
    mode: str,
    has_trafo: bool,
    started_at: datetime | None = None,
    duration_seconds: float | None = None,
    violations_under_voltage: int | None = None,
    violations_over_voltage: int | None = None,
    violations_line_overload: int | None = None,
    violations_trafo_overload: int | None = None,
    violations_total: int | None = None,
) -> bool:
    """
    Persist a completed simulation run.
    Returns True on success, False (non-fatal) if DB is unavailable or the
    insert fails — the HTTP response is returned to the client either way.
    """
    if not db_available():
        return False
    try:
        with _Session() as session:
            session.add(SimulationRun(
                run_id=run_id, network_id=network_id,
                horizon=horizon, year=year, month=month, day=day,
                mode=mode, status="completed", has_trafo=has_trafo,
                started_at=started_at, duration_seconds=duration_seconds,
                violations_under_voltage=violations_under_voltage,
                violations_over_voltage=violations_over_voltage,
                violations_line_overload=violations_line_overload,
                violations_trafo_overload=violations_trafo_overload,
                violations_total=violations_total,
            ))
            session.commit()
        logger.info("Run %s saved to PostgreSQL", run_id)
        return True
    except SQLAlchemyError as exc:
        logger.warning("Could not persist run %s: %s", run_id, exc)
        return False


# ---------------------------------------------------------------------------
# Network helpers (used by /networks and /networks/{id})
# ---------------------------------------------------------------------------

_NETWORK_KNOWN_KEYS = {
    "id", "name", "voltage", "type", "status", "created", "version",
    "buses", "lines", "transformers", "loads", "plot_url",
}


def _network_to_api(n: NetworkRecord) -> dict:
    """Convert a NetworkRecord ORM row to the original JSON shape."""
    result = {
        "id":           n.id,
        "name":         n.name,
        "voltage":      n.voltage,
        "type":         n.type,
        "status":       n.status,
        "created":      n.created,
        "version":      n.version,
        "buses":        n.buses,
        "lines":        n.lines,
        "transformers": n.transformers,
        "loads":        n.loads,
        "plot_url":     n.plot_url,
    }
    if n.extra:
        result.update(n.extra)
    return result


def get_db_networks() -> list[dict] | None:
    """
    Return all networks from PostgreSQL ordered by id.
    Returns None if DB unavailable — caller falls back to JSON file.
    """
    if not db_available():
        return None
    try:
        with _Session() as session:
            rows = session.query(NetworkRecord).order_by(NetworkRecord.id).all()
            return [_network_to_api(r) for r in rows]
    except SQLAlchemyError as exc:
        logger.warning("DB read failed for networks: %s", exc)
        return None


def get_db_network(network_id: str) -> dict | None:
    """
    Return a single network by ID from PostgreSQL.
    Returns None if not found OR if DB is unavailable.
    """
    if not db_available():
        return None
    try:
        with _Session() as session:
            row = session.query(NetworkRecord).filter_by(id=network_id).first()
            return _network_to_api(row) if row else None
    except SQLAlchemyError as exc:
        logger.warning("DB read failed for network %s: %s", network_id, exc)
        return None


def save_network(data: dict) -> bool:
    """
    Insert or update a network record. Silently returns False when DB is unavailable.
    Keys not in _NETWORK_KNOWN_KEYS are stored in the extra JSON column.
    """
    if not db_available():
        return False
    try:
        extra = {k: v for k, v in data.items() if k not in _NETWORK_KNOWN_KEYS} or None
        with _Session() as session:
            row = session.query(NetworkRecord).filter_by(id=data["id"]).first()
            if row:
                for key in _NETWORK_KNOWN_KEYS:
                    if key in data:
                        setattr(row, key, data[key])
                row.extra = extra
            else:
                session.add(NetworkRecord(
                    id=data["id"],
                    name=data["name"],
                    voltage=data.get("voltage"),
                    type=data.get("type"),
                    status=data.get("status"),
                    created=data.get("created"),
                    version=data.get("version"),
                    buses=data.get("buses"),
                    lines=data.get("lines"),
                    transformers=data.get("transformers"),
                    loads=data.get("loads"),
                    plot_url=data.get("plot_url"),
                    extra=extra,
                ))
            session.commit()
        logger.info("Network %s saved to PostgreSQL", data["id"])
        return True
    except SQLAlchemyError as exc:
        logger.warning("Could not save network %s: %s", data["id"], exc)
        return False


def seed_networks_from_file(data_file: Path) -> bool:
    """
    Populate the networks table from the JSON file if the table is currently empty.
    Idempotent — skips silently if rows already exist.
    Returns True if rows were inserted, False otherwise.
    """
    if not db_available():
        return False
    try:
        with _Session() as session:
            if session.query(NetworkRecord).count() > 0:
                logger.info("networks table already populated — skipping seed")
                return False

        with open(data_file, "r", encoding="utf-8") as f:
            networks = json.load(f)

        with _Session() as session:
            for n in networks:
                extra = {k: v for k, v in n.items() if k not in _NETWORK_KNOWN_KEYS} or None
                session.add(NetworkRecord(
                    id=n["id"],
                    name=n["name"],
                    voltage=n.get("voltage"),
                    type=n.get("type"),
                    status=n.get("status"),
                    created=n.get("created"),
                    version=n.get("version"),
                    buses=n.get("buses"),
                    lines=n.get("lines"),
                    transformers=n.get("transformers"),
                    loads=n.get("loads"),
                    plot_url=n.get("plot_url"),
                    extra=extra,
                ))
            session.commit()
        logger.info("Seeded %d networks from %s into PostgreSQL", len(networks), data_file)
        return True
    except (OSError, SQLAlchemyError, Exception) as exc:
        logger.warning("Failed to seed networks from file: %s", exc)
        return False
