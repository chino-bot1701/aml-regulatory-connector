"""
Conector INMOGES–el portal de avisos UIF · ALMENA  —  App ÚNICA (todo en este archivo).

Qué hace (SIN API de INMOGES):
  • Lee Snowflake DB_ANALYTICS.SCH_PLD: PLD_AVISO (datos) y
    PLD_AVISO_PAGOS (rentas reales del periodo).
  • Pre-carga lo capturado a mano por cliente desde PLD_AVISO_FORM
    (llave EMPRESA + ID_CONTRATO) y lo guarda al "Guardar datos" / "Enviar".
  • Arma el JSON y lo ENVÍA a el portal de avisos UIF (POST).

La ingesta de las 2 primeras tablas la hacen aparte: ingesta_inmoges.py / ingesta_pagos.py.

Ejecuta:  py -m streamlit run app.py
"""
from __future__ import annotations
import os
import re
import sys
import json
import datetime
from pathlib import Path

import requests
import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------- #
#  Autenticacion Cognito (utils/auth) — carga de .env y de modulos de auth
#  Ver guia completa en utils/auth/README.md
# ---------------------------------------------------------------------------- #
# Cargar .env solo si existe (en Docker las vars vienen del compose/--env-file).
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_ENV_FILE, override=False)

# Garantizar que Python encuentre el paquete utils/auth/ junto a este archivo.
_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from utils.auth.cognito_auth import CognitoAuth
from utils.auth.config import AuthConfig, ConfigurationError


def get_auth() -> CognitoAuth:
    """Inicializa y retorna la instancia de autenticacion Cognito.

    La configuracion se cachea en session_state para no recargarla en cada
    rerun; el objeto CognitoAuth se re-crea cada vez porque usa widgets de
    Streamlit (cookies)."""
    if "auth_config" not in st.session_state:
        try:
            config = AuthConfig.from_env()
            config.redirect_uri = os.getenv("SITIO_UIF", "http://localhost:8501")
            st.session_state["auth_config"] = config
        except ConfigurationError as e:
            st.error(f"Error de configuracion de autenticacion: {e}")
            st.stop()
    return CognitoAuth(st.session_state["auth_config"])

# The logic, the regulator client and the warehouse layer live in
# their own modules. DEMO_MODE swaps the warehouse for local fixtures.
from logica import *      # noqa: F401,F403
from regulador import *   # noqa: F401,F403
if os.environ.get("DEMO_MODE") == "1":
    from demo.almacen_demo import *   # noqa: F401,F403
else:
    from almacen import *  # noqa: F401,F403

# ============================================================================ #
#  4) UI — Streamlit
# ============================================================================ #
st.set_page_config(page_title="Conector INMOGES–el portal de avisos UIF · ALMENA", page_icon="🔻",
                   layout="wide", initial_sidebar_state="expanded")

ROJO, NEGRO, CREMA, BLANCO = "#F15D4D", "#212222", "#E7DFD1", "#FFFFFF"
VERDE_OK, FONDO_OK, FONDO_ERR = "#2e7d32", "#eef6ee", "#fbeaea"

ID_SUJETO = {"HL": 2, "HPI": 14, "PRUEBA (Josselyn)": 25}
st.session_state.setdefault("op", None)
st.session_state.setdefault("confirmar_envio", False)
st.session_state.setdefault("envio_res", None)
st.session_state.setdefault("grid_n", 0)

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@500;600;700;800&display=swap');
/* Tipografía oficial Almena: Gotham (fallback Montserrat) · interletrado 0 */
html, body, [class*="css"], .stMarkdown {{ font-family:'Gotham','Montserrat',Arial,sans-serif; letter-spacing:0; }}
h1, h2, h3, h4 {{ color:{NEGRO}; font-weight:800; }}
#MainMenu, footer {{ visibility:hidden; }}
[data-testid="stSidebarCollapseButton"] svg, [data-testid="collapsedControl"] svg {{ color:{ROJO} !important; }}
/* Sube el contenido: elimina el espacio muerto que Streamlit deja arriba */
header[data-testid="stHeader"] {{ background:transparent; }}
[data-testid="stAppViewContainer"] > .main .block-container,
.block-container {{ padding-top:0.8rem; padding-bottom:0.5rem; }}
div[data-testid="stDialog"] div[role="dialog"] {{ max-height:88vh; overflow-y:auto; }}
.topbar {{ background:{NEGRO}; border-radius:16px; padding:14px 24px; display:flex; align-items:center; gap:20px; margin-bottom:16px; box-shadow:0 2px 10px rgba(0,0,0,.12); }}
.wm {{ color:{CREMA}; font-weight:800; font-size:28px; letter-spacing:0; white-space:nowrap;
  font-family:'Gotham','Montserrat',Arial,sans-serif; }}
.wm span {{ color:{ROJO}; margin-right:6px; }}
.ttl {{ color:#fff; font-weight:800; font-size:17px; border-left:2px solid #444; padding-left:20px; }}
.ttl small {{ display:block; color:{CREMA}; font-weight:600; font-size:11px; opacity:.75; letter-spacing:.5px; }}
.kpi-wrap {{ margin-left:auto; display:flex; flex-direction:column; align-items:flex-end; gap:6px; }}
.topbar-user {{ color:{CREMA}; font-size:12px; font-weight:600; opacity:.85; text-align:right; white-space:nowrap; }}
.kpi-row {{ display:flex; align-items:center; }}
.kpi-chip {{ background:rgba(255,255,255,0.12); color:{CREMA}; padding:4px 12px; border-radius:20px; font-size:12px; font-weight:700; margin-left:8px; }}
.kpi-chip.err {{ background:{ROJO}; color:{NEGRO}; }}
.kpi-chip.ok  {{ background:{VERDE_OK}; color:#fff; }}
.card {{ background:#fff; border:1px solid {CREMA}; border-left:6px solid {ROJO}; border-radius:14px; padding:14px 18px; margin-bottom:12px; box-shadow:0 1px 6px rgba(0,0,0,.06); }}
.card h4 {{ margin:0 0 10px; font-size:15px; display:flex; justify-content:space-between; align-items:center; }}
.row {{ display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px dashed #efe9df; gap:16px; }}
.row:last-child {{ border-bottom:none; }}
.lbl {{ color:#8a8a8a; font-size:12px; font-weight:600; }}
.val {{ color:{NEGRO}; font-size:13px; font-weight:700; text-align:right; }}
.badge {{ background:{NEGRO}; color:{CREMA}; padding:2px 10px; border-radius:12px; font-size:11px; font-weight:700; }}
.badge.manual {{ background:{ROJO}; color:{NEGRO}; }}
.infobox {{ background:{CREMA}; border-left:6px solid {ROJO}; color:{NEGRO}; padding:10px 14px; border-radius:10px; font-weight:600; margin-bottom:12px; }}
.okbox {{ background:{FONDO_OK}; border-left:6px solid {VERDE_OK}; color:{NEGRO}; padding:10px 14px; border-radius:10px; font-weight:600; margin-bottom:12px; }}
.errbox {{ background:{FONDO_ERR}; border-left:6px solid {ROJO}; color:{NEGRO}; padding:10px 14px; border-radius:10px; font-weight:600; margin-bottom:8px; }}
.kpi {{ background:{NEGRO}; border-radius:14px; padding:14px 12px; text-align:center; }}
.kpi-val {{ color:#fff; font-size:22px; font-weight:800; line-height:1.1; }}
.kpi-lbl {{ color:{CREMA}; font-size:10px; font-weight:600; margin-top:5px; letter-spacing:.4px; }}
.stButton button {{ width:100%; font-weight:700; font-size:13px; border-radius:10px; }}
.footer {{ text-align:right; color:#9a9a9a; font-weight:800; letter-spacing:2px; font-size:11px; margin-top:14px; border-top:2px solid {CREMA}; padding-top:8px; }}

/* ---- Wizard del formulario (secciones navegables) ---- */
div[data-testid="stSegmentedControl"] button[kind="segmented_controlActive"] {{ background:{ROJO} !important; color:{NEGRO} !important; }}

/* ---- Login ---- */
.login-card {{ background:{NEGRO}; border-radius:24px; padding:46px 40px 34px; text-align:center;
  box-shadow:0 14px 50px rgba(0,0,0,.30); border-top:7px solid {ROJO}; margin-top:9vh; }}
.login-logo {{ color:{CREMA}; font-weight:800; font-size:58px; letter-spacing:3px; line-height:1; }}
.login-logo span {{ color:{ROJO}; }}
.login-sub {{ color:{ROJO}; font-weight:800; font-size:13px; letter-spacing:7px; margin-top:2px; }}
.login-ttl {{ color:#fff; font-weight:800; font-size:20px; margin-top:26px; }}
.login-small {{ color:{CREMA}; font-weight:600; font-size:11px; letter-spacing:1.5px; opacity:.7; margin-top:6px; }}
.login-card hr {{ border:none; border-top:1px solid #3a3a3a; margin:18px 0 4px; }}
</style>
""", unsafe_allow_html=True)


def _fh(aaaammdd):
    s = str(aaaammdd or "")
    return f"{s[6:8]}/{s[4:6]}/{s[0:4]}" if len(s) == 8 and s.isdigit() else (s or "—")


def _direccion(d):
    p = f"{d.get('calle','')} {d.get('numeroexterior','')}".strip()
    if d.get("numerointerior"):
        p += f" Int. {d['numerointerior']}"
    extra = ", ".join(x for x in [d.get("colonia", ""), f"CP {d['codigopostal']}" if d.get("codigopostal") else ""] if x)
    return ", ".join(x for x in [p, extra] if x) or "—"


def kpi(col, valor, etiqueta):
    col.markdown(f"<div class='kpi'><div class='kpi-val'>{valor}</div><div class='kpi-lbl'>{etiqueta}</div></div>", unsafe_allow_html=True)


def tarjeta(titulo, filas, badge_txt="SNOWFLAKE", manual=False):
    rows = "".join(f"<div class='row'><span class='lbl'>{l}</span><span class='val'>{(v if v not in (None,'') else '—')}</span></div>" for l, v in filas)
    cls = "badge manual" if manual else "badge"
    st.markdown(f"<div class='card'><h4>{titulo}<span class='{cls}'>{badge_txt}</span></h4>{rows}</div>", unsafe_allow_html=True)


@st.cache_data(show_spinner="Consultando Snowflake…", ttl=300)
def _tabla_sf():
    return leer_tabla()


@st.cache_data(show_spinner=False, ttl=300)
def _pagos_sf(periodo):
    return leer_pagos(periodo)


@st.cache_data(show_spinner=False, ttl=300)
def _cfdi_sf(periodo):
    return leer_cfdi(periodo)


def _fuente_pagos(periodo):
    """Datos del periodo: CFDI (facturas, nuevo) o PAGOS (viejo) según USAR_CFDI."""
    return _cfdi_sf(periodo) if USAR_CFDI else _pagos_sf(periodo)


@st.cache_data(show_spinner=False, ttl=60)
def _form_todos():
    return leer_form_todos()


@st.cache_data(show_spinner=False, ttl=300)
def _entidad_sf():
    return leer_entidad()


@st.cache_data(show_spinner=False, ttl=300)
def _benef_sf():
    """Beneficiario controlador (solo cambia cuando Legal recarga su Excel)."""
    return leer_beneficiarios()


def render_topbar(op, user_email=""):
    kpi_html = ""
    if op:
        total = sum(_f(l["montooperacion"]) for l in op["liquidaciones"])
        faltan = validate(op)
        kpi_html = (f"<span class='kpi-chip'>{len(op['liquidaciones'])} pagos</span>"
                    f"<span class='kpi-chip {'err' if faltan else 'ok'}'>{len(faltan)} faltantes</span>"
                    f"<span class='kpi-chip'>${total:,.0f}</span>")
    user_html = f"<div class='topbar-user'>{user_email}</div>" if user_email else ""
    st.markdown("<div class='topbar'><div class='wm'><span>▲</span>ALMENA</div>"
                "<div class='ttl'>Conector INMOGES–el portal de avisos UIF<small>PLD · ARRENDAMIENTO DE INMUEBLES</small></div>"
                f"<div class='kpi-wrap'>{user_html}<div class='kpi-row'>{kpi_html}</div></div></div>", unsafe_allow_html=True)


def render_consulta(filas, empresas_sel, periodo, id_sujeto):
    if not filas:
        st.warning("La tabla PLD_AVISO no tiene filas."); return
    if not empresas_sel:
        st.warning("Selecciona al menos una empresa."); return
    seleccionadas = set(empresas_sel)
    pagos = _fuente_pagos(periodo)
    tabla = operaciones_para_grid([r for r in filas if _s(r.get("EMPRESA")) in seleccionadas], pagos)
    if not tabla:
        st.warning("No hay operaciones para las empresas seleccionadas."); return

    est = _form_todos()
    for f in tabla:
        e = est.get((f["Empresa"], f["Contrato"]))
        f["Estatus"] = (f"Enviado {_s(e.get('ENVIO_FECHA'))}" if e and e.get("ENVIADO")
                        else "Guardado" if e else "Pendiente")

    with st.expander("Filtros", expanded=True):
        fcol = st.columns([1.2, 2, 2, 1.2, 1.2, 1.2, 0.9])
        f_emp = fcol[0].multiselect("Empresa", sorted({f["Empresa"] for f in tabla}), key="f_emp", placeholder="Todas")
        f_cli = fcol[1].multiselect("Razón social", sorted({f["Cliente"] for f in tabla}), key="f_cli", placeholder="Todos")
        f_inm = fcol[2].multiselect("Inmueble", sorted({f["Inmueble"] for f in tabla if f["Inmueble"]}), key="f_inm", placeholder="Todos")
        f_con = fcol[3].multiselect("Contrato", sorted({f["Contrato"] for f in tabla}), key="f_con", placeholder="Todos")
        f_fol = fcol[4].multiselect("Folio", sorted({f.get("Folio") for f in tabla if f.get("Folio")}), key="f_fol", placeholder="Todos")
        f_est = fcol[5].multiselect("Envío el portal de avisos UIF", ["Pendiente", "Guardado", "Enviado"], key="f_est", placeholder="Todos")
        fcol[6].markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if fcol[6].button("Limpiar", use_container_width=True):
            for k in ("f_emp", "f_cli", "f_inm", "f_con", "f_fol", "f_est"):
                st.session_state.pop(k, None)
            st.rerun()

    def keep(f):
        if f_emp and f["Empresa"] not in f_emp: return False
        if f_cli and f["Cliente"] not in f_cli: return False
        if f_inm and f["Inmueble"] not in f_inm: return False
        if f_con and f["Contrato"] not in f_con: return False
        if f_fol and f.get("Folio") not in f_fol: return False
        es = "Enviado" if f["Estatus"].startswith("Enviado") else f["Estatus"]
        if f_est and es not in f_est: return False
        return True

    vista = [f for f in tabla if keep(f)]
    if not vista:
        st.warning("Los filtros no coinciden con ninguna operación."); return

    # 1 FILA POR FACTURA (CFDI): cada factura puede tener N parcialidades (se ven en
    # la pestaña Parcialidades); aquí se colapsa a una sola fila por CFDI. El fallback
    # (path viejo PAGOS, sin _idcfdi) agrupa por cliente/contrato/inmueble/fecha.
    grouped = {}
    for f in vista:
        key = f.get("_idcfdi") or (f["Empresa"], f["Cliente"], f["Contrato"], f["Inmueble"], f.get("Fecha"))
        if key not in grouped:
            grouped[key] = f
    vista_final = list(grouped.values())

    st.caption("Selecciona una fila para abrir el formulario de ese cliente. "
               "(1 fila por factura; el desglose de parcialidades está en el formulario)")
    df = pd.DataFrame([{"Empresa": f["Empresa"], "Razón social": f["Cliente"],
                        "Cliente (local)": f.get("Local", "—"),
                        "Inmueble": f["Inmueble"],
                        "Fecha timbre": f.get("FechaTimbre", "—"),
                        "Fecha Pago": f.get("Fecha", "—"),
                        "Monto": "—" if f.get("MontoSinIVA") is None else f"${f['MontoSinIVA']:,.2f}",
                        "Monto + IVA": "—" if f.get("Monto") is None else (f"${f['Monto']:,.2f}" + (f" {f['Moneda']}" if f.get("Moneda") else "")),
                        "Monto Final": "—" if f.get("MontoFinal") is None else (f"${f['MontoFinal']:,.2f}" + (f" {f['Moneda']}" if f.get("Moneda") else "")),
                        "Pago": f.get("Pago", "—"),
                        "Partida (concepto)": f.get("Partida", "—"),
                        "Contrato": f["Contrato"],
                        "Envío el portal de avisos UIF": f["Estatus"] + (" ⚠ sin contrato" if f.get("_sin_contrato") else ""),
                        "Folio": f.get("Folio", "—"),
                        "Factura": f.get("Factura", "")} for f in vista_final])
    sel = st.dataframe(df, hide_index=True, use_container_width=True, height=430,
                       column_config={"Factura": st.column_config.LinkColumn("Factura", display_text="Ver")},
                       on_select="rerun", selection_mode="single-row", key=f"grid_{st.session_state['grid_n']}")
    rows = sel["selection"]["rows"]
    if rows and rows[0] < len(vista_final):
        f = vista_final[rows[0]]
        # FIX reapertura + FIX clic perdido:
        #  1) Se renueva la key de la rejilla (selección limpia -> el MISMO cliente
        #     se puede reabrir con 1 clic).
        #  2) La rejilla nueva se renderiza ANTES de abrir el modal (st.rerun +
        #     _abrir_modal): el reemplazo del widget ocurre tapado por el modal,
        #     no al cerrarlo. Antes, si se hacía clic mientras la rejilla se
        #     reconstruía (~1 s tras cerrar el modal), el clic caía en el widget
        #     viejo y se perdía ("carga un segundo y no pasa nada").
        st.session_state["grid_n"] += 1
        st.session_state["op"] = None
        st.session_state["op_meta"] = {"fila": f["_fila"], "empresa": f["Empresa"], "cliente": f["Cliente"],
                                       "contrato": f["Contrato"], "periodo": periodo}
        st.session_state["confirmar_envio"] = False
        st.session_state["envio_res"] = None
        st.session_state["form_sec"] = 0   # el formulario abre en la primera sección
        for _k in ("dc_calle", "dc_next", "dc_col", "dc_cp", "di_calle", "di_next", "di_col", "di_cp",
                   "liq", "sec_nav", "_sec_pending"):
            st.session_state.pop(_k, None)
        st.session_state["_abrir_modal"] = True
        st.rerun()

    # El modal abre en el ciclo SIGUIENTE a la selección, con la rejilla ya re-creada.
    if st.session_state.pop("_abrir_modal", False):
        modal_cliente(id_sujeto)


def _guardar(op, meta, payload=None, enviado=False, envio_status=None):
    guardar_form(op, meta.get("empresa", ""), meta.get("contrato", ""), payload=payload, enviado=enviado, envio_status=envio_status)
    _form_todos.clear()


def _rerun_dialogo():
    """Rerun SOLO del diálogo (fragment) para cambiar de sección SIN cerrarlo.
    (st.rerun() a secas cerraría la ventana modal)."""
    try:
        st.rerun(scope="fragment")
    except TypeError:      # Streamlit antiguo sin parámetro scope
        st.rerun()


SECCIONES_FORM = ["Representante legal", "Contacto y catálogos", "Domicilio cliente",
                  "Domicilio inmueble", "Pagos", "Beneficiarios"]
# "Beneficiarios" es la ÚLTIMA y es de SOLO LECTURA (no se captura ni se guarda).
SEC_BENEFICIARIOS = len(SECCIONES_FORM) - 1
SEC_ULTIMA_EDITABLE = SEC_BENEFICIARIOS - 1      # "Pagos"


def _seccion_beneficiarios(op: dict) -> None:
    """Beneficiario controlador — SOLO LECTURA.

    Fuente única: V_PLD_BENEFICIARIO_CONTROLADOR (Excel de Legal cargado a Snowflake).
    No se edita ni se guarda desde la app: si el dato se pudiera tocar en dos lugares
    —el Excel de Legal y la app— las dos versiones se separan y deja de saberse cuál
    es la buena. Si falta uno, se corrige en el Excel y se recarga."""
    bens = op.get("beneficiarios") or []
    if not op.get("_benef_ok", True):
        # FALLO DE LECTURA != SIN BENEFICIARIO. Nunca se muestran iguales.
        st.markdown("<div class='errbox'>⚠ NO SE PUDO CONSULTAR la fuente del beneficiario "
                    "controlador. <b>Este vacío NO significa que el contrato no tenga uno</b> — "
                    "significa que no pudimos verificarlo. No envíes el aviso hasta resolverlo.</div>",
                    unsafe_allow_html=True)
        with st.expander("Detalle técnico del error"):
            st.code(op.get("_benef_error") or "(sin detalle)")
            st.caption("Causa más común: el rol de la app perdió el SELECT sobre "
                       "`SCH_CORE.V_PLD_BENEFICIARIO_CONTROLADOR`.")
        if st.button("Reintentar consulta", key="benef_retry"):
            _benef_sf.clear()          # el error se cachea 300 s; esto lo purga ya
            st.session_state["op"] = None
            _rerun_dialogo()
    elif not bens:
        st.info("Sin beneficiario controlador registrado para este contrato. "
                "Es un estado válido: **no bloquea el envío**. Si debería tener uno, "
                "se agrega en el Excel de Legal y se recarga — no se captura aquí. "
                "(La fuente SÍ respondió: este vacío es real.)")
    else:
        st.dataframe(pd.DataFrame([{
            "Nombre": b.get("nombre", ""),
            "Apellido paterno": b.get("apellidopaterno", ""),
            "Apellido materno": b.get("apellidomaterno", "") or "—",
            "Fecha nac.": _fh(b.get("fechanacimiento")),
            "RFC": b.get("rfc", "") or "—",
            "CURP": b.get("curp", "") or "—",
            "País": b.get("paisnacionalidad_txt", "") or "—",
            "Origen": b.get("origen", "") or "—"} for b in bens]),
            hide_index=True, use_container_width=True)
        sin_mapa = sorted({b.get("paisnacionalidad_txt", "") for b in bens if not _clave_pais(
            b.get("paisnacionalidad_txt") or b.get("paisnacionalidad"))})
        if sin_mapa:
            st.warning(f"País sin equivalencia en el catálogo UIF: **{', '.join(sin_mapa) or '(vacío)'}**. "
                       f"Se enviará el campo VACÍO (no se inventa un valor). Avisa para agregarlo al catálogo.")
        if any(not b.get("rfc") for b in bens):
            st.caption("ℹ️ RFC vacío: el Excel de Legal no lo trae. Es opcional en el aviso y "
                       "NO se deriva de la CURP (saldría sin homoclave = RFC inválido).")
    st.caption("Solo lectura · fuente: Excel de Legal cargado en Snowflake "
               "(`V_PLD_BENEFICIARIO_CONTROLADOR`). La columna **Origen** dirá `INMOGES` "
               "el día que el proveedor pueble el campo.")


def _modal_formulario(op, meta):
    """Formulario por SECCIONES navegables (pestañas): puedes saltar libremente a
    cualquier sección, y el botón 'Guardar' persiste en Snowflake
    (PLD_AVISO_FORM) y avanza a la siguiente. Sin scroll: cada sección
    muestra solo sus campos."""
    c = op["cliente"]; hints = op.get("_hints", {})
    i0 = op["caracteristicas"][0]

    # Navegación pendiente (tras Guardar) — debe aplicarse ANTES de crear el widget.
    if "_sec_pending" in st.session_state:
        st.session_state["sec_nav"] = st.session_state.pop("_sec_pending")
    if st.session_state.get("sec_nav") not in SECCIONES_FORM:
        # Conservar la sección actual (no saltar al paso 1) si el widget se deseleccionó.
        st.session_state["sec_nav"] = SECCIONES_FORM[st.session_state.get("form_sec", 0)]
    sec = st.segmented_control("Sección", SECCIONES_FORM, key="sec_nav",
                               label_visibility="collapsed")
    if sec not in SECCIONES_FORM:      # deselección accidental -> conservar última
        sec = SECCIONES_FORM[st.session_state.get("form_sec", 0)]
    paso = SECCIONES_FORM.index(sec)
    st.session_state["form_sec"] = paso

    liq_edit = None
    if paso == 0:      # Representante legal
        x1, x2, x3 = st.columns(3)
        c["representante"]["nombre"] = x1.text_input("Nombre", c["representante"]["nombre"])
        c["representante"]["apellidopaterno"] = x2.text_input("Apellido paterno", c["representante"]["apellidopaterno"])
        c["representante"]["apellidomaterno"] = x3.text_input("Apellido materno", c["representante"]["apellidomaterno"])
        y1, y2, y3 = st.columns(3)
        c["representante"]["fechanacimiento"] = y1.text_input("Fecha nac. (AAAAMMDD)", c["representante"]["fechanacimiento"])
        c["representante"]["rfc"] = y2.text_input("RFC rep. (opcional)", c["representante"]["rfc"])
        c["representante"]["curp"] = y3.text_input("CURP rep. (opcional)", c["representante"]["curp"])
    elif paso == 1:    # Contacto y catálogos
        z1, z2, z3 = st.columns(3)
        c["telefono"]["numerotelefono"] = z1.text_input("Teléfono (obligatorio)", c["telefono"]["numerotelefono"])
        c["giromercantil"] = z2.text_input("Giro mercantil (clave SAT)", c["giromercantil"],
                                           help=f"INMOGES: {hints.get('giro_txt') or '—'}")
        i0["tipoinmueble"] = z3.text_input("Tipo inmueble (clave SAT)", i0["tipoinmueble"],
                                           help=f"INMOGES: {hints.get('tipoinmueble_txt') or '—'}")
        w1, w2 = st.columns(2)
        i0["valorreferencia"] = w1.text_input("Valor de referencia (catastral)", i0["valorreferencia"])
        i0["folioreal"] = w2.text_input("Folio real", i0["folioreal"])
    elif paso == 2:    # Domicilio del cliente
        d = c["domicilio"]; dd = st.columns(4)
        d["calle"] = dd[0].text_input("Calle", d["calle"], key="dc_calle")
        d["numeroexterior"] = dd[1].text_input("No. ext.", d["numeroexterior"], key="dc_next")
        d["colonia"] = dd[2].text_input("Colonia", d["colonia"], key="dc_col")
        d["codigopostal"] = dd[3].text_input("C.P.", d["codigopostal"], key="dc_cp")
    elif paso == 3:    # Domicilio del inmueble
        ii = st.columns(4)
        i0["calle"] = ii[0].text_input("Calle", i0["calle"], key="di_calle")
        i0["numeroexterior"] = ii[1].text_input("No. ext.", i0["numeroexterior"], key="di_next")
        i0["colonia"] = ii[2].text_input("Colonia", i0["colonia"], key="di_col")
        i0["codigopostal"] = ii[3].text_input("C.P.", i0["codigopostal"], key="di_cp")
    elif paso == 4:    # Pagos / liquidaciones
        _dfliq = pd.DataFrame(op["liquidaciones"])
        if _dfliq.empty:
            # Sin pagos del periodo: editor vacío pero CON columnas, para poder capturar a mano.
            _dfliq = pd.DataFrame(columns=["fechapago", "montooperacion", "moneda_txt", "partida"])
        elif "moneda" in _dfliq.columns:
            _dfliq["moneda_txt"] = _dfliq["moneda"].astype(str).map({"2": "USD", "1": "MXN"}).fillna("MXN")
        liq_edit = st.data_editor(_dfliq, num_rows="dynamic", use_container_width=True, key="liq", height=250,
            column_order=["fechapago", "montooperacion", "moneda_txt", "partida"],
            column_config={"fechapago": "Fecha (AAAAMMDD)", "montooperacion": "Monto",
                           "moneda_txt": "Moneda", "partida": "Partida (concepto)"})
    else:              # Beneficiario controlador (última sección) — SOLO LECTURA
        _seccion_beneficiarios(op)
        st.caption("Esta sección no se guarda: el beneficiario controlador se administra "
                   "fuera de la app (Excel de Legal → Snowflake).")
        return

    st.write("")
    es_ultimo = paso == SEC_ULTIMA_EDITABLE
    nav = st.columns([3.2, 1.8])
    if nav[1].button("Guardar" if es_ultimo else "Guardar y continuar ▶",
                     type="primary", use_container_width=True, key="w_next"):
        # Volcar los editores al op (solo en sus secciones)
        if paso == SEC_ULTIMA_EDITABLE and liq_edit is not None:
            nuevos = []
            for r in liq_edit.fillna("").to_dict("records"):
                r = {k: str(v) for k, v in r.items()}
                mt = (r.pop("moneda_txt", "") or "").strip().upper()   # USD/MXN -> clave SAT
                r["moneda"] = ("2" if mt == "USD" else "1") if mt else (r.get("moneda") or "1")
                r["formapago"] = r.get("formapago") or "1"
                r["instrumentomonetario"] = r.get("instrumentomonetario") or "8"
                nuevos.append(r)
            op["liquidaciones"] = nuevos
        st.session_state["op"] = op
        try:
            _guardar(op, meta)                      # persiste en Snowflake en CADA sección
            st.toast("Guardado en Snowflake ✓")
        except Exception as ex:
            st.error(f"No se pudo guardar: {ex}")
            return
        if es_ultimo:
            st.success("Formulario completo y guardado. Revisa la pestaña **Revisión / Enviar**.")
        else:
            st.session_state["_sec_pending"] = SECCIONES_FORM[paso + 1]
            _rerun_dialogo()


def _modal_revision_envio(op, meta, id_sujeto):
    c = op["cliente"]; i = op["caracteristicas"][0]
    faltan = validate(op)
    colA, colB = st.columns(2)
    with colA:
        tarjeta("Cliente", [("Razón social / Nombre", c["denominacionrazon"] or c["nombre"]), ("RFC", c["rfc"]),
                            ("Domicilio", _direccion(c["domicilio"])), ("Teléfono", c["telefono"]["numerotelefono"]),
                            ("Correo", c["telefono"]["correoelectronico"])])
        tarjeta("Representante legal", [
            ("Nombre", f"{c['representante']['nombre']} {c['representante']['apellidopaterno']} {c['representante']['apellidomaterno']}".strip()),
            ("Fecha de nacimiento", _fh(c["representante"]["fechanacimiento"])), ("CURP", c["representante"]["curp"])],
            badge_txt="Manual", manual=True)
    with colB:
        tarjeta("Inmueble", [("Dirección", _direccion(i)), ("Periodo", f"{_fh(i['fechainicio'])} – {_fh(i['fechatermino'])}"),
                             ("Tipo de inmueble", i["tipoinmueble"]),
                             ("Valor de referencia", f"${_f(i['valorreferencia']):,.2f}" if i["valorreferencia"] else "—"),
                             ("Folio real", i["folioreal"])])
        total = sum(_f(l["montooperacion"]) for l in op["liquidaciones"])
        tarjeta("Pagos del periodo", [("Número de pagos", len(op["liquidaciones"])), ("Monto total", f"${total:,.2f}")])
        bens = beneficiarios_validos(op)
        if bens:
            b0 = bens[0]
            tarjeta("Beneficiario controlador", [
                ("Nombre", f"{b0.get('nombre','')} {b0.get('apellidopaterno','')} {b0.get('apellidomaterno','')}".strip()),
                ("Fecha de nacimiento", _fh(b0.get("fechanacimiento"))), ("CURP", b0.get("curp") or "—"),
                ("País", f"{b0.get('paisnacionalidad_txt','')} → {_clave_pais(b0.get('paisnacionalidad_txt')) or '⚠ sin clave'}"),
                ("Registrados", len(bens))],
                badge_txt=(b0.get("origen") or "Excel").upper(), manual=(b0.get("origen") != "INMOGES"))
        elif not op.get("_benef_ok", True):
            tarjeta("Beneficiario controlador", [("Estado", "⚠ NO SE PUDO CONSULTAR"),
                                                 ("Confiable", "NO — verifica antes de enviar")],
                    badge_txt="ERROR", manual=True)
        else:
            tarjeta("Beneficiario controlador", [("Registrados", "— (no bloquea el envío)")],
                    badge_txt="SIN DATO")
    if not op.get("_benef_ok", True):
        st.markdown("<div class='errbox'>⚠ El beneficiario controlador <b>no pudo consultarse</b>: "
                    "si este contrato tuviera uno, el aviso saldría SIN él. Revisa la pestaña "
                    "<b>Beneficiarios</b> antes de enviar.</div>", unsafe_allow_html=True)
    payload = build_payload(op)
    with st.expander("Ver JSON técnico (lo que se enviará a el portal de avisos UIF)"):
        st.json(payload)
    if faltan:
        st.markdown(f"<div class='errbox'>No se puede enviar: faltan {len(faltan)} datos (corrige en Formulario):</div>", unsafe_allow_html=True)
        st.markdown("\n".join(f"- **{k}** — {m}" for k, m in faltan)); return
    st.markdown(f"<div class='infobox'>Destino: <b>idSujeto {id_sujeto}</b>. Cargar NO genera avisos al SAT.</div>", unsafe_allow_html=True)
    if st.button("Enviar a el portal de avisos UIF", type="primary", key="env_btn"):
        st.session_state["confirmar_envio"] = True
    if st.session_state.get("confirmar_envio"):
        st.markdown("<div class='errbox'>¿Enviar estos datos a el portal de avisos UIF?</div>", unsafe_allow_html=True)
        cc1, cc2 = st.columns(2)
        if cc1.button("Sí, enviar", type="primary", use_container_width=True, key="env_si"):
            with st.spinner("Enviando…"):
                res = pv_nueva_operacion(id_sujeto, payload)
            # Un 201 NO prueba que el beneficiario entró: se verifica contra el ECO.
            res["benef_ok"] = (not payload.get("dueñobeneficiario")
                               or _eco_confirma_benef(res.get("data"), bens))
            st.session_state["envio_res"] = res; st.session_state["confirmar_envio"] = False
            try:
                _guardar(op, meta, payload=payload, enviado=bool(res.get("ok")), envio_status=res.get("status"))
            except Exception as ex:
                st.warning(f"Se envió pero no se pudo guardar el estatus: {ex}")
        if cc2.button("No", use_container_width=True, key="env_no"):
            st.session_state["confirmar_envio"] = False
    res = st.session_state.get("envio_res")
    if res:
        if res["ok"]:
            st.markdown(f"<div class='okbox'>Operación cargada en el portal de avisos UIF (HTTP {res['status']}). Guardado en Snowflake.</div>", unsafe_allow_html=True)
            if res.get("benef_ok") is False:
                st.markdown("<div class='errbox'>⚠ HTTP 201 pero la respuesta NO confirma el bloque "
                            "<b>dueñobeneficiario</b>. el portal de avisos UIF descarta EN SILENCIO los bloques cuyo "
                            "nombre no reconoce (ya pasó con <i>liquidaciones</i> vs <i>datosliquidacion</i>): "
                            "verifica el aviso en el portal antes de darlo por presentado.</div>",
                            unsafe_allow_html=True)
            elif payload.get("dueñobeneficiario"):
                st.markdown("<div class='okbox'>✓ El beneficiario controlador viene de vuelta en la "
                            "respuesta de el portal de avisos UIF: el bloque SÍ se registró.</div>", unsafe_allow_html=True)
        else:
            st.markdown(f"<div class='errbox'>el portal de avisos UIF rechazó (HTTP {res['status']}):</div>", unsafe_allow_html=True)
            for e in res.get("errores", []):
                st.markdown(f"- {e}")
        with st.expander("Detalle técnico (request / response)"):
            st.code(f"POST {res.get('url','')}"); st.json(res.get("data"))


def _modal_parcialidades(op):
    """Solo lectura: indica si los pagos del cliente son parciales o completos y
    muestra el detalle de parcialidad (método de pago, número, saldos). No agrega
    widgets con estado, así que no toca la lista de limpieza de sesión."""
    liqs = op.get("liquidaciones", []) or []
    if not liqs:
        st.info("Este cliente no tiene pagos del periodo para mostrar."); return

    def _es_parcial(l):
        return _f(l.get("saldo_pendiente")) > 0   # aún debe tras este pago = parcial

    n_parc = sum(1 for l in liqs if _es_parcial(l))
    if n_parc:
        st.markdown(f"<div class='infobox'>⚠ Este cliente tiene <b>{n_parc}</b> pago(s) en "
                    f"parcialidades (abonos).</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div class='okbox'>✓ Todos los pagos del periodo son completos.</div>",
                    unsafe_allow_html=True)

    filas = []
    for i, l in enumerate(liqs):
        mon = "USD" if l.get("moneda") == "2" else "MXN"
        # La renta cruda se muestra UNA vez por factura: en la última parcialidad de
        # cada CFDI (o, en el path viejo, en la última del día).
        nxt = liqs[i + 1] if i + 1 < len(liqs) else None
        if l.get("_idcfdi"):
            es_ultima = nxt is None or l.get("_idcfdi") != nxt.get("_idcfdi")
        else:
            es_ultima = nxt is None or l.get("fechapago") != nxt.get("fechapago")
        # Importe de Renta = renta cruda del PDF (RENTA_SIN_IVA = Renta + Renta Variable),
        # valor exacto SIN cálculos (no se divide por 1.16, eso introduce error de redondeo).
        _renta_cruda = _f(l.get("monto_sin_iva"))
        importe_renta = (f"${_renta_cruda:,.2f} {mon}" if (es_ultima and _renta_cruda > 0) else "—")
        filas.append({
            "Pago": "Parcial" if _es_parcial(l) else "Completo",
            "Fecha Pago": _ddmmaaaa(l.get("fechapago", "")),
            "Método": _s(l.get("metodo_pago")) or "—",
            "Parc.": _s(l.get("num_parcialidad")) or "—",
            "Saldo anterior": f"${_f(l.get('saldo_anterior')):,.2f}",
            "Importe de Renta": importe_renta,
            "Importe de Renta + IVA": f"${_f(l.get('importe_pagado')):,.2f} {mon}",
            "Saldo pendiente": f"${_f(l.get('saldo_pendiente')):,.2f}",
            "Factura": l.get("factura_url") or "",
        })
    st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True,
                 column_config={"Factura": st.column_config.LinkColumn("Factura", display_text="Ver")})
    st.caption("'Importe de Renta' = renta cruda del PDF (solo en la última factura). "
               "'Importe de Renta + IVA' = monto total abonado (incluyendo IVA). "
               "La diferencia se ve en 'Saldo pendiente'.")


@st.dialog("Documentación del cliente", width="large")
def modal_cliente(id_sujeto):
    meta = st.session_state.get("op_meta", {})
    if st.session_state.get("op") is None:
        periodo = meta.get("periodo", "01-2026")
        # Prefetch: el formulario sale del dict cacheado (_form_todos), 0 consultas al abrir.
        form = _form_todos().get((meta.get("empresa", ""), meta.get("contrato", "")))
        ent = _entidad_sf().get(_s(meta.get("fila", {}).get("RFC")).upper())
        st.session_state["op"] = construir_operacion(meta.get("fila", {}), periodo,
                                                     pagos=_fuente_pagos(periodo), form=form, entidad=ent,
                                                     benef=_benef_sf())
    op = st.session_state.get("op")
    if not op:
        st.warning("No se pudieron cargar los datos."); return
    nombre = op["cliente"].get("denominacionrazon") or op["cliente"].get("nombre") or meta.get("cliente", "")
    pr = "" if op.get("_pagos_reales") else " · ⚠ sin pagos del periodo"
    fg = " · 📝 con datos guardados" if op.get("_form_guardado") else ""
    st.caption(f"{meta.get('empresa','')} · {nombre} · contrato {meta.get('contrato','')}{pr}{fg}")
    tab_form, tab_parc, tab_env = st.tabs(["Formulario", "Parcialidades", "Revisión / Enviar"])
    with tab_form:
        _modal_formulario(op, meta)
    with tab_parc:
        _modal_parcialidades(op)
    with tab_env:
        _modal_revision_envio(op, meta, id_sujeto)


# ---------------- Autenticacion (Cognito) ----------------
# Toggle global: ENABLE_AUTH=false desactiva la auth por completo (solo dev).
ENABLE_AUTH = os.getenv("ENABLE_AUTH", "true").lower() == "true"

if ENABLE_AUTH:
    # Logout diferido: cookies.save() solo puede ejecutarse UNA vez por ciclo de
    # Streamlit, y is_authenticated() ya lo usa internamente. Por eso el logout
    # real se ejecuta en el ciclo siguiente via un flag en session_state.
    if st.session_state.get("_pending_logout"):
        del st.session_state["_pending_logout"]
        get_auth().logout()
        st.rerun()

    # Verificar autenticacion ANTES de renderizar contenido protegido.
    auth = get_auth()
    if not auth.is_authenticated():
        auth.render_login_screen()
        st.stop()

    # Mantener cookies SSO con dominio padre en cada render.
    from utils.auth.cookie_domain_fix import ensure_cookie_domain_fix
    ensure_cookie_domain_fix()

    # Info del usuario (el email va en la topbar; el botón de cerrar sesion
    # se renderiza al final de la barra lateral, ver más abajo).
    _user = auth.get_current_user()
else:
    # Modo dev: sin auth real, pero mostramos el "chrome" autenticado
    # (email + botón de cerrar sesión) para ver la pantalla tal como
    # aparece en producción. Email configurable via DEV_FAKE_EMAIL.
    _user = {"email": os.getenv("DEV_FAKE_EMAIL", "test@almena.mx")}

# Email a mostrar en la topbar (junto al título).
_user_email = (_user.get("email") or _user.get("username", "")) if _user else ""


# ---------------- Layout ----------------
render_topbar(st.session_state["op"], _user_email)

with st.sidebar:
    st.markdown("### Selección")
    try:
        filas = _tabla_sf()
    except Exception as ex:
        filas = []
        st.error(f"No se pudo leer Snowflake:\n\n{ex}")
    todas = sorted({_s(r.get("EMPRESA")) for r in filas if _s(r.get("EMPRESA"))})
    seleccion = st.multiselect("Empresas", ["Todas"] + todas, default=["Todas"], help="'Todas' = todas las empresas.")
    empresas_sel = todas if (("Todas" in seleccion) or (not seleccion)) else seleccion
    periodo = st.text_input("Periodo (MM-AAAA)", "05-2026", help="Periodo de los pagos reales (PLD_AVISO_PAGOS).")
    destino = st.selectbox("Destino (POST)", list(ID_SUJETO), index=2)
    id_sujeto = ID_SUJETO[destino]

# Cerrar sesion al final de la barra lateral (patron foodplay).
with st.sidebar:
    st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
    if st.button("Cerrar sesión", use_container_width=True, type="secondary", key="btn_logout"):
        if ENABLE_AUTH:
            st.session_state["_pending_logout"] = True
            st.rerun()
        else:
            st.toast("Cierre de sesión deshabilitado en modo dev (ENABLE_AUTH=false).")

st.markdown("<div class='main-panel'>", unsafe_allow_html=True)
render_consulta(filas, empresas_sel, periodo, id_sujeto)
st.markdown("</div>", unsafe_allow_html=True)
st.markdown("<div class='footer'>CREAR / DESARROLLAR / ACTIVAR</div>", unsafe_allow_html=True)
