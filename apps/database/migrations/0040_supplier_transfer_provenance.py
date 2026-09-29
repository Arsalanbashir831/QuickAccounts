from importlib import import_module

from django.db import migrations

previous = str(import_module("apps.database.migrations.0039_partial_supplier_returns").SQL)
start = previous.index("CREATE OR REPLACE FUNCTION erp.check_supplier_return_complete()")
source = previous[start:]
needle = "m.warehouse_id IS DISTINCT FROM o.warehouse_id OR"
proof = r"""NOT (m.warehouse_id=o.warehouse_id OR EXISTS(
 SELECT 1 FROM erp.stock_movements inbound
 JOIN erp.stock_movements outbound ON outbound.company_id=inbound.company_id
 AND outbound.inventory_document_line_id=inbound.inventory_document_line_id
 AND outbound.quantity_delta=-inbound.quantity_delta
 AND outbound.value_delta_company=-inbound.value_delta_company
 JOIN erp.inventory_documents doc ON doc.company_id=inbound.company_id
 AND doc.id=inbound.source_id WHERE inbound.company_id=m.company_id
 AND inbound.id<>m.id AND inbound.source_type='inventory_document'
 AND inbound.movement_kind='transfer' AND doc.document_kind='transfer'
 AND doc.status='posted' AND inbound.item_id=o.item_id
 AND inbound.warehouse_id=m.warehouse_id
 AND inbound.quantity_delta=l.quantity AND inbound.value_delta_company=v
 AND inbound.lot_id IS NULL AND outbound.warehouse_id=o.warehouse_id
 AND outbound.item_id=o.item_id AND outbound.quantity_delta=-l.quantity
 AND outbound.value_delta_company=-v AND outbound.lot_id IS NULL
 AND NOT EXISTS(SELECT 1 FROM erp.stock_movements other
 WHERE other.company_id=o.company_id AND other.warehouse_id=o.warehouse_id
 AND other.item_id=o.item_id AND other.lot_id IS NULL
 AND other.quantity_delta>0 AND other.id<>o.id)
 AND NOT EXISTS(SELECT 1 FROM erp.stock_movements other
 WHERE other.company_id=m.company_id AND other.warehouse_id=m.warehouse_id
 AND other.item_id=m.item_id AND other.lot_id IS NULL
 AND other.quantity_delta>0 AND other.id<>inbound.id)
 AND NOT EXISTS(SELECT 1 FROM erp.stock_movements other
 WHERE other.company_id=m.company_id AND other.warehouse_id=m.warehouse_id
 AND other.item_id=m.item_id AND other.lot_id IS NULL
 AND other.quantity_delta<0 AND other.id<>m.id)
 )) OR"""
if source.count(needle) != 1:
    raise RuntimeError("Supplier return proof migration source changed")
SQL = source.replace(needle, proof)
REVERSE = source


class Migration(migrations.Migration):
    dependencies = [("database", "0039_partial_supplier_returns")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
