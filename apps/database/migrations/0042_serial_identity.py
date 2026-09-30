from django.db import migrations

SQL = r"""
CREATE TABLE erp.inventory_serials (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    item_id uuid NOT NULL,
    lot_id uuid NOT NULL,
    serial_number text NOT NULL CHECK(length(btrim(serial_number)) BETWEEN 1 AND 100),
    normalized_serial text GENERATED ALWAYS AS (upper(btrim(serial_number))) STORED,
    current_warehouse_id uuid,
    lifecycle_status text NOT NULL CHECK(lifecycle_status IN
        ('in_stock','with_customer','in_transit','disposed','outside')),
    first_receipt_movement_id uuid,
    last_sales_invoice_line_id uuid,
    row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id),
    UNIQUE(company_id,item_id,normalized_serial),
    UNIQUE(company_id,lot_id),
    FOREIGN KEY(company_id,item_id,lot_id) REFERENCES erp.inventory_lots(company_id,item_id,id),
    FOREIGN KEY(company_id,current_warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,first_receipt_movement_id) REFERENCES erp.stock_movements(company_id,id),
    FOREIGN KEY(company_id,last_sales_invoice_line_id)
        REFERENCES erp.sales_invoice_lines(company_id,id),
    CHECK((lifecycle_status='in_stock')=(current_warehouse_id IS NOT NULL))
);
CREATE INDEX ix_serial_exact ON erp.inventory_serials(company_id,normalized_serial,id);
CREATE UNIQUE INDEX uq_serial_lot_normalized ON erp.inventory_lots
    (company_id,item_id,upper(btrim(serial_code))) WHERE serial_code IS NOT NULL;
CREATE INDEX ix_serial_warehouse_page ON erp.inventory_serials
    (company_id,current_warehouse_id,id) WHERE lifecycle_status='in_stock';
CREATE INDEX ix_serial_invoice_line ON erp.inventory_serials
    (company_id,last_sales_invoice_line_id,id) WHERE last_sales_invoice_line_id IS NOT NULL;

CREATE TABLE erp.serial_movements (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    serial_id uuid NOT NULL,
    stock_movement_id uuid UNIQUE,
    movement_kind text NOT NULL,
    warehouse_id uuid,
    quantity_delta numeric(20,6) NOT NULL CHECK(quantity_delta IN (-1,0,1)),
    source_type text,
    source_id uuid,
    source_line_id uuid,
    occurred_at timestamptz NOT NULL,
    posted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor_user_id uuid,
    UNIQUE(company_id,id),
    FOREIGN KEY(company_id,serial_id) REFERENCES erp.inventory_serials(company_id,id),
    FOREIGN KEY(company_id,stock_movement_id) REFERENCES erp.stock_movements(company_id,id),
    FOREIGN KEY(company_id,warehouse_id) REFERENCES erp.warehouses(company_id,id)
);
CREATE INDEX ix_serial_movement_history ON erp.serial_movements
    (company_id,serial_id,posted_at,id);
CREATE INDEX ix_serial_movement_source ON erp.serial_movements
    (company_id,source_type,source_id,source_line_id,id);

ALTER TABLE erp.inventory_serials ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.inventory_serials FORCE ROW LEVEL SECURITY;
CREATE POLICY serial_tenant ON erp.inventory_serials
    USING (company_id IN (SELECT id FROM erp.companies
        WHERE tenant_id=erp.current_tenant_id()))
    WITH CHECK (company_id IN (SELECT id FROM erp.companies
        WHERE tenant_id=erp.current_tenant_id()));
ALTER TABLE erp.serial_movements ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.serial_movements FORCE ROW LEVEL SECURITY;
CREATE POLICY serial_movement_tenant ON erp.serial_movements
    USING (company_id IN (SELECT id FROM erp.companies
        WHERE tenant_id=erp.current_tenant_id()))
    WITH CHECK (company_id IN (SELECT id FROM erp.companies
        WHERE tenant_id=erp.current_tenant_id()));

CREATE FUNCTION erp.guard_serial_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Serial identity is permanent'; END IF;
    IF pg_trigger_depth()<2 THEN
        RAISE EXCEPTION 'Serial state changes require a stock movement';
    END IF;
    IF (NEW.company_id,NEW.item_id,NEW.lot_id,NEW.serial_number,NEW.first_receipt_movement_id)
       IS DISTINCT FROM
       (OLD.company_id,OLD.item_id,OLD.lot_id,OLD.serial_number,OLD.first_receipt_movement_id)
    THEN RAISE EXCEPTION 'Serial identity and provenance are immutable'; END IF;
    NEW.row_version:=OLD.row_version+1;
    NEW.updated_at:=clock_timestamp();
    RETURN NEW;
END $$;
CREATE TRIGGER trg_serial_identity BEFORE UPDATE OR DELETE ON erp.inventory_serials
    FOR EACH ROW EXECUTE FUNCTION erp.guard_serial_identity();
CREATE FUNCTION erp.guard_serial_movement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Serial movement history is immutable';
END $$;
CREATE TRIGGER trg_serial_movement_immutable BEFORE UPDATE OR DELETE ON erp.serial_movements
    FOR EACH ROW EXECUTE FUNCTION erp.guard_serial_movement();

CREATE TABLE erp.sales_return_serials (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    sales_return_id uuid NOT NULL,
    sales_return_line_id uuid NOT NULL,
    serial_id uuid NOT NULL,
    source_sales_invoice_line_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'inspected'
        CHECK(status IN ('inspected','posted','void')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id),
    UNIQUE(company_id,sales_return_line_id,serial_id),
    FOREIGN KEY(company_id,sales_return_id) REFERENCES erp.sales_returns(company_id,id),
    FOREIGN KEY(company_id,sales_return_line_id) REFERENCES erp.sales_return_lines(company_id,id),
    FOREIGN KEY(company_id,serial_id) REFERENCES erp.inventory_serials(company_id,id),
    FOREIGN KEY(company_id,source_sales_invoice_line_id)
        REFERENCES erp.sales_invoice_lines(company_id,id)
);
CREATE UNIQUE INDEX uq_active_serial_return ON erp.sales_return_serials
    (company_id,serial_id,source_sales_invoice_line_id) WHERE status<>'void';
CREATE INDEX ix_return_serial_line ON erp.sales_return_serials
    (company_id,sales_return_line_id,id);
ALTER TABLE erp.sales_return_serials ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.sales_return_serials FORCE ROW LEVEL SECURITY;
CREATE POLICY return_serial_tenant ON erp.sales_return_serials
    USING (company_id IN (SELECT id FROM erp.companies
        WHERE tenant_id=erp.current_tenant_id()))
    WITH CHECK (company_id IN (SELECT id FROM erp.companies
        WHERE tenant_id=erp.current_tenant_id()));
CREATE FUNCTION erp.guard_sales_return_serial() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_line uuid; v_return uuid; v_serial erp.inventory_serials%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Return serial history is permanent'; END IF;
    IF TG_OP='UPDATE' THEN
        IF (NEW.company_id,NEW.sales_return_id,NEW.sales_return_line_id,
            NEW.serial_id,NEW.source_sales_invoice_line_id) IS DISTINCT FROM
           (OLD.company_id,OLD.sales_return_id,OLD.sales_return_line_id,
            OLD.serial_id,OLD.source_sales_invoice_line_id)
           OR OLD.status<>'inspected' OR NEW.status NOT IN ('posted','void') THEN
            RAISE EXCEPTION 'Return serial identity/status is immutable';
        END IF;
        RETURN NEW;
    END IF;
    SELECT sales_return_id,sales_invoice_line_id INTO v_return,v_line
    FROM erp.sales_return_lines WHERE company_id=NEW.company_id
        AND id=NEW.sales_return_line_id FOR UPDATE;
    SELECT * INTO v_serial FROM erp.inventory_serials
        WHERE company_id=NEW.company_id AND id=NEW.serial_id FOR UPDATE;
    IF v_return IS DISTINCT FROM NEW.sales_return_id
       OR v_line IS DISTINCT FROM NEW.source_sales_invoice_line_id
       OR v_serial.last_sales_invoice_line_id IS DISTINCT FROM v_line
       OR v_serial.lifecycle_status<>'with_customer' THEN
        RAISE EXCEPTION 'Returned serial was not sold on the source invoice line';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_return_serial_guard BEFORE INSERT OR UPDATE OR DELETE
    ON erp.sales_return_serials FOR EACH ROW EXECUTE FUNCTION erp.guard_sales_return_serial();

-- Existing serial lots have one unit by the pre-existing stock-capacity invariant.
-- Fail closed if older raw serial labels collide after normalization.
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM erp.inventory_lots l JOIN erp.items i
        ON i.company_id=l.company_id AND i.id=l.item_id
        JOIN erp.stock_movements m ON m.company_id=l.company_id AND m.lot_id=l.id
        WHERE i.track_serials GROUP BY l.company_id,l.item_id,upper(btrim(l.serial_code))
        HAVING count(DISTINCT l.id)>1) THEN
        RAISE EXCEPTION 'Normalize duplicate historical serial labels before migration';
    END IF;
END $$;
DO $$ DECLARE v_tenant uuid;
BEGIN
FOR v_tenant IN SELECT id FROM erp.tenants LOOP
PERFORM set_config('app.tenant_id',v_tenant::text,true);
INSERT INTO erp.inventory_serials(company_id,item_id,lot_id,serial_number,
    current_warehouse_id,lifecycle_status,first_receipt_movement_id,last_sales_invoice_line_id)
SELECT l.company_id,l.item_id,l.id,l.serial_code,
    (SELECT m.warehouse_id FROM erp.stock_movements m
     WHERE m.company_id=l.company_id AND m.lot_id=l.id
     GROUP BY m.warehouse_id HAVING sum(m.quantity_delta)=1 LIMIT 1),
    CASE WHEN EXISTS(SELECT 1 FROM erp.stock_movements m
        WHERE m.company_id=l.company_id AND m.lot_id=l.id
        GROUP BY m.warehouse_id HAVING sum(m.quantity_delta)=1) THEN 'in_stock'
      WHEN (SELECT m.source_type FROM erp.stock_movements m
        WHERE m.company_id=l.company_id AND m.lot_id=l.id
        ORDER BY m.created_at DESC,m.id DESC LIMIT 1)='sales_invoice'
      THEN 'with_customer' ELSE 'outside' END,
    (SELECT m.id FROM erp.stock_movements m WHERE m.company_id=l.company_id
     AND m.lot_id=l.id AND m.quantity_delta=1 ORDER BY m.created_at,m.id LIMIT 1),
    (SELECT m.source_line_id FROM erp.stock_movements m WHERE m.company_id=l.company_id
     AND m.lot_id=l.id AND m.source_type='sales_invoice' AND m.quantity_delta=-1
     ORDER BY m.created_at DESC,m.id DESC LIMIT 1)
FROM erp.inventory_lots l JOIN erp.items i ON i.company_id=l.company_id AND i.id=l.item_id
WHERE i.track_serials AND EXISTS(SELECT 1 FROM erp.stock_movements m
    WHERE m.company_id=l.company_id AND m.lot_id=l.id)
  AND l.company_id IN (SELECT id FROM erp.companies WHERE tenant_id=v_tenant);
INSERT INTO erp.serial_movements(company_id,serial_id,stock_movement_id,movement_kind,
    warehouse_id,quantity_delta,source_type,source_id,source_line_id,occurred_at,posted_at)
SELECT m.company_id,s.id,m.id,m.movement_kind,m.warehouse_id,m.quantity_delta,
    m.source_type,m.source_id,m.source_line_id,m.occurred_at,m.created_at
FROM erp.stock_movements m JOIN erp.inventory_serials s
    ON s.company_id=m.company_id AND s.lot_id=m.lot_id
WHERE m.company_id IN (SELECT id FROM erp.companies WHERE tenant_id=v_tenant);
END LOOP;
PERFORM set_config('app.tenant_id','',true);
END $$;

CREATE FUNCTION erp.guard_serial_creation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth()<2 OR NOT EXISTS(
        SELECT 1 FROM erp.stock_movements m
        WHERE m.company_id=NEW.company_id AND m.id=NEW.first_receipt_movement_id
          AND m.item_id=NEW.item_id AND m.lot_id=NEW.lot_id
          AND m.quantity_delta=1 AND m.movement_kind IN ('receipt','production')
    ) THEN
        RAISE EXCEPTION 'Serial identity must originate from a posted stock receipt';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_serial_creation BEFORE INSERT ON erp.inventory_serials
    FOR EACH ROW EXECUTE FUNCTION erp.guard_serial_creation();

CREATE FUNCTION erp.sync_serial_movement() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE v_serial erp.inventory_serials%ROWTYPE; v_label text; v_sale_line uuid;
BEGIN
    IF NEW.lot_id IS NULL OR NOT EXISTS(SELECT 1 FROM erp.items
        WHERE company_id=NEW.company_id AND id=NEW.item_id AND track_serials) THEN
        RETURN NEW;
    END IF;
    IF NEW.quantity_delta NOT IN (-1,1) THEN
        RAISE EXCEPTION 'Serialized movement quantity must be exactly one';
    END IF;
    SELECT serial_code INTO v_label FROM erp.inventory_lots
        WHERE company_id=NEW.company_id AND item_id=NEW.item_id AND id=NEW.lot_id FOR SHARE;
    IF v_label IS NULL OR btrim(v_label)='' THEN
        RAISE EXCEPTION 'Serialized movement requires a registered lot label';
    END IF;
    SELECT * INTO v_serial FROM erp.inventory_serials
        WHERE company_id=NEW.company_id AND lot_id=NEW.lot_id FOR UPDATE;
    IF NOT FOUND THEN
        IF NEW.quantity_delta<>1 OR NEW.movement_kind NOT IN ('receipt','production') THEN
            RAISE EXCEPTION 'Serial must first enter through a receipt';
        END IF;
        INSERT INTO erp.inventory_serials(company_id,item_id,lot_id,serial_number,
            current_warehouse_id,lifecycle_status,first_receipt_movement_id)
        VALUES (NEW.company_id,NEW.item_id,NEW.lot_id,v_label,
            NEW.warehouse_id,'in_stock',NEW.id) RETURNING * INTO v_serial;
    ELSIF NEW.quantity_delta=-1 THEN
        IF v_serial.current_warehouse_id IS DISTINCT FROM NEW.warehouse_id
           OR v_serial.lifecycle_status<>'in_stock' THEN
            RAISE EXCEPTION 'Serial is not available in the source warehouse';
        END IF;
        IF NEW.source_type='sales_invoice' AND NOT EXISTS(
            SELECT 1 FROM erp.warehouses WHERE company_id=NEW.company_id
             AND id=NEW.warehouse_id AND stock_category='sellable') THEN
            RAISE EXCEPTION 'Serialized sale requires sellable warehouse';
        END IF;
        v_sale_line:=CASE WHEN NEW.source_type='sales_invoice'
            THEN NEW.source_line_id ELSE v_serial.last_sales_invoice_line_id END;
        UPDATE erp.inventory_serials SET current_warehouse_id=NULL,
            lifecycle_status=CASE WHEN NEW.source_type='sales_invoice' THEN 'with_customer'
                WHEN NEW.movement_kind='transfer' THEN 'in_transit'
                WHEN NEW.source_type='purchase_bill' THEN 'outside'
                ELSE 'disposed' END,
            last_sales_invoice_line_id=v_sale_line
        WHERE id=v_serial.id;
    ELSE
        IF v_serial.current_warehouse_id IS NOT NULL THEN
            RAISE EXCEPTION 'Serial already occupies a warehouse';
        END IF;
        IF v_serial.lifecycle_status='disposed' THEN
            RAISE EXCEPTION 'Disposed serial cannot re-enter inventory';
        END IF;
        IF v_serial.lifecycle_status='in_transit' AND NOT (
            NEW.movement_kind='transfer'
            AND EXISTS(SELECT 1 FROM erp.serial_movements prior
              WHERE prior.company_id=NEW.company_id AND prior.serial_id=v_serial.id
                AND prior.source_type=NEW.source_type AND prior.source_id=NEW.source_id
                AND prior.movement_kind='transfer' AND prior.quantity_delta=-1)) THEN
            RAISE EXCEPTION 'Transferred serial requires the paired receipt';
        END IF;
        IF v_serial.lifecycle_status='with_customer' AND NOT
            (NEW.source_type='sales_return' AND EXISTS(
                SELECT 1 FROM erp.sales_return_serials r
                WHERE r.company_id=NEW.company_id AND r.serial_id=v_serial.id
                AND r.sales_return_id=NEW.source_id
                AND r.sales_return_line_id=NEW.source_line_id
                AND r.status='inspected')) THEN
            RAISE EXCEPTION 'Sold serial requires a verified linked return before restock';
        END IF;
        UPDATE erp.inventory_serials SET current_warehouse_id=NEW.warehouse_id,
            lifecycle_status='in_stock' WHERE id=v_serial.id;
    END IF;
    INSERT INTO erp.serial_movements(company_id,serial_id,stock_movement_id,movement_kind,
        warehouse_id,quantity_delta,source_type,source_id,source_line_id,occurred_at,
        actor_user_id)
    VALUES (NEW.company_id,v_serial.id,NEW.id,NEW.movement_kind,NEW.warehouse_id,
        NEW.quantity_delta,NEW.source_type,NEW.source_id,NEW.source_line_id,NEW.occurred_at,
        identity.current_user_id());
    RETURN NEW;
END $$;
CREATE TRIGGER trg_serial_stock_movement AFTER INSERT ON erp.stock_movements
    FOR EACH ROW EXECUTE FUNCTION erp.sync_serial_movement();

ALTER TABLE erp.return_repair_jobs ADD COLUMN serial_id uuid,
    ADD CONSTRAINT fk_repair_serial FOREIGN KEY(company_id,serial_id)
        REFERENCES erp.inventory_serials(company_id,id);
CREATE INDEX ix_repair_serial ON erp.return_repair_jobs(company_id,serial_id,id)
    WHERE serial_id IS NOT NULL;
ALTER TABLE erp.sales_return_stock_actions ADD COLUMN serial_id uuid,
    ADD CONSTRAINT fk_return_action_serial FOREIGN KEY(company_id,serial_id)
        REFERENCES erp.inventory_serials(company_id,id);
CREATE INDEX ix_return_action_serial ON erp.sales_return_stock_actions(company_id,serial_id,id)
    WHERE serial_id IS NOT NULL;

CREATE FUNCTION erp.guard_serial_repair() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_serial erp.inventory_serials%ROWTYPE; v_line record; v_other uuid;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Serial repair history is permanent'; END IF;
    IF TG_OP='UPDATE' THEN
        IF NEW.serial_id IS DISTINCT FROM OLD.serial_id THEN
            RAISE EXCEPTION 'Repair serial identity is immutable';
        END IF;
        RETURN NEW;
    END IF;
    SELECT l.item_id,s.track_serials INTO v_line FROM erp.sales_return_lines r
      JOIN erp.sales_invoice_lines l ON l.company_id=r.company_id
        AND l.id=r.sales_invoice_line_id
      JOIN erp.items s ON s.company_id=l.company_id AND s.id=l.item_id
      WHERE r.company_id=NEW.company_id AND r.id=NEW.sales_return_line_id;
    IF v_line.track_serials THEN
        IF NEW.serial_id IS NULL OR NEW.quantity<>1 THEN
            RAISE EXCEPTION 'Serialized repair requires one exact serial';
        END IF;
        SELECT * INTO v_serial FROM erp.inventory_serials WHERE company_id=NEW.company_id
            AND id=NEW.serial_id FOR UPDATE;
        IF v_serial.item_id IS DISTINCT FROM v_line.item_id
           OR v_serial.current_warehouse_id IS DISTINCT FROM NEW.warehouse_id
           OR NOT EXISTS(SELECT 1 FROM erp.sales_return_serials r WHERE
              r.company_id=NEW.company_id AND r.sales_return_line_id=NEW.sales_return_line_id
              AND r.serial_id=NEW.serial_id AND r.status='posted') THEN
            RAISE EXCEPTION 'Repair serial must be retained from this posted return';
        END IF;
        SELECT id INTO v_other FROM erp.return_repair_jobs WHERE company_id=NEW.company_id
           AND serial_id=NEW.serial_id AND status NOT IN ('qc_passed','not_repairable') LIMIT 1;
        IF v_other IS NOT NULL THEN RAISE EXCEPTION 'Serial already has an open repair'; END IF;
    ELSIF NEW.serial_id IS NOT NULL THEN
        RAISE EXCEPTION 'Untracked repair cannot name a serial';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_serial_repair BEFORE INSERT OR UPDATE OR DELETE ON erp.return_repair_jobs
    FOR EACH ROW EXECUTE FUNCTION erp.guard_serial_repair();

CREATE OR REPLACE FUNCTION erp.guard_return_stock_action() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;r record;v_quantity numeric;v_value numeric;v_available numeric;
  v_date date;v_lot uuid;v_serial erp.inventory_serials%ROWTYPE;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Posted return stock actions are immutable'; END IF;
    SELECT * INTO l FROM erp.sales_return_lines WHERE company_id=NEW.company_id
        AND id=NEW.sales_return_line_id FOR UPDATE;
    SELECT * INTO r FROM erp.sales_returns WHERE company_id=NEW.company_id AND id=l.sales_return_id;
    IF r.status IS DISTINCT FROM 'posted' OR r.kind<>'return' OR l.disposition='write_off' THEN
        RAISE EXCEPTION 'Stock disposition requires an unconsumed posted physical return'; END IF;
    PERFORM 1 FROM erp.warehouses WHERE company_id=NEW.company_id AND id=NEW.from_warehouse_id
        AND is_active AND stock_category<>'sellable' FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Source must be an active non-sellable warehouse'; END IF;
    IF NEW.serial_id IS NOT NULL THEN
        SELECT * INTO v_serial FROM erp.inventory_serials WHERE company_id=NEW.company_id
            AND id=NEW.serial_id FOR UPDATE;
        IF v_serial.current_warehouse_id IS DISTINCT FROM NEW.from_warehouse_id
           OR NEW.quantity<>1 OR NOT EXISTS(SELECT 1 FROM erp.sales_return_serials sr
             WHERE sr.company_id=NEW.company_id AND sr.serial_id=NEW.serial_id
             AND sr.sales_return_line_id=NEW.sales_return_line_id AND sr.status='posted') THEN
            RAISE EXCEPTION 'Disposition serial is not retained from this return';
        END IF;
        v_lot:=v_serial.lot_id;
        SELECT value_delta_company INTO v_value FROM erp.stock_movements
          WHERE company_id=NEW.company_id AND source_type='sales_return'
            AND source_line_id=NEW.sales_return_line_id AND lot_id=v_lot AND quantity_delta=1;
        IF v_value IS NULL THEN RAISE EXCEPTION 'Return serial receipt cost missing'; END IF;
        SELECT greatest(r.return_date,coalesce(max(action_date),r.return_date)) INTO v_date
          FROM erp.sales_return_stock_actions WHERE company_id=NEW.company_id
            AND serial_id=NEW.serial_id;
        IF NEW.historical_cost<>v_value THEN
            RAISE EXCEPTION 'Serial historical cost must match return receipt';
        END IF;
    ELSE
        IF EXISTS(SELECT 1 FROM erp.sales_invoice_lines sl JOIN erp.items i
          ON i.company_id=sl.company_id AND i.id=sl.item_id
          WHERE sl.company_id=l.company_id AND sl.id=l.sales_invoice_line_id
          AND i.track_serials) THEN
            RAISE EXCEPTION 'Serialized disposition requires serial';
        END IF;
        v_quantity:=CASE WHEN l.warehouse_id=NEW.from_warehouse_id THEN l.quantity ELSE 0 END;
        v_value:=CASE WHEN l.warehouse_id=NEW.from_warehouse_id THEN l.historical_cost ELSE 0 END;
        SELECT v_quantity+coalesce(sum(CASE WHEN a.to_warehouse_id=NEW.from_warehouse_id
          THEN a.quantity WHEN a.from_warehouse_id=NEW.from_warehouse_id
          THEN -a.quantity ELSE 0 END),0),
          v_value+coalesce(sum(CASE WHEN a.to_warehouse_id=NEW.from_warehouse_id
          THEN a.historical_cost WHEN a.from_warehouse_id=NEW.from_warehouse_id
          THEN -a.historical_cost ELSE 0 END),0),
          greatest(r.return_date,coalesce(max(a.action_date),r.return_date))
          INTO v_quantity,v_value,v_date FROM erp.sales_return_stock_actions a
          WHERE a.company_id=NEW.company_id AND a.sales_return_line_id=l.id;
        IF NEW.quantity>v_quantity OR NEW.historical_cost>v_value
          OR (NEW.quantity=v_quantity AND NEW.historical_cost<>v_value) THEN
            RAISE EXCEPTION 'Return stock quantity or cost does not reconcile';
        END IF;
    END IF;
    IF NEW.action_date<v_date THEN RAISE EXCEPTION 'Disposition date precedes return'; END IF;
    SELECT on_hand_quantity-reserved_quantity INTO v_available FROM erp.inventory_positions
      WHERE company_id=NEW.company_id AND warehouse_id=NEW.from_warehouse_id
        AND item_id=(SELECT item_id FROM erp.sales_invoice_lines WHERE company_id=l.company_id
          AND id=l.sales_invoice_line_id) AND lot_id IS NOT DISTINCT FROM v_lot FOR UPDATE;
    IF v_available IS NULL OR NEW.quantity>v_available THEN
      RAISE EXCEPTION 'Return stock unavailable'; END IF;
    IF NEW.repair_job_id IS NOT NULL AND NEW.serial_id IS DISTINCT FROM
      (SELECT serial_id FROM erp.return_repair_jobs WHERE company_id=NEW.company_id
       AND id=NEW.repair_job_id) THEN
      RAISE EXCEPTION 'QC release serial does not match repair job';
    END IF;
    RETURN NEW;
END $$;

ALTER TABLE erp.sales_return_replacements
    ADD CONSTRAINT uq_replacement_company_id UNIQUE(company_id,id);
CREATE TABLE erp.replacement_serial_links (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    replacement_id uuid NOT NULL,
    source_return_serial_id uuid NOT NULL,
    replacement_serial_id uuid NOT NULL,
    replacement_invoice_line_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id),
    UNIQUE(company_id,source_return_serial_id),
    UNIQUE(company_id,replacement_serial_id,replacement_invoice_line_id),
    FOREIGN KEY(company_id,replacement_id) REFERENCES erp.sales_return_replacements(company_id,id),
    FOREIGN KEY(company_id,source_return_serial_id)
      REFERENCES erp.sales_return_serials(company_id,id),
    FOREIGN KEY(company_id,replacement_serial_id) REFERENCES erp.inventory_serials(company_id,id),
    FOREIGN KEY(company_id,replacement_invoice_line_id)
      REFERENCES erp.sales_invoice_lines(company_id,id),
    CHECK(source_return_serial_id<>replacement_serial_id)
);
CREATE INDEX ix_replacement_serial_page ON erp.replacement_serial_links
    (company_id,replacement_id,id);
ALTER TABLE erp.replacement_serial_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.replacement_serial_links FORCE ROW LEVEL SECURITY;
CREATE POLICY replacement_serial_tenant ON erp.replacement_serial_links
    USING(company_id IN (SELECT id FROM erp.companies
      WHERE tenant_id=erp.current_tenant_id()))
    WITH CHECK(company_id IN (SELECT id FROM erp.companies
      WHERE tenant_id=erp.current_tenant_id()));
CREATE FUNCTION erp.guard_replacement_serial_link() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_return uuid;v_invoice uuid;v_source_item uuid;v_new_item uuid;
  v_new_line uuid;v_new_status text;v_new_serial_item uuid;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Replacement serial chain is immutable'; END IF;
    SELECT rp.sales_return_id,rp.replacement_invoice_id INTO v_return,v_invoice
      FROM erp.sales_return_replacements rp
      WHERE rp.company_id=NEW.company_id AND rp.id=NEW.replacement_id;
    SELECT rl.item_id INTO v_source_item FROM erp.sales_return_serials rs
      JOIN erp.sales_return_lines sr ON sr.company_id=rs.company_id
        AND sr.id=rs.sales_return_line_id
      JOIN erp.sales_invoice_lines rl ON rl.company_id=sr.company_id
        AND rl.id=sr.sales_invoice_line_id
      WHERE rs.company_id=NEW.company_id AND rs.id=NEW.source_return_serial_id
        AND rs.sales_return_id=v_return AND rs.status='posted';
    SELECT l.item_id INTO v_new_item FROM erp.sales_invoice_lines l
      WHERE l.company_id=NEW.company_id AND l.id=NEW.replacement_invoice_line_id
        AND l.sales_invoice_id=v_invoice;
    SELECT last_sales_invoice_line_id,lifecycle_status,item_id
      INTO v_new_line,v_new_status,v_new_serial_item
      FROM erp.inventory_serials WHERE company_id=NEW.company_id
        AND id=NEW.replacement_serial_id FOR UPDATE;
    IF v_source_item IS NULL OR v_new_item IS NULL OR v_new_serial_item<>v_new_item
       OR v_new_line IS DISTINCT FROM NEW.replacement_invoice_line_id
       OR v_new_status<>'with_customer' THEN
       RAISE EXCEPTION 'Replacement serial must be shipped on the linked replacement invoice';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_replacement_serial_link BEFORE INSERT OR UPDATE OR DELETE
    ON erp.replacement_serial_links FOR EACH ROW
    EXECUTE FUNCTION erp.guard_replacement_serial_link();
"""

class Migration(migrations.Migration):
    dependencies = [("database", "0041_internal_jobs")]
    operations = [migrations.RunSQL(SQL)]
