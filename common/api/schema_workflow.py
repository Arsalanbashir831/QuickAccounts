"""Presentation-only OpenAPI grouping; endpoint URLs and behavior are unchanged."""

from typing import Any

TAGS = [
    {
        "name": "00 Platform administration",
        "description": (
            "Private, active-superuser-only onboarding and licensing commands. "
            "Provision tenants and companies, publish plans, issue and assign licenses. "
            "Every command requires an Idempotency-Key and an audit reason."
        ),
    },
    {
        "name": "01 Authentication & sessions",
        "description": "Log in, rotate refresh credentials, manage devices, and change passwords.",
    },
    {
        "name": "02 Workspace & licensing",
        "description": (
            "Discover assigned tenants and companies; view license status and manage "
            "device activation. Tenant owners cannot register tenants or assign licenses."
        ),
    },
    {
        "name": "03 Company setup & master data",
        "description": "Configure modules, partners, addresses, taxes, and reference units.",
    },
    {
        "name": "04 Catalog & warehouses",
        "description": "Define items, accounting profiles, warehouses, and stock identifiers.",
    },
    {
        "name": "05 Sales & fulfillment",
        "description": "Create orders and invoice drafts, calculate, post, and deliver goods.",
    },
    {
        "name": "06 Returns, refunds & replacements",
        "description": "Inspect returns, issue credits or refunds, and fulfill replacements.",
    },
    {
        "name": "07 Repairs & dead stock",
        "description": "Repair, inspect, release, dispose of, and report on segregated stock.",
    },
    {
        "name": "08 Purchasing & supplier claims",
        "description": "Draft and post bills, return stock, and manage supplier claims.",
    },
    {
        "name": "09 Inventory movements & valuation",
        "description": "Receive, transfer, ship, count, and trace stock costs and serials.",
    },
    {
        "name": "10 Payments & allocations",
        "description": "Create, allocate, post, and reverse customer and supplier payments.",
    },
    {
        "name": "11 Manufacturing",
        "description": "Manage BOMs and production orders through material issue and completion.",
    },
    {
        "name": "12 Accounting & period close",
        "description": "Maintain accounts and journals, post entries, and close periods.",
    },
    {
        "name": "13 Reports & reconciliation",
        "description": "Read financial, tax, aging, stock-valuation, and activity reports.",
    },
    {
        "name": "14 Background jobs",
        "description": "Inspect and control long-running jobs and generated artifacts.",
    },
]

TAG_NAMES = {tag["name"].split(" ", 1)[1]: tag["name"] for tag in TAGS}

RESOURCE_NAMES = {
    "auth": "session",
    "tenants": "tenant",
    "companies": "company",
    "accounts": "account",
    "chart-templates": "chart template",
    "dimension-types": "dimension type",
    "dimension-values": "dimension value",
    "entries": "journal entry",
    "journals": "journal",
    "periods": "fiscal period",
    "posting-rules": "posting rule",
    "items": "item",
    "warehouses": "warehouse",
    "lots": "lot",
    "serials": "serial",
    "documents": "stock document",
    "orders": "order",
    "invoices": "invoice",
    "returns": "return",
    "bills": "purchase bill",
    "supplier-claims": "supplier claim",
    "payments": "payment",
    "boms": "BOM",
    "partners": "partner",
    "jobs": "job",
    "repairs": "repair job",
    "disposal-cases": "disposal case",
    "return-intakes": "return intake",
    "reservations": "reservation",
    "activations": "activation",
    "licenses": "license",
    "renewal-orders": "renewal order",
    "channels": "sales channel",
    "addresses": "address",
    "modules": "module",
    "movements": "stock movement",
    "allocations": "allocation",
    "tax-registrations": "tax registration",
    "uoms": "unit of measure",
}

PLURAL_NAMES = {
    "company": "companies",
    "journal entry": "journal entries",
    "BOM": "BOMs",
    "address": "addresses",
    "sales channel": "sales channels",
    "tax registration": "tax registrations",
    "unit of measure": "units of measure",
}

ACTION_NAMES = {
    "calculate": "Calculate",
    "post": "Post",
    "reverse": "Reverse",
    "void": "Void",
    "inspect": "Inspect",
    "refund": "Refund",
    "replace": "Issue replacement for",
    "apply-credit": "Apply credit to",
    "credit-notes": "Create credit note for",
    "release": "Release",
    "complete": "Complete",
    "cancel": "Cancel",
    "activate": "Activate",
    "retire": "Retire",
    "close": "Close",
    "reopen": "Reopen",
    "suspend": "Suspend",
    "resume": "Resume",
    "revoke": "Revoke",
    "redeem": "Redeem",
    "heartbeat": "Heartbeat for",
}

SPECIAL_POST = {
    "apply": "Apply chart template",
    "validate": "Validate module changes",
    "deactivate": "Deactivate activation",
    "publish": "Publish plan version",
    "delivery": "Record invoice delivery",
    "rebuild": "Rebuild inventory valuation",
    "serial-bulk": "Register serial numbers",
    "stock-dispositions": "Route or dispose of return stock",
    "replacement-serials": "Link replacement serials",
    "material-issues": "Issue production materials",
    "outputs": "Record production output",
}

SPECIAL_GET = {
    "accounting-profile": "Get item accounting profile",
    "availability": "Check stock availability",
    "balances": "Get warehouse balances",
    "capabilities": "Get company capabilities",
    "cost-basis": "Get movement cost basis",
    "cost-policy": "Get inventory cost policy",
    "history": "Get serial history",
    "return-eligibility": "Check return eligibility",
    "replacement-serials": "Get linked replacement serials",
    "status": "Get job status",
    "artifact": "Get job artifact",
}

METHOD_NAMES = {
    "get": "Get",
    "post": "Create",
    "put": "Replace",
    "patch": "Update",
    "delete": "Delete",
}


def canonical_workflow_endpoints(
    endpoints: list[tuple[Any, ...]], **kwargs: Any
) -> list[tuple[Any, ...]]:
    """Show one canonical auth path in Swagger while preserving all live aliases."""
    return [
        endpoint
        for endpoint in endpoints
        if not endpoint[0].startswith("/api/v1/auth/")
        and not endpoint[0].startswith("/api/auth/token")
        and endpoint[0] != "/api/auth/me"
    ]


def _section(path: str) -> str:
    if path.startswith("/platform-api/"):
        return "Platform administration"
    if path.startswith("/api/auth/"):
        return "Authentication & sessions"
    if "/license" in path:
        return "Workspace & licensing"
    if path == "/api/v1/tenants/" or "/companies" in path and "/companies/" not in path:
        return "Workspace & licensing"
    if "/jobs" in path:
        return "Background jobs"
    if "/reports/dead-stock" in path:
        return "Repairs & dead stock"
    if "/reports/" in path:
        return "Reports & reconciliation"
    if "/sales/returns/" in path or "/returns" in path and "/sales/invoices/" in path:
        return "Returns, refunds & replacements"
    if "/inventory/return-intakes" in path:
        return "Returns, refunds & replacements"
    if any(part in path for part in ("/inventory/repairs", "/inventory/disposal-cases")):
        return "Repairs & dead stock"
    if "/sales/" in path:
        return "Sales & fulfillment"
    if "/purchasing/" in path:
        return "Purchasing & supplier claims"
    if "/payments" in path:
        return "Payments & allocations"
    if "/manufacturing/" in path:
        return "Manufacturing"
    if "/accounting/" in path:
        return "Accounting & period close"
    if any(part in path for part in ("/admin/", "/capabilities", "/partners", "/reference/")):
        return "Company setup & master data"
    if any(
        part in path for part in ("/inventory/items", "/inventory/warehouses", "/inventory/lots")
    ):
        return "Catalog & warehouses"
    if "/inventory/" in path:
        return "Inventory movements & valuation"
    return "Workspace & licensing"


def _summary(path: str, method: str) -> str:
    parts = [part for part in path.strip("/").split("/") if not part.startswith("{")]
    tail = parts[-1]
    if path.startswith("/platform-api/"):
        if tail == "tenants":
            return "Provision tenant, initial company, and owner"
        if tail == "companies":
            return "Provision another company for a tenant"
        if tail == "licenses":
            return "Issue a license to a tenant"
        if tail == "assign":
            return "Assign an issued license"
        if tail == "publish":
            return "Publish a plan version"
        return f"{tail.capitalize()} a license"
    if path.startswith("/api/auth/"):
        return {
            "csrf": "Get a CSRF token",
            "login": "Log in",
            "refresh": "Rotate refresh session",
            "logout": "Log out this device",
            "logout-all": "Log out all devices",
            "sessions": "List active sessions" if method == "get" else "Revoke a session",
            "change": "Change password",
            "me": "Get current user",
        }.get(tail, tail.replace("-", " ").capitalize())
    if method == "get" and "/reports/" in path:
        return f"Get {tail.replace('-', ' ')} report"
    if method == "get" and tail in SPECIAL_GET:
        return SPECIAL_GET[tail]
    if method in {"put", "patch"} and tail in {"accounting-profile", "allocations", "withholding"}:
        return {
            "accounting-profile": "Update item accounting profile",
            "allocations": "Update payment allocations",
            "withholding": "Update payment withholding",
        }[tail]
    if method == "post" and tail in SPECIAL_POST:
        return SPECIAL_POST[tail]
    if tail in ACTION_NAMES and len(parts) > 1:
        parent = RESOURCE_NAMES.get(parts[-2], parts[-2].replace("-", " "))
        return f"{ACTION_NAMES[tail]} {parent}"
    noun = RESOURCE_NAMES.get(tail, tail.replace("-", " "))
    detail = path.rstrip("/").endswith("}")
    if method == "get":
        if detail or tail not in RESOURCE_NAMES:
            return f"Get {noun}"
        return f"List {PLURAL_NAMES.get(noun, noun + 's')}"
    return f"{METHOD_NAMES.get(method, method.upper())} {noun}"


def organize_workflow_schema(
    result: dict[str, Any], generator: Any, **kwargs: Any
) -> dict[str, Any]:
    """Assign ordered workflow tags and readable titles to generated operations."""
    for path, methods in result.get("paths", {}).items():
        tag = TAG_NAMES[_section(path)]
        for method, operation in methods.items():
            if method.lower() not in METHOD_NAMES or not isinstance(operation, dict):
                continue
            operation["tags"] = [tag]
            operation.setdefault("summary", _summary(path, method.lower()))
    return result
