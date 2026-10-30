"""The migrations must build exactly the schema the models describe.

Fails when a model changes without a migration:
    python -m alembic revision --autogenerate -m "<what changed>"
"""

import os
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config

from src.core.config import get_settings

ROOT = Path(__file__).resolve().parents[1]


def test_migrations_match_models(monkeypatch):
    db = Path(tempfile.mkdtemp()) / "migrated.db"
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db}")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    command.check(cfg)  # raises if autogenerate would emit anything
    command.downgrade(cfg, "base")
    assert db.exists()
    os.remove(db)
