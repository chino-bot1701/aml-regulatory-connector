"""
ingesta_inmoges_entidades.py — Espejo CRUDO de las entidades de INMOGES a Snowflake.

Carga las entidades de INMOGES (empresa, inmueble, unidad, arrendatario, propietario,
sucursal, contrato + sus hijos) a DB_ANALYTICS.SCH_CORE.INMOGES_* — un
esquema SEPARADO del Streamlit (SCH_PLD), para no revolver.

Principios:
  • NO se pierde info: cada tabla lleva RAW_JSON con el registro original completo.
  • NO satura memoria: se transmite PÁGINA por PÁGINA (nunca >100 filas + hijos en RAM).
  • Idempotente: catálogos globales = full-refresh (DELETE-all + append por página);
    contrato/sucursal = DELETE WHERE EMPRESA por empresa; PLD = DELETE WHERE ORIGEN.
  • Respaldo CSV incremental por entidad (append por página).
  • Paginación robusta reutilizando Cobranza/inmoges_api.py (reintentos 502/503/504).

Uso (se pueden lanzar en PARALELO, cada uno toca tablas disjuntas):
    py ingesta_inmoges_entidades.py --entidad empresa       --enviar --crear-tabla
    py ingesta_inmoges_entidades.py --entidad inmueble      --enviar
    py ingesta_inmoges_entidades.py --entidad unidad        --enviar
    py ingesta_inmoges_entidades.py --entidad arrendatario  --enviar    # + contactos + PLD(arr)
    py ingesta_inmoges_entidades.py --entidad propietario   --enviar    # + PLD(prop)
    py ingesta_inmoges_entidades.py --entidad sucursal      --enviar
    py ingesta_inmoges_entidades.py --entidad contrato      --enviar    # + conceptos
    py ingesta_inmoges_entidades.py --entidad TODAS         --enviar --crear-tabla
Sin --enviar = SIMULACIÓN (extrae, imprime muestra y respaldo CSV; no toca Snowflake).
"""
from __future__ import annotations

import os
import os, sys, argparse, json, time, hashlib
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_here = os.path.dirname(os.path.abspath(__file__))
# Respaldos CSV centralizados en ProyectoALMENA/respaldos/ (no en esta subcarpeta).
_RESP = os.path.abspath(os.path.join(_here, "..", "respaldos"))
try:
    os.makedirs(_RESP, exist_ok=True)
except Exception:
    _RESP = _here
for _cand in (os.path.join(_here, "..", "Cobranza"), os.path.join(_here, "..", "..", "Cobranza")):
    if os.path.isdir(_cand):
        sys.path.insert(0, os.path.abspath(_cand)); break
import inmoges_api as api  # noqa: E402

# --------------------------------------------------------------------------- #
#  Conexión Snowflake — MISMO patrón, esquema SCH_CORE
# --------------------------------------------------------------------------- #
SF = dict(account=os.environ.get("SNOWFLAKE_ACCOUNT", ""), user="SVC_ANALYTICS",
          warehouse="WH_ANALYTICS", database="DB_ANALYTICS",
          schema="SCH_CORE", role="ROLE_ANALYTICS")
ESQUEMA = "DB_ANALYTICS.SCH_CORE"
PEM = next((p for p in (os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", ""),
                        os.path.join(_here, "SVC_ANALYTICS_key_pk8.pem"),
                        os.path.join(_here, "..", "SVC_ANALYTICS_key_pk8.pem"))
            if os.path.isfile(p)), os.path.join(_here, "SVC_ANALYTICS_key_pk8.pem"))
PAGE = 100


def conexion_snowflake():
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization
    with open(PEM, "rb") as f:
        pkey = serialization.load_pem_private_key(f.read(), password=None)
    pkb = pkey.private_bytes(encoding=serialization.Encoding.DER,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())
    return snowflake.connector.connect(private_key=pkb, **SF)


# --------------------------------------------------------------------------- #
#  Utilidades
# --------------------------------------------------------------------------- #
def _txt(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.lower() == "none" else s


def _num(v):
    try:
        return round(float(str(v).replace(",", "").strip()), 4)
    except (ValueError, TypeError):
        return None


def J(v) -> str:
    return json.dumps(v, ensure_ascii=False) if v not in (None, "", [], {}) else ""


def _hash(r) -> str:
    """Huella MD5 del contenido de NEGOCIO del registro, EXCLUYENDO los sellos de
    auditoría createdDate/modifiedDate (INMOGES los re-sella en MASA -> incluirlos
    dispararía miles de UPDATE espurios). Es la señal de cambio del MERGE incremental."""
    if not isinstance(r, dict):
        r = {"_": r}
    limpio = {k: v for k, v in r.items() if k not in ("createdDate", "modifiedDate")}
    return hashlib.md5(json.dumps(limpio, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _lista(data):
    """result.data -> lista (maneja llaves custom: unidades/sucursal/contratos/…)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("unidades", "sucursal", "contratos", "empresas", "inmuebles",
                  "arrendatarios", "propietarios", "sucursales", "data"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _get_data(ep, params):
    d = api.get_pagina(ep, params)
    res = (d or {}).get("result", {}) or {}
    if not res.get("process"):
        return []          # "no se encontraron registros" -> vacío, no error
    return _lista(res.get("data"))


def _paginar(ep, base_params):
    """Genera páginas (listas) sin acumular en memoria."""
    pagina = 1
    while True:
        p = dict(base_params); p["no_registros_x_pagina"] = PAGE; p["no_pagina"] = pagina
        lote = _get_data(ep, p)
        if not lote:
            break
        yield lote
        if len(lote) < PAGE:
            break
        pagina += 1
        time.sleep(0.1)


def empresas_alias():
    return sorted({_txt(e.get("alias")) for e in _get_data("empresa", {"no_registros_x_pagina": 1000, "no_pagina": 1}) if _txt(e.get("alias"))})


# --------------------------------------------------------------------------- #
#  Mapeos por entidad  (columnas EN MAYÚSCULAS = columnas de la tabla)
# --------------------------------------------------------------------------- #
def row_empresa(r):
    return {"ID_EMPRESA": _txt(r.get("id_empresa")), "ALIAS": _txt(r.get("alias")),
            "RAZON_SOCIAL": _txt(r.get("razon_social")), "RFC": _txt(r.get("rfc")).upper(),
            "TIPO_PERSONA": _txt(r.get("tipo_persona")), "REGIMEN_SOCIETARIO": _txt(r.get("regimen_societario")),
            "ESTATUS_MODULO": _txt(r.get("estatus_modulo")), "ID_EXTERNO": _txt(r.get("id_externo")),
            "DOMICILIO_FISCAL_PRINCIPAL": _txt(r.get("domicilio_fiscal_principal")),
            "DOMICILIO_FISCAL_JSON": J(r.get("domicilio_fiscal")), "RAW_JSON": J(r)}


def row_inmueble(r):
    return {"ID_INMUEBLE": _txt(r.get("id_inmueble")), "INMUEBLE": _txt(r.get("inmueble")),
            "FOLIO": _txt(r.get("folio")), "CUENTA_PREDIAL": _txt(r.get("cuenta_predial")),
            "CLAVE_CATASTRAL": _txt(r.get("clave_catastral")), "CATASTRO": _txt(r.get("catastro")),
            "TIPO_INMUEBLE": _txt(r.get("tipo_inmueble")), "ESTATUS": _txt(r.get("estatus")),
            "CALLE": _txt(r.get("calle")), "NO_EXTERIOR": _txt(r.get("no_exterior")), "NO_INTERIOR": _txt(r.get("no_interior")),
            "COLONIA": _txt(r.get("colonia")), "CODIGO_POSTAL": _txt(r.get("codigo_postal")),
            "CIUDAD": _txt(r.get("ciudad")), "ESTADO": _txt(r.get("estado")), "PAIS": _txt(r.get("pais")),
            "DIRECCION": _txt(r.get("direccion")), "MAPA": _txt(r.get("mapa")), "ZONA": _txt(r.get("zona")),
            "REGION": _txt(r.get("region")), "PLAZA": _txt(r.get("plaza")), "MERCADO": _txt(r.get("mercado")),
            "SEGMENTO_NEGOCIO": _txt(r.get("segmento_negocio")), "CENTRO_COSTOS": _txt(r.get("centro_costos")),
            "DIMENSION_FINANCIERA": _txt(r.get("dimension_financiera")), "ADMINISTRADOR": _txt(r.get("administrador")),
            "PRECIO_M2_RENTA": _num(r.get("precio_m2_renta")), "PRECIO_M2_MANTENIMIENTO": _num(r.get("precio_m2_mantenimiento")),
            "M2_RENTABLES": _num(r.get("m2_rentables")), "M2_CONSTRUCCION": _num(r.get("m2_construccion")),
            "M2_TERRENO": _num(r.get("m2_terreno")), "M2_ESTACIONAMIENTO": _num(r.get("m2_estacionamiento")),
            "CAJONES_ESTACIONAMIENTO": _txt(r.get("cajones_estacionamiento")),
            "CONSTRUCCION": _txt(r.get("construccion")), "TERRENO": _txt(r.get("terreno")),
            "ADQUISICION": _num(r.get("adquisicion")), "FECHA_ADQUISICION": _txt(r.get("fecha_adquisicion")),
            "ID_EXTERNO": _txt(r.get("id_externo")), "CREATED_DATE": _txt(r.get("createdDate")),
            "MODIFIED_DATE": _txt(r.get("modifiedDate")), "DOCUMENTOS_JSON": J(r.get("documentos")), "RAW_JSON": J(r)}


def row_unidad(r):
    return {"ID_UNIDAD": _txt(r.get("id_unidad")), "UNIDAD": _txt(r.get("unidad")), "INMUEBLE_TXT": _txt(r.get("inmueble")),
            "USO": _txt(r.get("uso")), "ESTATUS": _txt(r.get("estatus")), "CUENTA_PREDIAL": _txt(r.get("cuenta_predial")),
            "CLAVE_CATASTRAL": _txt(r.get("clave_catastral")), "PRECIO_RENTA": _num(r.get("precio_renta")),
            "PRECIO_M2_RENTA": _num(r.get("precio_m2_renta")), "PRECIO_MANTENIMIENTO": _num(r.get("precio_mantenimiento")),
            "PRECIO_DEPOSITO_GARANTIA": _num(r.get("precio_deposito_garantia")),
            "CALLE": _txt(r.get("calle")), "NO_EXTERIOR": _txt(r.get("no_exterior")), "NO_INTERIOR": _txt(r.get("no_interior")),
            "COLONIA": _txt(r.get("colonia")), "CODIGO_POSTAL": _txt(r.get("codigo_postal")),
            "CIUDAD": _txt(r.get("ciudad")), "ESTADO": _txt(r.get("estado")), "PAIS": _txt(r.get("pais")),
            "M2_RENTABLES": _num(r.get("m2_rentables")), "M2_CONSTRUCCION": _num(r.get("m2_construccion")),
            "M2_TERRENO": _num(r.get("m2_terreno")), "PISOS": _txt(r.get("pisos")), "HABITACIONES": _txt(r.get("habitaciones")),
            "BANOS_COMPLETOS": _txt(r.get("banos_completos")), "MEDIOS_BANOS": _txt(r.get("medios_banos")),
            "ANIOS_CONSTRUCCION": _txt(r.get("anios_construccion")), "DESCRIPCION": _txt(r.get("descripcion")),
            "CREATED_DATE": _txt(r.get("createdDate")), "MODIFIED_DATE": _txt(r.get("modifiedDate")),
            "DOCUMENTOS_JSON": J(r.get("documentos")), "PORTALES_JSON": J(r.get("portales")), "RAW_JSON": J(r)}


def row_arrendatario(r):
    return {"ID_ARRENDATARIO": _txt(r.get("id_arrendatario")), "RAZON_SOCIAL": _txt(r.get("razon_social")),
            "ALIAS": _txt(r.get("alias")), "RFC": _txt(r.get("rfc")).upper(), "TIPO_PERSONA": _txt(r.get("tipo_persona")),
            "FECHA": _txt(r.get("fecha")), "MEDIO_CONTACTO": _txt(r.get("medio_contacto")),
            "ESTATUS_MODULO": _txt(r.get("estatus_modulo")), "CONTACTO_PRINCIPAL": _txt(r.get("contacto_principal")),
            "SUCURSAL_PRINCIPAL": _txt(r.get("sucursal_principal")), "RAW_JSON": J(r)}


def rows_contacto(r):
    ida = _txt(r.get("id_arrendatario"))
    return [{"ID_ARRENDATARIO": ida, "ID_CONTACTO": _txt(c.get("id_contacto")), "NOMBRE": _txt(c.get("nombre")),
             "TELEFONO": _txt(c.get("telefono")), "CORREO": _txt(c.get("correo")).upper(), "RAW_JSON": J(c)}
            for c in api.as_list(r.get("contactos"))]


def row_propietario(r):
    return {"ID_PROPIETARIO": _txt(r.get("id_propietario")), "RAZON_SOCIAL": _txt(r.get("razon_social")),
            "ALIAS": _txt(r.get("alias")), "RFC": _txt(r.get("rfc")).upper(), "TIPO_PERSONA": _txt(r.get("tipo_persona")),
            "REGIMEN_FISCAL": _txt(r.get("regimen_fiscal")), "ESTATUS_MODULO": _txt(r.get("estatus_modulo")),
            "CALLE": _txt(r.get("calle")), "NO_EXTERIOR": _txt(r.get("no_exterior")), "NO_INTERIOR": _txt(r.get("no_interior")),
            "COLONIA": _txt(r.get("colonia")), "CODIGO_POSTAL": _txt(r.get("codigo_postal")),
            "CIUDAD": _txt(r.get("ciudad")), "ESTADO": _txt(r.get("estado")), "PAIS": _txt(r.get("pais")),
            "FECHA": _txt(r.get("fecha")), "RAW_JSON": J(r)}


def row_sucursal(r, emp):
    return {"EMPRESA": emp, "ID_SUCURSAL": _txt(r.get("id_sucursal")), "ID_ARRENDATARIO": _txt(r.get("id_arrendatario")),
            "RAZON_SOCIAL": _txt(r.get("razon_social")), "RFC": _txt(r.get("rfc")).upper(),
            "ALIAS_ARRENDATARIO": _txt(r.get("alias_arrendatario")), "ALIAS_SUCURSAL": _txt(r.get("alias_sucursal")),
            "REGIMEN_FISCAL": _txt(r.get("regimen_fiscal")), "CORREOS_CFDI": _txt(r.get("correos_cfdi")),
            "USO_CFDI": _txt(r.get("uso_cfdi")), "FORMA_PAGO": _txt(r.get("forma_pago")), "METODO_PAGO": _txt(r.get("metodo_pago")),
            "CUENTA_CONTABLE": _txt(r.get("cuenta_contable")), "CALLE": _txt(r.get("calle")),
            "NO_EXTERIOR": _txt(r.get("no_exterior")), "NO_INTERIOR": _txt(r.get("no_interior")), "COLONIA": _txt(r.get("colonia")),
            "CODIGO_POSTAL": _txt(r.get("codigo_postal")), "CIUDAD": _txt(r.get("ciudad")), "ESTADO": _txt(r.get("estado")),
            "ID_EXTERNO_ARRENDATARIO": _txt(r.get("id_externo_arrendatario")), "ID_EXTERNO_SUCURSAL": _txt(r.get("id_externo_sucursal")),
            "RAW_JSON": J(r)}


def row_contrato(r, emp):
    return {"EMPRESA": emp, "ID_CONTRATO": _txt(r.get("id_contrato")), "ARRENDATARIO": _txt(r.get("arrendatario")),
            "INMUEBLE": _txt(r.get("inmueble")), "ID_INMUEBLE": _txt(r.get("id_inmueble")),
            "UNIDAD": _txt(r.get("unidad")), "ID_UNIDAD": _txt(r.get("id_unidad")), "SUCURSAL": _txt(r.get("sucursal")),
            "DOMICILIO_FISCAL": _txt(r.get("domicilio_fiscal")), "GIRO": _txt(r.get("giro")), "ESTATUS": _txt(r.get("estatus")),
            "GRUPO_AVISO": _txt(r.get("grupo_aviso")), "FECHA_INICIAL": _txt(r.get("fecha_inicial")),
            "FECHA_FINAL": _txt(r.get("fecha_final")), "FECHA_FIRMA": _txt(r.get("fecha_firma")),
            "FECHA_RENOVACION": _txt(r.get("fecha_renovacion")), "FECHA_ENTREGA": _txt(r.get("fecha_entrega")),
            "FECHA_OPERACION": _txt(r.get("fecha_operacion")), "FECHA_CANCELACION": _txt(r.get("fecha_cancelacion")),
            "FRECUENCIA": _txt(r.get("frecuencia")), "MONEDA": _txt(r.get("moneda")), "TIPO_CAMBIO": _txt(r.get("tipo_cambio")),
            "SUBTOTAL": _num(r.get("subtotal")), "DESCUENTO": _num(r.get("descuento")), "IVA": _num(r.get("iva")),
            "RETENCION_IVA": _num(r.get("retencion_iva")), "ISR": _num(r.get("isr")), "TOTAL": _num(r.get("total")),
            "ID_EXTERNO": _txt(r.get("id_externo")), "RAW_JSON": J(r), "ROW_HASH": _hash(r)}


def rows_concepto(r, emp):
    idc = _txt(r.get("id_contrato"))
    out = []
    for i, c in enumerate(api.as_list(r.get("concepto_contrato")), 1):
        cc = c.get("concepto") if isinstance(c.get("concepto"), dict) else {}
        out.append({"EMPRESA": emp, "ID_CONTRATO": idc, "ORDEN": i, "DESCRIPCION": _txt(c.get("descripcion")),
                    "TIPO_CONCEPTO": _txt(c.get("tipo_concepto")), "PRODUCTO_SERVICIO": _txt(c.get("producto_servicio")),
                    "UNIDAD_MEDIDA": _txt(c.get("unidad_medida")), "VALOR_UNITARIO": _num(c.get("valor_unitario")),
                    "IMPUESTOS": _txt(c.get("grupo_impuestos")), "CUENTA_CONTABLE": _txt(cc.get("cuenta_contable")),
                    "RAW_JSON": J(c)})
    return out


def rows_pld(r, origen):
    """pld_entidad[] de arrendatario o propietario -> filas INMOGES_PLD_ENTIDAD."""
    idp = _txt(r.get("id_arrendatario")) or _txt(r.get("id_propietario"))
    out = []
    for p in api.as_list(r.get("pld_entidad")):
        out.append({"ORIGEN": origen, "ID_PADRE": idp, "RFC_PADRE": _txt(r.get("rfc")).upper(),
                    "RAZON_SOCIAL_PADRE": _txt(r.get("razon_social")), "EMPRESA": "",
                    "ID_PLD_ENTIDAD": _txt(p.get("id_pld_entidad")), "TIPO_PERSONA": _txt(p.get("tipo_persona")),
                    "PF_NOMBRE": _txt(p.get("pf_nombre")), "PF_APELLIDO_PAT": _txt(p.get("pf_apellido_paterno")),
                    "PF_APELLIDO_MAT": _txt(p.get("pf_apellido_materno")), "PF_FECHA_NAC": _txt(p.get("pf_fecha_nacimiento")),
                    "PF_CURP": _txt(p.get("pf_curp")).upper(), "ACTIVIDAD_ECONOMICA": _txt(p.get("actividad_economica")),
                    "FECHA_CONSTITUCION": _txt(p.get("fecha_constitucion")), "GIRO_MERCANTIL": _txt(p.get("giro_mercantil")),
                    "RPM_NOMBRE": _txt(p.get("rpm_nombre")), "RPM_APELLIDO_PAT": _txt(p.get("rpm_apellido_paterno")),
                    "RPM_APELLIDO_MAT": _txt(p.get("rpm_apellido_materno")), "RPM_FECHA_NAC": _txt(p.get("rpm_fecha_nacimiento")),
                    "RPM_RFC": _txt(p.get("rpm_rfc")).upper(), "RPM_CURP": _txt(p.get("rpm_curp")).upper(),
                    "CALLE": _txt(p.get("calle")), "COLONIA": _txt(p.get("colonia")), "NO_EXTERIOR": _txt(p.get("no_exterior")),
                    "NO_INTERIOR": _txt(p.get("no_interior")), "PAIS": _txt(p.get("pais")), "CLAVE": _txt(p.get("clave")),
                    "TELEFONO": _txt(p.get("telefono")), "CORREO": _txt(p.get("correo")).upper(), "RAW_JSON": J(p)})
    return out


# --------------------------------------------------------------------------- #
#  Carga (write_pandas por página; DELETE idempotente al inicio)
# --------------------------------------------------------------------------- #
def _write(conn, tabla, filas):
    if not filas:
        return 0
    from snowflake.connector.pandas_tools import write_pandas
    df = pd.DataFrame(filas)
    ok, _, n, _ = write_pandas(conn, df, tabla, database=SF["database"], schema=SF["schema"], quote_identifiers=False)
    return n


def merge_upsert(conn, tabla, filas, llaves, soft_delete=True):
    """SYNC INCREMENTAL por HASH. Carga 'filas' a un STAGE temporal y hace MERGE
    contra {tabla} por 'llaves', comparando ROW_HASH: INSERT nuevos / UPDATE si el
    hash cambió / NO toca los iguales (idempotente). Si soft_delete=True (pull
    COMPLETO verificado), marca ACTIVO=false los que ya no vinieron (NUNCA borrado
    físico). Devuelve (insertados, actualizados)."""
    if not filas:
        print(f"    {tabla}: 0 filas en el pull; no se hace MERGE"); return (0, 0)
    from snowflake.connector.pandas_tools import write_pandas
    stg = f"{tabla}_STG"
    cur = conn.cursor()
    # STAGE con la misma forma que el destino (incluye ROW_HASH/ACTIVO/LOAD_TS con sus defaults)
    cur.execute(f"CREATE OR REPLACE TEMPORARY TABLE {ESQUEMA}.{stg} LIKE {ESQUEMA}.{tabla}")
    write_pandas(conn, pd.DataFrame(filas), stg, database=SF["database"],
                 schema=SF["schema"], quote_identifiers=False)
    # columnas reales del destino -> arma el MERGE dinámico
    cur.execute("SELECT COLUMN_NAME FROM DB_ANALYTICS.INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA='SCH_CORE' AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (tabla,))
    cols = [c[0] for c in cur.fetchall()]
    on = " AND ".join(f"T.{k}=S.{k}" for k in llaves)
    part = ", ".join(llaves)
    setc = ", ".join(f"T.{c}=S.{c}" for c in cols if c not in llaves)
    ins_c = ", ".join(cols)
    ins_v = ", ".join(f"S.{c}" for c in cols)
    cur.execute(
        f"MERGE INTO {ESQUEMA}.{tabla} T USING ("
        f"  SELECT * FROM {ESQUEMA}.{stg} "
        f"  QUALIFY ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY LOAD_TS DESC)=1) S "
        f"ON {on} "
        f"WHEN MATCHED AND NOT EQUAL_NULL(T.ROW_HASH, S.ROW_HASH) THEN UPDATE SET {setc} "
        f"WHEN NOT MATCHED THEN INSERT ({ins_c}) VALUES ({ins_v})")
    try:
        st = cur.fetchone()                 # Snowflake MERGE -> (rows inserted, rows updated)
        ins, upd = int(st[0]), int(st[1])
    except Exception:
        ins, upd = -1, -1
    bajas = 0
    if soft_delete:
        nx = " AND ".join(f"S.{k}=T.{k}" for k in llaves)
        cur.execute(
            f"UPDATE {ESQUEMA}.{tabla} T SET ACTIVO=FALSE, "
            f"FECHA_BAJA=CAST(CONVERT_TIMEZONE('America/Mexico_City',CURRENT_TIMESTAMP()) AS TIMESTAMP_NTZ) "
            f"WHERE T.ACTIVO=TRUE AND NOT EXISTS (SELECT 1 FROM {ESQUEMA}.{stg} S WHERE {nx})")
        bajas = cur.rowcount
    else:
        print(f"    {tabla}: soft-delete OMITIDO (pull incompleto)")
    print(f"    {tabla}: MERGE -> insertados={ins}  actualizados={upd}  bajas_soft={bajas}")
    return (ins, upd)


def _csv_append(path, filas, primera):
    if not filas:
        return
    try:
        pd.DataFrame(filas).to_csv(path, index=False, mode="w" if primera else "a",
                                   header=primera, encoding="utf-8-sig")
    except Exception:
        pass


# Config: entidad -> (endpoint, tabla, tipo de carga)
GLOBALES = {  # catálogos globales paginados (la API ignora 'empresa')
    "empresa":      ("empresa", "INMOGES_EMPRESA", row_empresa),
    "inmueble":     ("inmueble", "INMOGES_INMUEBLE", row_inmueble),
    "unidad":       ("unidad", "INMOGES_UNIDAD", row_unidad),
    "arrendatario": ("arrendatario", "INMOGES_ARRENDATARIO", row_arrendatario),
    "propietario":  ("propietario", "INMOGES_PROPIETARIO", row_propietario),
}


def ingesta_global(conn, entidad, enviar):
    ep, tabla, row_fn = GLOBALES[entidad]
    cur = conn.cursor() if enviar else None
    if enviar:
        cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
        cur.execute(f"DELETE FROM {ESQUEMA}.{tabla}")
        if entidad == "arrendatario":
            cur.execute(f"DELETE FROM {ESQUEMA}.INMOGES_ARRENDATARIO_CONTACTO")
            cur.execute(f"DELETE FROM {ESQUEMA}.INMOGES_PLD_ENTIDAD WHERE ORIGEN='ARRENDATARIO'")
        if entidad == "propietario":
            cur.execute(f"DELETE FROM {ESQUEMA}.INMOGES_PLD_ENTIDAD WHERE ORIGEN='PROPIETARIO'")
    bak = os.path.join(_RESP, f"_respaldo_{tabla}.csv")
    total = 0; primera = True
    for lote in _paginar(ep, {}):
        filas = [row_fn(r) for r in lote]
        _csv_append(bak, filas, primera)
        if enviar:
            total += _write(conn, tabla, filas)
            if entidad == "arrendatario":
                _write(conn, "INMOGES_ARRENDATARIO_CONTACTO", [c for r in lote for c in rows_contacto(r)])
                _write(conn, "INMOGES_PLD_ENTIDAD", [p for r in lote for p in rows_pld(r, "ARRENDATARIO")])
            if entidad == "propietario":
                _write(conn, "INMOGES_PLD_ENTIDAD", [p for r in lote for p in rows_pld(r, "PROPIETARIO")])
        else:
            total += len(filas)
        primera = False
        print(f"   {tabla}: +{len(filas)} (acum {total})")
    print(f"  {tabla}: {total} filas {'cargadas' if enviar else '(simulación)'} | respaldo {bak}")
    return total


def ingesta_por_empresa(conn, entidad, enviar):
    """contrato (+conceptos) o sucursal — bloque = 1 empresa."""
    emps = empresas_alias()
    print(f"  Empresas: {len(emps)}")
    total = 0
    cur = conn.cursor() if enviar else None
    if enviar:
        cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
    for k, emp in enumerate(emps, 1):
        t = time.perf_counter()
        try:
            if entidad == "contrato":
                data = _get_data("contrato", {"empresa": emp})   # no pagina: todo de la empresa
                filas = [row_contrato(r, emp) for r in data]
                conceptos = [c for r in data for c in rows_concepto(r, emp)]
                bak = os.path.join(_RESP, f"_respaldo_INMOGES_CONTRATO_{emp}.csv")
                _csv_append(bak, filas, True)
                if enviar:
                    cur.execute(f"DELETE FROM {ESQUEMA}.INMOGES_CONTRATO WHERE EMPRESA=%s", (emp,))
                    cur.execute(f"DELETE FROM {ESQUEMA}.INMOGES_CONTRATO_CONCEPTO WHERE EMPRESA=%s", (emp,))
                    n = _write(conn, "INMOGES_CONTRATO", filas); _write(conn, "INMOGES_CONTRATO_CONCEPTO", conceptos)
                else:
                    n = len(filas)
                total += n
                print(f"  [{k}/{len(emps)}] {emp}: {n} contratos, {len(conceptos)} conceptos ({time.perf_counter()-t:.0f}s)")
            else:  # sucursal (pagina, requiere empresa)
                bak = os.path.join(_RESP, f"_respaldo_INMOGES_SUCURSAL_{emp}.csv")
                if enviar:
                    cur.execute(f"DELETE FROM {ESQUEMA}.INMOGES_SUCURSAL_ARRENDATARIO WHERE EMPRESA=%s", (emp,))
                n_emp = 0; primera = True
                for lote in _paginar("sucursal_arrendatario", {"empresa": emp}):
                    filas = [row_sucursal(r, emp) for r in lote]
                    _csv_append(bak, filas, primera); primera = False
                    n_emp += _write(conn, "INMOGES_SUCURSAL_ARRENDATARIO", filas) if enviar else len(filas)
                total += n_emp
                print(f"  [{k}/{len(emps)}] {emp}: {n_emp} sucursales ({time.perf_counter()-t:.0f}s)")
        except Exception as ex:
            print(f"  ! {emp}: error ({ex}); se omite"); continue
    print(f"  Total {entidad}: {total}")
    return total


def ingesta_contrato_merge(conn, enviar):
    """CONTRATO con SYNC INCREMENTAL (huella + MERGE + soft-delete).
    Pull completo por empresa -> acumula -> MERGE por (EMPRESA, ID_CONTRATO) comparando
    ROW_HASH. Los conceptos (hijos) siguen full-refresh por empresa (no son el foco de
    la prueba). Soft-delete SOLO si TODAS las empresas se leyeron sin error (pull completo)."""
    emps = empresas_alias()
    print(f"  Empresas: {len(emps)}")
    cur = conn.cursor() if enviar else None
    if enviar:
        cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
    todas, fallidas, total_conc = [], 0, 0
    for k, emp in enumerate(emps, 1):
        t = time.perf_counter()
        try:
            data = _get_data("contrato", {"empresa": emp})   # no pagina: todo de la empresa
            filas = [row_contrato(r, emp) for r in data]
            conceptos = [c for r in data for c in rows_concepto(r, emp)]
            _csv_append(os.path.join(_RESP, f"_respaldo_INMOGES_CONTRATO_{emp}.csv"), filas, True)
            if enviar:
                cur.execute(f"DELETE FROM {ESQUEMA}.INMOGES_CONTRATO_CONCEPTO WHERE EMPRESA=%s", (emp,))
                _write(conn, "INMOGES_CONTRATO_CONCEPTO", conceptos)
            todas.extend(filas); total_conc += len(conceptos)
            print(f"  [{k}/{len(emps)}] {emp}: {len(filas)} contratos, {len(conceptos)} conceptos ({time.perf_counter()-t:.0f}s)")
        except Exception as ex:
            fallidas += 1
            print(f"  ! {emp}: error ({ex}); se omite (pull incompleto -> sin soft-delete)"); continue
    print(f"  Pull: {len(todas)} contratos de {len(emps)-fallidas}/{len(emps)} empresas, {total_conc} conceptos")
    if not enviar:
        print(f"  [SIMULACIÓN] {len(todas)} contratos (no se hace MERGE)"); return len(todas)
    merge_upsert(conn, "INMOGES_CONTRATO", todas, ["EMPRESA", "ID_CONTRATO"], soft_delete=(fallidas == 0))
    return len(todas)


def ingesta_sucursal(conn, enviar):
    """/sucursal_arrendatario IGNORA el filtro 'empresa' y devuelve el catálogo
    GLOBAL (mismas ~5,339 para cualquier empresa). Por eso se carga UNA sola vez
    (se pasa una empresa cualquiera solo porque la API exige un filtro), en vez
    de iterar 49 empresas (que duplicaba 49×). Idempotente = full-refresh."""
    emp0 = (empresas_alias() or ["HPI"])[0]
    if enviar:
        cur = conn.cursor(); cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
        cur.execute(f"DELETE FROM {ESQUEMA}.INMOGES_SUCURSAL_ARRENDATARIO")
    bak = os.path.join(_RESP, "_respaldo_INMOGES_SUCURSAL.csv")
    total = 0; primera = True
    for lote in _paginar("sucursal_arrendatario", {"empresa": emp0}):
        filas = [row_sucursal(r, "") for r in lote]   # EMPRESA="" (catálogo global)
        _csv_append(bak, filas, primera); primera = False
        total += _write(conn, "INMOGES_SUCURSAL_ARRENDATARIO", filas) if enviar else len(filas)
        print(f"   INMOGES_SUCURSAL_ARRENDATARIO: +{len(filas)} (acum {total})")
    print(f"  INMOGES_SUCURSAL_ARRENDATARIO: {total} filas (GLOBAL, sin duplicar por empresa)")
    return total


ORDEN_TODAS = ["empresa", "inmueble", "unidad", "arrendatario", "propietario", "sucursal", "contrato"]


def correr(entidad, enviar):
    conn = conexion_snowflake() if enviar else None
    try:
        if entidad in GLOBALES:
            ingesta_global(conn, entidad, enviar)
        elif entidad == "sucursal":
            ingesta_sucursal(conn, enviar)
        elif entidad == "contrato":
            ingesta_contrato_merge(conn, enviar)   # SYNC INCREMENTAL (huella + MERGE)
        else:
            print(f"Entidad desconocida: {entidad}")
    finally:
        if conn:
            conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entidad", required=True,
                    help="empresa|inmueble|unidad|arrendatario|propietario|sucursal|contrato|TODAS")
    ap.add_argument("--enviar", action="store_true")
    ap.add_argument("--crear-tabla", action="store_true", help="(informativo: el DDL se corre aparte con ROLE_DEV)")
    a = ap.parse_args()
    ents = ORDEN_TODAS if a.entidad.upper() == "TODAS" else [a.entidad.lower()]
    print("=" * 64)
    print(f"  ENTIDADES INMOGES -> Snowflake (SCH_CORE) | {'ENVÍO REAL' if a.enviar else 'SIMULACIÓN'} | {ents}")
    print("=" * 64)
    if a.crear_tabla:
        print("  (Recuerda correr ddl_INMOGES_DATA_ALMENA_entidades.sql con ROLE_DEV antes de --enviar)")
    for e in ents:
        print(f"\n--- {e} ---")
        correr(e, a.enviar)
    if not a.enviar:
        print("\n[SIMULACIÓN] No se escribió. Repite con --enviar.")
    print("Listo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
