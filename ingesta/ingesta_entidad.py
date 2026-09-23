"""
ingesta_entidad.py — Carga los datos PLD del arrendatario (pld_entidad) a
DB_ANALYTICS.SCH_PLD.PLD_AVISO_ENTIDAD.

El equipo PLD captura en INMOGES (módulo Arrendatarios) la "entidad PLD" de cada
cliente: representante legal (rpm_*), datos de persona física (pf_*), giro,
fecha de constitución, actividad económica, domicilio, teléfono y correo.
La app usa esta tabla para PRE-LLENAR el formulario (la captura manual gana).

Extracción CRUDA de /arrendatario (todas las empresas), una fila por
(RFC, id_pld_entidad). Carga full-refresh: DELETE total + insert (tabla chica).

Uso:
    py ingesta_entidad.py                # SIMULA (no escribe)
    py ingesta_entidad.py --enviar      # escribe en Snowflake
"""
from __future__ import annotations

import os
import os, sys, argparse, time
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_here = os.path.dirname(os.path.abspath(__file__))
_RESP = os.path.abspath(os.path.join(_here, "..", "respaldos"))   # respaldos centralizados
try:
    os.makedirs(_RESP, exist_ok=True)
except Exception:
    _RESP = _here

# Cliente robusto de la API (vive en ../Cobranza)
sys.path.insert(0, os.path.abspath(os.path.join(_here, "..", "Cobranza")))
import inmoges_api as api  # noqa: E402

# --------------------------------------------------------------------------- #
#  Snowflake (mismo patrón que ingesta_pagos; PEM local primero)
# --------------------------------------------------------------------------- #
SF = dict(account=os.environ.get("SNOWFLAKE_ACCOUNT", ""), user="SVC_ANALYTICS",
          warehouse="WH_ANALYTICS", database="DB_ANALYTICS",
          schema="SCH_PLD", role="ROLE_ANALYTICS")
ESQUEMA = "DB_ANALYTICS.SCH_PLD"
TABLA = "PLD_AVISO_ENTIDAD"
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


def _txt(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.lower() == "none" else s


def _lista_arrendatarios(res: dict) -> list:
    """La respuesta de /arrendatario puede venir bajo distintas llaves; se toma
    la primera lista encontrada en result.data."""
    d = (res.get("result") or {}).get("data") or {}
    if isinstance(d, list):
        return d
    for k in ("arrendatarios", "arrendatario", "data"):
        if isinstance(d.get(k), list):
            return d[k]
    for v in d.values():
        if isinstance(v, list):
            return v
    return []


def arrendatarios_empresa(alias: str) -> list:
    out, pagina = [], 1
    while pagina <= 50:
        r = api.get_pagina("arrendatario", {"empresa": alias,
                                            "no_registros_x_pagina": 100, "no_pagina": pagina})
        lote = _lista_arrendatarios(r)
        if not lote:
            break
        out.extend(lote)
        if len(lote) < 100:
            break
        pagina += 1
        time.sleep(0.1)
    return out


def empresas_inmoges() -> list:
    r = api.get_pagina("empresa", {"no_registros_x_pagina": 1000, "no_pagina": 1})
    d = (r.get("result") or {}).get("data") or {}
    lst = d.get("empresas") if isinstance(d, dict) else d
    return lst or []


def construir() -> pd.DataFrame:
    emps = sorted({_txt(e.get("alias")) for e in empresas_inmoges() if _txt(e.get("alias"))})
    print(f"Empresas: {len(emps)}")
    filas = []
    for k, emp in enumerate(emps, 1):
        try:
            arrs = arrendatarios_empresa(emp)
        except Exception as ex:
            print(f"  ! {emp}: error ({ex}); se omite"); continue
        n_ent = 0
        for a in arrs:
            for ent in (a.get("pld_entidad") or []):
                n_ent += 1
                filas.append({
                    "EMPRESA": emp,
                    "ID_ARRENDATARIO": _txt(a.get("id_arrendatario")),
                    "RFC": _txt(a.get("rfc")).upper(),
                    "RAZON_SOCIAL": _txt(a.get("razon_social")),
                    "ALIAS": _txt(a.get("alias")),
                    "ID_PLD_ENTIDAD": _txt(ent.get("id_pld_entidad")),
                    "TIPO_PERSONA": _txt(ent.get("tipo_persona")),
                    "PF_NOMBRE": _txt(ent.get("pf_nombre")),
                    "PF_APELLIDO_PAT": _txt(ent.get("pf_apellido_paterno")),
                    "PF_APELLIDO_MAT": _txt(ent.get("pf_apellido_materno")),
                    "PF_FECHA_NAC": _txt(ent.get("pf_fecha_nacimiento")),
                    "PF_CURP": _txt(ent.get("pf_curp")),
                    "ACTIVIDAD_ECONOMICA": _txt(ent.get("actividad_economica")),
                    "FECHA_CONSTITUCION": _txt(ent.get("fecha_constitucion")),
                    "GIRO_MERCANTIL": _txt(ent.get("giro_mercantil")),
                    "RPM_NOMBRE": _txt(ent.get("rpm_nombre")),
                    "RPM_APELLIDO_PAT": _txt(ent.get("rpm_apellido_paterno")),
                    "RPM_APELLIDO_MAT": _txt(ent.get("rpm_apellido_materno")),
                    "RPM_FECHA_NAC": _txt(ent.get("rpm_fecha_nacimiento")),
                    "RPM_RFC": _txt(ent.get("rpm_rfc")).upper(),
                    "RPM_CURP": _txt(ent.get("rpm_curp")).upper(),
                    "CALLE": _txt(ent.get("calle")),
                    "COLONIA": _txt(ent.get("colonia")),
                    "NO_EXTERIOR": _txt(ent.get("no_exterior")),
                    "NO_INTERIOR": _txt(ent.get("no_interior")),
                    "PAIS": _txt(ent.get("pais")),
                    "TELEFONO": _txt(ent.get("telefono")),
                    "CORREO": _txt(ent.get("correo")).upper(),
                })
        print(f"  [{k}/{len(emps)}] {emp}: {len(arrs)} arrendatarios, {n_ent} entidades PLD")
    df = pd.DataFrame(filas)
    if not df.empty:
        # Un mismo arrendatario aparece en varias empresas con la misma entidad.
        df = df.drop_duplicates(subset=["RFC", "ID_PLD_ENTIDAD"]).reset_index(drop=True)
    return df


def cargar(df: pd.DataFrame):
    from snowflake.connector.pandas_tools import write_pandas
    bak = os.path.join(_RESP, "_respaldo_entidad.csv")
    try:
        df.to_csv(bak, index=False, encoding="utf-8-sig")
        print(f"  Respaldo CSV: {bak}")
    except Exception as ex:
        print(f"  (sin respaldo CSV: {ex})")
    conn = conexion_snowflake()
    try:
        cur = conn.cursor(); cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
        cur.execute(f"DELETE FROM {ESQUEMA}.{TABLA}")   # full refresh (tabla chica, todo re-extraíble)
        print(f"  Borradas filas previas: {cur.rowcount}")
        ok, _, nrows, _ = write_pandas(conn, df, TABLA, database=SF["database"],
                                       schema=SF["schema"], quote_identifiers=False)
        print(f"  write_pandas -> ok={ok}  filas={nrows}")
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enviar", action="store_true")
    a = ap.parse_args()
    print("=" * 64)
    print(f"  ENTIDAD PLD (pld_entidad) -> Snowflake | {'ENVÍO REAL' if a.enviar else 'SIMULACIÓN'}")
    print("=" * 64)
    df = construir()
    print(f"\nEntidades PLD: {len(df)}")
    if df.empty:
        print("Sin entidades. Fin."); return 0
    print(df[["EMPRESA", "RFC", "RAZON_SOCIAL", "RPM_NOMBRE", "TELEFONO", "CORREO"]].head(8).to_string())
    if not a.enviar:
        print("\n[SIMULACIÓN] No se escribió. Repite con --enviar."); return 0
    print("\nCargando a Snowflake…")
    cargar(df)
    print("Listo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
