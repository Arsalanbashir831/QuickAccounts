"""Read-only valuation/GL comparison from one PostgreSQL statement snapshot."""

import datetime as dt
import json
import uuid
from typing import Any

from apps.payments.services import _rows
from common.api.errors import Conflict

# Financial JSON values are text, never binary floats. Limit input ledgers before
# joining/aggregating; larger companies require a durable report-job workflow.
SQL = r"""
WITH movement_input AS MATERIALIZED (
 SELECT * FROM erp.stock_movements WHERE company_id=%s ORDER BY id LIMIT 10001
), profile_input AS MATERIALIZED (
 SELECT inventory_account_id FROM erp.item_accounting_profiles WHERE company_id=%s
 AND inventory_account_id IS NOT NULL ORDER BY item_id LIMIT 10001
), mapped AS MATERIALIZED (
 SELECT m.*,coalesce(dl.inventory_account_id_snapshot,sl.inventory_account_id_snapshot,
 pl.inventory_account_id_snapshot,rsl.inventory_account_id_snapshot) inventory_account_id
 FROM movement_input m
 LEFT JOIN erp.inventory_document_lines dl ON dl.company_id=m.company_id
 AND dl.id=m.inventory_document_line_id
 LEFT JOIN erp.sales_invoice_lines sl ON sl.company_id=m.company_id AND sl.id=m.source_line_id
 AND m.source_type='sales_invoice'
 LEFT JOIN erp.purchase_bill_lines pl ON pl.company_id=m.company_id AND pl.id=m.source_line_id
 AND m.source_type='purchase_bill'
 LEFT JOIN erp.sales_return_lines rl ON rl.company_id=m.company_id AND rl.id=m.source_line_id
 AND m.source_type IN ('sales_return','return_stock_action')
 LEFT JOIN erp.sales_invoice_lines rsl ON rsl.company_id=rl.company_id
 AND rsl.id=rl.sales_invoice_line_id
), account_ids AS (
 SELECT inventory_account_id id FROM profile_input UNION
 SELECT inventory_account_id FROM mapped WHERE inventory_account_id IS NOT NULL
), gl_input AS MATERIALIZED (
 SELECT jl.journal_entry_id,jl.account_id,jl.debit_amount-jl.credit_amount value_company
 FROM erp.journal_lines jl JOIN erp.journal_entries je ON je.company_id=jl.company_id
 AND je.id=jl.journal_entry_id WHERE jl.company_id=%s AND je.status='posted'
 AND je.entry_date<=%s AND jl.account_id IN (SELECT id FROM account_ids)
 ORDER BY jl.id LIMIT 10001
), allocation_input AS MATERIALIZED (
 SELECT a.* FROM erp.inventory_cost_allocations a WHERE a.company_id=%s
 AND a.issue_movement_id IN (SELECT id FROM mapped WHERE quantity_delta<0)
 ORDER BY a.id LIMIT 10001
), bounds AS (
 SELECT (SELECT count(*) FROM movement_input)>10000
 OR (SELECT count(*) FROM profile_input)>10000
 OR (SELECT count(*) FROM gl_input)>10000
 OR (SELECT count(*) FROM allocation_input)>10000 exceeded
), effective AS MATERIALIZED (
 SELECT * FROM mapped WHERE occurred_at<%s AND NOT (SELECT exceeded FROM bounds)
), stock_accounts AS (
 SELECT inventory_account_id account_id,sum(value_delta_company) value_company
 FROM effective WHERE inventory_account_id IS NOT NULL GROUP BY inventory_account_id
), gl_accounts AS (
 SELECT account_id,sum(value_company) value_company FROM gl_input GROUP BY account_id
), accounts AS MATERIALIZED (
 SELECT a.id account_id,a.code,a.name,coalesce(s.value_company,0) stock_value_company,
 coalesce(g.value_company,0) gl_value_company,
 coalesce(s.value_company,0)-coalesce(g.value_company,0) difference_company
 FROM account_ids x JOIN erp.accounts a ON a.company_id=%s AND a.id=x.id
 LEFT JOIN stock_accounts s ON s.account_id=a.id LEFT JOIN gl_accounts g ON g.account_id=a.id
), stock_journals AS (
 SELECT journal_entry_id,inventory_account_id account_id,sum(value_delta_company) value_company
 FROM effective WHERE inventory_account_id IS NOT NULL
 GROUP BY journal_entry_id,inventory_account_id
), gl_journals AS (
 SELECT journal_entry_id,account_id,sum(value_company) value_company
 FROM gl_input GROUP BY journal_entry_id,account_id
), journal_differences AS MATERIALIZED (
 SELECT coalesce(s.journal_entry_id,g.journal_entry_id) journal_entry_id,
 coalesce(s.account_id,g.account_id) account_id,coalesce(s.value_company,0) stock_value_company,
 coalesce(g.value_company,0) gl_value_company,
 coalesce(s.value_company,0)-coalesce(g.value_company,0) difference_company
 FROM stock_journals s FULL JOIN gl_journals g ON g.journal_entry_id=s.journal_entry_id
 AND g.account_id=s.account_id WHERE coalesce(s.value_company,0)<>coalesce(g.value_company,0)
), allocation_totals AS (
 SELECT issue_movement_id,sum(quantity) quantity,sum(value_company) value_company
 FROM allocation_input GROUP BY issue_movement_id
), costing_gaps AS MATERIALIZED (
 SELECT m.id,CASE WHEN s.id IS NULL AND h.id IS NULL THEN 'COST_BASIS_UNAVAILABLE'
 WHEN s.id IS NOT NULL AND h.id IS NOT NULL THEN 'CONFLICTING_COST_BASIS'
 WHEN coalesce(s.issue_quantity,h.issue_quantity) IS DISTINCT FROM -m.quantity_delta
 OR coalesce(s.issue_value_company,h.issue_value_company) IS DISTINCT FROM -m.value_delta_company
 THEN 'COST_BASIS_DIFFERENCE' ELSE 'COST_ALLOCATION_DIFFERENCE' END code
 FROM effective m LEFT JOIN erp.inventory_cost_basis_snapshots s ON s.company_id=m.company_id
 AND s.movement_id=m.id LEFT JOIN erp.inventory_return_cost_basis h ON h.company_id=m.company_id
 AND h.movement_id=m.id LEFT JOIN allocation_totals a ON a.issue_movement_id=m.id
 WHERE m.quantity_delta<0 AND (
 (s.id IS NULL AND h.id IS NULL) OR (s.id IS NOT NULL AND h.id IS NOT NULL)
 OR coalesce(s.issue_quantity,h.issue_quantity) IS DISTINCT FROM -m.quantity_delta
 OR coalesce(s.issue_value_company,h.issue_value_company) IS DISTINCT FROM -m.value_delta_company
 OR ((a.issue_movement_id IS NOT NULL OR EXISTS(
 SELECT 1 FROM erp.inventory_cost_checkpoints cp WHERE cp.company_id=m.company_id
 AND cp.warehouse_id=m.warehouse_id AND cp.item_id=m.item_id
 AND cp.lot_id IS NOT DISTINCT FROM m.lot_id AND NOT EXISTS(
 SELECT 1 FROM erp.inventory_cost_checkpoint_movements cm WHERE cm.company_id=cp.company_id
 AND cm.checkpoint_id=cp.id AND cm.movement_id=m.id)))
 AND (coalesce(a.quantity,0)<>-m.quantity_delta
 OR coalesce(a.value_company,0)<>-m.value_delta_company)))
), scopes AS MATERIALIZED (
 SELECT warehouse_id,item_id,lot_id,inventory_account_id,sum(quantity_delta) quantity,
 sum(value_delta_company) value_company FROM effective
 GROUP BY warehouse_id,item_id,lot_id,inventory_account_id
), diagnostics AS MATERIALIZED (
 SELECT 'UNATTRIBUTED_MOVEMENT' code,m.id movement_id,NULL::uuid journal_entry_id,
 NULL::uuid account_id,m.value_delta_company::text difference_company
 FROM effective m WHERE inventory_account_id IS NULL
 UNION ALL SELECT 'JOURNAL_VALUE_DIFFERENCE',NULL,journal_entry_id,account_id,
 difference_company::text FROM journal_differences
 UNION ALL SELECT code,id,NULL,NULL,NULL FROM costing_gaps
 UNION ALL SELECT 'NEGATIVE_ATTRIBUTED_VALUATION',NULL,NULL,inventory_account_id,
 value_company::text FROM scopes WHERE quantity<0 OR value_company<0
)
SELECT jsonb_build_object(
 'job_required',(SELECT exceeded FROM bounds),
 'currency',(SELECT functional_currency FROM erp.companies WHERE id=%s),
 'ledger_value_company',(SELECT coalesce(sum(value_delta_company),0)::text FROM effective),
 'attributed_value_company',(SELECT coalesce(sum(value_company),0)::text FROM stock_accounts),
 'gl_value_company',(SELECT coalesce(sum(value_company),0)::text FROM gl_input),
 'unattributed_movement_count',(SELECT count(*) FROM effective WHERE inventory_account_id IS NULL),
 'account_difference_count',(SELECT count(*) FROM accounts WHERE difference_company<>0),
 'journal_difference_count',(SELECT count(*) FROM journal_differences),
 'costing_gap_count',(SELECT count(*) FROM costing_gaps),
 'negative_scope_count',(SELECT count(*) FROM scopes WHERE quantity<0 OR value_company<0),
 'scope_count',(SELECT count(*) FROM scopes),
 'account_count',(SELECT count(*) FROM accounts),
 'diagnostic_count',(SELECT count(*) FROM diagnostics),
 'accounts',coalesce((SELECT jsonb_agg(to_jsonb(x)) FROM (
 SELECT account_id,code,name,stock_value_company::text,gl_value_company::text,
 difference_company::text FROM accounts ORDER BY code,account_id LIMIT %s) x),'[]'::jsonb),
 'valuation',coalesce((SELECT jsonb_agg(to_jsonb(x)) FROM (
 SELECT warehouse_id,item_id,lot_id,inventory_account_id,quantity::text,value_company::text
 FROM scopes ORDER BY warehouse_id,item_id,lot_id NULLS FIRST,inventory_account_id NULLS FIRST
 LIMIT %s) x),'[]'::jsonb),
 'diagnostics',coalesce((SELECT jsonb_agg(to_jsonb(x)) FROM (
 SELECT * FROM diagnostics ORDER BY code,movement_id NULLS FIRST,
 journal_entry_id NULLS FIRST,account_id NULLS FIRST LIMIT %s) x),'[]'::jsonb)
) report
"""


def valuation_reconciliation(company: uuid.UUID, *, as_of: dt.date, limit: int) -> dict[str, Any]:
    cutoff = dt.datetime.combine(as_of + dt.timedelta(days=1), dt.time(), dt.UTC)
    raw = _rows(
        SQL,
        [company, company, company, as_of, company, cutoff, company, company, limit, limit, limit],
    )[0]["report"]
    result: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else raw
    if result.pop("job_required"):
        raise Conflict(
            "INVENTORY_REPORT_JOB_REQUIRED",
            "Interactive reconciliation exceeds 10,000 input rows; "
            "a durable report job is required.",
        )
    result["financial_matches"] = not (
        result["unattributed_movement_count"]
        or result["account_difference_count"]
        or result["journal_difference_count"]
        or result["negative_scope_count"]
    )
    result["costing_coverage_complete"] = result["costing_gap_count"] == 0
    result["matches"] = result["financial_matches"] and result["costing_coverage_complete"]
    result["as_of"] = as_of.isoformat()
    result["cutoff_timezone"] = "UTC"
    result["date_basis"] = "stock occurred_at and posted journal entry_date"
    result["projection_checked"] = False
    result["limit"] = limit
    result["truncated"] = any(
        result[key] > limit for key in ("scope_count", "account_count", "diagnostic_count")
    )
    return result
