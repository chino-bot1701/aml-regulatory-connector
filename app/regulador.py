"""HTTP client for the regulator's reporting portal."""
from __future__ import annotations

import os

import requests

from logica import *  # noqa: F401,F403  catalogues and payload builders

# ============================================================================ #
#  2) UIF — login + POST del aviso
# ============================================================================ #
UIF_BASE = os.environ.get("UIF_BASE", "")
UIF_USER = os.environ.get("UIF_USER", "")
UIF_PASS = os.environ.get("UIF_PASS", "")
PV_TIMEOUT = (30, 90)


def pv_login() -> str:
    r = requests.post(UIF_BASE + "/api/usuarios/login",
                      json={"Username": UIF_USER, "Password": UIF_PASS}, timeout=PV_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Login el portal de avisos UIF falló: {r.status_code} — {r.text[:200]}")
    return r.text.strip().strip('"')


def _eco_confirma_benef(data, bens: list) -> bool:
    """¿el portal de avisos UIF devolvió el beneficiario que le mandamos?

    Un 201 NO prueba que el bloque entró: el portal de avisos UIF responde 201 aunque descarte EN
    SILENCIO un bloque cuyo nombre no reconoce. Precedente real: el manual decía
    'liquidaciones' cuando la API espera 'datosliquidacion', y los avisos quedaban
    registrados SIN liquidaciones devolviendo 201. Por eso se valida contra el ECO
    (el aviso que el portal de avisos UIF dice haber registrado), no contra el status.

    La búsqueda es por HUELLA sobre el eco serializado —CURP, o nombre+apellido si no
    hay CURP— para no depender de la forma exacta de la respuesta."""
    if not bens:
        return True
    eco = (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)).upper()
    for b in bens:
        curp = mayus(b.get("curp"))
        if curp:
            if curp not in eco:
                return False
        elif not (mayus(b.get("nombre")) in eco and mayus(b.get("apellidopaterno")) in eco):
            return False
    return True


def pv_nueva_operacion(id_sujeto: int, payload: dict) -> dict:
    token = pv_login()
    url = f"{UIF_BASE}/api/ari/nuevaoperacion?idSujeto={id_sujeto}"
    r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=PV_TIMEOUT)
    if r.status_code == 401:
        r = requests.post(url, headers={"Authorization": f"Bearer {pv_login()}"}, json=payload, timeout=PV_TIMEOUT)
    try:
        data = r.json()
    except Exception:
        data = r.text
    errores = data.get("Errores") or ([data["Message"]] if isinstance(data, dict) and data.get("Message") else []) if isinstance(data, dict) else []
    return {"status": r.status_code, "ok": r.status_code in (200, 201), "data": data, "errores": errores, "url": url}
