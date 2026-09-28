"""Retain empty adopted scopes when rebuilding a missing position projection."""

from importlib import import_module

from django.db import migrations

# Preserve the original SQL-owned lock, authorization, audit and grant contract.
# Only extend scope discovery; neither migration direction changes financial facts.
_original = import_module("apps.database.migrations.0021_inventory_rebuild").SQL
_function = _original[
    _original.index("CREATE FUNCTION erp.rebuild_inventory_positions") : _original.index(
        "REVOKE ALL"
    )
].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
_anchor = (
    "SELECT warehouse_id,item_id,lot_id FROM erp.inventory_positions WHERE company_id=p_company"
)
assert _function.count(_anchor) == 1
SQL = _function.replace(
    _anchor,
    _anchor + " UNION SELECT warehouse_id,item_id,lot_id FROM erp.inventory_cost_checkpoints "
    "WHERE company_id=p_company UNION SELECT warehouse_id,item_id,lot_id "
    "FROM erp.inventory_cost_layers WHERE company_id=p_company",
)


class Migration(migrations.Migration):
    dependencies = [("database", "0029_physical_supplier_returns")]
    operations = [migrations.RunSQL(SQL, _function)]
