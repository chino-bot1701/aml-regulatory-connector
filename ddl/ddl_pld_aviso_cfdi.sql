-- =====================================================================
-- Tabla NUEVA: PLD_AVISO_CFDI  (correr con ROLE_DEV)
-- =====================================================================
-- Rediseño del pipeline: se entra por la FACTURA (CFDI timbrada), NO por
-- el pago. 1 fila por CFDI. Reemplaza funcionalmente a PLD_AVISO_PAGOS.
--
-- La renta ahora incluye "Renta" Y "Renta Variable" (filtro a nivel PARTIDA:
-- concepto LIKE 'renta%'). Las partidas y parcialidades van embebidas como
-- JSON (texto) porque write_pandas no carga VARIANT sin stage.
--
-- GRANTS a los DOS roles consumidores: ROLE_ANALYTICS (ingesta/local) y
-- ROLE_DASHBOARD (dashboard desplegado).
-- =====================================================================
USE ROLE ROLE_DEV;

CREATE TABLE IF NOT EXISTS DB_ANALYTICS.SCH_PLD.PLD_AVISO_CFDI (
    LOAD_TS            TIMESTAMP_NTZ(9) DEFAULT CAST(CONVERT_TIMEZONE('America/Mexico_City', CAST(CURRENT_TIMESTAMP() AS TIMESTAMP_TZ(9))) AS TIMESTAMP_NTZ(9)),
    PERIODO           VARCHAR,          -- 1MM-AAAA (derivado de FECHA de emisión; llave de recarga idempotente)
    EMPRESA           VARCHAR,
    -- ---------- Cabecera /cfdi ----------
    ID_CFDI           VARCHAR,          -- folio interno INMOGES (llave natural del CFDI)
    UUID              VARCHAR,          -- folio_fiscal
    FOLIO             VARCHAR,          -- ej. 'A-71426'  (se muestra en el grid)
    SERIE             VARCHAR,
    FECHA             DATE,             -- fecha de EMISIÓN de la factura
    TITULO            VARCHAR,          -- ej. 'Renta Variable Mayo 2026'
    ESTATUS_DOCUMENTO VARCHAR,          -- Pagado / Cuenta por cobrar / Cancelado
    ESTATUS_TIMBRE    VARCHAR,          -- siempre 'Timbrada' en esta carga
    FECHA_TIMBRE      TIMESTAMP_NTZ(9), -- fecha de timbrado
    TIPO              VARCHAR,          -- tipo_documento (Factura)
    MONEDA            VARCHAR,          -- 'MXN' / 'USD'
    SUBTOTAL          NUMBER(18,2),     -- header (todos los conceptos, sin IVA)
    TOTAL             NUMBER(18,2),     -- header (todos los conceptos, con IVA)
    SALDO             NUMBER(18,2),     -- 0 => pagada
    CONTRATO_INMOGES    VARCHAR,          -- contrato REAL de la factura
    UNIDAD            VARCHAR,          -- unidad/local (ej. PLF-B-14/B-15)
    INMUEBLE_TXT      VARCHAR,
    RFC_RECEPTOR      VARCHAR,
    RAZON_SOCIAL      VARCHAR,          -- 'arrendatario'
    PDF_URL           VARCHAR,          -- link S3 del PDF
    -- ---------- Derivados de renta (Renta + Renta Variable) ----------
    RENTA_SIN_IVA     NUMBER(18,2),     -- Σ subtotal de partidas concepto LIKE 'renta%'
    RENTA_CON_IVA     NUMBER(18,2),     -- Σ total de esas partidas (monto del aviso)
    MONTO_FINAL       NUMBER(18,2),     -- = TOTAL del CFDI (todos los conceptos)
    RENTA_CONCEPTOS   VARCHAR,          -- 'Renta' | 'Renta Variable' (auditoría)
    NUM_PARCIALIDADES NUMBER,           -- len(PARCIALIDADES_JSON)
    -- ---------- Detalle embebido (JSON como texto) ----------
    PARTIDAS_JSON     VARCHAR,          -- [{concepto,descripcion,subtotal,iva,total}, ...]  (TODAS las partidas)
    PARCIALIDADES_JSON VARCHAR          -- [{num_parcialidad,fecha_pago,saldo_anterior,importe_pagado,saldo_pendiente,metodo_pago}, ...]
);

-- GRANTS (SIEMPRE a los dos roles consumidores)
GRANT SELECT, INSERT, DELETE ON TABLE DB_ANALYTICS.SCH_PLD.PLD_AVISO_CFDI TO ROLE ROLE_ANALYTICS;
GRANT SELECT ON TABLE DB_ANALYTICS.SCH_PLD.PLD_AVISO_CFDI TO ROLE ROLE_DASHBOARD;
