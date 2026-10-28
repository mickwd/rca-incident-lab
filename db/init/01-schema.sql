-- Esquema del servicio de órdenes
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE products (
    id      SERIAL PRIMARY KEY,
    sku     VARCHAR(32)    NOT NULL UNIQUE,
    name    VARCHAR(120)   NOT NULL,
    price   NUMERIC(10, 2) NOT NULL,
    stock   INTEGER        NOT NULL
);

CREATE TABLE orders (
    id          BIGSERIAL PRIMARY KEY,
    customer_id INTEGER        NOT NULL,
    product_id  INTEGER        NOT NULL REFERENCES products (id),
    quantity    INTEGER        NOT NULL,
    total       NUMERIC(12, 2) NOT NULL,
    status      VARCHAR(16)    NOT NULL DEFAULT 'pending',
    reference   VARCHAR(128),
    created_at  TIMESTAMPTZ    NOT NULL DEFAULT now()
);
