"""
Fix para forzar el dominio correcto en cookies de streamlit-cookies-manager.

streamlit-cookies-manager establece cookies desde JavaScript en el navegador,
por lo que nginx no puede modificar el dominio. Este módulo inyecta JavaScript
para modificar las cookies después de que se crean.
"""

import streamlit as st
import streamlit.components.v1 as components
from typing import Optional


def inject_cookie_domain_fix(domain: str = ".firmasglobales.mx"):
    """
    Inyecta JavaScript para modificar el dominio de las cookies gfg_*.
    
    Este script se ejecuta en el navegador y modifica las cookies existentes
    para cambiar su dominio al especificado.
    
    Args:
        domain: Dominio a usar (debe empezar con punto para subdominios)
    """
    
    js_code = f"""
    <script>
    (function() {{
        // Parsea cookies desde document.cookie sin corromper valores que contengan '='
        // (los tokens Fernet de EncryptedCookieManager son base64 y terminan en '=' o '==')
        function getAllCookies() {{
            const cookies = {{}};
            const cookieString = document.cookie;

            if (cookieString) {{
                const cookieArray = cookieString.split(';');
                cookieArray.forEach(cookie => {{
                    const trimmed = cookie.trim();
                    const eqIndex = trimmed.indexOf('=');
                    if (eqIndex > 0) {{
                        const name = trimmed.substring(0, eqIndex);
                        // Preserva el valor íntegro (incluyendo cualquier '=' de padding base64)
                        const value = trimmed.substring(eqIndex + 1);
                        cookies[name] = value;
                    }}
                }});
            }}

            return cookies;
        }}

        // Re-crea una cookie con el dominio padre; el valor se copia raw sin decode/encode
        // para evitar corromper tokens encriptados
        function fixCookieDomain(name, value) {{
            // Eliminar cookie con dominio actual (subdomain)
            document.cookie = name + "=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";

            // Crear cookie con dominio padre
            const secure = window.location.protocol === 'https:' ? '; Secure' : '';
            document.cookie = name + "=" + value +
                            "; path=/" +
                            "; domain={domain}" +
                            secure +
                            "; SameSite=Lax";

            console.log('Cookie domain fixed:', name, '-> {domain}');
        }}

        // Obtener todas las cookies y corregir solo las gfg_*
        const cookies = getAllCookies();

        Object.keys(cookies).forEach(name => {{
            if (name.startsWith('gfg_')) {{
                fixCookieDomain(name, cookies[name]);
            }}
        }});

        console.log('Cookie domain fix applied for {domain}');
    }})();
    </script>
    """
    
    # Renderizar JavaScript (invisible)
    components.html(js_code, height=0)


def apply_cookie_domain_fix_on_login(domain: Optional[str] = None):
    """
    Aplica el fix de dominio de cookies después del login.
    
    Debe llamarse después de que el usuario haga login exitosamente.
    
    Args:
        domain: Dominio a usar (None = usar de config)
    """
    from .config import AuthConfig
    
    if domain is None:
        try:
            config = AuthConfig.from_env()
            domain = config.cookie_domain
        except:
            # Fallback a localhost si no hay config
            domain = "localhost"
    
    # Solo aplicar si no es localhost (en producción/stage)
    if domain != "localhost":
        inject_cookie_domain_fix(domain)
        
        # Guardar en session state que ya se aplicó el fix
        st.session_state['_cookie_domain_fix_applied'] = True


def ensure_cookie_domain_fix():
    """
    Asegura que el fix de dominio se aplique si hay una sesión activa.

    Debe llamarse en cada render de cada app (después del auth check exitoso).
    Esto compensa el hecho de que EncryptedCookieManager puede re-escribir
    cookies sin atributo domain en renders subsiguientes.
    """
    from .config import AuthConfig

    try:
        config = AuthConfig.from_env()
    except Exception:
        return

    # En desarrollo local no hay subdominios que compartir
    if config.cookie_domain == "localhost":
        return

    # Solo inyectar si hay un token activo (en session_state o en cookies)
    if 'auth_token' in st.session_state:
        inject_cookie_domain_fix(config.cookie_domain)
