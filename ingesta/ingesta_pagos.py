"""
ingesta_pagos.py — Carga las LIQUIDACIONES REALES (pagos de renta del periodo) a
DB_ANALYTICS.SCH_PLD.PLD_AVISO_PAGOS.

Trae la cobranza real de INMOGES (complementos de pago/REP, vía ../Cobranza/Cobranza.py)
y la filtra con la regla validada contra el aviso SAT:
  • Un PAGO va al aviso solo si su factura tiene partida de "Renta" (incl. "Renta Variable").
  • El inmueble sale del texto de la partida ("...ubicado en el inmueble X...").
  • El monto = el pago completo (importe_pagado); la fecha = la del pago real.

Uso:
    py ingesta_pagos.py --periodo 05-2026                       # SIMULA (no escribe)
    py ingesta_pagos.py --periodo 05-2026 --empresas HPI --enviar --crear-tabla
    py ingesta_pagos.py --periodo 05-2026 --enviar              # todas las empresas
    py ingesta_pagos.py --periodo 05-2026 --empresas HPI --csv <cobranza.csv> --enviar
"""
from __future__ import annotations

import os
import os, sys, argparse, calendar, datetime, time, re
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_here = os.path.dirname(os.path.abspath(__file__))
_RESP = os.path.abspath(os.path.join(_here, "..", "respaldos"))   # respaldos centralizados
try:
    os.makedirs(_RESP, exist_ok=True)
except Exception:
    _RESP = _here

# Cliente de cobranza (Cobranza puede estar en ../Cobranza o ../../Cobranza
# según si este archivo vive en ProyectoALMENA/ o en ProyectoALMENA/temporal/).
for _cand in (os.path.join(_here, "..", "Cobranza"), os.path.join(_here, "..", "..", "Cobranza")):
    if os.path.isdir(_cand):
        sys.path.insert(0, os.path.abspath(_cand))
        break

# --------------------------------------------------------------------------- #
#  Conexión Snowflake (SCH_PLD)
# --------------------------------------------------------------------------- #
SF = dict(account=os.environ.get("SNOWFLAKE_ACCOUNT", ""), user="SVC_ANALYTICS",
          warehouse="WH_ANALYTICS", database="DB_ANALYTICS",
          schema="SCH_PLD", role="ROLE_ANALYTICS")
ESQUEMA = "DB_ANALYTICS.SCH_PLD"
TABLA = "PLD_AVISO_PAGOS"
PEM = next((p for p in (os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", ""),  # copia LOCAL (Drive falla a veces)
                        os.path.join(_here, "SVC_ANALYTICS_key_pk8.pem"),
                        os.path.join(_here, "..", "SVC_ANALYTICS_key_pk8.pem"))
            if os.path.isfile(p)), os.path.join(_here, "SVC_ANALYTICS_key_pk8.pem"))


def conexion_snowflake():
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization
    with open(PEM, "rb") as f:
        pkey = serialization.load_pem_private_key(f.read(), password=None)
    pkb = pkey.private_bytes(encoding=serialization.Encoding.DER,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())
    return snowflake.connector.connect(private_key=pkb, **SF)


DDL = f"""
CREATE TABLE IF NOT EXISTS {ESQUEMA}.{TABLA} (
    LOAD_TS        TIMESTAMP_NTZ DEFAULT CONVERT_TIMEZONE('America/Mexico_City', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
    PERIODO VARCHAR, EMPRESA VARCHAR, RFC_RECEPTOR VARCHAR, RAZON_SOCIAL VARCHAR,
    INMUEBLE_TXT VARCHAR, FOLIO_PAGO VARCHAR, FECHA_PAGO DATE, MONTO NUMBER(18,2),
    MONTO_SIN_IVA NUMBER(18,2),
    FORMA_PAGO_TXT VARCHAR, INSTRUMENTO_TXT VARCHAR, MONEDA_TXT VARCHAR, CONCEPTOS VARCHAR,
    METODO_PAGO VARCHAR, NUM_PARCIALIDAD VARCHAR, SALDO_ANTERIOR NUMBER(18,2),
    IMPORTE_PAGADO NUMBER(18,2), SALDO_PENDIENTE NUMBER(18,2),
    FACTURA_URL VARCHAR, FACTURA_UUID VARCHAR,
    MONTO_FINAL NUMBER(18,2), CONTRATO_INMOGES VARCHAR, UNIDAD VARCHAR, LOCAL_COMERCIAL VARCHAR
)
"""

# --------------------------------------------------------------------------- #
#  Cerebro: cobranza -> liquidaciones (regla "solo Renta")
# --------------------------------------------------------------------------- #
PALABRAS_RENTA = ("renta",)
_RE_INMUEBLE = re.compile(r"inmueble\s+(.+?)(?:\s+Cuenta\s+predial|\s*$)", re.IGNORECASE)
_RE_LOCAL = re.compile(r"identificado como\s+(.+?)\s+ubicado", re.IGNORECASE)


def _inmueble_de(txt: str) -> str:
    m = _RE_INMUEBLE.search(str(txt or ""))
    return m.group(1).strip() if m else ""


def _local_de(txt: str) -> str:
    """Nombre comercial del local, del texto de la partida ('identificado como BERSHKA ubicado…')."""
    m = _RE_LOCAL.search(str(txt or ""))
    return m.group(1).strip() if m else ""


def _txt(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.lower() == "none" else s


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _first(g, col, default=""):
    """Primer valor de una columna del grupo (o default si la columna no existe)."""
    return g[col].iloc[0] if col in g.columns else default


def a_liquidaciones(df_cobranza: pd.DataFrame) -> pd.DataFrame:
    """Una fila por FACTURA de renta liquidada — DATOS CRUDOS de la factura.

    MONTO / MONTO_SIN_IVA = la renta TAL CUAL la factura (suma de partidas de
    renta, con y sin IVA), SIN prorrateo: cuadran siempre contra el PDF.
    Lo realmente abonado y el saldo viven en IMPORTE_PAGADO / SALDO_PENDIENTE.
    MONTO_FINAL = total de la factura (todos los conceptos, con IVA).
    CONTRATO_INMOGES / UNIDAD / LOCAL_COMERCIAL = contrato y local reales de la
    factura (de /cfdi y del texto de la partida).
    """
    cols = ["RFC_RECEPTOR", "RAZON_SOCIAL", "INMUEBLE_TXT", "FECHA_PAGO", "MONTO", "MONTO_SIN_IVA",
            "MONTO_FINAL", "FOLIO_PAGO", "CONCEPTOS", "MONEDA_TXT", "METODO_PAGO", "NUM_PARCIALIDAD",
            "SALDO_ANTERIOR", "IMPORTE_PAGADO", "SALDO_PENDIENTE", "FACTURA_URL", "FACTURA_UUID",
            "CONTRATO_INMOGES", "UNIDAD", "LOCAL_COMERCIAL"]
    if df_cobranza is None or df_cobranza.empty:
        return pd.DataFrame(columns=cols)
    df = df_cobranza.copy()
    df["importe_pagado"] = pd.to_numeric(df["importe_pagado"], errors="coerce").fillna(0.0)
    if "total_partida" not in df.columns:
        raise RuntimeError("La cobranza no trae 'total_partida'. Actualiza Cobranza.py "
                           "(debe exponer el total por partida).")
    df["total_partida"] = pd.to_numeric(df["total_partida"], errors="coerce").fillna(0.0)
    if "subtotal_partida" not in df.columns:
        df["subtotal_partida"] = 0.0   # cobranza vieja (CSV) sin subtotales -> quedará 0/None
    df["subtotal_partida"] = pd.to_numeric(df["subtotal_partida"], errors="coerce").fillna(0.0)
    filas = []
    # Agrupar por (pago, factura): un mismo pago puede cubrir >1 factura. Así cada
    # liquidación = UNA factura y el monto/prorrateo queda consistente (total_factura,
    # importe_pagado y suma_renta del mismo documento).
    for (folio, _idfac), g in df.groupby(["folio_int", "id_factura_pagada"]):
        es_renta = (g["concepto"].fillna("").str.strip().str.lower() == "renta")
        if not es_renta.any():
            continue   # puro mantenimiento/servicio -> no va al aviso
        inmu = next((_inmueble_de(c) for c in g["partida"] if _inmueble_de(c)), "")
        # moneda REAL del pago (INMOGES la trae en rel_moneda: MXN / USD)
        mon = str(_first(g, "rel_moneda", "") or "").strip().upper()

        # --- RENTA CRUDA de la factura (sin prorrateo: cuadra con el PDF) ---
        monto = round(float(g.loc[es_renta, "total_partida"].sum()), 2)          # renta CON IVA
        monto_sin = round(float(g.loc[es_renta, "subtotal_partida"].sum()), 2)   # renta SIN IVA
        total_fac = round(_num(_first(g, "fac_total_factura", 0.0)), 2)          # total factura (todo con IVA)
        importe = _num(g["importe_pagado"].iloc[0])                               # lo realmente abonado
        local = next((_local_de(c) for c in g["partida"] if _local_de(c)), "")
        conceptos_renta = " | ".join(sorted(set(str(c) for c in g.loc[es_renta, "concepto"].dropna())))

        filas.append({"RFC_RECEPTOR": str(g["rfc_receptor"].iloc[0] or "").strip().upper(),
                      "RAZON_SOCIAL": str(g["razon_social"].iloc[0] or "").strip(),
                      "INMUEBLE_TXT": inmu, "FECHA_PAGO": str(g["fecha_completa_pago"].iloc[0])[:10],
                      "MONTO": monto, "MONTO_SIN_IVA": monto_sin, "MONTO_FINAL": total_fac,
                      "FOLIO_PAGO": str(folio),
                      "CONCEPTOS": conceptos_renta, "MONEDA_TXT": mon or "MXN",
                      # --- Parcialidad (para columna "Pago" y pestaña "Parcialidades" en la app) ---
                      "METODO_PAGO": str(_first(g, "rel_metodo_pago", "") or "").strip(),
                      "NUM_PARCIALIDAD": str(_first(g, "rel_num_parcialidad", "") or "").strip(),
                      "SALDO_ANTERIOR": round(_num(_first(g, "rel_saldo_anterior", 0.0)), 2),
                      "IMPORTE_PAGADO": round(importe, 2),
                      "SALDO_PENDIENTE": round(_num(_first(g, "rel_saldo_pendiente", 0.0)), 2),
                      "FACTURA_URL": str(_first(g, "factura_url", "") or ""),
                      "FACTURA_UUID": str(_first(g, "rel_uuid", "") or ""),
                      # --- Contrato y local REALES de la factura (datos crudos INMOGES) ---
                      "CONTRATO_INMOGES": _txt(_first(g, "factura_contrato", "")),
                      "UNIDAD": _txt(_first(g, "factura_unidad", "")),
                      "LOCAL_COMERCIAL": local})
    return pd.DataFrame(filas)


def obtener_cobranza(empresa, fi, ff) -> pd.DataFrame:
    import Cobranza   # cliente robusto del proyecto ../Cobranza
    return Cobranza.construir_cobranza(empresa, fi, ff, 0)


def _enriquecer(liq, empresa, periodo):
    liq = liq.copy()
    liq["PERIODO"] = periodo; liq["EMPRESA"] = empresa
    liq["FORMA_PAGO_TXT"] = "Contado"; liq["INSTRUMENTO_TXT"] = "Transferencia interbancaria"
    # MONEDA_TXT ya viene REAL desde a_liquidaciones (rel_moneda: MXN/USD) — no se pisa.
    return liq[["PERIODO", "EMPRESA", "RFC_RECEPTOR", "RAZON_SOCIAL", "INMUEBLE_TXT", "FOLIO_PAGO",
                "FECHA_PAGO", "MONTO", "MONTO_SIN_IVA", "MONTO_FINAL", "FORMA_PAGO_TXT", "INSTRUMENTO_TXT",
                "MONEDA_TXT", "CONCEPTOS", "METODO_PAGO", "NUM_PARCIALIDAD", "SALDO_ANTERIOR",
                "IMPORTE_PAGADO", "SALDO_PENDIENTE", "FACTURA_URL", "FACTURA_UUID",
                "CONTRATO_INMOGES", "UNIDAD", "LOCAL_COMERCIAL"]]


def _rango(periodo):
    mm, yyyy = periodo.split("-"); y, m = int(yyyy), int(mm)
    return datetime.date(y, m, 1).isoformat(), datetime.date(y, m, calendar.monthrange(y, m)[1]).isoformat()


def construir(empresas, periodo, csv=""):
    if csv:
        liq = a_liquidaciones(pd.read_csv(csv, dtype=str))
        return _enriquecer(liq, empresas[0] if empresas else "", periodo) if not liq.empty else pd.DataFrame()
    fi, ff = _rango(periodo)
    print(f"Periodo {periodo} -> {fi} a {ff}")
    bloques = []
    for k, emp in enumerate(empresas, 1):
        print(f"[{k}/{len(empresas)}] Cobranza de {emp} …")
        t = time.perf_counter()
        try:
            liq = a_liquidaciones(obtener_cobranza(emp, fi, ff))
        except Exception as ex:
            print(f"   ! {emp}: error ({ex}); se omite"); continue
        if liq.empty:
            print(f"   {emp}: 0 pagos de renta ({time.perf_counter()-t:.0f}s)"); continue
        bloques.append(_enriquecer(liq, emp, periodo))
        print(f"   {emp}: {len(liq)} liquidaciones ({time.perf_counter()-t:.0f}s)")
    return pd.concat(bloques, ignore_index=True) if bloques else pd.DataFrame()


def cargar(df, periodo, crear_tabla=False):
    from snowflake.connector.pandas_tools import write_pandas
    # Respaldo CSV ANTES de tocar Snowflake: si la carga fallara, no se pierde lo extraído.
    try:
        bak = os.path.join(_RESP, f"_respaldo_pagos_{periodo}.csv")
        df.to_csv(bak, index=False, encoding="utf-8-sig")
        print(f"  Respaldo CSV: {bak}")
    except Exception as ex:
        print(f"  (no se pudo escribir respaldo CSV: {ex})")
    conn = conexion_snowflake()
    try:
        cur = conn.cursor(); cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
        if crear_tabla:
            cur.execute(DDL); print("  Tabla verificada/creada.")
        cur.execute(f"DELETE FROM {ESQUEMA}.{TABLA} WHERE PERIODO = %s", (periodo,))
        print(f"  Borradas filas previas del periodo {periodo}: {cur.rowcount}")
        ok, _, nrows, _ = write_pandas(conn, df, TABLA, database=SF["database"], schema=SF["schema"], quote_identifiers=False)
        print(f"  write_pandas -> ok={ok}  filas={nrows}")
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--periodo", required=True, help="MM-AAAA, ej. 05-2026")
    ap.add_argument("--empresas", default="", help="lista separada por comas (default: todas)")
    ap.add_argument("--enviar", action="store_true")
    ap.add_argument("--crear-tabla", action="store_true")
    ap.add_argument("--csv", default="", help="atajo: arma desde un CSV de Cobranza.py (1 sola --empresas)")
    a = ap.parse_args()

    if a.empresas:
        empresas = [x.strip() for x in a.empresas.split(",") if x.strip()]
    elif a.csv:
        empresas = []
    else:
        import Cobranza, inmoges_api  # noqa
        # todas las empresas de INMOGES (alias)
        empresas = sorted({str(e.get("alias", "")).strip()
                           for e in Cobranza.api.extraer_lista(Cobranza.api.get_pagina("empresa", {"no_registros_x_pagina": 1000, "no_pagina": 1}))
                           if str(e.get("alias", "")).strip()})
    print("=" * 64)
    print(f"  COBRANZA -> Snowflake (SCH_PLD) | periodo {a.periodo} | {'ENVÍO REAL' if a.enviar else 'SIMULACIÓN'}")
    print("=" * 64)
    df = construir(empresas, a.periodo, csv=a.csv)
    print(f"\nLiquidaciones: {len(df)}")
    if df.empty:
        print("Sin pagos de renta. Fin."); return 0
    print(df.head(8).to_string())
    if not a.enviar:
        print("\n[SIMULACIÓN] No se escribió. Repite con --enviar."); return 0
    print("\nCargando a Snowflake…")
    cargar(df, a.periodo, crear_tabla=a.crear_tabla)
    print("Listo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
