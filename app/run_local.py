#!/usr/bin/env python
"""Ejecuta el dashboard localmente sin autenticación real, mostrando la
pantalla tal como aparece autenticado. Multiplataforma (Win/mac/Linux).

Uso:
    python run_local.py

Para cambiar el email mostrado en la barra lateral, define DEV_FAKE_EMAIL
antes de ejecutar:
    Windows (cmd):        set DEV_FAKE_EMAIL=you@example.com
    Windows (PowerShell): $env:DEV_FAKE_EMAIL="you@example.com"
    bash/zsh:             export DEV_FAKE_EMAIL=you@example.com

Solo omite la autenticación Cognito; Snowflake y el portal de avisos UIF siguen usando su
configuración real (.env y el .pem), igual que con ENABLE_AUTH=false.
"""
import os
import sys
import subprocess

os.environ["ENABLE_AUTH"] = "false"
os.environ.setdefault("DEV_FAKE_EMAIL", "test@almena.mx")

sys.exit(subprocess.call(
    [sys.executable, "-m", "streamlit", "run", "app.py"]
))
