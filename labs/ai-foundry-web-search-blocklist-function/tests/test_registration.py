import copy
import json
from unittest.mock import Mock

import pytest

import lab_helpers


MISSING_PERMISSION = (
    "InvalidValue: Property api.preAuthorizedApplications.delegatedPermissionIds "
    "has a Permission Id that cannot be found in the AppPermissions sets."
)


@pytest.fixture
def graph(monkeypatch):
    applications, principals, writes = {}, set(), []

    def az(*args):
        if args[:3] == ("ad", "app", "create"):
            name = args[args.index("--display-name") + 1]
            assert name not in applications, "Must reuse saved registrations on retry"
            app = {"id": name, "appId": f"client-{name}", "api": {}}
            applications[name] = app
            return copy.deepcopy(app)
        if args[:3] == ("ad", "sp", "list"):
            client_id = args[-1].split("'")[1]
            return [{"appId": client_id}] if client_id in principals else []
        if args[:3] == ("ad", "sp", "create"):
            principals.add(args[-1])
            return {}
        if args[:3] == ("rest", "--method", "GET"):
            object_id = args[-1].split("/applications/")[1].split("?")[0]
            return copy.deepcopy(applications[object_id])
        pytest.fail(f"Unexpected Azure command: {args}")

    def patch(object_id, body):
        writes.append((object_id, copy.deepcopy(body)))
        api = body.get("api", {})
        if "preAuthorizedApplications" in api:
            # Reproduce Graph's error on publishing and referencing a new scope
            # in the same PATCH. The reference must already exist in Graph.
            persisted = {scope["id"] for scope in applications[object_id]["api"].get("oauth2PermissionScopes", [])}
            for client in api["preAuthorizedApplications"]:
                if not set(client["delegatedPermissionIds"]) <= persisted:
                    raise RuntimeError(MISSING_PERMISSION)
            assert "oauth2PermissionScopes" not in api
        applications[object_id]["api"].update(copy.deepcopy(api))

    monkeypatch.setattr(lab_helpers, "az", az)
    monkeypatch.setattr(lab_helpers, "graph_patch", patch)
    monkeypatch.setattr(lab_helpers.time, "sleep", Mock())
    return applications, principals, writes, patch


def test_publishes_scopes_before_preauthorization_and_reuses_state(tmp_path, graph):
    path = tmp_path / "state.json"
    first = lab_helpers.create_lab_apps(path, "tenant", "lab")
    second = lab_helpers.create_lab_apps(path, "tenant", "lab")
    assert first == second == json.loads(path.read_text())
    applications, principals, writes, _ = graph
    assert len(applications) == len(principals) == 3
    for kind, scope_key in (("gateway", "scope_id"), ("function", "function_scope_id")):
        api = applications[first[f"{kind}_app"]["id"]]["api"]
        assert api["preAuthorizedApplications"] == [{
            "appId": first["notebook_app"]["appId"], "delegatedPermissionIds": [first[scope_key]],
        }]
    assert writes[-1][0] == first["notebook_app"]["id"]


def test_resume_after_partial_failure_preserves_application_and_scope_ids(tmp_path, graph, monkeypatch):
    path = tmp_path / "state.json"
    _, _, _, patch = graph

    def fail_function_preauthorization(object_id, body):
        if object_id == "lab-function" and "preAuthorizedApplications" in body.get("api", {}):
            raise RuntimeError("Authorization_RequestDenied")
        patch(object_id, body)

    monkeypatch.setattr(lab_helpers, "graph_patch", fail_function_preauthorization)
    with pytest.raises(RuntimeError, match="Authorization_RequestDenied"):
        lab_helpers.create_lab_apps(path, "tenant", "lab")
    saved = json.loads(path.read_text())
    monkeypatch.setattr(lab_helpers, "graph_patch", patch)
    assert lab_helpers.create_lab_apps(path, "tenant", "lab") == saved


def test_waits_for_visible_scope_and_retries_replica_validation(monkeypatch):
    ready = {"api": {"oauth2PermissionScopes": [{"id": "scope", "isEnabled": True}]}}
    read = Mock(side_effect=[{"api": {}}, ready, ready])
    patch = Mock(side_effect=[RuntimeError(MISSING_PERMISSION), None])
    sleep = Mock()
    monkeypatch.setattr(lab_helpers, "az", read)
    monkeypatch.setattr(lab_helpers, "graph_patch", patch)
    monkeypatch.setattr(lab_helpers.time, "sleep", sleep)
    lab_helpers.preauthorize_notebook("api", "client", "scope")
    assert read.call_count == 3 and patch.call_count == 2
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]


@pytest.mark.parametrize("message", ["Authorization_RequestDenied", "Authentication required", "InvalidValue: invalid client"])
def test_does_not_retry_unrelated_graph_errors(monkeypatch, message):
    monkeypatch.setattr(lab_helpers, "az", Mock(return_value={
        "api": {"oauth2PermissionScopes": [{"id": "scope", "isEnabled": True}]},
    }))
    patch, sleep = Mock(side_effect=RuntimeError(message)), Mock()
    monkeypatch.setattr(lab_helpers, "graph_patch", patch)
    monkeypatch.setattr(lab_helpers.time, "sleep", sleep)
    with pytest.raises(RuntimeError, match=message):
        lab_helpers.preauthorize_notebook("api", "client", "scope")
    assert patch.call_count == 1
    sleep.assert_not_called()


def test_scope_visibility_wait_is_bounded(monkeypatch):
    read, patch, sleep = Mock(return_value={"api": None}), Mock(), Mock()
    monkeypatch.setattr(lab_helpers, "az", read)
    monkeypatch.setattr(lab_helpers, "graph_patch", patch)
    monkeypatch.setattr(lab_helpers.time, "sleep", sleep)
    with pytest.raises(RuntimeError, match="saved application and scope IDs"):
        lab_helpers.preauthorize_notebook("api", "client", "scope")
    assert read.call_count == 6 and sleep.call_count == 5
    patch.assert_not_called()
