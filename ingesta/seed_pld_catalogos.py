"""
seed_pld_catalogos.py — Carga los catálogos SAT/PLD (País, Giro, Actividad,
Tipo de inmueble, Forma de pago, etc.) desde las plantillas oficiales de el portal de avisos UIF
(z1) a DB_ANALYTICS.SCH_CORE.PLD_CATALOGO. Son estáticos (seed único);
NO salen de la API de INMOGES. Formato del aviso = VALOR ('clave-descripción').

Uso:  py seed_pld_catalogos.py [--enviar]
"""
import os, sys, argparse, re
import pandas as pd

_PAT = re.compile(r"^\s*(\d+)\s*-\s*(.+)$", re.DOTALL)   # 'clave-descripción'

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_here = os.path.dirname(os.path.abspath(__file__))
_RESP = os.path.abspath(os.path.join(_here, "..", "respaldos"))   # respaldos centralizados
try:
    os.makedirs(_RESP, exist_ok=True)
except Exception:
    _RESP = _here
XLSX = r"r:\Mi unidad\Transformacion_GFG\ALMENA\APPS\AlmenaIntelligence_1-Principal\Documentacion\API INMOGES\el portal de avisos UIF\ArrendamientoPrevenent.app\z1.-Plantilla Arrendamiento.xlsx"

SF = dict(account=os.environ.get("SNOWFLAKE_ACCOUNT", ""), user="SVC_ANALYTICS",
          warehouse="WH_ANALYTICS", database="DB_ANALYTICS",
          schema="SCH_CORE", role="ROLE_ANALYTICS")
ESQUEMA = "DB_ANALYTICS.SCH_CORE"
TABLA = "PLD_CATALOGO"
PEM = next((p for p in (os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", ""),
                        os.path.join(_here, "SVC_ANALYTICS_key_pk8.pem"),
                        os.path.join(_here, "..", "SVC_ANALYTICS_key_pk8.pem"))
            if os.path.isfile(p)), os.path.join(_here, "SVC_ANALYTICS_key_pk8.pem"))


def conexion():
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization
    with open(PEM, "rb") as f:
        pkey = serialization.load_pem_private_key(f.read(), password=None)
    pkb = pkey.private_bytes(encoding=serialization.Encoding.DER,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())
    return snowflake.connector.connect(private_key=pkb, **SF)


def _txt(v):
    s = "" if v is None else str(v).strip()
    return "" if s.lower() in ("none", "nan") else s


def construir():
    xl = pd.ExcelFile(XLSX)
    cats = [s for s in xl.sheet_names if s.lower().startswith("cat")]
    filas = []
    for hoja in cats:
        df = pd.read_excel(XLSX, sheet_name=hoja, header=None)
        for _, r in df.iterrows():
            c0 = _txt(r.get(0)); c2 = _txt(r.get(2))
            # VALOR = col2 ('clave-descripción') en hojas de 3 col; si no hay, col0
            # (hojas de 1 col tipo CatFormaPago). Descarta encabezados (sin dash).
            valor = c2 if "-" in c2 else (c0 if "-" in c0 else "")
            if not valor or c0.lower() == "clave":
                continue
            clave, desc = valor.split("-", 1)
            filas.append({"CATALOGO": hoja, "CLAVE": clave.strip(),
                          "DESCRIPCION": desc.strip(), "VALOR": valor})
        print(f"  {hoja}: {sum(1 for f in filas if f['CATALOGO']==hoja)} valores")
    return pd.DataFrame(filas, columns=["CATALOGO", "CLAVE", "DESCRIPCION", "VALOR"])


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--enviar", action="store_true")
    a = ap.parse_args()
    print("=" * 60); print(f"  SEED PLD_CATALOGO | {'ENVÍO REAL' if a.enviar else 'SIMULACIÓN'}"); print("=" * 60)
    df = construir()
    print(f"\nTotal valores de catálogo: {len(df)}  ({df['CATALOGO'].nunique()} catálogos)")
    bak = os.path.join(_RESP, "_respaldo_PLD_CATALOGO.csv")
    df.to_csv(bak, index=False, encoding="utf-8-sig"); print(f"Respaldo CSV: {bak}")
    if not a.enviar:
        print("\n[SIMULACIÓN] No se escribió. Repite con --enviar."); return 0
    from snowflake.connector.pandas_tools import write_pandas
    conn = conexion()
    try:
        cur = conn.cursor(); cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
        cur.execute(f"DELETE FROM {ESQUEMA}.{TABLA}")
        ok, _, n, _ = write_pandas(conn, df, TABLA, database=SF["database"], schema=SF["schema"], quote_identifiers=False)
        print(f"  write_pandas -> ok={ok} filas={n}")
    finally:
        conn.close()
    print("Listo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
