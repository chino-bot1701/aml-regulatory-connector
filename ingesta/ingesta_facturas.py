"""
ingesta_facturas.py — Carga las FACTURAS (CFDI timbradas) de renta a
DB_ANALYTICS.SCH_PLD.PLD_AVISO_CFDI.

Rediseño del pipeline: se ENTRA POR LA FACTURA (CFDI timbrada), no por el pago.
  • 1 fila por CFDI (factura). Reemplaza funcionalmente a PLD_AVISO_PAGOS.
  • La renta = "Renta" + "Renta Variable" (filtro a nivel PARTIDA: concepto
    empieza con 'renta') -> ya NO se pierde la Renta Variable.
  • Las partidas y las parcialidades van embebidas como JSON (texto).
  • Solo se conservan las facturas con renta (RENTA_CON_IVA > 0).

Flujo por empresa:
  1. /cfdi  (tipo Factura, Timbrada) por rango de EMISIÓN -> encabezados.
  2. /partida por id_cfdi -> desglose de conceptos (Renta / Renta Variable / …).
  3. /pago  con VENTANA ANCHA (emisión + 60 días) -> parcialidades enganchadas
     por id_cfdi (atrapa pagos tardíos: p.ej. Renta Variable timbrada al mes siguiente).

Uso:
    py ingesta_facturas.py --periodo 05-2026 --empresas HPI --contrato 3823                 # SIMULA
    py ingesta_facturas.py --periodo 05-2026 --empresas HPI --contrato 3823 --enviar --crear-tabla
    py ingesta_facturas.py --periodo 05-2026 --enviar                                        # todas las empresas
"""
from __future__ import annotations

import os
import os, sys, argparse, calendar, datetime, time, json
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_here = os.path.dirname(os.path.abspath(__file__))
_RESP = os.path.abspath(os.path.join(_here, "..", "respaldos"))   # respaldos centralizados
try:
    os.makedirs(_RESP, exist_ok=True)
except Exception:
    _RESP = _here

# Cliente HTTP compartido (../Cobranza/inmoges_api.py)
for _cand in (os.path.join(_here, "..", "Cobranza"), os.path.join(_here, "..", "..", "Cobranza")):
    if os.path.isdir(_cand):
        sys.path.insert(0, os.path.abspath(_cand))
        break
import inmoges_api as api  # noqa: E402

# --------------------------------------------------------------------------- #
#  Conexión Snowflake (SCH_PLD) — idéntico a ingesta_pagos.py
# --------------------------------------------------------------------------- #
SF = dict(account=os.environ.get("SNOWFLAKE_ACCOUNT", ""), user="SVC_ANALYTICS",
          warehouse="WH_ANALYTICS", database="DB_ANALYTICS",
          schema="SCH_PLD", role="ROLE_ANALYTICS")
ESQUEMA = "DB_ANALYTICS.SCH_PLD"
TABLA = "PLD_AVISO_CFDI"
PEM = next((p for p in (os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", ""),
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
    LOAD_TS           TIMESTAMP_NTZ DEFAULT CONVERT_TIMEZONE('America/Mexico_City', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
    PERIODO VARCHAR, EMPRESA VARCHAR,
    ID_CFDI VARCHAR, UUID VARCHAR, FOLIO VARCHAR, SERIE VARCHAR, FECHA DATE, TITULO VARCHAR,
    ESTATUS_DOCUMENTO VARCHAR, ESTATUS_TIMBRE VARCHAR, FECHA_TIMBRE TIMESTAMP_NTZ, TIPO VARCHAR, MONEDA VARCHAR,
    SUBTOTAL NUMBER(18,2), TOTAL NUMBER(18,2), SALDO NUMBER(18,2),
    CONTRATO_INMOGES VARCHAR, UNIDAD VARCHAR, INMUEBLE_TXT VARCHAR, RFC_RECEPTOR VARCHAR, RAZON_SOCIAL VARCHAR,
    PDF_URL VARCHAR,
    RENTA_SIN_IVA NUMBER(18,2), RENTA_CON_IVA NUMBER(18,2), MONTO_FINAL NUMBER(18,2),
    RENTA_CONCEPTOS VARCHAR, NUM_PARCIALIDADES NUMBER,
    PARTIDAS_JSON VARCHAR, PARCIALIDADES_JSON VARCHAR
)
"""

# Orden de columnas del DataFrame (MAYÚSCULAS; LOAD_TS lo pone el DEFAULT).
COLS = ["PERIODO", "EMPRESA", "ID_CFDI", "UUID", "FOLIO", "SERIE", "FECHA", "TITULO",
        "ESTATUS_DOCUMENTO", "ESTATUS_TIMBRE", "FECHA_TIMBRE", "TIPO", "MONEDA",
        "SUBTOTAL", "TOTAL", "SALDO", "CONTRATO_INMOGES", "UNIDAD", "INMUEBLE_TXT",
        "RFC_RECEPTOR", "RAZON_SOCIAL", "PDF_URL", "RENTA_SIN_IVA", "RENTA_CON_IVA",
        "MONTO_FINAL", "RENTA_CONCEPTOS", "NUM_PARCIALIDADES", "PARTIDAS_JSON", "PARCIALIDADES_JSON"]


# --------------------------------------------------------------------------- #
#  Utilidades
# --------------------------------------------------------------------------- #
def _txt(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.lower() == "none" else s


def _es_renta(concepto: str) -> bool:
    return _txt(concepto).lower().startswith("renta")   # incluye 'Renta' y 'Renta Variable'


_RE_INMUEBLE = None
def _inmueble_de(txt: str) -> str:
    import re
    global _RE_INMUEBLE
    if _RE_INMUEBLE is None:
        _RE_INMUEBLE = re.compile(r"inmueble\s+(.+?)(?:\s+Cuenta\s+predial|\s*$)", re.IGNORECASE)
    m = _RE_INMUEBLE.search(str(txt or ""))
    return m.group(1).strip() if m else ""


# --------------------------------------------------------------------------- #
#  Extracción: CFDI + partidas + parcialidades -> 1 fila por CFDI
# --------------------------------------------------------------------------- #
def construir_cfdi(empresa: str, fi: str, ff: str, periodo: str,
                   contrato: str = "", pago_fi: str = "", pago_ff: str = "",
                   ventana_dias: int = 1) -> pd.DataFrame:
    base = {"empresa": empresa}

    # 1) Encabezados de facturas TIMBRADAS por fecha de emisión.
    # Día-por-día (ventana_dias=1): más estable que bloques grandes, que paginan
    # profundo y se parten (probado: bloques de 7d resultaron MÁS lentos).
    print(f"→ /cfdi Timbradas de {empresa} {fi}..{ff}")
    cfdis = api.consultar_rango("cfdi", {**base, "tipo_documento": "Factura",
                                         "incluir_partidas": "false", "estatus_timbre": "Timbrada"},
                                fi, ff, "cfdi", ventana_dias=ventana_dias)
    if contrato:
        cfdis = [c for c in cfdis if _txt(c.get("contrato")) == str(contrato)]
    print(f"  → {len(cfdis)} CFDI" + (f" del contrato {contrato}" if contrato else ""))
    if not cfdis:
        return pd.DataFrame(columns=COLS)

    ids = [_txt(c.get("id_cfdi")) for c in cfdis if _txt(c.get("id_cfdi"))]

    # 2) Partidas por id_cfdi.
    print(f"→ /partida de {len(ids)} facturas")
    partidas = api.consultar_por_ids(empresa, ids)
    mapa_part: dict[str, list[dict]] = {}
    for p in partidas:
        idc = _txt(p.get("id_cfdi"))
        if idc:
            mapa_part.setdefault(idc, []).append({
                "concepto": _txt(p.get("concepto")),
                "descripcion": _txt(p.get("descripcion")),
                "subtotal": round(api.num(p.get("subtotal")), 2),
                "iva": round(api.num(p.get("iva")), 2),
                "total": round(api.num(p.get("total")), 2)})

    # 3) Parcialidades: /pago con VENTANA ANCHA (atrapa pagos tardíos), en bloques de varios días.
    print(f"→ /pago {pago_fi}..{pago_ff} (ventana ancha para pagos tardíos)")
    reps = api.consultar_rango("pago", base, pago_fi, pago_ff, "pago", ventana_dias=ventana_dias)
    idset = set(ids)
    mapa_parc: dict[str, list[dict]] = {}
    for rep in reps:
        for pago in api.as_list(rep.get("pago")):
            fpago = f"{_txt(pago.get('fecha'))} {_txt(pago.get('hora'))}".strip()
            for dr in api.as_list(pago.get("documento_relacionado_pago")):
                idc = _txt(dr.get("id_cfdi_pagando")) or _txt(dr.get("id_cfdi"))
                if idc in idset:
                    mapa_parc.setdefault(idc, []).append({
                        "num_parcialidad": _txt(dr.get("numero_parcialidad")),
                        "fecha_pago": fpago,
                        "saldo_anterior": round(api.num(dr.get("saldo_anterior")), 2),
                        "importe_pagado": round(api.num(dr.get("importe_pagado")), 2),
                        "saldo_pendiente": round(api.num(dr.get("importe_insoluto")), 2),
                        "metodo_pago": _txt(dr.get("metodo_pago"))})

    # 4) Ensamble: 1 fila por CFDI (solo las que tienen renta).
    filas = []
    for c in cfdis:
        idc = _txt(c.get("id_cfdi"))
        lineas = mapa_part.get(idc, [])
        renta = [l for l in lineas if _es_renta(l["concepto"])]
        renta_con = round(sum(l["total"] for l in renta), 2)
        if renta_con <= 0:
            continue   # sin renta -> no es aviso (mantenimiento/servicio suelto)
        renta_sin = round(sum(l["subtotal"] for l in renta), 2)
        conceptos = " | ".join(sorted(set(l["concepto"] for l in renta)))
        parc = sorted(mapa_parc.get(idc, []), key=lambda x: (x["num_parcialidad"] or ""))
        inmu = _txt(c.get("inmueble")) or next((_inmueble_de(l["descripcion"]) for l in renta if _inmueble_de(l["descripcion"])), "")

        filas.append({
            "PERIODO": periodo, "EMPRESA": empresa,
            "ID_CFDI": idc, "UUID": _txt(c.get("folio_fiscal")), "FOLIO": _txt(c.get("folio")),
            "SERIE": _txt(c.get("serie")), "FECHA": _txt(c.get("fecha"))[:10] or None,
            "TITULO": _txt(c.get("titulo")),
            "ESTATUS_DOCUMENTO": _txt(c.get("estatus_documento")),
            "ESTATUS_TIMBRE": _txt(c.get("estatus_timbre")) or "Timbrada",
            "FECHA_TIMBRE": _txt(c.get("fecha_timbre")) or None,
            "TIPO": _txt(c.get("tipo_documento")) or "Factura",
            "MONEDA": _txt(c.get("moneda")) or "MXN",
            "SUBTOTAL": round(api.num(c.get("subtotal")), 2),
            "TOTAL": round(api.num(c.get("total")), 2),
            "SALDO": round(api.num(c.get("saldo")), 2),
            "CONTRATO_INMOGES": _txt(c.get("contrato")),
            "UNIDAD": _txt(c.get("unidad")),
            "INMUEBLE_TXT": inmu,
            "RFC_RECEPTOR": _txt(c.get("rfc_receptor")).upper(),
            "RAZON_SOCIAL": _txt(c.get("arrendatario")),
            "PDF_URL": _txt(c.get("pdf")),
            "RENTA_SIN_IVA": renta_sin, "RENTA_CON_IVA": renta_con,
            "MONTO_FINAL": round(api.num(c.get("total")), 2),
            "RENTA_CONCEPTOS": conceptos, "NUM_PARCIALIDADES": len(parc),
            "PARTIDAS_JSON": json.dumps(lineas, ensure_ascii=False),
            "PARCIALIDADES_JSON": json.dumps(parc, ensure_ascii=False)})
    return pd.DataFrame(filas, columns=COLS)


def _rango(periodo):
    mm, yyyy = periodo.split("-"); y, m = int(yyyy), int(mm)
    return datetime.date(y, m, 1), datetime.date(y, m, calendar.monthrange(y, m)[1])


def construir(empresas, periodo, contrato="", dias_pago_extra=60):
    d0, d1 = _rango(periodo)
    fi, ff = d0.isoformat(), d1.isoformat()
    # Ventana de /pago: emisión + N días, sin pasar de hoy (atrapa pagos tardíos).
    pago_ff = min(d1 + datetime.timedelta(days=dias_pago_extra), datetime.date.today())
    if pago_ff < d1:
        pago_ff = d1
    print(f"Periodo {periodo} -> emisión {fi}..{ff} | pagos {fi}..{pago_ff.isoformat()}")
    bloques = []
    for k, emp in enumerate(empresas, 1):
        print(f"\n[{k}/{len(empresas)}] {emp} …")
        t = time.perf_counter()
        try:
            df = construir_cfdi(emp, fi, ff, periodo, contrato=contrato,
                                pago_fi=fi, pago_ff=pago_ff.isoformat())
        except Exception as ex:
            print(f"   ! {emp}: error ({ex}); se omite"); continue
        if df.empty:
            print(f"   {emp}: 0 facturas de renta ({time.perf_counter()-t:.0f}s)"); continue
        bloques.append(df)
        print(f"   {emp}: {len(df)} facturas de renta ({time.perf_counter()-t:.0f}s)")
    return pd.concat(bloques, ignore_index=True) if bloques else pd.DataFrame(columns=COLS)


def cargar(df, periodo, crear_tabla=False):
    from snowflake.connector.pandas_tools import write_pandas
    try:
        bak = os.path.join(_RESP, f"_respaldo_cfdi_{periodo}.csv")
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
    ap.add_argument("--contrato", default="", help="filtra un solo contrato (prueba, ej. 3823)")
    ap.add_argument("--enviar", action="store_true")
    ap.add_argument("--crear-tabla", action="store_true")
    a = ap.parse_args()

    if a.empresas:
        empresas = [x.strip() for x in a.empresas.split(",") if x.strip()]
    else:
        empresas = sorted({str(e.get("alias", "")).strip()
                           for e in api.extraer_lista(api.get_pagina("empresa", {"no_registros_x_pagina": 1000, "no_pagina": 1}))
                           if str(e.get("alias", "")).strip()})
    print("=" * 64)
    print(f"  FACTURAS(CFDI) -> Snowflake (SCH_PLD) | periodo {a.periodo} | "
          f"{'ENVÍO REAL' if a.enviar else 'SIMULACIÓN'}" + (f" | contrato {a.contrato}" if a.contrato else ""))
    print("=" * 64)

    if not a.enviar:
        # SIMULACIÓN: arma todo en memoria y muestra (no escribe).
        df = construir(empresas, a.periodo, contrato=a.contrato)
        print(f"\nFacturas de renta: {len(df)}")
        if df.empty:
            print("Sin facturas de renta. Fin."); return 0
        print(df[["FOLIO", "TITULO", "RENTA_CONCEPTOS", "RENTA_SIN_IVA", "RENTA_CON_IVA",
                  "MONTO_FINAL", "SALDO", "NUM_PARCIALIDADES"]].head(12).to_string(index=False))
        print("\n[SIMULACIÓN] No se escribió. Repite con --enviar."); return 0

    # ENVÍO REAL — CARGA INCREMENTAL POR EMPRESA: cada empresa se escribe a
    # Snowflake apenas termina (DELETE WHERE PERIODO+EMPRESA + insert). Ventajas:
    # progreso visible, a prueba de fallos (si truena una empresa, las demás ya
    # están), y menos memoria. La tabla y las filas son IDÉNTICAS al modo batch.
    from snowflake.connector.pandas_tools import write_pandas
    d0, d1 = _rango(a.periodo)
    fi, ff = d0.isoformat(), d1.isoformat()
    pago_ff = min(d1 + datetime.timedelta(days=60), datetime.date.today())
    if pago_ff < d1:
        pago_ff = d1
    print(f"Periodo {a.periodo} -> emisión {fi}..{ff} | pagos {fi}..{pago_ff.isoformat()}")
    conn = conexion_snowflake()
    total = 0
    try:
        cur = conn.cursor(); cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
        if a.crear_tabla:
            cur.execute(DDL); print("  Tabla verificada/creada.")
        for k, emp in enumerate(empresas, 1):
            print(f"\n[{k}/{len(empresas)}] {emp} …")
            t = time.perf_counter()
            try:
                df = construir_cfdi(emp, fi, ff, a.periodo, contrato=a.contrato,
                                    pago_fi=fi, pago_ff=pago_ff.isoformat())
            except Exception as ex:
                print(f"   ! {emp}: error ({ex}); se omite (las demás no se pierden)"); continue
            # Respaldo CSV por empresa ANTES de tocar Snowflake.
            try:
                df.to_csv(os.path.join(_RESP, f"_respaldo_cfdi_{a.periodo}_{emp}.csv"),
                          index=False, encoding="utf-8-sig")
            except Exception:
                pass
            # Idempotente por (periodo, empresa): borra lo previo de ESA empresa y reinserta.
            cur.execute(f"DELETE FROM {ESQUEMA}.{TABLA} WHERE PERIODO=%s AND EMPRESA=%s", (a.periodo, emp))
            if df.empty:
                print(f"   {emp}: 0 facturas de renta ({time.perf_counter()-t:.0f}s)"); continue
            ok, _, nrows, _ = write_pandas(conn, df, TABLA, database=SF["database"],
                                           schema=SF["schema"], quote_identifiers=False)
            total += nrows
            print(f"   {emp}: {nrows} facturas cargadas ({time.perf_counter()-t:.0f}s)  [acumulado: {total}]")
        print(f"\nListo. Total de facturas cargadas en {a.periodo}: {total}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
