import stat

import pytest

from services import secret_file


def test_a_posix_secret_is_owner_only_and_leaves_no_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_file, "get_platform", lambda: "linux")
    target = tmp_path / "session.json"

    assert secret_file.write_secret(target, "secret") is True

    assert target.read_text() == "secret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["session.json"]


def test_a_failed_write_removes_the_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_file, "get_platform", lambda: "linux")

    def boom(_fd):
        raise OSError("disk full")

    monkeypatch.setattr(secret_file.os, "fsync", boom)
    with pytest.raises(OSError):
        secret_file.write_secret(tmp_path / "session.json", "secret")

    assert list(tmp_path.iterdir()) == []


def test_drvfs_is_hardened_with_icacls_while_the_file_is_still_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_file, "get_platform", lambda: "wsl2")
    monkeypatch.setattr(secret_file, "is_drvfs", lambda _path: True)
    monkeypatch.setattr(secret_file, "_windows_path", lambda path: f"C:\\x\\{path.name}")
    monkeypatch.setattr(secret_file, "_windows_user", None)
    seen = []

    def run(args):
        if args[0] == "whoami.exe":
            return 0, "pc\\pepe"
        seen.append((args, (tmp_path / args[1].split("\\")[-1]).read_text()))
        return 0, ""

    monkeypatch.setattr(secret_file, "_run", run)
    target = tmp_path / "session.json"

    assert secret_file.write_secret(target, "secret") is True

    (args, content_when_hardened), = seen
    assert args[0] == "icacls.exe" and args[2:] == ["/inheritance:r", "/grant:r", "pc\\pepe:F"]
    assert content_when_hardened == "" and target.read_text() == "secret"


def test_a_failing_icacls_reports_an_unprotected_file(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_file, "get_platform", lambda: "windows")
    monkeypatch.setattr(secret_file, "_run", lambda args: (1, ""))

    assert secret_file.write_secret(tmp_path / "session.json", "secret") is False
    assert (tmp_path / "session.json").read_text() == "secret"


def test_only_mnt_letter_paths_count_as_drvfs(monkeypatch):
    monkeypatch.setattr(secret_file, "get_platform", lambda: "wsl2")

    assert secret_file.is_drvfs(secret_file.Path("/mnt/c/dev/leasure/data"))
    assert not secret_file.is_drvfs(secret_file.Path("/home/user/data"))
    assert secret_file._windows_path(secret_file.Path("/mnt/c/dev/data/x.json")) == "C:\\dev\\data\\x.json"


def test_a_failed_whoami_is_asked_again_next_time(monkeypatch):
    monkeypatch.setattr(secret_file, "_windows_user", None)
    answers = iter([(-1, ""), (0, "pc\\pepe")])
    monkeypatch.setattr(secret_file, "_run", lambda args: next(answers))

    assert secret_file._wsl_windows_user() is None
    assert secret_file._wsl_windows_user() == "pc\\pepe"
