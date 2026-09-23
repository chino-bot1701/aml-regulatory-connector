"""
inmoges_a_snowflake.py
====================
Lee INMOGES (todas las empresas, ventana de N días) y carga en la tabla
DB_ANALYTICS.SCH_CORE.PLD_AVISO_ARI todos los campos que se
necesitan de INMOGES para armar el JSON de el portal de avisos UIF (ARI).

Conexión Snowflake copiada de App.R (key-pair / JWT con el .pem).

Requisitos (una sola vez):
    pip install snowflake-connector-python cryptography pandas requests

Uso:
    py inmoges_a_snowflake.py                 # 49 empresas, últimos 7 días, SIMULA (no escribe)
    py inmoges_a_snowflake.py --enviar        # carga de verdad a Snowflake
    py inmoges_a_snowflake.py --empresas HL,HPI --dias 7 --enviar
    py inmoges_a_snowflake.py --crear-tabla --enviar   # crea la tabla si no existe y carga

NOTA: por ahora la "ventana de 7 días" se estampa (VENTANA_INICIO/FIN) y se toman
los contratos VIGENTES (cada uno = una operación del periodo) con el monto/fechas
del contrato. Cuando se conecte la cobranza real (pagos por rango), aquí se
sustituye el monto/fecha de pago por los pagos efectivos del periodo (marcado TODO).
"""
from __future__ import annotations

import os
import sys, os, time, argparse, datetime
import requests
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------------------- #
#  INMOGES (solo lectura)
# --------------------------------------------------------------------------- #
INMOGES_BASE = os.environ.get("ERP_API_BASE", "")
INMOGES_TOKEN = os.environ.get("ERP_API_TOKEN", "")
TIMEOUT = (60, 120)

# Mapeo estático de respaldo (solo se usa si el portal de avisos UIF no responde).
ID_SUJETO = {"HL": 2, "HPI": 14}

# --- el portal de avisos UIF: para mapear el idSujeto por RFC de forma dinámica ---
UIF_BASE = "https://api.uif.com.mx"
UIF_USER = os.environ.get("UIF_USER", "apiuif@almena.mx")
UIF_PASS = os.environ.get("UIF_PASS", "x3zytyMOvDIMLumiEX##")


def sujetos_por_rfc() -> dict:
    """Login a el portal de avisos UIF y devuelve {RFC: idSujeto} de la actividad ARI. {} si falla."""
    try:
        r = requests.post(UIF_BASE + "/api/usuarios/login",
                          json={"Username": UIF_USER, "Password": UIF_PASS}, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  (el portal de avisos UIF login {r.status_code}: idSujeto quedará por mapeo estático)")
            return {}
        tok = r.text.strip().strip('"')
        r2 = requests.get(UIF_BASE + "/api/suscripcion/sujetos",
                          headers={"Authorization": f"Bearer {tok}"},
                          params={"idActividad": "ARI"}, timeout=TIMEOUT)
        idx = {}
        for s in (r2.json() if r2.status_code == 200 else []):
            rfc = str(s.get("Rfc") or s.get("rfc") or "").strip().upper()
            sid = s.get("Id") or s.get("id") or s.get("IdSujeto")
            if rfc and sid is not None:
                idx[rfc] = sid
        return idx
    except Exception as ex:
        print(f"  (No se pudo consultar sujetos el portal de avisos UIF: {ex}; uso mapeo estático)")
        return {}


def _get(ep, params=None, reintentos=3):
    for i in range(1, reintentos + 1):
        try:
            r = requests.get(INMOGES_BASE + ep, headers={"Authorization-token": INMOGES_TOKEN},
                             params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            if i == reintentos:
                raise
        time.sleep(1.5 * i)
    return {}


def _data(ep, params=None):
    res = (_get(ep, params) or {}).get("result", {}) or {}
    return res.get("data") if res.get("process") else None


def _lista(d, *llaves):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in llaves + ("data",):
            if isinstance(d.get(k), list):
                return d[k]
        return [d]
    return []


def limpia(v):
    s = str(v or "").strip()
    return "" if s.lower() == "none" else s


def empresas_inmoges():
    return _lista(_data("/empresa"), "empresas")


def sucursales_index():
    """Trae TODAS las sucursales en bloque (paginado) → dict por alias de arrendatario."""
    idx, pagina = {}, 1
    while True:
        lote = _lista(_data("/sucursal_arrendatario", {"no_registros_x_pagina": 1000, "no_pagina": pagina}),
                      "sucursal", "sucursales")
        if not lote:
            break
        for s in lote:
            alias = limpia(s.get("alias_arrendatario"))
            if alias and alias not in idx:   # primera sucursal por cliente
                idx[alias] = s
        if len(lote) < 1000:
            break
        pagina += 1
    return idx


def tipo_persona_de(regimen: str) -> str:
    return "Moral" if "moral" in (regimen or "").lower() else "Fisica"


def contratos_empresa(alias_empresa):
    return _lista(_data("/contrato", {"empresa": alias_empresa}), "contratos")


_inm_cache = {}
def inmueble_de(nombre):
    nombre = limpia(nombre)
    if not nombre:
        return {}
    if nombre not in _inm_cache:
        items = _lista(_data("/inmueble", {"inmueble": nombre}), "inmuebles", "inmueble")
        _inm_cache[nombre] = items[0] if items else {}
    return _inm_cache[nombre]


def f_iso(v):
    v = limpia(v)
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def num2(v):
    try:
        return round(float(str(v).replace(",", "").strip()), 2)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
#  Extracción → filas
# --------------------------------------------------------------------------- #
def construir_filas(empresas_filtro, dias):
    hoy = datetime.date.today()
    ini = (hoy - datetime.timedelta(days=dias)).isoformat()
    fin = hoy.isoformat()

    print("Trayendo sucursales en bloque (clientes)…")
    sidx = sucursales_index()
    print(f"  sucursales/clientes en índice: {len(sidx)}")

    emps = empresas_inmoges()
    if empresas_filtro:
        fset = {e.strip().upper() for e in empresas_filtro}
        emps = [e for e in emps if limpia(e.get("alias")).upper() in fset]
    print(f"Empresas a procesar: {len(emps)}")

    print("Mapeando idSujeto por RFC desde el portal de avisos UIF…")
    suj_rfc = sujetos_por_rfc()
    print(f"  sujetos el portal de avisos UIF (ARI): {len(suj_rfc)}")

    filas = []
    for k, e in enumerate(emps, 1):
        alias_emp = limpia(e.get("alias"))
        id_emp = e.get("id_empresa")
        rfc_emp = limpia(e.get("rfc")).upper()
        id_sujeto = suj_rfc.get(rfc_emp) or ID_SUJETO.get(alias_emp.upper())
        cs = contratos_empresa(alias_emp)
        # Datos CRUDOS: TODOS los contratos MENOS los Cancelados. Antes se filtraba
        # ("Vigente","Vencido"), lo que dejaba fuera los "Por comenzar" (contratos
        # NUEVOS que aún no arrancan) y cualquier "Renovación". Blacklist de solo
        # "Cancelado" = no se queda ni un cliente fuera (los vencidos siguen
        # facturando/cobrando; los nuevos ya deben aparecer para captura).
        vig = [c for c in cs if str(c.get("estatus")).strip().lower() != "cancelado"]
        print(f"  [{k}/{len(emps)}] {alias_emp}: {len(cs)} contratos, {len(vig)} activos (no cancelados)")
        for c in vig:
            alias_cli = limpia(c.get("arrendatario"))
            suc = sidx.get(alias_cli, {})
            inm = inmueble_de(c.get("inmueble"))
            regimen = limpia(suc.get("regimen_fiscal"))
            filas.append({
                "VENTANA_INICIO": ini, "VENTANA_FIN": fin,
                "EMPRESA": alias_emp, "ID_EMPRESA": id_emp,
                "ID_SUJETO": id_sujeto,
                "REFERENCIA_AVISO": f"ARI{alias_emp}{limpia(c.get('id_contrato'))}",
                # cliente
                "TIPO_PERSONA": tipo_persona_de(regimen),
                "DENOMINACION_RAZON": limpia(suc.get("razon_social")) or alias_cli,
                "RFC": limpia(suc.get("rfc")),
                "PAIS_NACIONALIDAD": "MX",
                "GIRO_MERCANTIL_TXT": limpia(c.get("giro")),
                "IDENTIFICADOR_UNICO": limpia(suc.get("id_externo_sucursal")).lstrip("´'` ").strip() or limpia(suc.get("rfc")),
                "CLI_COLONIA": limpia(suc.get("colonia")), "CLI_CALLE": limpia(suc.get("calle")),
                "CLI_NO_EXTERIOR": limpia(suc.get("no_exterior")), "CLI_NO_INTERIOR": limpia(suc.get("no_interior")),
                "CLI_CODIGO_POSTAL": limpia(suc.get("codigo_postal")),
                "TEL_CLAVE_PAIS": "MX", "TEL_NUMERO": None,        # MANUAL
                "CORREO": limpia(suc.get("correos_cfdi")).split(";")[0].upper() or None,
                # representante (MANUAL → NULL)
                "REP_NOMBRE": None, "REP_APELLIDO_PAT": None, "REP_APELLIDO_MAT": None,
                "REP_FECHA_NAC": None, "REP_RFC": None, "REP_CURP": None,
                # inmueble
                "INMUEBLE": limpia(c.get("inmueble")), "ID_INMUEBLE": limpia(c.get("id_inmueble")),
                "INM_TIPO_TXT": limpia(inm.get("tipo_inmueble")),
                "INM_VALOR_REFERENCIA": None, "INM_FOLIO_REAL": None,   # MANUAL
                "INM_COLONIA": limpia(inm.get("colonia")), "INM_CALLE": limpia(inm.get("calle")),
                "INM_NO_EXTERIOR": limpia(inm.get("no_exterior")), "INM_NO_INTERIOR": limpia(inm.get("no_interior")),
                "INM_CODIGO_POSTAL": limpia(inm.get("codigo_postal")),
                "FECHA_INICIO": f_iso(c.get("fecha_inicial")), "FECHA_TERMINO": f_iso(c.get("fecha_final")),
                # operación / liquidación  (TODO: sustituir por cobranza real del periodo)
                "FECHA_OPERACION": fin, "FECHA_PAGO": None,
                "FORMA_PAGO_TXT": limpia(c.get("forma_pago")), "INSTRUMENTO_TXT": None,
                "MONEDA_TXT": limpia(c.get("moneda")), "MONTO_OPERACION": num2(c.get("total")),
                # control
                "ID_CONTRATO": limpia(c.get("id_contrato")), "ESTATUS_CONTRATO": limpia(c.get("estatus")),
                "ORIGEN": "INMOGES", "COMPLETO_PARA_ENVIO": False,
            })
    return pd.DataFrame(filas)


# --------------------------------------------------------------------------- #
#  Snowflake (key-pair / JWT, copiado de App.R)
# --------------------------------------------------------------------------- #
SF = dict(account=os.environ.get("SNOWFLAKE_ACCOUNT", ""), user="SVC_ANALYTICS",
          warehouse="WH_ANALYTICS", database="DB_ANALYTICS",
          schema="SCH_PLD", role="ROLE_DEV")
PEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SVC_ANALYTICS_key_pk8.pem")
TABLA = "PLD_AVISO"


def conexion_snowflake():
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization
    with open(PEM, "rb") as f:
        pkey = serialization.load_pem_private_key(f.read(), password=None)
    pkb = pkey.private_bytes(encoding=serialization.Encoding.DER,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())
    return snowflake.connector.connect(private_key=pkb, **SF)


DDL = """
CREATE TABLE IF NOT EXISTS DB_ANALYTICS.SCH_PLD.PLD_AVISO (
    LOAD_TS              TIMESTAMP_NTZ DEFAULT CONVERT_TIMEZONE('America/Mexico_City', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,
    VENTANA_INICIO       DATE,
    VENTANA_FIN          DATE,
    EMPRESA              VARCHAR,
    ID_EMPRESA           NUMBER,
    ID_SUJETO            NUMBER,
    REFERENCIA_AVISO     VARCHAR,
    TIPO_PERSONA         VARCHAR,
    DENOMINACION_RAZON   VARCHAR,
    RFC                  VARCHAR,
    PAIS_NACIONALIDAD    VARCHAR,
    GIRO_MERCANTIL_TXT   VARCHAR,
    IDENTIFICADOR_UNICO  VARCHAR,
    CLI_COLONIA          VARCHAR,
    CLI_CALLE            VARCHAR,
    CLI_NO_EXTERIOR      VARCHAR,
    CLI_NO_INTERIOR      VARCHAR,
    CLI_CODIGO_POSTAL    VARCHAR,
    TEL_CLAVE_PAIS       VARCHAR,
    TEL_NUMERO           VARCHAR,
    CORREO               VARCHAR,
    REP_NOMBRE           VARCHAR,
    REP_APELLIDO_PAT     VARCHAR,
    REP_APELLIDO_MAT     VARCHAR,
    REP_FECHA_NAC        VARCHAR,
    REP_RFC              VARCHAR,
    REP_CURP             VARCHAR,
    INMUEBLE             VARCHAR,
    ID_INMUEBLE          VARCHAR,
    INM_TIPO_TXT         VARCHAR,
    INM_VALOR_REFERENCIA VARCHAR,
    INM_FOLIO_REAL       VARCHAR,
    INM_COLONIA          VARCHAR,
    INM_CALLE            VARCHAR,
    INM_NO_EXTERIOR      VARCHAR,
    INM_NO_INTERIOR      VARCHAR,
    INM_CODIGO_POSTAL    VARCHAR,
    FECHA_INICIO         DATE,
    FECHA_TERMINO        DATE,
    FECHA_OPERACION      DATE,
    FECHA_PAGO           DATE,
    FORMA_PAGO_TXT       VARCHAR,
    INSTRUMENTO_TXT      VARCHAR,
    MONEDA_TXT           VARCHAR,
    MONTO_OPERACION      NUMBER(18,2),
    ID_CONTRATO          VARCHAR,
    ESTATUS_CONTRATO     VARCHAR,
    ORIGEN               VARCHAR DEFAULT 'INMOGES',
    COMPLETO_PARA_ENVIO  BOOLEAN DEFAULT FALSE
)
"""


def cargar_snowflake(df: pd.DataFrame, crear_tabla=False, reemplazar=False):
    from snowflake.connector.pandas_tools import write_pandas
    conn = conexion_snowflake()
    try:
        cur = conn.cursor()
        cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
        if crear_tabla and DDL:
            cur.execute(DDL)
            print("  Tabla verificada/creada.")
        if reemplazar:
            cur.execute(f"TRUNCATE TABLE IF EXISTS {SF['database']}.{SF['schema']}.{TABLA}")
            print("  Tabla vaciada (TRUNCATE) antes de cargar.")
        ok, nchunks, nrows, _ = write_pandas(conn, df, TABLA,
                                              database=SF["database"], schema=SF["schema"],
                                              quote_identifiers=False)
        print(f"  write_pandas -> ok={ok}  filas={nrows}")
        return nrows
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--empresas", default="", help="lista separada por comas (default: todas)")
    ap.add_argument("--dias", type=int, default=7)
    ap.add_argument("--enviar", action="store_true", help="escribe en Snowflake (sin esto solo simula)")
    ap.add_argument("--crear-tabla", action="store_true", help="crea la tabla si no existe")
    ap.add_argument("--reemplazar", action="store_true", help="vacía la tabla (TRUNCATE) antes de cargar")
    a = ap.parse_args()

    empresas = [x for x in a.empresas.split(",") if x.strip()] if a.empresas else None
    print("=" * 64)
    print(f"  INMOGES → Snowflake | ventana {a.dias} días | {'ENVÍO REAL' if a.enviar else 'SIMULACIÓN'}")
    print("=" * 64)
    t0 = time.perf_counter()
    df = construir_filas(empresas, a.dias)
    print(f"\nFilas construidas: {len(df)}  (en {time.perf_counter()-t0:.1f}s)")
    if df.empty:
        print("Sin filas. Fin."); return 0
    print(df.head(3).to_string())

    if not a.enviar:
        print("\n[SIMULACIÓN] No se escribió en Snowflake. Repite con --enviar para cargar.")
        return 0
    print("\nCargando a Snowflake…")
    cargar_snowflake(df, crear_tabla=a.crear_tabla, reemplazar=a.reemplazar)
    print("Listo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
