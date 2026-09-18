# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""The OpenAPI document must be valid and must not lie.

It is generated from the route table rather than hand-written, which removes the usual failure
(a spec that drifts from the code) and introduces a different one: a generator bug produces a
document that is confidently wrong for all 200 operations at once. These tests defend the
properties an integrator depends on — every path parameter declared, unique operationIds, tags
that actually group — plus the honesty rules the module claims for itself.
"""
from __future__ import annotations

import json
import re

import openapi
import pytest
from app import app as flask_app


@pytest.fixture(scope="module")
def spec():
    return openapi.build_spec(flask_app, version="1.2.3")


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


class TestDocumentShape:
    def test_it_is_openapi_31_with_the_deployment_version(self, spec):
        assert spec["openapi"] == "3.1.0"
        assert spec["info"]["version"] == "1.2.3"
        assert spec["info"]["title"]

    def test_it_describes_a_substantial_surface(self, spec):
        stats = openapi.spec_stats(spec)
        # Guards against a generator that silently filters everything out.
        assert stats["paths"] > 100
        assert stats["operations"] > 150

    def test_only_api_paths_are_published(self, spec):
        # SPA page routes and static assets are not an API surface.
        assert all(p.startswith("/api/") for p in spec["paths"])

    def test_a_security_scheme_is_declared_since_the_surface_needs_auth(self, spec):
        assert "sessionCookie" in spec["components"]["securitySchemes"]
        assert spec["security"]


class TestValidity:
    """The rules a consumer's OpenAPI parser will enforce."""

    def test_every_templated_path_parameter_is_declared(self, spec):
        """The classic invalid-spec error: `/x/{id}` with no `id` parameter.

        A generated spec gets this wrong for every route at once if the converter parsing breaks,
        so it is worth asserting across the whole document rather than on a sample.
        """
        missing = []
        for path, methods in spec["paths"].items():
            templated = set(re.findall(r"\{([^}]+)\}", path))
            for method, op in methods.items():
                declared = {
                    p["name"] for p in op.get("parameters", []) if p.get("in") == "path"
                }
                if templated - declared:
                    missing.append(f"{method.upper()} {path}: {sorted(templated - declared)}")
        assert not missing, f"path parameters not declared: {missing[:5]}"

    def test_path_parameters_are_required_as_the_spec_demands(self, spec):
        for path, methods in spec["paths"].items():
            for op in methods.values():
                for param in op.get("parameters", []):
                    if param["in"] == "path":
                        assert param["required"] is True, path

    def test_operation_ids_are_unique(self, spec):
        seen: dict[str, str] = {}
        for path, methods in spec["paths"].items():
            for op in methods.values():
                oid = op["operationId"]
                assert oid not in seen, f"duplicate operationId {oid}: {seen.get(oid)} and {path}"
                seen[oid] = path

    def test_every_operation_has_a_summary_and_a_200(self, spec):
        for path, methods in spec["paths"].items():
            for method, op in methods.items():
                assert op["summary"], f"{method} {path} has no summary"
                assert "200" in op["responses"], f"{method} {path} has no 200"

    def test_every_used_tag_is_declared_at_the_top_level(self, spec):
        declared = {t["name"] for t in spec["tags"]}
        used = {t for methods in spec["paths"].values()
                for op in methods.values() for t in op["tags"]}
        assert used <= declared, f"undeclared tags: {sorted(used - declared)}"

    def test_every_response_key_is_a_real_http_status(self, spec):
        """OpenAPI requires response keys to match `^[1-5][0-9]{2}$`.

        The first version scraped any three digits following a comma, which turned a
        `timeout=900` literal into a `900` response on /api/universal/scan. Every structural test
        here passed; only a real validator rejected it. Hence this test *and* the one below.
        """
        bad = []
        for path, methods in spec["paths"].items():
            for method, op in methods.items():
                for code in op["responses"]:
                    if not (code.isdigit() and 100 <= int(code) <= 599):
                        bad.append(f"{method.upper()} {path}: {code}")
        assert not bad, f"not HTTP statuses: {bad}"

    def test_a_real_openapi_validator_accepts_the_document(self, spec):
        """The check that actually catches generator bugs, when the library is installed.

        Deliberately not a runtime dependency — it is a development check, so it skips rather
        than forcing openapi-spec-validator into the container image.
        """
        validator = pytest.importorskip(
            "openapi_spec_validator", reason="dev-only check; pip install openapi-spec-validator"
        )
        validator.validate(spec)

    def test_the_document_is_json_serialisable(self, spec):
        json.dumps(spec)


class TestDerivedContent:
    """What the module claims to derive, it must actually derive."""

    def test_a_query_parameter_the_handler_reads_is_documented(self, spec):
        op = spec["paths"]["/api/node-report/status"]["get"]
        assert "job_id" in {p["name"] for p in op["parameters"]}

    def test_a_status_code_the_handler_returns_is_documented(self, spec):
        # api_node_report_status returns 404 for an unknown job id.
        assert "404" in spec["paths"]["/api/node-report/status"]["get"]["responses"]

    def test_the_docstring_becomes_summary_and_description(self, spec):
        op = spec["paths"]["/api/node-report/status"]["get"]
        assert op["summary"].startswith("Progress for a job")
        # The rest of the docstring explains why the document is not returned here.
        assert "several MB" in op.get("description", "")

    def test_a_request_body_is_declared_only_where_json_is_read(self, spec):
        start = spec["paths"]["/api/node-report/start"]["post"]
        assert "requestBody" in start
        # A GET never carries one.
        status = spec["paths"]["/api/node-report/status"]["get"]
        assert "requestBody" not in status

    def test_bodies_are_not_given_a_fabricated_schema(self, spec):
        body = spec["paths"]["/api/node-report/start"]["post"]["requestBody"]
        schema = body["content"]["application/json"]["schema"]
        # Free-form on purpose: inventing per-endpoint schemas would be a contract we do not keep.
        assert schema == {"type": "object"}
        assert "description" in body


class TestTagging:
    def test_tags_group_rather_than_naming_every_endpoint(self, spec):
        """Grouping by raw path segment gave 106 tags for 201 operations — no grouping at all.

        Stated as a **ratio**, not as a tag ceiling. The original `len(counts) < 50` was a proxy
        for the same property and held for a year, then failed the day a genuinely new domain
        (`/api/setup`, five endpoints under one tag) was added — which is correct grouping, not
        the failure the test was written for. A ceiling on domains makes every new domain look
        like a regression and gets raised by one each time, which is a gate that has stopped
        deciding anything.

        Measured 2026-09-14: 263 operations across 50 tags, mean 5.3. The regression this
        guards against sat at 1.9.
        """
        counts: dict[str, int] = {}
        for methods in spec["paths"].values():
            for op in methods.values():
                counts[op["tags"][0]] = counts.get(op["tags"][0], 0) + 1
        operations = sum(counts.values())
        mean = operations / len(counts)
        assert mean >= 3.0, (
            f"tags have stopped grouping: {len(counts)} tags for {operations} operations "
            f"(mean {mean:.1f}); a tag per endpoint measures 1.0"
        )
        assert not [t for t, n in counts.items() if n == 1], "a tag holding one operation"

    def test_an_endpoint_family_shares_a_tag(self, spec):
        starts = spec["paths"]["/api/node-report/start"]["post"]["tags"]
        status = spec["paths"]["/api/node-report/status"]["get"]["tags"]
        assert starts == status

    def test_async_and_status_siblings_are_one_family(self):
        assert openapi._base_of("gpu-reset-async") == "gpu-reset"
        assert openapi._base_of("hw-check-status") == "hw-check"
        assert openapi._base_of("catalog") == "catalog"


class TestEndpoints:
    def test_the_json_endpoint_serves_the_spec(self, client):
        resp = client.get("/api/openapi.json")
        assert resp.status_code == 200
        assert resp.mimetype == "application/json"
        body = resp.get_json()
        assert body["openapi"] == "3.1.0"
        assert "/api/openapi.json" in body["paths"]  # it describes itself

    def test_the_yaml_endpoint_serves_the_same_document(self, client):
        yaml = pytest.importorskip("yaml")
        resp = client.get("/api/openapi.yaml")
        assert resp.status_code == 200
        parsed = yaml.safe_load(resp.get_data(as_text=True))
        assert parsed["openapi"] == "3.1.0"
        assert parsed["paths"].keys() == client.get("/api/openapi.json").get_json()["paths"].keys()
