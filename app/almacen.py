"""Snowflake reads and writes.

Swapped out wholesale by `demo/almacen_demo.py` when DEMO_MODE=1.
"""
from __future__ import annotations

import os

import streamlit as st

from logica import *  # noqa: F401,F403

# ============================================================================ #
#  3) SNOWFLAKE — conexión, lectura de tablas y guardado del formulario
# ============================================================================ #
SF = dict(account=os.environ.get("SNOWFLAKE_ACCOUNT", ""),
          user=os.environ.get("SNOWFLAKE_USER", ""),
          warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", ""),
          database=os.environ.get("SNOWFLAKE_DATABASE", ""),
          schema=os.environ.get("SNOWFLAKE_SCHEMA", ""),
          role=os.environ.get("SNOWFLAKE_ROLE", ""))
ESQUEMA = f"{SF['database']}.{SF['schema']}"
# La app se conecta con schema SCH_PLD; el beneficiario controlador vive en la capa
# de datos (SCH_CORE), así que SIEMPRE se referencia calificado.
ESQUEMA_DATA = f"{SF['database']}.{os.environ.get('SNOWFLAKE_SCHEMA_DATA', 'SCH_CORE')}"
# Ruta al PEM de Snowflake (key-pair auth). El archivo es secreto; la ruta es
# configurable y por defecto apunta junto a este app.py.
PEM = os.environ.get(
    "SNOWFLAKE_PRIVATE_KEY_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "SVC_ANALYTICS_key_pk8.pem"),
)


def sf_conexion():
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization
    with open(PEM, "rb") as f:
        pkey = serialization.load_pem_private_key(f.read(), password=None)
    pkb = pkey.private_bytes(encoding=serialization.Encoding.DER,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())
    return snowflake.connector.connect(private_key=pkb, **SF)


def _s(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.lower() == "none" else s


@st.cache_resource(show_spinner=False)
def _sf_conn():
    """Conexión ÚNICA reutilizada entre consultas (el handshake TCP/TLS/llave
    cuesta ~1-2 s; abrirla en cada query era el 80-90% de la latencia).
    El warehouse se fija una vez; la sesión lo conserva."""
    conn = sf_conexion()
    cur = conn.cursor()
    cur.execute(f"USE WAREHOUSE {SF['warehouse']}")
    cur.close()
    return conn


def sf_query(sql, params=None) -> list:
    # Reintenta UNA vez con conexión fresca si la sesión cacheada murió
    # (expiración de token / red); así el caché nunca deja la app caída.
    for intento in (1, 2):
        conn = _sf_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description]
            out = [dict(zip(cols, row)) for row in cur.fetchall()]
            cur.close()
            return out
        except Exception:
            _sf_conn.clear()          # descarta la conexión muerta
            if intento == 2:
                raise


def leer_tabla() -> list:
    return sf_query(f"""
        SELECT * FROM {ESQUEMA}.PLD_AVISO
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ID_CONTRATO ORDER BY LOAD_TS DESC) = 1
    """)


def leer_pagos(periodo: str) -> list:
    try:
        return sf_query(f"SELECT * FROM {ESQUEMA}.PLD_AVISO_PAGOS WHERE PERIODO = %s", (periodo,))
    except Exception:
        return []


# --- Migración a FACTURAS (CFDI) --------------------------------------------
# USAR_CFDI=true (default): la app lee PLD_AVISO_CFDI (facturas timbradas),
# que YA incluyen Renta + Renta Variable y traen 1 fila por factura + sus
# parcialidades en JSON. USAR_CFDI=false revierte a la tabla vieja PAGOS
# (rollback sin redeploy).
USAR_CFDI = os.getenv("USAR_CFDI", "true").lower() == "true"


def leer_cfdi(periodo: str) -> list:
    try:
        return sf_query(f"SELECT * FROM {ESQUEMA}.PLD_AVISO_CFDI WHERE PERIODO = %s", (periodo,))
    except Exception:
        return []


def leer_form(empresa: str, id_contrato: str):
    try:
        r = sf_query(f"SELECT * FROM {ESQUEMA}.PLD_AVISO_FORM WHERE EMPRESA=%s AND ID_CONTRATO=%s "
                     f"QUALIFY ROW_NUMBER() OVER (PARTITION BY EMPRESA,ID_CONTRATO ORDER BY UPDATE_TS DESC)=1",
                     (empresa, id_contrato))
        return r[0] if r else None
    except Exception:
        return None


def leer_form_todos() -> dict:
    """TODOS los formularios completos en UNA consulta (prefetch). El modal ya
    no consulta Snowflake al abrir un cliente: busca en este dict cacheado."""
    out = {}
    try:
        for r in sf_query(f"SELECT * FROM {ESQUEMA}.PLD_AVISO_FORM "
                          f"QUALIFY ROW_NUMBER() OVER (PARTITION BY EMPRESA,ID_CONTRATO ORDER BY UPDATE_TS DESC)=1"):
            out[(_s(r.get("EMPRESA")), _s(r.get("ID_CONTRATO")))] = r
    except Exception:
        pass
    return out


def leer_entidad() -> dict:
    """Entidad PLD por RFC (pld_entidad capturada en INMOGES) para PRE-LLENAR el
    formulario. Si la tabla aún no existe, la app sigue funcionando sin prefill."""
    out = {}
    try:
        for r in sf_query(f"SELECT * FROM {ESQUEMA}.PLD_AVISO_ENTIDAD "
                          f"QUALIFY ROW_NUMBER() OVER (PARTITION BY RFC ORDER BY LOAD_TS DESC, ID_PLD_ENTIDAD) = 1"):
            out[_s(r.get("RFC")).upper()] = r
    except Exception:
        pass
    return out


def leer_beneficiarios() -> dict:
    """Beneficiario controlador por FOLIO (= ID_CONTRATO), desde la VISTA.

    Se lee SOLO la vista (nunca la tabla): es el contrato de interfaz, si cambia el
    modelo físico la vista absorbe el cambio. Es SOLO LECTURA: la app jamás escribe
    aquí — la tabla se regenera completa desde el Excel de Legal
    (cargadirecta/carga_beneficiario_controlador.py), así que cualquier escritura
    desde la app se perdería en la siguiente carga.
    Se prefetchea completa (hoy 14 filas) y se indexa en memoria, igual que ENTIDAD:
    abrir un cliente no cuesta una consulta.
    Devuelve {"ok": bool, "error": str, "por_folio": {folio: [beneficiario, ...]}}.
    *** El "ok" es lo importante: NO basta con devolver vacío. Una consulta que FALLA
    (permiso perdido, vista renombrada, Snowflake caído) se vería idéntica a "este
    contrato no tiene beneficiario", y el aviso saldría sin él SIN QUE NADIE SE ENTERE.
    Por eso el error se propaga y la UI lo pinta en rojo. ***
    La lista por folio es porque Cumplimiento aún no confirma si un cliente puede
    declarar más de un beneficiario (caso INNOVASPORT)."""
    out = {}
    try:
        for r in sf_query(f"""SELECT FOLIO, BC_NOMBRE, BC_APELLIDO_PATERNO, BC_APELLIDO_MATERNO,
                                     BC_FECHA_NACIMIENTO, BC_RFC, BC_CURP,
                                     BC_PAIS_NACIONALIDAD, ORIGEN
                                FROM {ESQUEMA_DATA}.V_PLD_BENEFICIARIO_CONTROLADOR"""):
            out.setdefault(_s(r.get("FOLIO")), []).append({   # FOLIO es VARCHAR: se compara como texto
                "nombre": _s(r.get("BC_NOMBRE")),
                "apellidopaterno": _s(r.get("BC_APELLIDO_PATERNO")),
                "apellidomaterno": _s(r.get("BC_APELLIDO_MATERNO")),
                "fechanacimiento": a_fecha(r.get("BC_FECHA_NACIMIENTO")),
                "rfc": _s(r.get("BC_RFC")), "curp": _s(r.get("BC_CURP")),
                # Texto CRUDO de Legal (sin normalizar, a propósito): se muestra tal cual
                # y se traduce a clave UIF solo al armar el payload.
                "paisnacionalidad_txt": _s(r.get("BC_PAIS_NACIONALIDAD")),
                "origen": _s(r.get("ORIGEN")) or "Excel",
            })
    except Exception as ex:
        # La app NO se cae, pero el fallo queda MARCADO (no se disfraza de "sin datos").
        return {"ok": False, "error": str(ex)[:300], "por_folio": {}}
    return {"ok": True, "error": "", "por_folio": out}


def _ddmmaaaa(s):
    s = str(s or "")
    return f"{s[6:8]}/{s[4:6]}/{s[0:4]}" if len(s) == 8 and s.isdigit() else (s or "—")


def operaciones_para_grid(filas: list, pagos: list | None = None) -> list:
    """DETALLE: UNA FILA POR RENTA/PAGO. El cliente se repite por cada liquidación,
    cada fila con su fecha, monto (con divisa) y partida (concepto). Los contratos
    sin pago del periodo aparecen como una sola fila con '—'."""
    pagos = pagos or []
    out = []
    consumidos = set()   # _pkey de pagos ya mostrados (para detectar huérfanos)

    def _fila_de_liq(base, l):
        # Estatus de pago a nivel FACTURA (CFDI): SALDO 0 = Completo; sin pago = "Sin pago".
        if l.get("_sin_pago"):
            pago = "Sin pago"
        elif l.get("cfdi_saldo") is not None:
            pago = "Parcial" if _f(l.get("cfdi_saldo")) > 0 else "Completo"
        else:
            pago = "Parcial" if _f(l.get("saldo_pendiente")) > 0 else "Completo"
        consumidos.add(l.get("_pkey"))
        return {**base,
                "Fecha": _ddmmaaaa(l.get("fecha_pago_ultima") or ""),   # última fecha de PAGO
                "FechaTimbre": _ddmmaaaa(l.get("fecha_timbre") or ""),
                "Folio": _s(l.get("folio")),
                "Monto": _f(l["montooperacion"]),
                "MontoSinIVA": (_f(l.get("monto_sin_iva")) or None),   # 0 = aún sin dato -> "—"
                "MontoFinal": (_f(l.get("monto_final")) or None),
                "Moneda": "USD" if l.get("moneda") == "2" else "MXN",
                "Partida": l.get("partida") or "—",
                "Local": l.get("local") or l.get("unidad") or "—",
                "Pago": pago,
                "Contrato": _s(l.get("contrato_inmoges")) or base["Contrato"],   # contrato REAL de la factura
                "Factura": l.get("factura_url") or "",
                "_idcfdi": _s(l.get("_idcfdi"))}

    for r in filas:
        base = {"Empresa": _s(r.get("EMPRESA")), "Cliente": _s(r.get("DENOMINACION_RAZON")),
                "Inmueble": _s(r.get("INMUEBLE")), "Contrato": _s(r.get("ID_CONTRATO")),
                "ID_SUJETO": r.get("ID_SUJETO"), "_emp": _s(r.get("EMPRESA")), "_fila": r}
        realliq = _liquidaciones_reales(pagos, _s(r.get("RFC")), _s(r.get("INMUEBLE")),
                                        _s(r.get("ID_CONTRATO"))) if pagos else []
        if realliq:
            for l in realliq:
                out.append(_fila_de_liq(base, l))
        else:
            # Sin pago real del periodo -> no se muestra el total del contrato (evita confusión).
            out.append({**base, "Fecha": "—", "FechaTimbre": "—", "Folio": "—", "Monto": None,
                        "MontoSinIVA": None, "MontoFinal": None, "Moneda": "", "Partida": "—",
                        "Local": "—", "Pago": "—", "Factura": "", "_idcfdi": None})

    # HUÉRFANOS: pagos que ningún contrato reclamó (p.ej. contrato cancelado que
    # sigue cobrando). Se sintetiza su fila desde el propio pago: NADA queda invisible.
    emps_visibles = {_s(r.get("EMPRESA")) for r in filas}
    for p in pagos:
        if _s(p.get("EMPRESA")) not in emps_visibles:
            continue
        # Expandir el pago/factura huérfano a su(s) liquidación(es) según la fuente.
        liqs_p = _liqs_de_cfdi(p) if USAR_CFDI else [_map_liq(p)]
        if not liqs_p or liqs_p[0].get("_pkey") in consumidos:
            continue
        fila_syn = {"EMPRESA": _s(p.get("EMPRESA")), "DENOMINACION_RAZON": _s(p.get("RAZON_SOCIAL")),
                    "RFC": _s(p.get("RFC_RECEPTOR")), "INMUEBLE": _s(p.get("INMUEBLE_TXT")),
                    "ID_CONTRATO": _s(p.get("CONTRATO_INMOGES"))}
        base = {"Empresa": fila_syn["EMPRESA"], "Cliente": fila_syn["DENOMINACION_RAZON"],
                "Inmueble": fila_syn["INMUEBLE"], "Contrato": fila_syn["ID_CONTRATO"] or "—",
                "ID_SUJETO": None, "_emp": fila_syn["EMPRESA"], "_fila": fila_syn, "_sin_contrato": True}
        for l in liqs_p:
            out.append(_fila_de_liq(base, l))
    return out


def _map_liq(p: dict) -> dict:
    """Fila de PLD_AVISO_PAGOS -> dict de liquidación (llaves extra son
    solo visuales: build_payload/validate las ignoran)."""
    return {"fechapago": a_fecha(p.get("FECHA_PAGO")),
            "formapago": mapear(p.get("FORMA_PAGO_TXT"), FORMA_PAGO, "1"),
            "instrumentomonetario": mapear(p.get("INSTRUMENTO_TXT"), INSTRUMENTO, "8"),
            "moneda": mapear(p.get("MONEDA_TXT"), MONEDA, "1"),
            "montooperacion": a_monto(p.get("MONTO")),
            "partida": _s(p.get("CONCEPTOS")),
            "metodo_pago": _s(p.get("METODO_PAGO")),
            "num_parcialidad": _s(p.get("NUM_PARCIALIDAD")),
            "saldo_anterior": _f(p.get("SALDO_ANTERIOR")),
            "importe_pagado": _f(p.get("IMPORTE_PAGADO")),
            "saldo_pendiente": _f(p.get("SALDO_PENDIENTE")),
            "monto_sin_iva": _f(p.get("MONTO_SIN_IVA")),
            "monto_final": _f(p.get("MONTO_FINAL")),
            "contrato_inmoges": _s(p.get("CONTRATO_INMOGES")),
            "unidad": _s(p.get("UNIDAD")),
            "local": _s(p.get("LOCAL_COMERCIAL")),
            "factura_url": _s(p.get("FACTURA_URL")),
            "_pkey": (str(p.get("FOLIO_PAGO")), str(p.get("FACTURA_UUID")),
                      str(p.get("FECHA_PAGO")), str(p.get("MONTO")))}


_RE_LOCAL_APP = re.compile(r"identificado como\s+(.+?)\s+ubicado", re.IGNORECASE)


def _liqs_de_cfdi(row: dict) -> list:
    """Fila de PLD_AVISO_CFDI -> lista de liquidaciones (1 por parcialidad).
    Cada liquidación lleva los datos a nivel FACTURA (para el grid: folio, fechas,
    renta) y los de su parcialidad (para la pestaña Parcialidades). La renta ya
    incluye Renta + Renta Variable (RENTA_SIN_IVA / RENTA_CON_IVA)."""
    try:
        parc = json.loads(row.get("PARCIALIDADES_JSON") or "[]")
    except Exception:
        parc = []
    try:
        parts = json.loads(row.get("PARTIDAS_JSON") or "[]")
    except Exception:
        parts = []
    # Nombre comercial del local, del texto de una partida ("identificado como INNVICTUS ubicado…").
    local = ""
    for p in parts:
        m = _RE_LOCAL_APP.search(str(p.get("descripcion") or ""))
        if m:
            local = m.group(1).strip(); break
    cfdi_saldo = _f(row.get("SALDO"))
    # Fecha de pago a mostrar en el grid = la del ÚLTIMO abono (el más reciente)
    # de esta factura. Si no hay pagos, queda vacía ("—" en el grid).
    _fps = [pp.get("fecha_pago") for pp in parc if pp.get("fecha_pago")]
    fecha_pago_ultima = a_fecha(max(_fps)) if _fps else ""
    base_liq = {
        "montooperacion": a_monto(row.get("RENTA_CON_IVA")),
        "monto_sin_iva": _f(row.get("RENTA_SIN_IVA")),
        "monto_final": _f(row.get("MONTO_FINAL")),
        "moneda": mapear(row.get("MONEDA"), MONEDA, "1"),
        "formapago": "1", "instrumentomonetario": "8",
        "partida": _s(row.get("RENTA_CONCEPTOS")),
        "contrato_inmoges": _s(row.get("CONTRATO_INMOGES")),
        "unidad": _s(row.get("UNIDAD")),
        "local": local or _s(row.get("UNIDAD")),
        "factura_url": _s(row.get("PDF_URL")),
        # --- datos a nivel FACTURA (para el grid) ---
        "_idcfdi": _s(row.get("ID_CFDI")),
        "folio": _s(row.get("FOLIO")),
        "fecha_emision": a_fecha(row.get("FECHA")),
        "fecha_timbre": a_fecha(row.get("FECHA_TIMBRE")),
        "fecha_pago_ultima": fecha_pago_ultima,   # última fecha de pago (grid)
        "cfdi_saldo": cfdi_saldo,
    }
    if not parc:
        # Factura sin pago registrado en la ventana: 1 liquidación "Sin pago".
        return [{**base_liq, "fechapago": a_fecha(row.get("FECHA")),
                 "metodo_pago": "", "num_parcialidad": "",
                 "saldo_anterior": _f(row.get("TOTAL")), "importe_pagado": 0.0,
                 "saldo_pendiente": cfdi_saldo, "_sin_pago": True,
                 "_pkey": (_s(row.get("ID_CFDI")), "")}]
    out = []
    for pp in parc:
        out.append({**base_liq,
                    "fechapago": a_fecha(pp.get("fecha_pago")),
                    "metodo_pago": _s(pp.get("metodo_pago")),
                    "num_parcialidad": _s(pp.get("num_parcialidad")),
                    "saldo_anterior": _f(pp.get("saldo_anterior")),
                    "importe_pagado": _f(pp.get("importe_pagado")),
                    "saldo_pendiente": _f(pp.get("saldo_pendiente")),
                    "_pkey": (_s(row.get("ID_CFDI")), _s(pp.get("num_parcialidad")))})
    return out


def _liquidaciones_reales(pagos: list, rfc: str, inmueble: str, id_contrato: str = "") -> list:
    """Pagos que pertenecen a un contrato. Regla:
      1) Si el contrato tiene pagos con SU CONTRATO_INMOGES exacto -> esos son los
         suyos y PUNTO (caso multi-tienda / multi-sucursal tipo INDITEX o los
         cines de OPERADORA DE CINEMAS, que comparten un mismo RFC). No se le
         pegan huérfanos de otros inmuebles del mismo RFC.
      2) Solo si NO hay empate exacto se usa la heurística histórica RFC +
         inmueble ESTRICTO (facturas de meses previos sin CONTRATO_INMOGES). Si
         ningún huérfano coincide con el inmueble, queda vacío — NO se jalan
         todos los huérfanos del RFC (ese era el bug que contaminaba contratos
         hermanos del mismo cliente)."""
    rfc = (rfc or "").strip().upper(); inm = (inmueble or "").strip().lower()
    idc = str(id_contrato or "").strip()
    cand = [p for p in pagos if str(p.get("RFC_RECEPTOR", "")).strip().upper() == rfc]
    exactos = [p for p in cand if idc and _s(p.get("CONTRATO_INMOGES")) == idc]
    if exactos:
        sel = exactos
    else:
        sin_id = [p for p in cand if not _s(p.get("CONTRATO_INMOGES"))]
        # Huérfanos SOLO si su inmueble coincide de forma estricta con el contrato.
        sel = [p for p in sin_id if inm in str(p.get("INMUEBLE_TXT", "")).strip().lower()] if inm else sin_id
    # CFDI ordena por fecha de EMISIÓN (col FECHA); PAGOS por FECHA_PAGO.
    sel = sorted(sel, key=lambda r: str(r.get("FECHA_PAGO") or r.get("FECHA") or ""))
    if USAR_CFDI:
        return [liq for p in sel for liq in _liqs_de_cfdi(p)]
    return [_map_liq(p) for p in sel]


def _benef_de_inmoges(fila: dict, entidad: dict | None) -> list:
    """PASO 1 de la cadena del beneficiario controlador: lo que traiga INMOGES.

    HOY DEVUELVE SIEMPRE [] — se verificó endpoint por endpoint (163 contratos,
    2026-08-13) que INMOGES no expone el campo: /arrendatario.pld_entidad trae el
    representante legal (rpm_*), el giro y el domicilio, pero CERO beneficiario
    controlador. Se deja escrita la cadena para que el día que INMOGES lo pueble gane
    automáticamente y ORIGEN empiece a decir 'INMOGES' solo, SIN tocar la app: esa
    será la señal de que el parche del Excel ya se puede retirar."""
    for src in (entidad or {}, fila or {}):
        nom = _s(src.get("BC_NOMBRE")) or _s(src.get("BENEFICIARIO_NOMBRE"))
        if nom:
            gv = lambda *ks: next((_s(src.get(k)) for k in ks if _s(src.get(k))), "")
            return [{"nombre": nom,
                     "apellidopaterno": gv("BC_APELLIDO_PATERNO", "BENEFICIARIO_APELLIDO_PAT"),
                     "apellidomaterno": gv("BC_APELLIDO_MATERNO", "BENEFICIARIO_APELLIDO_MAT"),
                     "fechanacimiento": a_fecha(src.get("BC_FECHA_NACIMIENTO") or src.get("BENEFICIARIO_FECHA_NAC")),
                     "rfc": gv("BC_RFC", "BENEFICIARIO_RFC"), "curp": gv("BC_CURP", "BENEFICIARIO_CURP"),
                     "paisnacionalidad_txt": gv("BC_PAIS_NACIONALIDAD", "BENEFICIARIO_PAIS"),
                     "origen": "INMOGES"}]
    return []


def construir_operacion(fila: dict, periodo="01-2026", pagos=None, form=None, entidad=None, benef=None) -> dict:
    g = lambda k: _s(fila.get(k))
    es_moral = g("TIPO_PERSONA").lower().startswith("m")
    razon = g("DENOMINACION_RAZON")
    cliente = {
        "tipo_persona": "Moral" if es_moral else "Fisica",
        "denominacionrazon": razon if es_moral else "", "nombre": "" if es_moral else razon,
        "apellidopaterno": "", "apellidomaterno": "",
        "rfc": g("RFC"), "curp": "", "fechanacimiento": "", "fechaconstitucion": "",
        "paisnacionalidad": g("PAIS_NACIONALIDAD") or "MX", "giromercantil": "", "actividadeconomica": "",
        "representante": {"nombre": "", "apellidopaterno": "", "apellidomaterno": "",
                          "fechanacimiento": "", "rfc": "", "curp": ""},
        "domicilio": {"colonia": g("CLI_COLONIA"), "calle": g("CLI_CALLE"), "numeroexterior": g("CLI_NO_EXTERIOR"),
                      "numerointerior": g("CLI_NO_INTERIOR"), "codigopostal": g("CLI_CODIGO_POSTAL")},
        "telefono": {"clavepais": g("TEL_CLAVE_PAIS") or "MX", "numerotelefono": g("TEL_NUMERO"),
                     "correoelectronico": mayus(g("CORREO"))},
        "identificadorunico": g("IDENTIFICADOR_UNICO"),
    }
    caracteristica = {
        "fechainicio": a_fecha(fila.get("FECHA_INICIO")), "fechatermino": a_fecha(fila.get("FECHA_TERMINO")),
        "tipoinmueble": mapear(g("INM_TIPO_TXT"), TIPO_INMUEBLE, ""),
        "valorreferencia": g("INM_VALOR_REFERENCIA"), "folioreal": g("INM_FOLIO_REAL") or "XXXX",
        "colonia": g("INM_COLONIA"), "calle": g("INM_CALLE"), "numeroexterior": g("INM_NO_EXTERIOR"),
        "numerointerior": g("INM_NO_INTERIOR"), "codigopostal": g("INM_CODIGO_POSTAL"),
    }
    # --- Pre-llenado desde la ENTIDAD PLD de INMOGES (pld_entidad); solo llena
    #     huecos: lo del contrato ya puesto arriba y la captura manual (form,
    #     que se aplica después) siempre ganan. ---
    if entidad:
        e = lambda k: _s(entidad.get(k))
        rep = cliente["representante"]
        rep["nombre"] = rep["nombre"] or e("RPM_NOMBRE")
        rep["apellidopaterno"] = rep["apellidopaterno"] or e("RPM_APELLIDO_PAT")
        rep["apellidomaterno"] = rep["apellidomaterno"] or e("RPM_APELLIDO_MAT")
        rep["fechanacimiento"] = rep["fechanacimiento"] or a_fecha(entidad.get("RPM_FECHA_NAC"))
        rep["rfc"] = rep["rfc"] or e("RPM_RFC")
        rep["curp"] = rep["curp"] or e("RPM_CURP")
        if not es_moral:
            cliente["fechanacimiento"] = cliente["fechanacimiento"] or a_fecha(entidad.get("PF_FECHA_NAC"))
            cliente["curp"] = cliente["curp"] or e("PF_CURP")
        cliente["giromercantil"] = cliente["giromercantil"] or e("GIRO_MERCANTIL")
        cliente["actividadeconomica"] = cliente["actividadeconomica"] or e("ACTIVIDAD_ECONOMICA")
        cliente["fechaconstitucion"] = cliente["fechaconstitucion"] or a_fecha(entidad.get("FECHA_CONSTITUCION"))
        dom = cliente["domicilio"]
        for k_dst, k_src in (("calle", "CALLE"), ("colonia", "COLONIA"),
                             ("numeroexterior", "NO_EXTERIOR"), ("numerointerior", "NO_INTERIOR")):
            dom[k_dst] = dom[k_dst] or e(k_src)
        tel = cliente["telefono"]
        tel["numerotelefono"] = tel["numerotelefono"] or re.sub(r"\D", "", e("TELEFONO"))
        tel["correoelectronico"] = tel["correoelectronico"] or mayus(e("CORREO"))

    liqs = _liquidaciones_reales(pagos, g("RFC"), g("INMUEBLE"), g("ID_CONTRATO")) if pagos else []
    pagos_reales = bool(liqs)
    # Sin pago real del periodo -> NO se usa el total del contrato como placeholder:
    # las liquidaciones quedan vacías y en la app se muestra "—" (evita confundir contrato con pago).
    # BENEFICIARIO CONTROLADOR — cadena de resolución (ver _benef_de_inmoges):
    #   1) INMOGES  ->  2) Snowflake (Excel de Legal, ORIGEN='Excel')  ->  3) nada.
    # Un contrato SIN beneficiario es un estado VÁLIDO: no bloquea ni se pide capturarlo.
    _bsrc = benef or {}
    bens = _benef_de_inmoges(fila, entidad) or _bsrc.get("por_folio", {}).get(g("ID_CONTRATO"), [])
    op = {
        "referenciaaviso": g("REFERENCIA_AVISO") or f"ARI{g('EMPRESA')}{g('ID_CONTRATO')}",
        "tipoalerta": "100", "prioridad": "1", "cliente": cliente, "beneficiarios": bens,
        "fechaoperacion": (liqs[0]["fechapago"] if liqs else "") or a_fecha(fila.get("FECHA_OPERACION")),
        "caracteristicas": [caracteristica], "liquidaciones": liqs,
        "_pagos_reales": pagos_reales, "_form_guardado": False,
        # ¿la fuente del beneficiario respondió? (False = vacío NO confiable)
        "_benef_ok": bool(_bsrc.get("ok", True)) or bool(bens),
        "_benef_error": "" if _bsrc.get("ok", True) else _s(_bsrc.get("error")),
        "_hints": {"giro_txt": g("GIRO_MERCANTIL_TXT"), "tipoinmueble_txt": g("INM_TIPO_TXT")},
    }
    if form:
        _overlay_form(op, form)
    return op


def _overlay_form(op: dict, form: dict) -> None:
    c = op["cliente"]; i = op["caracteristicas"][0]
    def pon(dst, key, val):
        v = _s(val)
        if v:
            dst[key] = v
    r = c["representante"]
    pon(r, "nombre", form.get("REP_NOMBRE")); pon(r, "apellidopaterno", form.get("REP_APELLIDO_PAT"))
    pon(r, "apellidomaterno", form.get("REP_APELLIDO_MAT")); pon(r, "fechanacimiento", form.get("REP_FECHA_NAC"))
    pon(r, "rfc", form.get("REP_RFC")); pon(r, "curp", form.get("REP_CURP"))
    pon(c["telefono"], "numerotelefono", form.get("TEL_NUMERO"))
    pon(c, "giromercantil", form.get("GIRO_MERCANTIL"))
    pon(i, "tipoinmueble", form.get("TIPO_INMUEBLE"))
    pon(i, "valorreferencia", form.get("INM_VALOR_REFERENCIA")); pon(i, "folioreal", form.get("INM_FOLIO_REAL"))
    for col, k in (("CLI_CALLE", "calle"), ("CLI_NO_EXTERIOR", "numeroexterior"),
                   ("CLI_COLONIA", "colonia"), ("CLI_CODIGO_POSTAL", "codigopostal")):
        pon(c["domicilio"], k, form.get(col))
    for col, k in (("INM_CALLE", "calle"), ("INM_NO_EXTERIOR", "numeroexterior"),
                   ("INM_COLONIA", "colonia"), ("INM_CODIGO_POSTAL", "codigopostal")):
        pon(i, k, form.get(col))
    # El beneficiario controlador YA NO sale del formulario: su única fuente es la
    # vista de Snowflake (Excel de Legal). Si se pudiera editar aquí habría dos
    # versiones del mismo dato y dejaría de saberse cuál es la buena.
    try:
        liq = json.loads(form.get("LIQUIDACIONES_JSON") or "[]")
        if liq: op["liquidaciones"] = liq
    except Exception:
        pass
    op["_form_guardado"] = True


def guardar_form(op: dict, empresa: str, id_contrato: str, payload=None, enviado=False, envio_status=None) -> None:
    c = op["cliente"]; i = op["caracteristicas"][0]; rep = c["representante"]
    fila = {
        "EMPRESA": empresa, "ID_CONTRATO": id_contrato, "RFC": c.get("rfc", ""),
        "DENOMINACION_RAZON": c.get("denominacionrazon") or c.get("nombre", ""),
        "REP_NOMBRE": rep["nombre"], "REP_APELLIDO_PAT": rep["apellidopaterno"], "REP_APELLIDO_MAT": rep["apellidomaterno"],
        "REP_FECHA_NAC": rep["fechanacimiento"], "REP_RFC": rep["rfc"], "REP_CURP": rep["curp"],
        "TEL_NUMERO": c["telefono"]["numerotelefono"], "GIRO_MERCANTIL": c["giromercantil"],
        "TIPO_INMUEBLE": i["tipoinmueble"], "INM_VALOR_REFERENCIA": i["valorreferencia"], "INM_FOLIO_REAL": i["folioreal"],
        "CLI_CALLE": c["domicilio"]["calle"], "CLI_NO_EXTERIOR": c["domicilio"]["numeroexterior"],
        "CLI_COLONIA": c["domicilio"]["colonia"], "CLI_CODIGO_POSTAL": c["domicilio"]["codigopostal"],
        "INM_CALLE": i["calle"], "INM_NO_EXTERIOR": i["numeroexterior"], "INM_COLONIA": i["colonia"], "INM_CODIGO_POSTAL": i["codigopostal"],
        # BENEFICIARIOS_JSON ya NO se escribe: el beneficiario controlador es de solo
        # lectura y su fuente es la vista de Snowflake, no la captura manual.
        "LIQUIDACIONES_JSON": json.dumps(op.get("liquidaciones", []), ensure_ascii=False),
        "PAYLOAD_JSON": json.dumps(payload, ensure_ascii=False) if payload else None,
        "ENVIADO": bool(enviado), "ENVIO_STATUS": str(envio_status) if envio_status is not None else None,
        "ENVIO_FECHA": datetime.datetime.now().strftime("%Y-%m-%d %H:%M") if enviado else None,
    }
    cols = list(fila.keys())
    ph = ", ".join(["%s"] * len(cols))
    # Reusa la conexión cacheada (el handshake de ~2 s era casi todo el "Guardar").
    # Reintento seguro: DELETE+INSERT de la misma llave es idempotente, así que
    # si la sesión cacheada murió se rehace completo con conexión fresca.
    for intento in (1, 2):
        conn = _sf_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {ESQUEMA}.PLD_AVISO_FORM WHERE EMPRESA=%s AND ID_CONTRATO=%s", (empresa, id_contrato))
            cur.execute(f"INSERT INTO {ESQUEMA}.PLD_AVISO_FORM ({', '.join(cols)}) VALUES ({ph})", [fila[k] for k in cols])
            cur.close()
            return
        except Exception:
            _sf_conn.clear()          # conexión muerta -> se descarta y se reintenta una vez
            if intento == 2:
                raise
