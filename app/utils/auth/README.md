# Guia de Implementacion: Amazon Cognito en Aplicaciones Streamlit

**Fecha:** 5 de Marzo de 2026
**Version:** 2.0
**Estado:** Produccion
**Audiencia:** Desarrolladores que necesiten integrar autenticacion Cognito en cualquier aplicacion Streamlit

---

## Tabla de Contenidos

1. [Resumen](#1-resumen)
2. [Prerequisitos](#2-prerequisitos)
3. [Arquitectura del Modulo de Autenticacion](#3-arquitectura-del-modulo-de-autenticacion)
4. [Configuracion de AWS Cognito](#4-configuracion-de-aws-cognito)
5. [Variables de Entorno](#5-variables-de-entorno)
6. [Integracion en una App Streamlit](#6-integracion-en-una-app-streamlit)
7. [Cognito con Microsoft Entra ID (SAML Federation)](#7-cognito-con-microsoft-entra-id-saml-federation)
8. [Toggle de Funcionalidad (Prender/Apagar Auth)](#8-toggle-de-funcionalidad-prenderrapagar-auth)
9. [Single Sign-On (SSO) entre Apps](#9-single-sign-on-sso-entre-apps)
10. [Configuracion de Nginx para SSO](#10-configuracion-de-nginx-para-sso)
11. [Docker y Deployment](#11-docker-y-deployment)
12. [Troubleshooting](#12-troubleshooting)
13. [Referencia de API del Modulo](#13-referencia-de-api-del-modulo)
14. [Checklist de Implementacion](#14-checklist-de-implementacion)

---

## 1. Resumen

Este documento describe como integrar autenticacion Amazon Cognito en **cualquier** aplicacion Streamlit, de forma generica e independiente del puerto o dominio. Cubre dos modos de autenticacion:

| Modo | Descripcion | Variable de control |
|------|-------------|---------------------|
| **Cognito Local** | Login con usuario/password directo contra Cognito User Pool | Siempre activo cuando auth esta habilitado |
| **Cognito + Entra ID** | Login federado con Microsoft Entra ID via SAML 2.0 | `ENABLE_FEDERATION=true` |

Adicionalmente, toda la autenticacion se puede **desactivar** completamente con la variable `ENABLE_AUTH=false` (ver seccion 8).

---

## 2. Prerequisitos

### 2.1 Cuenta AWS

- AWS Cognito User Pool creado
- App Client configurado con `USER_PASSWORD_AUTH` habilitado
- App Client **con** Client Secret (requerido para SECRET_HASH)

### 2.2 Dependencias Python

Agregar al `requirements.txt` de tu aplicacion:

```txt
# AWS Cognito Authentication
boto3>=1.42.34
python-jose[cryptography]>=3.3.0
PyJWT>=2.8.0
streamlit-cookies-manager>=0.2.0
requests>=2.31.0
python-dotenv>=1.0.1
```

### 2.3 Modulo de Autenticacion

Copiar el directorio completo `utils/auth/` a tu proyecto Streamlit:

```
tu_app_streamlit/
├── tu_app.py
├── requirements.txt
├── .env
└── utils/
    └── auth/
        ├── __init__.py
        ├── cognito_auth.py       # Orquestador principal (905 lineas)
        ├── config.py             # Configuracion desde env vars (227 lineas)
        ├── token_validator.py    # Validacion JWT con JWKS (312 lineas)
        ├── session_manager.py    # Cookies y sesiones SSO (316 lineas)
        ├── oauth2_handler.py     # OAuth2 para federation (179 lineas)
        └── cookie_domain_fix.py  # Fix JS para SSO cross-domain (135 lineas)
```

---

## 3. Arquitectura del Modulo de Autenticacion

### 3.1 Diagrama de Componentes

```
                    ┌──────────────────────────────┐
                    │    AWS Cognito User Pool      │
                    │  - Gestion de usuarios        │
                    │  - Tokens JWT (ID, Access,    │
                    │    Refresh)                    │
                    │  - SAML Federation (opcional)  │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │   Modulo utils/auth/          │
                    │                               │
                    │  ┌─────────────────────────┐  │
                    │  │    CognitoAuth          │  │
                    │  │  - login()              │  │
                    │  │  - is_authenticated()   │  │
                    │  │  - render_login_screen()│  │
                    │  │  - logout()             │  │
                    │  │  - get_current_user()   │  │
                    │  └────────┬────────────────┘  │
                    │           │ usa                │
                    │  ┌────────▼────────────────┐  │
                    │  │   TokenValidator        │  │
                    │  │  - JWKS cache (24h)     │  │
                    │  │  - RS256 verification   │  │
                    │  └─────────────────────────┘  │
                    │  ┌─────────────────────────┐  │
                    │  │   SessionManager        │  │
                    │  │  - Encrypted cookies    │  │
                    │  │  - SSO cross-app        │  │
                    │  └─────────────────────────┘  │
                    │  ┌─────────────────────────┐  │
                    │  │   OAuth2Handler         │  │
                    │  │  - Authorization Code   │  │
                    │  │  - HMAC state (CSRF)    │  │
                    │  └─────────────────────────┘  │
                    │  ┌─────────────────────────┐  │
                    │  │   AuthConfig            │  │
                    │  │  - from_env()           │  │
                    │  │  - validate()           │  │
                    │  └─────────────────────────┘  │
                    └──────────────────────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │    Tu Aplicacion Streamlit    │
                    │    (cualquier puerto)         │
                    └──────────────────────────────┘
```

### 3.2 Flujos de Autenticacion

**Flujo A: Login Local (usuario/password)**

```
1. Usuario accede a la app
2. is_authenticated() → False
3. render_login_screen() muestra formulario
4. Usuario ingresa credenciales
5. login(username, password):
   a. Calcula SECRET_HASH = Base64(HMAC_SHA256(username+client_id, client_secret))
   b. Llama a Cognito initiate_auth(AuthFlow='USER_PASSWORD_AUTH')
   c. Si NEW_PASSWORD_REQUIRED → muestra formulario de cambio
   d. Recibe tokens JWT (id_token, access_token, refresh_token)
6. SessionManager.set_session_token() guarda en cookies + session_state
7. apply_cookie_domain_fix_on_login() inyecta JS para SSO
8. st.rerun() recarga la app
9. is_authenticated() → True
```

**Flujo B: Login Federado (Microsoft Entra ID)**

```
1. Usuario accede a la app
2. render_login_screen() muestra boton "Iniciar sesion con Microsoft"
3. Usuario hace clic → redirigido a Cognito OAuth2/authorize
4. Cognito redirige a Microsoft Entra ID (SAML)
5. Usuario se autentica en Microsoft
6. Microsoft envia SAML assertion a Cognito
7. Cognito redirige a la app con authorization code
8. _handle_oauth_callback():
   a. Valida HMAC state (CSRF protection)
   b. exchange_code_for_tokens(code) → POST a /oauth2/token
   c. Recibe tokens JWT (identicos al login local)
9. Tokens se guardan en SessionManager (mismo flujo que login local)
10. st.rerun() → is_authenticated() → True
```

---

## 4. Configuracion de AWS Cognito

### 4.1 Crear User Pool

1. AWS Console → Cognito → Create User Pool
2. **Sign-in options:** Email + Username
3. **Password policy:** Minimo 8 caracteres, mayusculas, minusculas, numeros, especiales
4. **MFA:** Opcional (recomendado para produccion)

### 4.2 Crear App Client

1. En el User Pool → App clients → Create app client
2. **App type:** Confidential client
3. **Generate client secret:** Si
4. **Authentication flows:**
   - `USER_PASSWORD_AUTH` ← **CRITICO: Sin esto, initiate_auth falla con ClientError**
   - `REFRESH_TOKEN_AUTH` ← Para renovacion automatica de tokens
5. **OAuth 2.0 Grant types** (solo si usaras Federation):
   - `Authorization code grant`
6. **Callback URLs** (solo si usaras Federation):
   - Agregar URL de cada app (dev + stage/prod)
   - Ejemplo: `http://localhost:8501/`, `https://mi-app.ejemplo.com/`
7. **Scopes:** `openid`, `email`, `phone`

> **ADVERTENCIA:** Si creas un nuevo App Client, actualiza TODAS las variables de entorno
> (`COGNITO_CLIENT_ID`, `COGNITO_CLIENT_SECRET`) en todos los ambientes antes de hacer deploy.

### 4.3 Configurar Cognito Domain (solo para Federation)

1. User Pool → App integration → Domain
2. Crear dominio: `tu-dominio.auth.{region}.amazoncognito.com`
3. Guardar este valor como `COGNITO_DOMAIN`

---

## 5. Variables de Entorno

### 5.1 Variables Requeridas (Autenticacion Basica)

```bash
# ============================================
# AWS Cognito - REQUERIDAS
# ============================================
COGNITO_USER_POOL_ID=us-east-2_XXXXXXXXX     # Formato: region_ID
COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx   # App Client ID (min 10 chars)
COGNITO_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxx  # App Client Secret (min 10 chars)
AWS_REGION=us-east-2                           # Region del User Pool

# ============================================
# Cookies y Sesion - REQUERIDAS
# ============================================
COOKIE_ENCRYPTION_KEY=una-clave-de-al-menos-32-caracteres  # Min 32 chars, compartida entre apps para SSO
COOKIE_DOMAIN=localhost                        # localhost (dev) o .tudominio.com (prod)
COOKIE_SECURE=false                            # false (dev/HTTP) o true (prod/HTTPS)
SESSION_TIMEOUT=3600                           # Segundos (default: 1 hora)
REFRESH_THRESHOLD=300                          # Segundos antes de expirar para refrescar (default: 5 min)
```

### 5.2 Variables Opcionales (Federation con Entra ID)

```bash
# ============================================
# Federation Microsoft Entra ID - OPCIONALES
# ============================================
ENABLE_FEDERATION=false                        # true para habilitar boton "Login con Microsoft"
COGNITO_DOMAIN=tu-dominio.auth.us-east-2.amazoncognito.com  # Requerido si ENABLE_FEDERATION=true
SAML_PROVIDER_NAME=MicrosoftEntraID            # Nombre del IdP en Cognito. Requerido si ENABLE_FEDERATION=true
```

### 5.3 Variable de Control de Autenticacion

```bash
# ============================================
# Toggle de Autenticacion - OPCIONAL
# ============================================
ENABLE_AUTH=true                               # false para desactivar auth completamente (solo dev!)
```

### 5.4 Variable de Redirect URI (por app)

```bash
# ============================================
# URL de tu app - Para OAuth2 redirect (por app)
# ============================================
SITIO_MI_APP=http://localhost:8501             # URL publica de tu app (dev)
# En produccion: SITIO_MI_APP=https://mi-app.tudominio.com
```

### 5.5 Ejemplo Completo - Desarrollo

```bash
# .env (desarrollo local)
COGNITO_USER_POOL_ID=us-east-2_NWHFVYCau
COGNITO_CLIENT_ID=drpdppqmn15ajbd7i6emv1ice
COGNITO_CLIENT_SECRET=1upmhmiobvtkva5tstokv7ko1e613qhkipr0mo8oi6os8bbnlr89
AWS_REGION=us-east-2

COOKIE_DOMAIN=localhost
COOKIE_SECURE=false
COOKIE_ENCRYPTION_KEY=pIWj_wMBdN-epIRXG2dg_WrmIw1SuiLLFiduF95Dr3I
SESSION_TIMEOUT=3600
REFRESH_THRESHOLD=300

ENABLE_FEDERATION=false
ENABLE_AUTH=true

SITIO_MI_APP=http://localhost:8501
```

### 5.6 Ejemplo Completo - Produccion/Stage

```bash
# .env.stage (produccion)
COGNITO_USER_POOL_ID=us-east-2_HG1xcjpUr
COGNITO_CLIENT_ID=2jfk0ra99pp7pg7soq4mbakum2
COGNITO_CLIENT_SECRET=f3172degfsijdrsg5frs90jmdb25f3bi1gt9t9hgk7sssim1cmp
AWS_REGION=us-east-2

COOKIE_DOMAIN=.tudominio.com
COOKIE_SECURE=true
COOKIE_ENCRYPTION_KEY=ooghah0ouchuz5Sae-hethiemida_4oh
SESSION_TIMEOUT=3600
REFRESH_THRESHOLD=300

ENABLE_FEDERATION=true
COGNITO_DOMAIN=tu-app.auth.us-east-2.amazoncognito.com
SAML_PROVIDER_NAME=EntraId

ENABLE_AUTH=true

SITIO_MI_APP=https://mi-app.tudominio.com
```

### 5.7 Validaciones Automaticas

El modulo `config.py` valida automaticamente:

| Variable | Validacion | Error si falla |
|----------|-----------|----------------|
| `COGNITO_USER_POOL_ID` | Contiene `_` (formato `region_ID`) | `ConfigurationError` |
| `COGNITO_CLIENT_ID` | Minimo 10 caracteres | `ConfigurationError` |
| `COGNITO_CLIENT_SECRET` | Minimo 10 caracteres | `ConfigurationError` |
| `AWS_REGION` | Empieza con `us-`, `eu-`, `ap-`, `sa-`, `ca-`, `me-`, `af-` | `ConfigurationError` |
| `COOKIE_ENCRYPTION_KEY` | Minimo 32 caracteres, no valor default | `ConfigurationError` |
| `SESSION_TIMEOUT` | Entero entre 1 y 86400 | `ConfigurationError` |
| `REFRESH_THRESHOLD` | Entero positivo, menor que SESSION_TIMEOUT | `ConfigurationError` |
| `COGNITO_DOMAIN` | No vacio si `ENABLE_FEDERATION=true` | `ConfigurationError` |
| `SAML_PROVIDER_NAME` | No vacio si `ENABLE_FEDERATION=true` | `ConfigurationError` |

---

## 6. Integracion en una App Streamlit

### 6.1 Patron Completo (Copiar y Adaptar)

```python
"""
mi_nueva_app.py - Aplicacion Streamlit con autenticacion Cognito
"""
import os
import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

# ============================================
# PASO 1: Cargar variables de entorno
# ============================================
# Cargar .env solo si existe (en Docker las vars vienen del compose)
env_file = Path(__file__).parent / '.env'
if env_file.exists():
    load_dotenv(dotenv_path=env_file, override=False)

# ============================================
# PASO 2: Configurar sys.path
# ============================================
# Esto garantiza que Python encuentre utils/auth/ correctamente
current_file = Path(__file__).resolve()
streamlit_dir = current_file.parent
root_dir = streamlit_dir.parent.parent  # Ajustar segun estructura

# Asegurar que streamlit_dir tenga prioridad sobre root_dir
for p in [str(root_dir), str(streamlit_dir)]:
    if p in sys.path:
        sys.path.remove(p)
sys.path.insert(0, str(streamlit_dir))
sys.path.insert(1, str(root_dir))

# ============================================
# PASO 3: Importar modulos de autenticacion
# ============================================
from utils.auth.cognito_auth import CognitoAuth
from utils.auth.config import AuthConfig, ConfigurationError

# ============================================
# PASO 4: Funcion de inicializacion de auth
# ============================================
def get_auth():
    """
    Inicializa y retorna la instancia de autenticacion.

    Se cachea la configuracion en session_state para no recargarla
    en cada rerun de Streamlit. El objeto CognitoAuth se re-crea
    cada vez porque usa widgets de Streamlit (cookies).

    IMPORTANTE: Ajustar redirect_uri con la variable de entorno
    correspondiente a esta app y su URL por defecto.
    """
    if 'auth_config' not in st.session_state:
        try:
            config = AuthConfig.from_env()
            # ADAPTAR: Cambiar variable de entorno y URL default segun tu app
            config.redirect_uri = os.getenv(
                'SITIO_MI_APP',           # Variable de entorno para esta app
                'http://localhost:8501'     # URL default (cambiar puerto si es diferente)
            )
            st.session_state['auth_config'] = config
        except ConfigurationError as e:
            st.error(f"Error de configuracion de autenticacion: {str(e)}")
            st.stop()

    return CognitoAuth(st.session_state['auth_config'])

# ============================================
# PASO 5: Funcion principal con auth guard
# ============================================
def main():
    st.set_page_config(
        page_title="Mi App",
        page_icon="🏢",
        layout="wide"
    )

    # --- Toggle de autenticacion ---
    enable_auth = os.getenv('ENABLE_AUTH', 'true').lower() == 'true'

    if enable_auth:
        # --- Logout diferido (ver nota abajo) ---
        # cookies.save() solo puede ejecutarse UNA VEZ por ciclo de Streamlit.
        # is_authenticated() ya lo usa internamente, asi que logout() no puede
        # llamar save() en el mismo ciclo. Solucion: diferir el logout al
        # siguiente ciclo usando un flag en session_state.
        if st.session_state.get('_pending_logout'):
            del st.session_state['_pending_logout']
            auth = get_auth()
            auth.logout()
            st.rerun()

        # Verificar autenticacion ANTES de renderizar contenido
        auth = get_auth()

        if not auth.is_authenticated():
            auth.render_login_screen()
            st.stop()

        # Mantener cookies SSO con dominio padre en cada render
        from utils.auth.cookie_domain_fix import ensure_cookie_domain_fix
        ensure_cookie_domain_fix()

        # Obtener info del usuario autenticado
        user = auth.get_current_user()

        # Mostrar info de usuario en sidebar
        with st.sidebar:
            if user:
                st.write(f"**{user['email']}**")
            if st.button("Cerrar Sesion", use_container_width=True, type="secondary"):
                st.session_state['_pending_logout'] = True
                st.rerun()

    # ============================================
    # CONTENIDO PROTEGIDO DE TU APP
    # ============================================
    st.title("Mi Aplicacion")
    st.write("Este contenido solo es visible para usuarios autenticados.")

    # ... tu codigo aqui ...

# ============================================
# PASO 6: Entry point
# ============================================
if __name__ == "__main__":
    main()
```

### 6.2 Que Adaptar en Cada App Nueva

| Elemento | Que cambiar | Ejemplo |
|----------|-------------|---------|
| `SITIO_MI_APP` | Nombre de la variable de entorno para redirect_uri de esta app | `SITIO_DENUE`, `SITIO_CENSO` |
| URL default | Puerto de desarrollo de esta app | `http://localhost:8502` |
| `page_title` | Titulo de la pagina | `"DENUE Analysis"` |
| `sys.path` | Ajustar `root_dir` segun profundidad de directorios | `streamlit_dir.parent.parent` |

### 6.3 Funcionalidades Incluidas Automaticamente

Al usar el patron anterior, tu app obtiene:

- Login con usuario/password (Cognito directo)
- Login con Microsoft Entra ID (si `ENABLE_FEDERATION=true`)
- Cambio de password obligatorio en primer login
- Sesiones persistentes con cookies encriptadas
- Auto-refresh de tokens antes de expirar
- SSO con otras apps (si comparten `COOKIE_ENCRYPTION_KEY`)
- UI de login en espanol con branding GFG
- Toggle para desactivar auth (`ENABLE_AUTH=false`)

---

## 7. Cognito con Microsoft Entra ID (SAML Federation)

### 7.1 Arquitectura de Federation

```
Tu App Streamlit                AWS Cognito                Microsoft Entra ID
     │                              │                           │
     │  1. Click "Login Microsoft"  │                           │
     │─────────────────────────────>│                           │
     │                              │  2. SAML AuthnRequest     │
     │                              │──────────────────────────>│
     │                              │                           │  3. User authenticates
     │                              │                           │     (SSO if logged in)
     │                              │  4. SAML Assertion        │
     │                              │<──────────────────────────│
     │  5. Authorization Code       │                           │
     │<─────────────────────────────│                           │
     │  6. Exchange code for tokens │                           │
     │─────────────────────────────>│                           │
     │  7. JWT Tokens (id, access,  │                           │
     │     refresh)                 │                           │
     │<─────────────────────────────│                           │
     │                              │                           │
     │  [Tokens identicos al login local - mismo flujo de ahi en adelante]
```

### 7.2 Configuracion en Microsoft Entra ID

1. **Azure Portal → Enterprise Applications → New Application**
   - Nombre: `GFG ALMENA INEGI` (o el nombre de tu proyecto)

2. **Single sign-on → SAML**
   - **Entity ID (Identifier):**
     ```
     urn:amazon:cognito:sp:{USER_POOL_ID}
     ```
     Ejemplo: `urn:amazon:cognito:sp:us-east-2_HG1xcjpUr`

   - **Reply URL (Assertion Consumer Service):**
     ```
     https://{COGNITO_DOMAIN}/saml2/idpresponse
     ```
     Ejemplo: `https://gfg-almena.auth.us-east-2.amazoncognito.com/saml2/idpresponse`

   - **Sign on URL:** Dejar en blanco (SP-initiated flow)

3. **Attributes & Claims:**
   - **NameID:** `user.objectid` (immutable, evita duplicados)
   - Claims adicionales: `user.email`, `user.displayname`

4. **Users and Groups:**
   - Asignar los usuarios o grupos que tendran acceso

5. **Obtener Federation Metadata URL:**
   - Copiar la URL del XML de metadata (necesaria para Cognito)
   - Formato: `https://login.microsoftonline.com/{TENANT_ID}/federationmetadata/2007-06/federationmetadata.xml?appid={APP_ID}`

### 7.3 Configuracion en AWS Cognito

1. **User Pool → Sign-in experience → Federated identity provider sign-in**
   - Add identity provider → SAML

2. **Configurar SAML IdP:**
   - **Provider name:** `EntraId` (o `MicrosoftEntraID`)
     - Este es el valor de `SAML_PROVIDER_NAME`
   - **Metadata document:** Pegar URL de metadata de Entra ID
   - **Attribute mapping:**
     - email → http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress

3. **App Client → Hosted UI:**
   - **Identity providers:** Seleccionar `EntraId`
   - **OAuth 2.0 Grant types:** `Authorization code grant`
   - **Callback URLs:** Agregar TODAS las URLs de tus apps:
     ```
     http://localhost:8501/
     http://localhost:8502/
     http://localhost:8503/
     https://app1.tudominio.com/
     https://app2.tudominio.com/
     https://app3.tudominio.com/
     ```
   - **Scopes:** `openid`, `email`, `phone`

### 7.4 Variables de Entorno para Federation

```bash
ENABLE_FEDERATION=true
COGNITO_DOMAIN=tu-app.auth.us-east-2.amazoncognito.com
SAML_PROVIDER_NAME=EntraId
```

### 7.5 Que Sucede en el Codigo

Cuando `ENABLE_FEDERATION=true`, el modulo automaticamente:

1. **En `render_login_screen()`:** Muestra el boton "Iniciar sesion con Microsoft" encima del formulario de login local, separado por un divider `--- o ---`.

2. **En `is_authenticated()`:** Antes de verificar tokens, llama a `_handle_oauth_callback()` que detecta si hay un `code` en los query params (redirect de Cognito) y lo intercambia por tokens.

3. **Proteccion CSRF:** El `state` parameter usa HMAC-SHA256 firmado con el client_secret. No requiere almacenamiento server-side (Streamlit pierde session_state en redirects). Formato: `{random}.{timestamp}.{signature}`. Expira en 600 segundos.

---

## 8. Toggle de Funcionalidad (Prender/Apagar Auth)

### 8.1 Desactivar Autenticacion Completamente

Para desactivar la autenticacion (solo para desarrollo o demos):

```bash
# En .env
ENABLE_AUTH=false
```

En tu app, el patron de la seccion 6.1 ya lo maneja:

```python
enable_auth = os.getenv('ENABLE_AUTH', 'true').lower() == 'true'

if enable_auth:
    auth = get_auth()
    if not auth.is_authenticated():
        auth.render_login_screen()
        st.stop()
    # ... SSO fix, user info, logout button ...

# Contenido de la app (siempre se ejecuta si auth esta desactivado)
```

> **ADVERTENCIA:** Nunca desactivar autenticacion en produccion/stage.
> La variable `ENABLE_AUTH` debe ser `true` (o no estar definida) en cualquier
> ambiente que no sea desarrollo local.

### 8.2 Desactivar Solo Federation (Mantener Login Local)

```bash
# En .env
ENABLE_AUTH=true
ENABLE_FEDERATION=false  # Solo login con usuario/password
```

Esto oculta el boton "Iniciar sesion con Microsoft" pero mantiene el formulario de login local contra Cognito.

### 8.3 Matriz de Configuracion

| ENABLE_AUTH | ENABLE_FEDERATION | Resultado |
|-------------|-------------------|-----------|
| `true` | `true` | Login local + Login Microsoft |
| `true` | `false` | Solo login local (username/password) |
| `false` | (ignorado) | Sin autenticacion - acceso libre |
| (no definido) | (no definido) | `true` / `false` por default = Solo login local |

---

## 9. Single Sign-On (SSO) entre Apps

### 9.1 Como Funciona el SSO

El SSO se logra mediante cookies compartidas entre todas las apps Streamlit:

```
App 1 (puerto 8501)  ─┐
App 2 (puerto 8502)  ─┤── Comparten cookies gfg_* ──> SSO
App 3 (puerto 8503)  ─┤   (misma COOKIE_ENCRYPTION_KEY)
App N (puerto XXXX)  ─┘
```

Las cookies almacenadas:

| Cookie | Contenido |
|--------|-----------|
| `gfg_auth_token` | JWT ID token (encriptado) |
| `gfg_token_expiry` | Timestamp de expiracion |
| `gfg_refresh_token` | Refresh token (encriptado) |

### 9.2 Requisitos para SSO

1. **Mismo `COOKIE_ENCRYPTION_KEY`** en TODAS las apps
   - Si una app tiene una key diferente, no podra leer las cookies de las demas

2. **Mismo `COOKIE_DOMAIN`** en TODAS las apps
   - Desarrollo: `localhost` (funciona automaticamente entre puertos)
   - Produccion: `.tudominio.com` (con punto al inicio, para subdominios)

3. **HTTPS en produccion** (requerido para cookies `Secure`)

4. **Nginx con sub_filter** para fix de dominio de cookies en produccion (ver seccion 10)

### 9.3 Por que se Necesita un Fix de Cookies

`streamlit-cookies-manager` establece cookies desde **JavaScript** (`document.cookie`), no desde headers HTTP `Set-Cookie`. Esto significa:

- `proxy_cookie_domain` de nginx **NO funciona** (solo modifica headers HTTP)
- Las cookies se crean en el subdominio actual (ej: `app1.tudominio.com`)
- NO son visibles desde otros subdominios (`app2.tudominio.com`)

**Solucion:** Inyectar JavaScript que intercepta los writes de cookies y agrega `domain=.tudominio.com`.

El modulo `cookie_domain_fix.py` maneja esto automaticamente:

- `apply_cookie_domain_fix_on_login()`: Se llama despues de login exitoso
- `ensure_cookie_domain_fix()`: Se llama en cada render para mantener el fix

### 9.4 SSO en Desarrollo Local

En desarrollo local (`COOKIE_DOMAIN=localhost`), el SSO funciona automaticamente entre diferentes puertos del mismo host. No se necesita nginx ni fix de cookies.

---

## 10. Configuracion de Nginx para SSO

### 10.1 Nginx Server Block (Template Generico)

Para **cada app Streamlit** que necesite SSO, usar este template:

```nginx
# Template: Reemplazar {APP_NAME}, {SUBDOMAIN}, {PORT}, {DOMAIN}
server {
    server_name {SUBDOMAIN}.{DOMAIN};  # ej: mi-app.tudominio.com

    location / {
        proxy_pass http://localhost:{PORT};  # ej: 8501
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
        proxy_connect_timeout 86400;
        proxy_send_timeout 86400;

        # SSO: Migrar cookies HTTP del upstream al dominio padre
        proxy_cookie_domain {SUBDOMAIN}.{DOMAIN} .{DOMAIN};
        proxy_cookie_flags ~ secure samesite=lax;

        # SSO: Inyectar JavaScript para fix de dominio en cookies JS
        # CRITICO: Deshabilitar gzip para que sub_filter pueda procesar HTML
        proxy_set_header Accept-Encoding "";

        # El script tiene 2 capas:
        # 1) Interceptor Document.prototype.cookie: auto-agrega domain a gfg_*
        # 2) Polling (1s): re-crea cookies gfg_* en dominio padre
        sub_filter '</head>' '<script>(function(){var d=Object.getOwnPropertyDescriptor(Document.prototype,"cookie"),os=d.set,og=d.get;Object.defineProperty(Document.prototype,"cookie",{get:function(){return og.call(this)},set:function(v){var e=v.indexOf("=");if(e>0&&v.substring(0,e).trim().indexOf("gfg_")===0&&v.toLowerCase().indexOf("domain=")===-1)v+=";domain=.{DOMAIN}";os.call(this,v)},configurable:!0});function m(){var c=og.call(document).split(";"),i,p,e,n,val;for(i=0;i<c.length;i++){p=c[i].trim();e=p.indexOf("=");if(e<=0)continue;n=p.substring(0,e);if(n.indexOf("gfg_")!==0)continue;val=p.substring(e+1);os.call(document,n+"=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/");os.call(document,n+"="+val+";path=/;domain=.{DOMAIN};SameSite=Lax"+(location.protocol==="https:"?";Secure":""))}}m();setInterval(m,1000);console.log("[SSO]init")})();</script></head>';
        sub_filter_once on;
    }

    # WebSocket support for Streamlit (requerido para st.rerun y widgets)
    location /_stcore/stream {
        proxy_pass http://localhost:{PORT}/_stcore/stream;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }

    access_log /var/log/nginx/{SUBDOMAIN}.{DOMAIN}.access.log;
    error_log /var/log/nginx/{SUBDOMAIN}.{DOMAIN}.error.log;

    # SSL (gestionado por Certbot)
    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/{SUBDOMAIN}.{DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{SUBDOMAIN}.{DOMAIN}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

# Redirect HTTP → HTTPS
server {
    if ($host = {SUBDOMAIN}.{DOMAIN}) {
        return 301 https://$host$request_uri;
    }
    listen 80;
    server_name {SUBDOMAIN}.{DOMAIN};
    return 404;
}
```

### 10.2 Ejemplo Concreto

Para una app en puerto 8506 con dominio `mi-nueva-app.firmasglobales.mx`:

1. Reemplazar `{SUBDOMAIN}` → `mi-nueva-app`
2. Reemplazar `{DOMAIN}` → `firmasglobales.mx`
3. Reemplazar `{PORT}` → `8506`

### 10.3 Aplicar Configuracion

```bash
# 1. Guardar configuracion
sudo nano /etc/nginx/sites-available/mi-nueva-app.firmasglobales.mx

# 2. Crear symlink
sudo ln -s /etc/nginx/sites-available/mi-nueva-app.firmasglobales.mx \
           /etc/nginx/sites-enabled/

# 3. Validar sintaxis
sudo nginx -t

# 4. Recargar nginx
sudo systemctl reload nginx

# 5. Obtener certificado SSL
sudo certbot --nginx -d mi-nueva-app.firmasglobales.mx
```

### 10.4 Verificar SSO

1. Login en `app1.tudominio.com`
2. En DevTools → Application → Cookies verificar:
   - `gfg_auth_token` tiene `Domain: .tudominio.com`
   - `Secure: true`
   - `SameSite: Lax`
3. En Console buscar: `[SSO]init`
4. Navegar a `app2.tudominio.com` → NO debe pedir login

---

## 11. Docker y Deployment

### 11.1 Docker Compose (Template)

```yaml
services:
  mi-app-streamlit:
    image: tu-registro/mi-app:latest
    ports:
      - "8501:8501"
    environment:
      # Cognito Auth
      COGNITO_USER_POOL_ID: ${COGNITO_USER_POOL_ID}
      COGNITO_CLIENT_ID: ${COGNITO_CLIENT_ID}
      COGNITO_CLIENT_SECRET: ${COGNITO_CLIENT_SECRET}
      AWS_REGION: ${AWS_REGION}

      # Cookies y SSO
      COOKIE_DOMAIN: ${COOKIE_DOMAIN}
      COOKIE_SECURE: ${COOKIE_SECURE}
      COOKIE_ENCRYPTION_KEY: ${COOKIE_ENCRYPTION_KEY}
      SESSION_TIMEOUT: ${SESSION_TIMEOUT:-3600}
      REFRESH_THRESHOLD: ${REFRESH_THRESHOLD:-300}

      # Federation (opcional)
      COGNITO_DOMAIN: ${COGNITO_DOMAIN:-}
      SAML_PROVIDER_NAME: ${SAML_PROVIDER_NAME:-}
      ENABLE_FEDERATION: ${ENABLE_FEDERATION:-false}

      # Toggle auth
      ENABLE_AUTH: ${ENABLE_AUTH:-true}

      # Redirect URI de esta app
      SITIO_MI_APP: ${SITIO_MI_APP}
```

### 11.2 Ejecutar con Archivo de Entorno

```bash
# Desarrollo (lee .env por default)
docker-compose up -d

# Stage/Produccion (CRITICO: usar --env-file)
docker-compose --env-file .env.stage up -d
```

> **ADVERTENCIA:** Sin `--env-file .env.stage`, docker-compose lee `.env` de desarrollo
> y los contenedores reciben credenciales de Cognito equivocadas.

### 11.3 Dockerfile (Dependencias Relevantes)

Asegurar que el Dockerfile instale las dependencias de auth:

```dockerfile
# En el Dockerfile
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Verificar que incluya:
# boto3, python-jose[cryptography], PyJWT, streamlit-cookies-manager
```

---

## 12. Troubleshooting

### 12.1 Errores Comunes

#### Error: "Faltan las siguientes variables de entorno requeridas"

**Causa:** Las variables de Cognito no estan definidas.

**Solucion:**
- Desarrollo: Verificar que `.env` existe y contiene las variables
- Docker: Verificar que `docker-compose.yml` pasa las variables y que se usa `--env-file` correcto

```bash
# Verificar variables en el contenedor
docker exec -it mi-contenedor env | grep COGNITO
```

#### Error: "Error de conexion con el servicio de autenticacion" (ClientError)

**Causa:** El App Client de Cognito no tiene habilitado `USER_PASSWORD_AUTH`.

**Solucion:**
1. AWS Console → Cognito → User Pool → App clients
2. Seleccionar el App Client
3. Authentication Flows → Habilitar `USER_PASSWORD_AUTH`
4. Guardar cambios

#### Error: "COOKIE_ENCRYPTION_KEY debe tener al menos 32 caracteres"

**Causa:** La clave de encriptacion es muy corta.

**Solucion:** Generar una clave segura:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

#### SSO no funciona entre apps

**Verificar:**
1. Todas las apps usan el mismo `COOKIE_ENCRYPTION_KEY`
2. Todas las apps usan el mismo `COOKIE_DOMAIN`
3. En produccion: nginx tiene `sub_filter` configurado
4. En produccion: `Accept-Encoding ""` esta habilitado en nginx
5. Verificar en Console del browser que aparezca `[SSO]init`

#### Error: "StreamlitDuplicateElementKey" al cerrar sesion

**Causa:** `streamlit-cookies-manager` usa un componente con key fija (`CookieManager.sync_cookies.save`). `is_authenticated()` renderiza el componente de cookies (primer `.save()`), y luego `auth.logout()` intenta un segundo `.save()` en el mismo ciclo de Streamlit, lo que causa el error de key duplicada.

**Solucion:** Nunca llamar `auth.logout()` directamente en el mismo ciclo donde ya se llamo `is_authenticated()`. Usar el patron de logout diferido:

```python
# AL INICIO del bloque de auth (antes de is_authenticated)
if st.session_state.get('_pending_logout'):
    del st.session_state['_pending_logout']
    auth = get_auth()
    auth.logout()
    st.rerun()

# ... is_authenticated(), contenido, etc. ...

# EN EL BOTON de cerrar sesion (NO llamar auth.logout() aqui)
if st.button("Cerrar Sesion"):
    st.session_state['_pending_logout'] = True
    st.rerun()
```

Flujo: click → flag + rerun → siguiente ciclo ejecuta logout (unico save) → rerun → login screen.

#### Federation: "Error de seguridad: estado de autenticacion invalido"

**Causa:** El HMAC state expiro (>600 segundos) o fue manipulado.

**Solucion:** Intentar login de nuevo. Si persiste:
- Verificar que el reloj del servidor esta sincronizado (NTP)
- Verificar que `COGNITO_CLIENT_SECRET` es el correcto

#### Federation: Token exchange falla

**Causa:** La `redirect_uri` no coincide con la registrada en Cognito.

**Solucion:**
1. Verificar que `SITIO_MI_APP` tenga la URL exacta de la app
2. Verificar que esa URL este registrada como Callback URL en el App Client de Cognito
3. La URL debe incluir trailing slash si Cognito la tiene configurada asi

### 12.2 Debugging

Activar logs detallados:

```bash
# En .env
LOG_LEVEL=DEBUG
```

Los logs aparecen en stdout con prefijos:
- `[AUTH-DEBUG]` - Flujo de autenticacion
- `[OAUTH2-DEBUG]` - Flujo de federation
- `[TOKEN-DEBUG]` - Validacion de tokens

---

## 13. Referencia de API del Modulo

### 13.1 CognitoAuth

```python
from utils.auth.cognito_auth import CognitoAuth

auth = CognitoAuth(config: AuthConfig)

# Verificar si el usuario esta autenticado
auth.is_authenticated() -> bool

# Autenticar con usuario/password
auth.login(username: str, password: str) -> bool

# Completar cambio de password obligatorio
auth.complete_new_password_challenge(new_password: str) -> bool

# Obtener informacion del usuario actual
auth.get_current_user() -> Optional[Dict]
# Retorna: {'username': str, 'email': str, 'user_id': str, 'attributes': dict}

# Cerrar sesion
auth.logout() -> None

# Renderizar pantalla de login
auth.render_login_screen() -> None
```

### 13.2 AuthConfig

```python
from utils.auth.config import AuthConfig, ConfigurationError

# Cargar desde variables de entorno
config = AuthConfig.from_env() -> AuthConfig

# Campos del config
config.user_pool_id: str
config.client_id: str
config.client_secret: str
config.region: str
config.cookie_domain: str          # default: "localhost"
config.cookie_secure: bool         # default: False
config.cookie_encryption_key: str  # min 32 chars
config.session_timeout: int        # default: 3600
config.refresh_threshold: int      # default: 300
config.cognito_domain: str         # default: "" (para federation)
config.redirect_uri: str           # default: "" (se setea por app)
config.saml_provider_name: str     # default: ""
config.enable_federation: bool     # default: False
```

### 13.3 SessionManager

```python
from utils.auth.session_manager import SessionManager

sm = SessionManager(config: AuthConfig)

sm.get_session_token() -> Optional[str]     # Obtener JWT token
sm.set_session_token(token, expires_in, refresh_token=None) -> None
sm.clear_session() -> None                  # Logout
sm.is_token_expiring_soon() -> bool         # Necesita refresh?
sm.get_time_until_expiry() -> Optional[int] # Segundos hasta expirar
sm.get_refresh_token() -> Optional[str]     # Obtener refresh token
```

### 13.4 TokenValidator

```python
from utils.auth.token_validator import TokenValidator, ValidationError

tv = TokenValidator(config: AuthConfig)

tv.validate_token(token: str) -> Tuple[bool, Optional[Dict]]
tv.is_token_expired(token: str) -> bool
tv.get_time_until_expiry(token: str) -> int
tv.decode_token(token: str) -> Dict  # Sin validacion (debug)
```

### 13.5 OAuth2Handler

```python
from utils.auth.oauth2_handler import OAuth2Handler, OAuth2Error

oh = OAuth2Handler(config: AuthConfig)

oh.get_authorize_url(identity_provider="EntraId", secret_key="") -> Tuple[str, str]
oh.exchange_code_for_tokens(code: str) -> Dict

# Metodos estaticos
OAuth2Handler.generate_state(secret_key: str) -> str
OAuth2Handler.validate_state(state: str, secret_key: str, max_age: int = 600) -> bool
```

### 13.6 Cookie Domain Fix

```python
from utils.auth.cookie_domain_fix import (
    inject_cookie_domain_fix,
    apply_cookie_domain_fix_on_login,
    ensure_cookie_domain_fix,
)

inject_cookie_domain_fix(domain=".tudominio.com")  # Inyectar JS
apply_cookie_domain_fix_on_login(domain=None)       # Llamar despues de login
ensure_cookie_domain_fix()                          # Llamar en cada render
```

---

## 14. Checklist de Implementacion

### Para una Nueva App Streamlit

- [ ] Copiar `utils/auth/` al directorio de la app
- [ ] Agregar dependencias al `requirements.txt` (boto3, python-jose, PyJWT, streamlit-cookies-manager)
- [ ] Crear/actualizar `.env` con variables de Cognito y cookies
- [ ] Implementar patron de auth en el entry point de la app (seccion 6.1)
- [ ] Definir variable `SITIO_MI_APP` con URL de la app
- [ ] Probar login local en desarrollo
- [ ] Probar logout y persistencia de sesion

### Para Habilitar Federation (Entra ID)

- [ ] Configurar Enterprise Application en Entra ID (seccion 7.2)
- [ ] Configurar SAML IdP en Cognito (seccion 7.3)
- [ ] Agregar Callback URL de la nueva app en Cognito App Client
- [ ] Setear `ENABLE_FEDERATION=true`, `COGNITO_DOMAIN`, `SAML_PROVIDER_NAME`
- [ ] Probar login con Microsoft

### Para SSO entre Apps

- [ ] Todas las apps usan el mismo `COOKIE_ENCRYPTION_KEY`
- [ ] Todas las apps usan el mismo `COOKIE_DOMAIN`
- [ ] Configurar nginx con sub_filter para cada app (seccion 10)
- [ ] Verificar cookies en DevTools: domain `.tudominio.com`, Secure, SameSite=Lax
- [ ] Verificar `[SSO]init` en Console del browser
- [ ] Probar: login en app1 → navegar a app2 sin re-login

### Para Docker/Produccion

- [ ] Variables en `docker-compose.yml` o `.env.stage`
- [ ] Usar `docker-compose --env-file .env.stage up -d`
- [ ] Verificar que `COOKIE_SECURE=true` y `COOKIE_DOMAIN=.tudominio.com`
- [ ] SSL habilitado (Certbot)
- [ ] nginx configurado con sub_filter para SSO
- [ ] Probar flujo completo end-to-end

---

## Archivos de Referencia

| Archivo | Descripcion |
|---------|-------------|
| `frontend/streamlit/utils/auth/cognito_auth.py` | Clase principal de autenticacion |
| `frontend/streamlit/utils/auth/config.py` | Configuracion y validacion |
| `frontend/streamlit/utils/auth/token_validator.py` | Validacion JWT con JWKS |
| `frontend/streamlit/utils/auth/session_manager.py` | Gestion de sesiones y cookies |
| `frontend/streamlit/utils/auth/oauth2_handler.py` | OAuth2 Authorization Code flow |
| `frontend/streamlit/utils/auth/cookie_domain_fix.py` | Fix JavaScript para SSO |
| `frontend/streamlit/nginx-sso-config.conf` | Configuracion nginx real (referencia) |

---

**Ultima actualizacion:** 5 de Marzo de 2026
**Version del documento:** 2.0
