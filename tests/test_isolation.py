from pathlib import Path

import pytest

from config import settings

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(("name", "folder"), [("data_dir", "data"), ("library_dir", "library"),
                                              ("download_dir", "downloads")])
def test_guard_stops_the_run_when_a_settings_dir_is_the_repos(isolated_paths, monkeypatch, name, folder):
    monkeypatch.setattr(settings, name, REPO / folder)

    with pytest.raises(pytest.exit.Exception, match=f"Refusing to run: {name}"):
        isolated_paths()


def test_guard_stops_the_run_when_the_database_is_the_repos(isolated_paths):
    with pytest.raises(pytest.exit.Exception, match="Refusing to run: database"):
        isolated_paths(REPO / "data" / "leasure.db")


def test_guard_stops_the_run_for_a_relative_dir_resolving_into_the_repo(isolated_paths, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", Path("./data"))

    with pytest.raises(pytest.exit.Exception, match="Refusing to run: data_dir"):
        isolated_paths()
