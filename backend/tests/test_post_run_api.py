import app as app_module


def test_post_run_report_route_returns_frontend_contract(monkeypatch):
    report = {
        "generated_at": "2026-08-13T17:00:00+00:00",
        "history_path": "/tmp/post_run_nodes.json",
        "summary": {
            "nodes": 1,
            "participations": 2,
            "attributed_failures": 0,
            "flagged_nodes": 0,
            "drained_nodes": 0,
        },
        "racks": [],
        "nodes": [],
        "refresh": {
            "activities": 1,
            "merge": {"merged": 0},
            "accounting_error": "",
            "inventory_error": "",
        },
    }
    monkeypatch.setattr(
        app_module,
        "_build_post_run_report",
        lambda **_kwargs: report,
    )

    with app_module.app.test_client() as client:
        response = client.get("/api/post-run-node-report?refresh=1")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["summary"]["nodes"] == 1
    assert payload["refresh"]["accounting_error"] == ""


def test_post_run_report_rejects_invalid_node_filter():
    with app_module.app.test_client() as client:
        response = client.get("/api/post-run-node-report?node=bad%20node")

    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "error": "invalid node"}


def test_companion_post_run_endpoints_are_registered():
    routes = {rule.rule for rule in app_module.app.url_map.iter_rules()}

    assert "/api/post-run-node-report" in routes
    assert "/api/post-run-node-report/mark" in routes
    assert "/api/node-issues" in routes
    assert "/api/deep-audits" in routes
