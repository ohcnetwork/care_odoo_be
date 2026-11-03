-- Metabase parameters: facility_id (number), selected_date (date).
-- Hospital-owned quantity = available quantity + incoming internal transfers.
-- Join frozen Odoo costs by item_uuid for month-end valuation.
WITH parameters AS (
    SELECT CAST({{facility_id}} AS bigint) AS facility_id,
        (CAST({{selected_date}} AS date) + INTERVAL '1 day')
            AT TIME ZONE 'Asia/Kolkata' AS cutoff
), supplies AS (
    SELECT sd.status, sd.supplied_item_quantity AS quantity,
        d.origin_id, d.destination_id,
        COALESCE(ii.location_id, d.origin_id) AS source_id,
        COALESCE(ii.product_id, sd.supplied_item_id) AS product_id,
        src.inventory_usage AS source_usage,
        dst.inventory_usage AS destination_usage
    FROM emr_supplydelivery sd
    JOIN emr_deliveryorder d ON d.id = sd.order_id
    LEFT JOIN emr_inventoryitem ii ON ii.id = sd.supplied_inventory_item_id
    LEFT JOIN emr_facilitylocation src
        ON src.id = COALESCE(ii.location_id, d.origin_id)
    JOIN emr_facilitylocation dst ON dst.id = d.destination_id
    CROSS JOIN parameters p
    WHERE sd.deleted = FALSE AND dst.facility_id = p.facility_id
        AND sd.created_date < p.cutoff
), entries AS (
    SELECT product_id, destination_id AS location_id, quantity,
        CASE WHEN status = 'in_progress' THEN quantity ELSE 0 END AS incoming
    FROM supplies
    WHERE destination_usage IN ('store', 'none')
        AND (status = 'completed' OR (
            status = 'in_progress' AND origin_id IS NOT NULL
            AND source_usage IN ('store', 'none')
        ))
    UNION ALL
    SELECT product_id, source_id, -quantity, 0
    FROM supplies
    WHERE status IN ('in_progress', 'completed') AND origin_id IS NOT NULL
        AND source_usage IN ('store', 'none')
    UNION ALL
    SELECT ii.product_id, ii.location_id, -md.quantity, 0
    FROM emr_medicationdispense md
    JOIN emr_inventoryitem ii ON ii.id = md.item_id
    JOIN emr_facilitylocation l ON l.id = ii.location_id
    CROSS JOIN parameters p
    WHERE md.deleted = FALSE
        AND md.status NOT IN ('cancelled', 'entered_in_error', 'stopped', 'declined')
        AND md.created_date < p.cutoff AND l.facility_id = p.facility_id
        AND l.inventory_usage IN ('store', 'none')
)
SELECT pk.external_id AS item_uuid, pk.name AS item,
    l.external_id AS location_uuid, l.name AS location,
    SUM(e.quantity) - SUM(e.incoming) AS available_quantity,
    SUM(e.incoming) AS incoming_quantity,
    SUM(e.quantity) AS owned_quantity
FROM entries e
JOIN emr_product product ON product.id = e.product_id
JOIN emr_productknowledge pk ON pk.id = product.product_knowledge_id
JOIN emr_facilitylocation l ON l.id = e.location_id
GROUP BY pk.external_id, pk.name, l.external_id, l.name
HAVING SUM(e.quantity) <> 0 OR SUM(e.incoming) <> 0
ORDER BY pk.name, l.name;
