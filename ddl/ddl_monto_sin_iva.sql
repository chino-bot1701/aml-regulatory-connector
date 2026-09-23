-- =====================================================================
-- Agrega MONTO_SIN_IVA (renta sin IVA) a PLD_AVISO_PAGOS.
-- ALTER ADD COLUMN: NO recrea la tabla -> conserva datos y GRANTS.
-- Correr con ROLE_DEV. Luego se recarga 04/05/06 para llenarla.
-- =====================================================================
USE ROLE ROLE_DEV;

ALTER TABLE DB_ANALYTICS.SCH_PLD.PLD_AVISO_PAGOS
    ADD COLUMN MONTO_SIN_IVA NUMBER(18,2);   -- renta SIN IVA (subtotal de las partidas de renta, prorrateado en parciales)
