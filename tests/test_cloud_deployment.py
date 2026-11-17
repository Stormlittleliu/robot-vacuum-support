# -*- coding: utf-8 -*-
"""Cloud-deployment contract tests: auth, origins, static split, container files.

All runtime roots are temporary; no model, network or existing 8914 service is
touched.  Docker build/up smoke is run separately because it needs a daemon.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "runtime", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from modules.instances.runtime import security  # noqa: E402
from modules.instances.runtime.server import create_app  # noqa: E402


class FakeConfig:
    instance_id = "inst-cloud-test"
    artifact_id = "art-cloud-test"
    scenario = "robot_vacuum"
    version = 4
    name = "cloud test"
    port = 8999
    workflows = ["agent"]
    default_workflow = "agent"
    issues = []
    manifest = {}

    def __init__(self, root: Path, host: str = "127.0.0.1"):
        self.host = host
        self.storage_root = root
        self.sessions_root = root / "sessions"
        self.artifact_root = root / "artifact"
        self.runtime_root = root / "artifact" / "runtime"
        self.product_ui_root = ROOT / "product_ui"
        self.model_root = root / "models"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        (self.artifact_root / "manifest.json").write_text("{}", encoding="utf-8")
        spec = self.artifact_root / self.scenario
        spec.mkdir(parents=True, exist_ok=True)
        (spec / "active.json").write_text("{}", encoding="utf-8")
        self.model_root.mkdir(parents=True, exist_ok=True)

    def credential_names(self):
        return []

    def required_assets(self):
        return []

    def missing_required_assets(self):
        return []

    def missing_bindings(self):
        return []


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RVS_ADMIN_TOKEN", "admin-test-token")
    monkeypatch.setenv("RVS_ACCESS_TOKEN", "user-test-token")
    monkeypatch.delenv("RVS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("RVS_SERVE_STATIC", raising=False)
    app = create_app(FakeConfig(tmp_path))
    with TestClient(app) as test_client:
        yield app, test_client


def auth(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def test_health_and_identity_are_public_safe_summaries(tmp_path, monkeypatch):
    monkeypatch.setenv("RVS_ADMIN_TOKEN", "admin-test-token")
    monkeypatch.setenv("RVS_ACCESS_TOKEN", "user-test-token")
    monkeypatch.setenv("RVS_SERVE_STATIC", "false")
    app = create_app(FakeConfig(tmp_path))
    local_path = str(tmp_path).replace("\\", "/").lower()
    with TestClient(app) as test_client:
        health = test_client.get("/api/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["deployment"]["single_tenant"] is True
        assert payload["deployment"]["admin_auth"] == "bearer_token"
        assert payload["deployment"]["user_auth"] == "bearer_token"
        assert "credential_names" not in json.dumps(payload)
        assert local_path not in json.dumps(payload).lower()
        assert "DEEPSEEK_API_KEY" not in health.text
        assert payload["model"]["configured"] is False
        assert test_client.get("/api/health/live").json()["status"] == "alive"
        assert test_client.get("/api/health/ready").status_code == 503
        # Static hosting is disabled for the separated nginx deployment.
        assert test_client.get("/").status_code == 404
        assert test_client.get("/admin.html").status_code == 404
        assert test_client.get("/api/docs").status_code == 200


def test_user_and_admin_tokens_are_separate_and_required(client):
    _, test_client = client
    assert test_client.get("/api/sessions").status_code == 401
    assert test_client.get("/api/sessions", headers=auth("wrong")).status_code == 401
    assert test_client.get("/api/sessions", headers=auth("user-test-token")).status_code == 200
    assert test_client.get("/api/v1/sessions", headers=auth("user-test-token")).status_code == 200
    assert (
        test_client.get("/api/sessions", cookies={"rvs_access_token": "user-test-token"}).status_code
        == 200
    )
    assert test_client.get("/api/admin/handoff").status_code == 401
    assert test_client.get("/api/admin/handoff", headers=auth("user-test-token")).status_code == 401
    assert test_client.get("/api/admin/handoff", headers=auth("admin-test-token")).status_code == 200
    assert (
        test_client.get("/api/v1/admin/handoff", headers=auth("admin-test-token")).status_code
        == 200
    )
    # Headers cannot turn a remote-style auth failure into loopback mode.
    spoofed = test_client.get(
        "/api/admin/handoff",
        headers={**auth("wrong"), "X-Forwarded-Host": "127.0.0.1:8999", "Forwarded": "for=127.0.0.1"},
    )
    assert spoofed.status_code == 401


def test_admin_token_mode_works_on_remote_bind_and_origin_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("RVS_ADMIN_TOKEN", "admin-test-token")
    monkeypatch.setenv("RVS_ACCESS_TOKEN", "user-test-token")
    monkeypatch.setenv("RVS_ALLOWED_ORIGINS", "https://ui.example")
    app = create_app(FakeConfig(tmp_path, host="0.0.0.0"))
    with TestClient(app) as test_client:
        assert test_client.get("/api/admin/handoff").status_code == 401
        assert test_client.get("/api/admin/handoff", headers=auth("admin-test-token")).status_code == 200
        assert test_client.get("/admin.html").status_code == 200
        # A separately hosted UI is allowed only when explicitly listed.
        allowed = test_client.post(
            "/api/admin/handoff/missing/accept",
            headers={**auth("admin-test-token"), "Origin": "https://ui.example"},
            json={},
        )
        assert allowed.status_code == 404
        evil = test_client.post(
            "/api/admin/handoff/missing/accept",
            headers={**auth("admin-test-token"), "Origin": "https://evil.example"},
            json={},
        )
        assert evil.status_code == 403
        assert evil.json()["detail"]["code"] == "origin_rejected"
        # Preflight for the listed origin is handled by CORS without auth.
        preflight = test_client.options(
            "/api/sessions",
            headers={
                "Origin": "https://ui.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "https://ui.example"
        assert preflight.headers["access-control-allow-credentials"] == "true"


def test_origin_allow_list_rejects_wildcard_and_malformed_values():
    assert security.parse_allowed_origins("https://a.example, https://b.example/path, *") == [
        "https://a.example"
    ]
    assert security.constant_time_equals("abc", "abc")
    assert not security.constant_time_equals("abc", "abd")
    assert not security.constant_time_equals(None, "abc")


def test_openapi_has_chinese_tags_models_and_dual_routes(client):
    _, test_client = client
    document = test_client.get("/api/openapi.json").json()
    tags = [item["name"] for item in document.get("tags", [])]
    assert "会话" in tags and "人工后台" in tags
    assert "/api/sessions" in document["paths"]
    assert "/api/v1/sessions" in document["paths"]
    components = document["components"]["schemas"]
    assert "SessionListResponse" in components
    assert "ErrorResponse" in components
    operations = document["paths"]["/api/sessions"]["get"]
    assert "401" in operations["responses"]
    operation_ids = [
        operation.get("operationId")
        for path in document["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and operation.get("operationId")
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_frontend_uses_shared_api_base_and_session_only_tokens():
    index = (ROOT / "product_ui" / "index.html").read_text(encoding="utf-8")
    admin = (ROOT / "product_ui" / "admin.html").read_text(encoding="utf-8")
    config = (ROOT / "product_ui" / "config.js").read_text(encoding="utf-8")
    assert 'apiBase: "/api"' in config
    for page in (index, admin):
        assert '<script src="config.js"></script>' in page
        assert "apiBase" in page
        assert "apiUrl(" in page
        assert 'fetch("/api' not in page
        assert "127.0.0.1" not in page and "localhost:" not in page
    assert "sessionStorage" in index and "rvs_access_token" in index
    assert "sessionStorage" in admin and "rvs_admin_token" in admin
    assert "localStorage.setItem" not in admin and "localStorage.getItem" not in admin
    assert re.search(r"[?&](token|access_token)=", index) is None
    assert re.search(r"[?&](token|access_token)=", admin) is None
    assert "<input id=\"admin-token\" type=\"password\"" in admin


def test_container_files_are_separated_and_asset_safe():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"backend", "frontend"}
    assert "ports" not in services["backend"]
    assert services["backend"]["expose"] == ["8000"]
    assert len(services["frontend"]["ports"]) == 1
    assert services["backend"]["environment"]["RVS_SERVE_STATIC"] == "false"
    assert ":?" in services["backend"]["environment"]["RVS_ADMIN_TOKEN"]
    assert ":?" in services["backend"]["environment"]["RVS_ACCESS_TOKEN"]
    volumes = "\n".join(services["backend"]["volumes"])
    assert "/assets/models/bge-small-zh-v1.5:ro" in volumes
    assert "rvs-data:/data" in volumes
    assert services["backend"]["stop_grace_period"] == "30s"

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.11" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "COPY assets" not in dockerfile
    assert "COPY outputs" not in dockerfile

    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for item in ("assets", "outputs", "runtime/data", "runtime/outputs", ".env", "*.key"):
        assert item in ignored

    nginx = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    for marker in (
        "listen 8080",
        "proxy_buffering off",
        "proxy_cache off",
        "proxy_read_timeout 3600s",
        "X-Accel-Buffering",
    ):
        assert marker in nginx


def _load_entrypoint():
    path = ROOT / "docker" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("rvs_entrypoint", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_container_entrypoint_plan_and_config_use_writable_paths(tmp_path, monkeypatch):
    module = _load_entrypoint()
    monkeypatch.setattr(module, "APP_ROOT", ROOT)
    monkeypatch.setattr(module, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(module, "ASSET_ROOT", tmp_path / "assets")
    monkeypatch.setenv("RVS_ADMIN_TOKEN", "admin-test-token")
    monkeypatch.setenv("RVS_ACCESS_TOKEN", "user-test-token")
    plan = module.plan()
    assert plan["app_root"] == str(ROOT)
    assert plan["data_root"] == str(tmp_path / "data")
    assert Path(plan["asset_bindings"]["models"]).as_posix().endswith(
        "assets/models/bge-small-zh-v1.5"
    )
    assert plan["admin_token_configured"] is True

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_id": "art-test",
                "name": "robot_vacuum",
                "version": 4,
                "entry": {"default_workflow": "agent"},
            }
        ),
        encoding="utf-8",
    )
    env_file = module._write_env_file(tmp_path / "data" / "instance" / "runtime.env")
    config_path = module._write_instance_config(artifact, env_file)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["artifact_root"] == str(artifact)
    assert config["storage_root"] == str(tmp_path / "data" / "runtime")
    assert Path(config["asset_bindings"]["models"]).as_posix().endswith("bge-small-zh-v1.5")
    assert config["environment_file"] == str(env_file)
    assert "admin-test-token" not in config_path.read_text(encoding="utf-8")
    assert env_file.exists()
    if os.name != "nt":
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert config_path.stat().st_mode & 0o777 == 0o600


def test_generated_serve_entry_uses_one_uvicorn_worker():
    from modules.artifacts.service_entry import SERVICE_ENTRY  # noqa: PLC0415

    assert "workers=1" in SERVICE_ENTRY
    assert "timeout_graceful_shutdown=10" in SERVICE_ENTRY
