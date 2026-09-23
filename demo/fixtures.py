# -*- coding: utf-8 -*-
"""
Synthetic leases, invoices and payments for the demo.

Everything is invented: companies, tenants, properties, tax IDs, amounts.
Shapes match what the ingestion writes into `PLD_AVISO_CFDI` and
`PLD_AVISO` / `PLD_AVISO_ENTIDAD`, so the real logic in `app/logica.py`
consumes them unchanged.

The interesting part is the payment patterns. A month of leasing is not one
invoice, one payment: it is partials, one payment covering several invoices,
VAT on top of rent, and the occasional foreign-currency contract. Those are
exactly the cases that decide whether the reported amount is right.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

SEED = 1701

# Mexican AML law states the leasing threshold in UMA (a statutory unit that is
# re-published every year), not in pesos. It is public law, not a company rule.
#
# The figures below are ILLUSTRATIVE, so the demo has something to decide. A
# real deployment reads the current UMA and the current multiple from the
# published catalogue — hard-coding either is how a connector quietly starts
# reporting the wrong set of operations.
UMA_DEMO = 113.14
UMBRAL_UMA = 1_605
UMBRAL_PLD = UMA_DEMO * UMBRAL_UMA

EMPRESAS = {
    "AAI": "ALMENA ADMINISTRACION INMOBILIARIA SA DE CV",
    "ADP": "ALMENA DESARROLLOS Y PROYECTOS SA DE CV",
    "ILP": "INMOBILIARIA LOMA PRIETA SA DE CV",
    "EDP": "EDIFICIOS DE LA PRADERA SA DE CV",
}

INMUEBLES = [
    ("Paseo Altamira", "Queretaro", "Centro", "Av. Constituyentes", "1450", "76000"),
    ("Plaza Bernal", "Aguascalientes", "Del Valle", "Blvd. Independencia", "820", "20000"),
    ("Paseo San Isidro", "Leon", "Las Lomas", "Av. Reforma", "3310", "37000"),
    ("Paseo La Rioja", "Merida", "San Angel", "Calle 60 Norte", "275", "97000"),
]

ARRENDATARIOS = [
    ("Cafe Bonanza SA de CV", "Moral", "722110"),
    ("Farmacia Lucero SA de CV", "Moral", "464111"),
    ("Banco del Istmo SA", "Moral", "522110"),
    ("Gimnasio Vertice SA de CV", "Moral", "713943"),
    ("Telecom Sierra SA de CV", "Moral", "517110"),
    ("Burger Nogal SA de CV", "Moral", "722513"),
    ("Tiendas Almendro SA de CV", "Moral", "462111"),
    ("Sushi Kaiso SA de CV", "Moral", "722511"),
    ("Martin Andres Perez Lara", "Fisica", "531311"),
    ("Laura Sofia Rendon Vega", "Fisica", "531311"),
]

NOMBRES = ["Ana", "Carlos", "Sofia", "Miguel", "Paula", "Andres", "Elena", "Ricardo"]
PATERNOS = ["Martinez", "Duarte", "Ibarra", "Rios", "Serna", "Quiroz", "Palacios"]
MATERNOS = ["Lozano", "Vega", "Cordero", "Nava", "Salas", "Trevino"]

# Payment patterns, and how often each shows up. This distribution is the
# reason the connector exists.
PATRONES = [
    ("pago_unico", 0.45),        # one invoice, one payment, done
    ("parcialidades", 0.25),     # one invoice paid in 2-4 instalments
    ("multi_factura", 0.15),     # one payment covering several invoices
    ("sin_pago", 0.10),          # invoice issued, not yet paid
    ("moneda_usd", 0.05),        # contract denominated in USD
]


def _rfc(rng: random.Random, moral: bool) -> str:
    letras = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
                     for _ in range(3 if moral else 4))
    d = date(1975, 1, 1) + timedelta(days=rng.randint(0, 16000))
    homo = "".join(rng.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(3))
    return f"{letras}{d:%y%m%d}{homo}"


def _curp(rng: random.Random) -> str:
    return ("".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4))
            + f"{rng.randint(60, 99)}{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}"
            + rng.choice("HM")
            + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(8)))


def _elige_patron(rng: random.Random) -> str:
    r, acc = rng.random(), 0.0
    for nombre, peso in PATRONES:
        acc += peso
        if r <= acc:
            return nombre
    return PATRONES[-1][0]


def generar_contratos(n: int = 28, semilla: int = SEED) -> list[dict]:
    rng = random.Random(semilla)
    contratos = []
    for i in range(n):
        razon, tipo, giro = ARRENDATARIOS[i % len(ARRENDATARIOS)]
        inm, ciudad, colonia, calle, numext, cp = INMUEBLES[i % len(INMUEBLES)]
        emp = list(EMPRESAS)[i % len(EMPRESAS)]
        moral = tipo == "Moral"
        inicio = date(2022, 1, 1) + timedelta(days=rng.randint(0, 1000))
        contratos.append({
            "ID_CONTRATO": f"CTR-{2000 + i:05d}",
            "EMPRESA": emp,
            "EMPRESA_RAZON": EMPRESAS[emp],
            "DENOMINACION_RAZON": razon,
            "TIPO_PERSONA": tipo,
            "RFC": _rfc(rng, moral),
            "CURP": "" if moral else _curp(rng),
            "GIRO_MERCANTIL": giro if moral else "",
            "ACTIVIDAD_ECONOMICA": "" if moral else giro,
            "PAIS_NACIONALIDAD": "MX",
            "IDENTIFICADOR_UNICO": f"UID-{i:06d}",
            "INMUEBLE": inm,
            "UNIDAD": f"L-{rng.randint(1, 240):03d}",
            "CIUDAD": ciudad,
            "COLONIA": colonia,
            "CALLE": calle,
            "NUMERO_EXTERIOR": numext,
            "CODIGO_POSTAL": cp,
            "TIPO_INMUEBLE": rng.choice(["1", "2", "3"]),
            # Two fields the source ERP does not expose. They are captured by
            # hand in the app; here some are deliberately left blank so the
            # validation has something to complain about.
            "VALOR_REFERENCIA": (round(rng.uniform(4e6, 6e7), 2)
                                 if rng.random() > 0.25 else None),
            "FOLIO_REAL": (f"FR-{rng.randint(100000, 999999)}"
                           if rng.random() > 0.3 else ""),
            "FECHA_INICIO": inicio.isoformat(),
            "FECHA_TERMINO": (inicio + timedelta(days=365 * rng.randint(3, 10))).isoformat(),
            "REP_NOMBRE": rng.choice(NOMBRES) if moral else "",
            "REP_PATERNO": rng.choice(PATERNOS) if moral else "",
            "REP_MATERNO": rng.choice(MATERNOS) if moral else "",
            "REP_NACIMIENTO": (date(1965, 1, 1) + timedelta(days=rng.randint(0, 11000))
                               ).isoformat() if moral else "",
            "NOMBRE": rng.choice(NOMBRES) if not moral else "",
            "APELLIDO_PATERNO": rng.choice(PATERNOS) if not moral else "",
            "APELLIDO_MATERNO": rng.choice(MATERNOS) if not moral else "",
            "FECHA_NACIMIENTO": (date(1970, 1, 1) + timedelta(days=rng.randint(0, 10000))
                                 ).isoformat() if not moral else "",
            "TELEFONO": f"55{rng.randint(10000000, 99999999)}",
            "CORREO": f"contacto{i:03d}@almena.mx",
            "BC_NOMBRE": (rng.choice(NOMBRES) if moral and rng.random() > 0.5 else ""),
            "BC_PATERNO": rng.choice(PATERNOS),
            "BC_MATERNO": rng.choice(MATERNOS),
        })
    return contratos


def generar_cfdi(contratos: list[dict], periodo: str = "03-2026",
                 semilla: int = SEED) -> list[dict]:
    """One row per stamped invoice, with its payment schedule attached."""
    rng = random.Random(semilla + 31)
    mes, anio = int(periodo[:2]), int(periodo[3:])
    facturas = []

    for c in contratos:
        patron = _elige_patron(rng)
        usd = patron == "moneda_usd"
        # A high enough base rent that some contracts cross the threshold and
        # some do not: the decision has to be made, not assumed.
        renta = round(rng.uniform(18_000, 420_000), 2)
        iva = round(renta * 0.16, 2)
        mantenimiento = round(renta * rng.uniform(0.02, 0.08), 2)
        total = round(renta + iva + mantenimiento, 2)
        emision = date(anio, mes, rng.randint(1, 5))

        n_facturas = 2 if patron == "multi_factura" else 1
        for k in range(n_facturas):
            fecha_f = emision + timedelta(days=k * 12)
            if patron == "sin_pago":
                parcialidades, saldo = [], total
            elif patron == "parcialidades":
                n = rng.randint(2, 4)
                trozo = round(total / n, 2)
                parcialidades = [
                    {"fecha": (fecha_f + timedelta(days=8 * (j + 1))).isoformat(),
                     "monto": trozo,
                     "forma_pago": rng.choice(["03", "02", "01"])}
                    for j in range(n - 1)]
                saldo = round(total - trozo * (n - 1), 2)
            else:
                parcialidades = [
                    {"fecha": (fecha_f + timedelta(days=rng.randint(3, 20))).isoformat(),
                     "monto": total,
                     "forma_pago": rng.choice(["03", "02"])}]
                saldo = 0.0

            facturas.append({
                "PERIODO": periodo,
                "EMPRESA": c["EMPRESA"],
                "ID_CFDI": f"CFDI-{len(facturas) + 1:06d}",
                "UUID": "-".join("".join(rng.choice("0123456789abcdef") for _ in range(n))
                                 for n in (8, 4, 4, 4, 12)),
                "FOLIO": str(40000 + len(facturas)),
                "SERIE": c["EMPRESA"],
                "FECHA": fecha_f.isoformat(),
                "ESTATUS_TIMBRE": "Timbrada",
                "MONEDA": "2" if usd else "1",
                "SUBTOTAL": round(renta + mantenimiento, 2),
                "TOTAL": total,
                "SALDO": saldo,
                "CONTRATO_ERP": c["ID_CONTRATO"],
                "UNIDAD": c["UNIDAD"],
                "INMUEBLE_TXT": c["INMUEBLE"],
                "RFC_RECEPTOR": c["RFC"],
                "RAZON_SOCIAL": c["DENOMINACION_RAZON"],
                # Rent only, and rent + VAT. The reported amount is the invoice
                # total, not either of these — that distinction is the whole point.
                "RENTA_SIN_IVA": renta,
                "RENTA_CON_IVA": round(renta + iva, 2),
                "MONTO_FINAL": total,
                "RENTA_CONCEPTOS": "Renta" if rng.random() > 0.2 else "Renta Variable",
                "NUM_PARCIALIDADES": len(parcialidades),
                "PARCIALIDADES": parcialidades,
                "_PATRON": patron,
            })
    return facturas


def rebasa_umbral(facturas_del_cliente: list[dict]) -> tuple[bool, float]:
    """Threshold is tested on rent; the reported amount is the invoice total."""
    renta = sum(f["RENTA_SIN_IVA"] for f in facturas_del_cliente)
    return renta >= UMBRAL_PLD, renta


def operacion_desde(contrato: dict, facturas: list[dict]) -> dict:
    """Build the `op` dict that app/logica.py expects, from fixture rows."""
    moral = contrato["TIPO_PERSONA"] == "Moral"
    liquidaciones = []
    for f in facturas:
        for p in f["PARCIALIDADES"]:
            liquidaciones.append({
                "fechapago": p["fecha"],
                "formapago": p["forma_pago"],
                "instrumentomonetario": "1",
                "moneda": f["MONEDA"],
                # The invoice total, split across its instalments.
                "montooperacion": round(p["monto"], 2),
            })
    return {
        "referenciaaviso": f"{contrato['EMPRESA']}-{contrato['ID_CONTRATO']}",
        "tipoalerta": "100",
        "prioridad": "1",
        "fechaoperacion": facturas[0]["FECHA"] if facturas else "",
        "cliente": {
            "tipo_persona": contrato["TIPO_PERSONA"],
            "denominacionrazon": contrato["DENOMINACION_RAZON"] if moral else "",
            "nombre": contrato["NOMBRE"],
            "apellidopaterno": contrato["APELLIDO_PATERNO"],
            "apellidomaterno": contrato["APELLIDO_MATERNO"],
            "fechanacimiento": contrato["FECHA_NACIMIENTO"],
            "rfc": contrato["RFC"],
            "curp": contrato["CURP"],
            "paisnacionalidad": contrato["PAIS_NACIONALIDAD"],
            "giromercantil": contrato["GIRO_MERCANTIL"],
            "actividadeconomica": contrato["ACTIVIDAD_ECONOMICA"],
            "identificadorunico": contrato["IDENTIFICADOR_UNICO"],
            "representante": {
                "nombre": contrato["REP_NOMBRE"],
                "apellidopaterno": contrato["REP_PATERNO"],
                "apellidomaterno": contrato["REP_MATERNO"],
                "fechanacimiento": contrato["REP_NACIMIENTO"],
            },
            "domicilio": {
                "colonia": contrato["COLONIA"], "calle": contrato["CALLE"],
                "numeroexterior": contrato["NUMERO_EXTERIOR"],
                "numerointerior": "", "codigopostal": contrato["CODIGO_POSTAL"],
            },
            "telefono": {"clavepais": "MX", "numerotelefono": contrato["TELEFONO"],
                         "correoelectronico": contrato["CORREO"]},
        },
        "caracteristicas": [{
            "fechainicio": contrato["FECHA_INICIO"],
            "fechatermino": contrato["FECHA_TERMINO"],
            "tipoinmueble": contrato["TIPO_INMUEBLE"],
            "valorreferencia": contrato["VALOR_REFERENCIA"],
            "colonia": contrato["COLONIA"], "calle": contrato["CALLE"],
            "numeroexterior": contrato["NUMERO_EXTERIOR"], "numerointerior": "",
            "codigopostal": contrato["CODIGO_POSTAL"],
            "folioreal": contrato["FOLIO_REAL"],
        }],
        "liquidaciones": liquidaciones,
        "beneficiarios": ([{"nombre": contrato["BC_NOMBRE"],
                            "apellidopaterno": contrato["BC_PATERNO"],
                            "apellidomaterno": contrato["BC_MATERNO"]}]
                          if contrato["BC_NOMBRE"] else []),
    }
