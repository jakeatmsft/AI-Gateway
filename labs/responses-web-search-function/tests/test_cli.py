from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import lab_helpers


@pytest.mark.parametrize("launcher", [
    r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
    "/usr/bin/az",
])
def test_cli_uses_resolved_launcher(monkeypatch, launcher):
    monkeypatch.setattr(lab_helpers.shutil, "which", lambda name: launcher)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"id":"subscription"}', stderr=""))
    monkeypatch.setattr(lab_helpers.subprocess, "run", run)
    assert lab_helpers.az("account", "show") == {"id": "subscription"}
    assert run.call_args.args[0] == [launcher, "account", "show", "--only-show-errors", "--output", "json"]
    assert not run.call_args.kwargs.get("shell", False)


def test_cli_falls_back_to_current_kernel_package(monkeypatch):
    monkeypatch.setattr(lab_helpers.shutil, "which", lambda name: None)
    monkeypatch.setattr(lab_helpers.importlib.util, "find_spec", lambda name: object())
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(lab_helpers.subprocess, "run", run)
    assert lab_helpers.az("login") is None
    assert run.call_args.args[0][:3] == [lab_helpers.sys.executable, "-m", "azure.cli"]


@pytest.mark.parametrize("namespace_exists", [True, False])
def test_missing_cli_explains_install_and_kernel_restart(monkeypatch, namespace_exists):
    monkeypatch.setattr(lab_helpers.shutil, "which", lambda name: None)

    def find_spec(name):
        if not namespace_exists:
            raise ModuleNotFoundError("No module named 'azure'")
        return None

    monkeypatch.setattr(lab_helpers.importlib.util, "find_spec", find_spec)
    run = Mock()
    monkeypatch.setattr(lab_helpers.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Azure CLI is unavailable") as exc:
        lab_helpers.az("account", "show")
    assert "Microsoft.AzureCLI" in str(exc.value)
    assert "reopen VS Code/Jupyter" in str(exc.value)
    assert lab_helpers.sys.executable in str(exc.value)
    run.assert_not_called()


def test_cli_preserves_service_error_message(monkeypatch):
    monkeypatch.setattr(lab_helpers.shutil, "which", lambda name: "/usr/bin/az")
    monkeypatch.setattr(lab_helpers.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=1, stdout="", stderr="Please run 'az login' to setup account.\n")))
    with pytest.raises(RuntimeError, match="az login"):
        lab_helpers.az("account", "show")
