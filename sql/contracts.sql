-- =============================================================
-- Captain of Industry — таблица контрактов с деревнями
-- =============================================================

CREATE TABLE contracts (
    id                  SERIAL        PRIMARY KEY,
    village             SMALLINT      NOT NULL CHECK (village > 0),
    required_reputation SMALLINT      CHECK (required_reputation >= 0),

    -- Что вы отдаёте кораблю
    export_item         VARCHAR(200),
    export_item_id      INTEGER       REFERENCES items (id) ON DELETE SET NULL,
    export_qty          INTEGER       CHECK (export_qty > 0),

    -- Что получаете от корабля
    import_item         VARCHAR(200),
    import_item_id      INTEGER       REFERENCES items (id) ON DELETE SET NULL,
    import_qty          INTEGER       CHECK (import_qty > 0),

    -- Unity-очки
    unity_per_month     NUMERIC(8, 2),
    unity_per_ship      NUMERIC(8, 2),
    unity_at_establish  NUMERIC(8, 2),

    CONSTRAINT uq_contract UNIQUE (village, export_item, import_item)
);

COMMENT ON TABLE  contracts                    IS 'Контракты с деревнями (торговые соглашения)';
COMMENT ON COLUMN contracts.village            IS 'Номер деревни';
COMMENT ON COLUMN contracts.required_reputation IS 'Необходимая репутация для активации';
COMMENT ON COLUMN contracts.export_item        IS 'Предмет, который вы отправляете кораблю';
COMMENT ON COLUMN contracts.export_item_id     IS 'FK на items (может быть NULL если предмета нет в таблице)';
COMMENT ON COLUMN contracts.export_qty         IS 'Количество отправляемого предмета за рейс';
COMMENT ON COLUMN contracts.import_item        IS 'Предмет, который привозит корабль';
COMMENT ON COLUMN contracts.import_item_id     IS 'FK на items';
COMMENT ON COLUMN contracts.import_qty         IS 'Количество получаемого предмета за рейс';
COMMENT ON COLUMN contracts.unity_per_month    IS 'Unity в месяц от контракта';
COMMENT ON COLUMN contracts.unity_per_ship     IS 'Unity за один рейс корабля';
COMMENT ON COLUMN contracts.unity_at_establish IS 'Unity при установке контракта';

CREATE INDEX idx_contracts_village        ON contracts (village);
CREATE INDEX idx_contracts_export_item_id ON contracts (export_item_id);
CREATE INDEX idx_contracts_import_item_id ON contracts (import_item_id);


-- =============================================================
-- INSERT — вставить содержимое contracts.json между $json$...$json$
-- =============================================================

DO $do$
DECLARE
    data json := $json$

[{"village": 1, "ВСТАВИТЬ СЮДА СОДЕРЖИМОЕ contracts.json": null}]

    $json$;
    c      json;
BEGIN
    FOR c IN SELECT * FROM json_array_elements(data) LOOP
        INSERT INTO contracts (
            village,
            required_reputation,
            export_item,
            export_item_id,
            export_qty,
            import_item,
            import_item_id,
            import_qty,
            unity_per_month,
            unity_per_ship,
            unity_at_establish
        )
        VALUES (
            (c->>'village')::smallint,
            (c->>'required_reputation')::smallint,
            c->>'export_item',
            (SELECT id FROM items WHERE name = c->>'export_item'),
            (c->>'export_qty')::integer,
            c->>'import_item',
            (SELECT id FROM items WHERE name = c->>'import_item'),
            (c->>'import_qty')::integer,
            (c->>'unity_per_month')::numeric,
            (c->>'unity_per_ship')::numeric,
            (c->>'unity_at_establish')::numeric
        )
        ON CONFLICT (village, export_item, import_item) DO NOTHING;
    END LOOP;
END;
$do$;
