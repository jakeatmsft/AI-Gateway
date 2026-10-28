import zipfile
from pathlib import Path

import pytest

import lab_helpers


def test_package_targets_linux_python312_and_excludes_workspace_credentials(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    files = ("function_app.py", "processing.py", "filtering.py", "metrics.py", "streaming.py", "relay.py", "host.json", "requirements.txt")
    for name in files:
        (src / name).write_text("runtime")
    (src / ".env").write_text("not for deployment")
    (tmp_path / ".lab-state.json").write_text("not for deployment")
    monkeypatch.setattr(lab_helpers.shutil, "which", lambda name: "uv")

    def install(command, **kwargs):
        assert command[command.index("--python-version") + 1] == "3.12"
        assert command[command.index("--python-platform") + 1] == "x86_64-manylinux_2_17"
        assert command[command.index("--only-binary") + 1] == ":all:"
        dependencies = Path(command[command.index("--target") + 1])
        dependencies.mkdir(parents=True)
        (dependencies / "native.cpython-312-x86_64-linux-gnu.so").write_bytes(b"linux wheel")

    monkeypatch.setattr(lab_helpers.subprocess, "run", install)
    result = lab_helpers.build_function_package(tmp_path, tmp_path / "function.zip")
    with zipfile.ZipFile(result) as package:
        assert set(files).issubset(package.namelist())
        assert ".python_packages/lib/site-packages/native.cpython-312-x86_64-linux-gnu.so" in package.namelist()
        assert not any(".env" in name or ".lab-state" in name for name in package.namelist())


def test_missing_uv_fails_before_packaging(tmp_path, monkeypatch):
    monkeypatch.setattr(lab_helpers.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="Install uv"):
        lab_helpers.build_function_package(tmp_path, tmp_path / "function.zip")


def test_indexing_retries_startup_errors_without_republishing(monkeypatch):
    replies = iter([RuntimeError("Operation returned an invalid status 'Bad Request'"), [],
                    [{"name": "app/redact"}], [{"name": "app/redact"}, {"name": "app/responses"}]])

    def azure(*args):
        assert args[:3] == ("functionapp", "function", "list")
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(lab_helpers, "az", azure)
    monkeypatch.setattr(lab_helpers.time, "sleep", lambda _: None)
    assert lab_helpers.wait_for_function_indexing("group", "app") == [{"name": "app/redact"}, {"name": "app/responses"}]


def test_indexing_does_not_hide_authorization_failures(monkeypatch):
    def denied(*args):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(lab_helpers, "az", denied)
    with pytest.raises(RuntimeError, match="Forbidden"):
        lab_helpers.wait_for_function_indexing("group", "app")
