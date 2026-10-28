-- Datos de ejemplo. orders tiene 1.000.000 de filas para que un Seq Scan
-- se note (escenario slow-query). Tarda unos segundos en el primer arranque.
INSERT INTO products (sku, name, price, stock)
SELECT 'SKU-' || lpad(g::text, 4, '0'),
       'Producto ' || g,
       round((random() * 900 + 100)::numeric, 2),
       1000000000
FROM generate_series(1, 200) AS g;

INSERT INTO orders (customer_id, product_id, quantity, total, status, reference, created_at)
SELECT (random() * 49999)::int + 1,
       (g % 200) + 1,
       q,
       q * 150.00,
       'processed',
       'PO-' || g,
       now() - (random() * interval '365 days')
FROM generate_series(1, 1000000) AS g,
     LATERAL (SELECT (g % 3) + 1 AS q) AS x;

-- Índices después de la carga masiva (más rápido que mantenerlos durante el INSERT)
CREATE INDEX ix_orders_customer_id ON orders (customer_id);
CREATE INDEX ix_orders_pending ON orders (id) WHERE status = 'pending';

ANALYZE products;
ANALYZE orders;
