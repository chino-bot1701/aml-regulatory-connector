"""Business logic: SAT catalogues, AML report payload, validation.

Pure functions. No Streamlit, no database, no network — so the rules
that decide what gets reported can be tested on their own.
"""
from __future__ import annotations

import datetime
import re

# ============================================================================ #
#  1) LÓGICA — catálogos SAT, armado del JSON y validación
# ============================================================================ #
def a_fecha(v) -> str:
    if v in (None, ""):
        return ""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y%m%d")
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    return s


def a_monto(v) -> str:
    try:
        return f"{float(str(v).replace(',', '').strip()):.2f}"
    except (ValueError, TypeError):
        return str(v or "")


def _f(v) -> float:
    """float tolerante (ignora comas y $); nunca crashea -> 0.0. Evita romper la
    app si el usuario captura un monto con formato (p.ej. '1,500.00') en el editor."""
    try:
        return float(str(v).replace(",", "").replace("$", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def mayus(s) -> str:
    return ("" if s is None else str(s)).strip().upper()


FORMA_PAGO = {"contado": "1", "credito": "2", "crédito": "2"}
INSTRUMENTO = {"transferencia": "8", "transferencia interbancaria": "8",
               "efectivo": "1", "cheque": "2", "tarjeta": "4"}
MONEDA = {"peso": "1", "peso mexicano": "1", "mxn": "1", "dolar": "2", "usd": "2"}
TIPO_INMUEBLE = {"local en centro comercial": "7", "local": "7", "oficina": "3",
                 "bodega": "5", "casa": "1", "departamento": "2", "terreno": "6"}
# País del beneficiario controlador: Legal lo captura como TEXTO LIBRE ("Mexicana",
# "Chilena") y el portal de avisos UIF espera la clave UIF de 2 letras. El mapeo se hace aquí, al
# armar el payload; en pantalla se muestra el texto original de Legal.
PAIS_NACIONALIDAD = {"MEXICANA": "MX", "MEXICO": "MX", "CHILENA": "CL", "CHILE": "CL",
                     "CHINA": "CN", "ESTADOUNIDENSE": "US", "ESTADOS UNIDOS": "US",
                     "ESPAÑOLA": "ES", "ESPANOLA": "ES", "ESPAÑA": "ES"}


def _clave_pais(txt) -> str:
    """Texto libre -> clave UIF de 2 letras. Si NO mapea devuelve VACÍO a propósito:
    el dato viaja a un aviso regulatorio, y un hueco visible es mejor que un valor
    inventado ante la UIF. Si ya viene una clave de 2 letras, se respeta."""
    s = mayus(txt).translate(str.maketrans("ÁÉÍÓÚ", "AEIOU"))
    if len(s) == 2 and s.isalpha():
        return s
    return PAIS_NACIONALIDAD.get(s, "")


def mapear(valor, tabla: dict, default: str = "") -> str:
    if not valor:
        return default
    s = str(valor).replace("\xa0", " ").strip()
    if "-" in s and s.split("-", 1)[0].strip().isalnum():
        return s.split("-", 1)[0].strip()
    return tabla.get(s.lower(), default)


def build_payload(op: dict) -> dict:
    c = op["cliente"]
    rep = {"nombre": c["representante"]["nombre"],
           "apellidopaterno": c["representante"]["apellidopaterno"],
           "apellidomaterno": c["representante"]["apellidomaterno"] or "XXXX",
           "fechanacimiento": a_fecha(c["representante"]["fechanacimiento"])}
    if c["representante"].get("rfc"):  rep["rfc"] = c["representante"]["rfc"]
    if c["representante"].get("curp"): rep["curp"] = c["representante"]["curp"]

    if c["tipo_persona"] == "Moral":
        persona = {"personamoral": {"denominacionrazon": c["denominacionrazon"], "rfc": c["rfc"],
                   "paisnacionalidad": c.get("paisnacionalidad", "MX"),
                   "giromercantil": c["giromercantil"], "representanteapoderado": rep}}
    else:
        persona = {"personafisica": {"nombre": c["nombre"], "apellidopaterno": c["apellidopaterno"],
                   "apellidomaterno": c["apellidomaterno"] or "XXXX",
                   "fechanacimiento": a_fecha(c["fechanacimiento"]), "rfc": c["rfc"], "curp": c["curp"],
                   "paisnacionalidad": c.get("paisnacionalidad", "MX"), "actividadeconomica": c["actividadeconomica"]}}

    dom = {"colonia": c["domicilio"]["colonia"], "calle": c["domicilio"]["calle"],
           "numeroexterior": c["domicilio"]["numeroexterior"], "codigopostal": c["domicilio"]["codigopostal"]}
    if c["domicilio"].get("numerointerior"):
        dom["numerointerior"] = c["domicilio"]["numerointerior"]

    tel = {"clavepais": c["telefono"].get("clavepais", "MX"), "numerotelefono": c["telefono"]["numerotelefono"]}
    if c["telefono"].get("correoelectronico"):
        tel["correoelectronico"] = mayus(c["telefono"]["correoelectronico"])

    payload = {
        "referenciaaviso": op["referenciaaviso"],
        "alerta": {"tipoalerta": op.get("tipoalerta", "100")},
        "prioridad": op.get("prioridad", "1"),
        "personaaviso": [{"tipopersona": persona, "tipodomicilio": {"domicilionacional": dom},
                          "telefono": tel, "identificadorunico": c["identificadorunico"]}],
        "datosoperacion": {
            "fechaoperacion": a_fecha(op["fechaoperacion"]),
            "caracteristicas": [{
                "fechainicio": a_fecha(i["fechainicio"]), "fechatermino": a_fecha(i["fechatermino"]),
                "tipoinmueble": i["tipoinmueble"], "valorreferencia": a_monto(i["valorreferencia"]),
                "colonia": i["colonia"], "calle": i["calle"], "numeroexterior": i["numeroexterior"],
                "numerointerior": i.get("numerointerior", ""), "codigopostal": i["codigopostal"],
                "folioreal": i["folioreal"] or "XXXX"} for i in op["caracteristicas"]],
            "datosliquidacion": [{
                "fechapago": a_fecha(l["fechapago"]), "formapago": l["formapago"],
                "instrumentomonetario": l["instrumentomonetario"], "moneda": l["moneda"],
                "montooperacion": a_monto(l["montooperacion"])} for l in op["liquidaciones"]],
        },
    }
    # DUEÑO/BENEFICIARIO CONTROLADOR (solo lectura; fuente: Excel de Legal en Snowflake).
    # La llave lleva Ñ ("dueñobeneficiario") porque refleja el modelo C# DueñoBeneficiario;
    # el manual de el portal de avisos UIF la OMITE. Va al primer nivel y es un ARREGLO (preparado para
    # N beneficiarios aunque hoy siempre venga uno por contrato).
    # SIN beneficiario -> la llave NO se manda (ni arreglo vacío ni objeto con nulos).
    bens = beneficiarios_validos(op)
    if bens:
        payload["dueñobeneficiario"] = [_benef_payload(b) for b in bens]
    return payload


def beneficiarios_validos(op: dict) -> list:
    return [b for b in (op.get("beneficiarios") or []) if mayus(b.get("nombre"))]


def _benef_payload(b: dict) -> dict:
    """Un beneficiario -> su bloque del payload ARI.
      · fechanacimiento en AAAAMMDD (la vista lo entrega como DATE).
      · apellidomaterno vacío -> "XXXX" (convención del resto del payload).
      · rfc/curp son OPCIONALES: se OMITE la llave si no hay dato (no cadena vacía).
        Hoy BC_RFC viene vacío en los 14 registros de EDN; NO se deriva de la CURP
        (saldría sin homoclave = RFC inválido)."""
    pf = {"nombre": mayus(b.get("nombre")),
          "apellidopaterno": mayus(b.get("apellidopaterno")),
          "apellidomaterno": mayus(b.get("apellidomaterno")) or "XXXX",
          "fechanacimiento": a_fecha(b.get("fechanacimiento"))}
    for k in ("rfc", "curp"):
        v = mayus(b.get(k))
        if v:
            pf[k] = v
    pf["paisnacionalidad"] = _clave_pais(b.get("paisnacionalidad_txt") or b.get("paisnacionalidad"))
    return {"tipopersona": {"personafisica": pf}}


def validate(op: dict) -> list:
    e, c = [], op["cliente"]
    def f8(s): return len(a_fecha(s)) == 8 and a_fecha(s).isdigit()
    if c["tipo_persona"] == "Moral":
        if not c["denominacionrazon"]: e.append(("razón social", "obligatoria"))
        if not c["giromercantil"]:     e.append(("giro mercantil", "catálogo obligatorio"))
        r = c["representante"]
        if not r["nombre"]:          e.append(("representante.nombre", "obligatorio"))
        if not r["apellidopaterno"]: e.append(("representante.apellidopaterno", "obligatorio"))
        if not f8(r["fechanacimiento"]): e.append(("representante.fechanacimiento", "AAAAMMDD inválida"))
    else:
        if not c["nombre"]:             e.append(("nombre", "obligatorio"))
        if not c["curp"]:               e.append(("CURP", "obligatoria"))
        if not c["actividadeconomica"]: e.append(("actividad económica", "catálogo obligatorio"))
    if not c["rfc"]: e.append(("RFC", "obligatorio"))
    d = c["domicilio"]
    for k in ("colonia", "calle", "numeroexterior", "codigopostal"):
        if not d[k]: e.append((f"domicilio.{k}", "obligatorio"))
    if not c["telefono"]["numerotelefono"]: e.append(("teléfono", "obligatorio"))
    if not c["identificadorunico"]: e.append(("identificador único", "obligatorio"))
    if not op["caracteristicas"]: e.append(("inmueble", "falta al menos uno"))
    for idx, i in enumerate(op["caracteristicas"]):
        if not (f8(i["fechainicio"]) and f8(i["fechatermino"])): e.append((f"inmueble[{idx}].fechas", "inválidas"))
        if not i["tipoinmueble"]:    e.append((f"inmueble[{idx}].tipoinmueble", "catálogo obligatorio"))
        if not i["valorreferencia"]: e.append((f"inmueble[{idx}].valorreferencia", "obligatorio (catastral)"))
        if not i["folioreal"]:       e.append((f"inmueble[{idx}].folioreal", "obligatorio (o XXXX)"))
        for k in ("colonia", "calle", "numeroexterior", "codigopostal"):
            if not i[k]: e.append((f"inmueble[{idx}].{k}", "obligatorio"))
    if not op["liquidaciones"]: e.append(("liquidación", "falta al menos un pago"))
    for idx, l in enumerate(op["liquidaciones"]):
        if not f8(l["fechapago"]):        e.append((f"liquidacion[{idx}].fechapago", "inválida"))
        if not l["formapago"]:            e.append((f"liquidacion[{idx}].formapago", "catálogo obligatorio"))
        if not l["instrumentomonetario"]: e.append((f"liquidacion[{idx}].instrumentomonetario", "obligatorio"))
        if not l["moneda"]:               e.append((f"liquidacion[{idx}].moneda", "obligatoria"))
        if not l["montooperacion"]:       e.append((f"liquidacion[{idx}].montooperacion", "obligatorio"))
    return e
