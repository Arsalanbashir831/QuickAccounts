import pytest
from django.urls import resolve
from drf_spectacular.generators import SchemaGenerator

pytestmark = pytest.mark.unit


def test_openapi_is_grouped_by_business_workflow() -> None:
    schema = SchemaGenerator().get_schema(public=True)
    assert schema is not None
    tags = [tag["name"] for tag in schema["tags"]]
    assert tags == sorted(tags)
    paths = schema["paths"]
    assert "/api/auth/login/" in paths
    assert "/api/v1/auth/login/" not in paths
    assert "/api/auth/token" not in paths
    assert resolve("/api/v1/auth/login/").view_name == "auth-login"
    assert resolve("/api/auth/token").view_name == "auth-token-legacy"
    company = "/api/v1/tenants/{tenant_id}/companies/{company_id}"
    expected = {
        "/api/auth/refresh/": "01 Authentication & sessions",
        "/platform-api/v1/tenants": "00 Platform administration",
        "/platform-api/v1/tenants/{tenant_id}/companies": "00 Platform administration",
        "/platform-api/v1/licenses": "00 Platform administration",
        "/platform-api/v1/plan-versions": "00 Platform administration",
        "/platform-api/v1/licenses/{license_id}/assign": "00 Platform administration",
        f"{company}/sales/invoices/{{invoice_id}}/post": "05 Sales & fulfillment",
        f"{company}/sales/returns/{{return_id}}/refund": "06 Returns, refunds & replacements",
        f"{company}/inventory/repairs": "07 Repairs & dead stock",
        f"{company}/payments/{{payment_id}}/post": "10 Payments & allocations",
        f"{company}/reports/trial-balance": "13 Reports & reconciliation",
    }
    for path, tag in expected.items():
        assert path in paths
        for operation in paths[path].values():
            assert operation["tags"] == [tag]
            assert operation["summary"]
    for methods in paths.values():
        for operation in methods.values():
            assert len(operation["tags"]) == 1
            assert operation["tags"][0] in tags
            assert operation["summary"]


def test_platform_openapi_describes_private_onboarding_contract() -> None:
    schema = SchemaGenerator().get_schema(public=True)
    assert schema is not None
    paths = schema["paths"]
    assert "/api/v1/tenants/{tenant_id}/license/redeem" not in paths
    for path in (
        "/platform-api/v1/tenants",
        "/platform-api/v1/tenants/{tenant_id}/companies",
        "/platform-api/v1/licenses",
        "/platform-api/v1/licenses/{license_id}/assign",
        "/platform-api/v1/plan-versions",
        "/platform-api/v1/plan-versions/{plan_version_id}/publish",
    ):
        operation = paths[path]["post"]
        assert operation["tags"] == ["00 Platform administration"]
        assert "superuser" in operation["description"].lower()
        assert any(
            parameter["name"] == "Idempotency-Key" and parameter["required"]
            for parameter in operation["parameters"]
        )
    tenant_request = paths["/platform-api/v1/tenants"]["post"]["requestBody"]
    schema_ref = tenant_request["content"]["application/json"]["schema"]["$ref"]
    component = schema["components"]["schemas"][schema_ref.rsplit("/", 1)[-1]]
    assert component["properties"]["owner_password"]["writeOnly"] is True
    create_plan = paths["/platform-api/v1/plan-versions"]["post"]
    examples = create_plan["requestBody"]["content"]["application/json"]["examples"]
    assert examples["MonthlyStandardPlan"]["value"]["price_currency"] == "PKR"
