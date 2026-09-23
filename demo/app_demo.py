# -*- coding: utf-8 -*-
"""
Simplified public demo of the AML reporting connector.

This is NOT the production app — that one is in `app/`, with Cognito sign-in,
Snowflake reads and the live POST to the regulator. What runs here is the part
worth showing: the real business logic in `app/logica.py`, over synthetic data,
with no credentials and no network.

    streamlit run demo/app_demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "app"))
sys.path.insert(0, str(RAIZ))

from logica import build_payload, validate          # noqa: E402  production logic
from demo import fixtures as fx                     # noqa: E402

st.set_page_config(page_title="AML Reporting Connector — demo",
                   page_icon="📄", layout="wide")


# --------------------------------------------------------------------------- #
@st.cache_data
def cargar():
    contratos = fx.generar_contratos()
    facturas = fx.generar_cfdi(contratos)
    return contratos, facturas


contratos, facturas = cargar()
por_contrato: dict[str, list[dict]] = {}
for f in facturas:
    por_contrato.setdefault(f["CONTRATO_ERP"], []).append(f)


# --------------------------------------------------------------------------- #
st.title("AML Reporting Connector")
st.caption(
    "Leasing invoices → threshold test → regulatory report payload. "
    "**Simplified demo on synthetic data** — the production app uses Cognito "
    "sign-in, Snowflake and a live POST to the regulator."
)

with st.expander("What this is deciding, and why it is not obvious"):
    st.markdown(
        """
Mexican AML law makes property leasing a *vulnerable activity*: above a
statutory threshold, the lessor has to file a report. Getting the number right
is harder than it looks, because a month of leasing is not one invoice and one
payment:

- **A payment can cover part of an invoice.** Reporting the invoice as paid in
  full overstates the operation.
- **A payment can cover several invoices.** Reporting it once per invoice
  reports the same money two or three times.
- **VAT and service charges sit on top of the rent.** The threshold is tested
  on rent; the amount reported is the invoice total. Mixing them up moves the
  number in both directions.
- **Some contracts are in dollars.**

Before this connector, compliance reconciled that by hand every month. The
over-reporting it removed is the reason it exists.
        """
    )

# --------------------------------------------------------------------------- #
col1, col2, col3, col4 = st.columns(4)
sobre_umbral = [c for c in contratos
                if fx.rebasa_umbral(por_contrato.get(c["ID_CONTRATO"], []))[0]]
col1.metric("Contracts", len(contratos))
col2.metric("Invoices in period", len(facturas))
col3.metric("Above threshold", len(sobre_umbral))
col4.metric("Threshold (MXN)", f"{fx.UMBRAL_PLD:,.0f}")

st.divider()

# --------------------------------------------------------------------------- #
izq, der = st.columns([2, 3])

with izq:
    st.subheader("Contracts")
    solo_umbral = st.checkbox("Only those above the threshold", value=True)
    lista = sobre_umbral if solo_umbral else contratos

    filas = []
    for c in lista:
        fs = por_contrato.get(c["ID_CONTRATO"], [])
        rebasa, renta = fx.rebasa_umbral(fs)
        filas.append({
            "Contract": c["ID_CONTRATO"],
            "Tenant": c["DENOMINACION_RAZON"],
            "Property": c["INMUEBLE"],
            "Invoices": len(fs),
            "Rent (threshold basis)": round(renta, 2),
            "Reported total": round(sum(f["MONTO_FINAL"] for f in fs), 2),
            "Pattern": fs[0]["_PATRON"] if fs else "—",
            "Reportable": "yes" if rebasa else "no",
        })
    st.dataframe(filas, hide_index=True, width="stretch", height=420)

    elegido = st.selectbox(
        "Build the report for",
        [c["ID_CONTRATO"] for c in lista],
        format_func=lambda cid: f"{cid} — "
        f"{next(c['DENOMINACION_RAZON'] for c in contratos if c['ID_CONTRATO'] == cid)}")

with der:
    contrato = next(c for c in contratos if c["ID_CONTRATO"] == elegido)
    fs = por_contrato.get(elegido, [])
    rebasa, renta = fx.rebasa_umbral(fs)

    st.subheader(contrato["DENOMINACION_RAZON"])
    a, b, c_ = st.columns(3)
    a.metric("Rent (no VAT)", f"{renta:,.2f}")
    b.metric("Reported amount", f"{sum(f['MONTO_FINAL'] for f in fs):,.2f}")
    c_.metric("Instalments", sum(len(f["PARCIALIDADES"]) for f in fs))

    if not rebasa:
        st.info("Below the threshold — no report is due for this contract.")

    st.markdown("**Payment schedule** — one row per instalment, which is how "
                "the report itemises the operation.")
    st.dataframe(
        [{"Invoice": f["FOLIO"], "Invoice date": f["FECHA"],
          "Rent": f["RENTA_SIN_IVA"], "Total (with VAT)": f["MONTO_FINAL"],
          "Payment date": p["fecha"], "Paid": p["monto"],
          "Currency": "USD" if f["MONEDA"] == "2" else "MXN",
          "Outstanding": f["SALDO"]}
         for f in fs for p in f["PARCIALIDADES"]] or
        [{"Invoice": "—", "Invoice date": "—", "Rent": 0, "Total (with VAT)": 0,
          "Payment date": "unpaid", "Paid": 0, "Currency": "—", "Outstanding": 0}],
        hide_index=True, width="stretch")

    # ---- the real logic, unchanged ---------------------------------------- #
    op = fx.operacion_desde(contrato, fs)
    errores = validate(op)

    st.markdown("**Validation** — `validate()` from `app/logica.py`, unchanged")
    if errores:
        st.warning(f"{len(errores)} field(s) block submission. "
                   "In production these are the boxes compliance fills in — the "
                   "source ERP does not expose them.")
        st.dataframe([{"Field": campo, "Problem": motivo}
                      for campo, motivo in errores],
                     hide_index=True, width="stretch")
    else:
        st.success("Complete — ready to submit.")

    st.markdown("**Generated payload** — `build_payload()` from `app/logica.py`, "
                "unchanged. This is the JSON the production app POSTs.")
    st.code(json.dumps(build_payload(op), indent=2, ensure_ascii=False),
            language="json")

st.divider()
st.caption(
    "Synthetic data, generated from a fixed seed by `demo/fixtures.py`. "
    "No real company, tenant, property, tax ID or amount appears anywhere in "
    "this repository."
)
