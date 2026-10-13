import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path):
    """Every test gets its own %APPDATA% / cache roots."""
    from acs.core.settings import AppDirs
    old = (AppDirs.data, AppDirs.cache)
    AppDirs.configure(tmp_path / "appdirs")
    yield tmp_path
    AppDirs.data, AppDirs.cache = old


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app
