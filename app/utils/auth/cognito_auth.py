"""
Main authentication class for AWS Cognito integration.

This module provides the CognitoAuth class that orchestrates authentication
flows including login, logout, session validation, and UI rendering.
"""

import hmac
import hashlib
import base64
import logging
from typing import Optional, Dict, Any
import streamlit as st
import boto3
from botocore.exceptions import ClientError, BotoCoreError

from .config import AuthConfig, ConfigurationError
from .session_manager import SessionManager
from .token_validator import TokenValidator, ValidationError
from .cookie_domain_fix import apply_cookie_domain_fix_on_login
from .oauth2_handler import OAuth2Handler, OAuth2Error
from .almena_logo import ALMENA_LOGO_B64  # logo ALMENA embebido (base64) para el login


# Configure logging
logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Base exception for authentication errors."""
    def __init__(self, message: str, error_type: str, details: Dict = None):
        self.message = message
        self.error_type = error_type
        self.details = details or {}
        super().__init__(self.message)


class NetworkError(AuthError):
    """Network and connectivity errors."""
    def __init__(self, message: str, details: Dict = None):
        super().__init__(message, "network", details)


class AuthenticationError(AuthError):
    """Authentication and authorization errors."""
    def __init__(self, message: str, details: Dict = None):
        super().__init__(message, "authentication", details)


class CognitoAuth:
    """
    Main authentication class integrating with AWS Cognito.
    
    This class provides:
    - User authentication with Cognito User Pool
    - Session management with JWT tokens
    - Single Sign-On (SSO) across all 4 Streamlit apps
    - Login UI rendering
    - Logout functionality
    - Token validation and refresh
    
    Usage:
        config = AuthConfig.from_env()
        auth = CognitoAuth(config)
        
        if not auth.is_authenticated():
            auth.render_login_screen()
            st.stop()
        
        user = auth.get_current_user()
        st.write(f"Bienvenido, {user['email']}")
    """
    
    def __init__(self, config: AuthConfig):
        """
        Initialize CognitoAuth with configuration.
        
        Args:
            config: AuthConfig object with Cognito settings
            
        Raises:
            ConfigurationError: If configuration is invalid
        """
        self.config = config
        self.session_manager = SessionManager(config)
        self.token_validator = TokenValidator(config)
        
        # Initialize boto3 Cognito client
        try:
            self.cognito_client = boto3.client(
                'cognito-idp',
                region_name=config.region
            )
            logger.info("Cognito client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Cognito client: {e}")
            raise ConfigurationError(
                f"No se pudo inicializar el cliente de Cognito: {str(e)}"
            )
    
    def _get_secret_hash(self, username: str) -> str:
        """
        Calculate SECRET_HASH for Cognito authentication.
        
        Cognito requires a SECRET_HASH when the app client has a client secret.
        The hash is calculated as: Base64(HMAC_SHA256(username + client_id, client_secret))
        
        Args:
            username: User's username or email
            
        Returns:
            Base64-encoded SECRET_HASH string
        """
        message = bytes(username + self.config.client_id, 'utf-8')
        secret = bytes(self.config.client_secret, 'utf-8')
        dig = hmac.new(secret, message, hashlib.sha256).digest()
        return base64.b64encode(dig).decode()
    
    def _handle_oauth_callback(self) -> None:
        """
        Detect and process OAuth2 callback (authorization code in query params).

        When Cognito redirects back after SAML authentication, the URL contains
        a 'code' parameter. This method exchanges it for JWT tokens and stores
        them in the SessionManager, then clears the URL and reruns the app.
        """
        query_params = st.query_params
        code = query_params.get("code")

        if not code:
            return

        # Validate HMAC-signed state (no server-side storage needed)
        received_state = query_params.get("state", "")
        secret_key = self.config.client_secret
        if not OAuth2Handler.validate_state(received_state, secret_key):
            logger.warning("OAuth2 state validation failed - invalid signature or expired")
            st.query_params.clear()
            st.error("Error de seguridad: estado de autenticacion invalido. Intente de nuevo.")
            return

        # Exchange authorization code for tokens
        try:
            print(f"[OAUTH2-DEBUG] Exchanging code for tokens, redirect_uri={self.config.redirect_uri}", flush=True)
            oauth2 = OAuth2Handler(self.config)
            tokens = oauth2.exchange_code_for_tokens(code)
            print(f"[OAUTH2-DEBUG] Token exchange OK, keys={list(tokens.keys())}", flush=True)

            # Store tokens in SessionManager (identical to local login)
            self.session_manager.set_session_token(
                token=tokens["id_token"],
                expires_in=tokens.get("expires_in", 3600),
                refresh_token=tokens.get("refresh_token")
            )
            print("[OAUTH2-DEBUG] Tokens stored in session manager", flush=True)

            # Store access token in session state
            st.session_state['access_token'] = tokens.get("access_token")

            # Apply SSO cookie domain fix
            apply_cookie_domain_fix_on_login(self.config.cookie_domain)

            # Clean up state and query params
            if 'oauth2_state' in st.session_state:
                del st.session_state['oauth2_state']
            st.query_params.clear()

            print("[OAUTH2-DEBUG] Callback complete, calling rerun", flush=True)
            logger.info("OAuth2 callback processed successfully (federated login)")
            st.rerun()

        except OAuth2Error as e:
            print(f"[OAUTH2-DEBUG] OAuth2Error: {e}", flush=True)
            logger.error(f"OAuth2 token exchange failed: {e}")
            st.query_params.clear()
            st.error(f"Error al completar la autenticacion con Microsoft: {e}")
        except Exception as e:
            print(f"[OAUTH2-DEBUG] Exception: {type(e).__name__}: {e}", flush=True)
            logger.error(f"Unexpected error in OAuth2 callback: {e}", exc_info=True)
            st.query_params.clear()
            st.error(f"Error inesperado durante la autenticacion: {type(e).__name__}: {e}")

    def is_authenticated(self) -> bool:
        """
        Check if current user is authenticated.

        This method:
        1. Handles OAuth2 callback if authorization code is present
        2. Retrieves token from session/cookies
        3. Validates token signature and expiration
        4. Checks if token refresh is needed
        5. Returns authentication status

        Returns:
            True if valid session exists, False otherwise
        """
        # Handle OAuth2 callback if federation is enabled
        if self.config.enable_federation:
            self._handle_oauth_callback()

        # Get token from session manager
        token = self.session_manager.get_session_token()

        if not token:
            has_auth = 'auth_token' in st.session_state
            print(f"[AUTH-DEBUG] No token from session_manager. auth_token in session_state={has_auth}", flush=True)
            return False

        # Validate token
        print(f"[AUTH-DEBUG] Token found, length={len(token)}, validating...", flush=True)
        is_valid, payload = self.token_validator.validate_token(token)

        if not is_valid:
            print(f"[AUTH-DEBUG] Token validation FAILED, clearing session", flush=True)
            self.session_manager.clear_session()
            return False

        print(f"[AUTH-DEBUG] Token valid! user={payload.get('cognito:username', payload.get('email', 'unknown'))}", flush=True)
        
        # Check if token needs refresh
        if self.session_manager.is_token_expiring_soon():
            logger.info("Token expiring soon, attempting refresh")
            success = self._refresh_token()
            if not success:
                logger.warning("Token refresh failed, user needs to re-authenticate")
                self.session_manager.clear_session()
                return False
        
        # Store user info in session state for quick access
        if 'user_info' not in st.session_state:
            st.session_state['user_info'] = self._extract_user_info(payload)
        
        return True
    
    def get_current_user(self) -> Optional[Dict[str, Any]]:
        """
        Get current authenticated user information.
        
        Returns:
            Dict with user information:
                - username: Cognito username
                - email: User's email address
                - user_id: Cognito sub (unique user ID)
                - attributes: Full token payload
            Returns None if not authenticated
        """
        # Check if user info is cached in session state
        if 'user_info' in st.session_state:
            return st.session_state['user_info']
        
        # Get and validate token
        token = self.session_manager.get_session_token()
        if not token:
            return None
        
        is_valid, payload = self.token_validator.validate_token(token)
        if not is_valid:
            return None
        
        # Extract and cache user info
        user_info = self._extract_user_info(payload)
        st.session_state['user_info'] = user_info
        
        return user_info
    
    def _extract_user_info(self, token_payload: Dict) -> Dict[str, Any]:
        """
        Extract user information from token payload.
        
        Args:
            token_payload: Decoded JWT token payload
            
        Returns:
            Dict with user information
        """
        return {
            'username': token_payload.get('cognito:username', 'unknown'),
            'email': token_payload.get('email', 'no-email'),
            'user_id': token_payload.get('sub', ''),
            'attributes': token_payload
        }
    
    def login(self, username: str, password: str) -> bool:
        """
        Authenticate user with Cognito.
        
        This method:
        1. Validates input credentials
        2. Authenticates with Cognito using USER_PASSWORD_AUTH flow
        3. Stores tokens in session manager on success
        4. Returns authentication result
        
        Args:
            username: User's username or email
            password: User's password
            
        Returns:
            True if authentication successful, False otherwise
        """
        # Validate inputs
        if not username or not password:
            st.error("Por favor, ingrese usuario y contraseña")
            return False
        
        try:
            logger.info(f"Attempting authentication for user: {username}")
            
            # Calculate SECRET_HASH
            secret_hash = self._get_secret_hash(username)
            
            # Initiate authentication with Cognito
            response = self.cognito_client.initiate_auth(
                ClientId=self.config.client_id,
                AuthFlow='USER_PASSWORD_AUTH',
                AuthParameters={
                    'USERNAME': username,
                    'PASSWORD': password,
                    'SECRET_HASH': secret_hash
                }
            )
            
            # Check if user needs to change password (first login)
            if 'ChallengeName' in response and response['ChallengeName'] == 'NEW_PASSWORD_REQUIRED':
                logger.info(f"User {username} needs to change password (first login)")
                # Store challenge session for password change
                st.session_state['password_challenge'] = {
                    'session': response['Session'],
                    'username': username,
                    'challenge_params': response.get('ChallengeParameters', {})
                }
                # No mostrar warning aquí, el formulario lo mostrará
                # Forzar rerun para mostrar el formulario de cambio de contraseña
                st.rerun()
            
            # Extract tokens from response
            auth_result = response.get('AuthenticationResult', {})
            id_token = auth_result.get('IdToken')
            access_token = auth_result.get('AccessToken')
            refresh_token = auth_result.get('RefreshToken')
            expires_in = auth_result.get('ExpiresIn', 3600)
            
            if not id_token:
                logger.error("Authentication response missing ID token")
                st.error("Error de autenticación: respuesta inválida del servidor")
                return False
            
            # Store tokens in session manager
            self.session_manager.set_session_token(
                token=id_token,
                expires_in=expires_in,
                refresh_token=refresh_token
            )
            
            # Store access token in session state (for API calls if needed)
            st.session_state['access_token'] = access_token
            
            # CRÍTICO: Aplicar fix de dominio de cookies para SSO
            # streamlit-cookies-manager establece cookies desde JavaScript,
            # por lo que nginx no puede modificar el dominio.
            # Este fix modifica las cookies en el navegador después del login.
            apply_cookie_domain_fix_on_login(self.config.cookie_domain)
            
            logger.info(f"Authentication successful for user: {username}")
            return True
            
        except self.cognito_client.exceptions.NotAuthorizedException as e:
            logger.warning(f"Authentication failed for user {username}: {str(e)}")
            st.error("Usuario o contraseña incorrectos")
            return False
            
        except self.cognito_client.exceptions.UserNotFoundException as e:
            logger.warning(f"User not found: {username}")
            st.error("Usuario no encontrado")
            return False
            
        except self.cognito_client.exceptions.UserNotConfirmedException as e:
            logger.warning(f"User not confirmed: {username}")
            st.error("Usuario no confirmado. Por favor, verifique su correo electrónico")
            return False
            
        except self.cognito_client.exceptions.TooManyRequestsException as e:
            logger.warning(f"Too many authentication attempts for user: {username}")
            st.error("Demasiados intentos de inicio de sesión. Por favor, intente más tarde")
            return False
            
        except (ClientError, BotoCoreError) as e:
            logger.error(f"AWS client error during authentication: {str(e)}")
            st.error("Error de conexión con el servicio de autenticación. Por favor, intente más tarde")
            return False
            
        except Exception as e:
            logger.error(f"Unexpected error during authentication: {str(e)}", exc_info=True)
            st.error("Error inesperado durante la autenticación. Por favor, contacte al administrador")
            return False
    
    def complete_new_password_challenge(self, new_password: str) -> bool:
        """
        Complete the NEW_PASSWORD_REQUIRED challenge for first-time login.
        
        Args:
            new_password: The new password to set
            
        Returns:
            True if password change successful, False otherwise
        """
        if 'password_challenge' not in st.session_state:
            st.error("No hay un desafío de cambio de contraseña pendiente")
            return False
        
        challenge_data = st.session_state['password_challenge']
        username = challenge_data['username']
        session = challenge_data['session']
        
        try:
            logger.info(f"Completing NEW_PASSWORD_REQUIRED challenge for user: {username}")
            
            # Calculate SECRET_HASH
            secret_hash = self._get_secret_hash(username)
            
            # Respond to the challenge with new password
            response = self.cognito_client.respond_to_auth_challenge(
                ClientId=self.config.client_id,
                ChallengeName='NEW_PASSWORD_REQUIRED',
                Session=session,
                ChallengeResponses={
                    'USERNAME': username,
                    'NEW_PASSWORD': new_password,
                    'SECRET_HASH': secret_hash,
                    # Cognito puede requerir atributos adicionales
                    # Usar el email como nombre si no se proporciona
                    'userAttributes.name': username.split('@')[0] if '@' in username else username
                }
            )
            
            # Extract tokens from response
            auth_result = response.get('AuthenticationResult', {})
            id_token = auth_result.get('IdToken')
            access_token = auth_result.get('AccessToken')
            refresh_token = auth_result.get('RefreshToken')
            expires_in = auth_result.get('ExpiresIn', 3600)
            
            if not id_token:
                logger.error("Password change response missing ID token")
                st.error("Error al cambiar contraseña: respuesta inválida del servidor")
                return False
            
            # Store tokens in session manager
            self.session_manager.set_session_token(
                token=id_token,
                expires_in=expires_in,
                refresh_token=refresh_token
            )
            
            # Store access token in session state
            st.session_state['access_token'] = access_token

            # Clear the challenge from session state
            del st.session_state['password_challenge']

            # CRÍTICO: Aplicar fix de dominio de cookies para SSO
            # (mismo fix que en login(), necesario porque este flujo también guarda tokens)
            apply_cookie_domain_fix_on_login(self.config.cookie_domain)

            logger.info(f"Password change successful for user: {username}")
            return True
            
        except self.cognito_client.exceptions.InvalidPasswordException as e:
            logger.warning(f"Invalid password for user {username}: {str(e)}")
            st.error("La contraseña no cumple con los requisitos de seguridad. Debe tener al menos 8 caracteres, incluir mayúsculas, minúsculas, números y caracteres especiales.")
            return False
        
        except self.cognito_client.exceptions.InvalidParameterException as e:
            logger.error(f"Invalid parameter during password change for user {username}: {str(e)}")
            st.error(f"Error de parámetros: {str(e)}")
            return False
            
        except (ClientError, BotoCoreError) as e:
            error_msg = str(e)
            logger.error(f"AWS client error during password change: {error_msg}")
            st.error(f"Error al cambiar contraseña: {error_msg}")
            return False
            
        except Exception as e:
            logger.error(f"Unexpected error during password change: {str(e)}", exc_info=True)
            st.error("Error inesperado al cambiar contraseña. Por favor, contacte al administrador")
            return False
    
    def _refresh_token(self) -> bool:
        """
        Refresh authentication token using refresh token.
        
        This method is called automatically when a token is approaching expiration.
        It uses the stored refresh token to obtain a new ID token and access token.
        
        Returns:
            True if refresh successful, False otherwise
        """
        refresh_token = self.session_manager.get_refresh_token()
        
        if not refresh_token:
            logger.warning("No refresh token available")
            return False
        
        try:
            # Get current user info to calculate SECRET_HASH
            user_info = self.get_current_user()
            if not user_info:
                logger.warning("Cannot refresh token: no user info available")
                return False
            
            username = user_info['username']
            secret_hash = self._get_secret_hash(username)
            
            logger.info(f"Refreshing token for user: {username}")
            
            # Initiate token refresh with Cognito
            response = self.cognito_client.initiate_auth(
                ClientId=self.config.client_id,
                AuthFlow='REFRESH_TOKEN_AUTH',
                AuthParameters={
                    'REFRESH_TOKEN': refresh_token,
                    'SECRET_HASH': secret_hash
                }
            )
            
            # Extract new tokens
            auth_result = response.get('AuthenticationResult', {})
            id_token = auth_result.get('IdToken')
            access_token = auth_result.get('AccessToken')
            expires_in = auth_result.get('ExpiresIn', 3600)
            
            if not id_token:
                logger.error("Token refresh response missing ID token")
                return False
            
            # Store new tokens (keep the same refresh token)
            self.session_manager.set_session_token(
                token=id_token,
                expires_in=expires_in,
                refresh_token=refresh_token
            )
            
            # Update access token in session state
            st.session_state['access_token'] = access_token
            
            logger.info("Token refresh successful")
            return True
            
        except self.cognito_client.exceptions.NotAuthorizedException as e:
            logger.warning(f"Token refresh failed: not authorized - {str(e)}")
            return False
            
        except (ClientError, BotoCoreError) as e:
            logger.error(f"AWS client error during token refresh: {str(e)}")
            return False
            
        except Exception as e:
            logger.error(f"Unexpected error during token refresh: {str(e)}", exc_info=True)
            return False
    
    def logout(self) -> None:
        """
        Terminate user session and clear cookies.
        
        This method:
        1. Clears all session data from cookies and session state
        2. Invalidates the session across all 4 apps (due to shared cookies)
        3. Logs the logout event
        
        After logout, the user will need to re-authenticate to access any app.
        """
        user_info = self.get_current_user()
        username = user_info['username'] if user_info else 'unknown'
        
        logger.info(f"Logging out user: {username}")
        
        # Clear session data
        self.session_manager.clear_session()
        
        # Clear user info from session state
        if 'user_info' in st.session_state:
            del st.session_state['user_info']
        if 'access_token' in st.session_state:
            del st.session_state['access_token']
        if 'oauth2_state' in st.session_state:
            del st.session_state['oauth2_state']

        logger.info(f"Logout successful for user: {username}")
    
    def render_login_screen(self) -> None:
        """
        Render Streamlit login interface.
        
        This method displays a user-friendly login form with:
        - Username/email input field
        - Password input field
        - Login button
        - Password change form (if NEW_PASSWORD_REQUIRED challenge)
        - Loading indicator during authentication
        - Error messages in Spanish
        - GFG branding
        
        The form handles user input and calls the login() method on submission.
        """
        # --- Estilo de marca ALMENA (rojo/negro, logo en CSS, sin imágenes) ---
        st.markdown(
            """
            <style>
            @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@500;600;700;800&display=swap');
            html, body, [class*="css"], .stMarkdown { font-family:'Montserrat',Arial,sans-serif; }
            [data-testid="stSidebar"], header { display:none !important; }
            .block-container { padding-top:4vh; }
            .almena-card { background:#212222; border-radius:24px; padding:42px 36px 28px; text-align:center;
              box-shadow:0 14px 50px rgba(0,0,0,.30); border-top:7px solid #F15D4D; margin-bottom:16px; }
            .almena-logo { color:#E7DFD1; font-weight:800; font-size:56px; letter-spacing:3px; line-height:1; }
            .almena-logo span { color:#F15D4D; }
            .almena-logo-img { width:82%; max-width:280px; height:auto; border-radius:14px; display:block; margin:0 auto; }
            .almena-sub { color:#F15D4D; font-weight:800; font-size:13px; letter-spacing:7px; margin-top:2px; }
            .almena-ttl { color:#ffffff; font-weight:800; font-size:18px; margin-top:24px; }
            .almena-small { color:#E7DFD1; font-weight:600; font-size:11px; letter-spacing:1.2px; opacity:.7; margin-top:6px; }
            .almena-card hr { border:none; border-top:1px solid #3a3a3a; margin:16px 0 4px; }
            /* Botón primario en rojo Almena (login / cambiar contraseña) */
            .stButton button[kind="primary"], .stFormSubmitButton button, [data-testid="stBaseButton-primary"] {
              background:#F15D4D !important; color:#212222 !important; border:none !important; font-weight:800 !important; }
            /* Botón de Microsoft (federación) en negro con borde rojo */
            .stLinkButton a, [data-testid="stBaseLinkButton-primary"] {
              background:#212222 !important; color:#E7DFD1 !important; border:1px solid #F15D4D !important; font-weight:700 !important; }
            </style>
            """,
            unsafe_allow_html=True
        )

        # Center the login form
        col1, col2, col3 = st.columns([1, 2, 1])
        
        with col2:
            # Logo de marca ALMENA (imagen embebida en base64, sin archivos externos)
            st.markdown(
                "<div class='almena-card'>"
                "<img class='almena-logo-img' src='data:image/jpeg;base64," + ALMENA_LOGO_B64 + "' alt='ALMENA'/>"
                "<div class='almena-sub'>INMOBILIARIA</div>"
                "<hr>"
                "<div class='almena-ttl'>Conector INMOGES–el portal de avisos UIF</div>"
                "<div class='almena-small'>PLD · ARRENDAMIENTO DE INMUEBLES</div>"
                "</div>",
                unsafe_allow_html=True
            )
            
            # Check if user needs to change password
            if 'password_challenge' in st.session_state:
                st.markdown("### Cambio de Contraseña Requerido")
                st.markdown("---")
                
                st.info("🔐 Es su primer inicio de sesión. Por favor, establezca una nueva contraseña.")
                
                # Password change form
                with st.form("password_change_form", clear_on_submit=False):
                    st.markdown("**Requisitos de contraseña:**")
                    st.markdown("""
                    - Mínimo 8 caracteres
                    - Al menos una letra mayúscula
                    - Al menos una letra minúscula
                    - Al menos un número
                    - Al menos un carácter especial (!@#$%^&*)
                    """)
                    
                    new_password = st.text_input(
                        "Nueva Contraseña",
                        type="password",
                        placeholder="••••••••",
                        help="Ingrese su nueva contraseña"
                    )
                    
                    confirm_password = st.text_input(
                        "Confirmar Contraseña",
                        type="password",
                        placeholder="••••••••",
                        help="Confirme su nueva contraseña"
                    )
                    
                    submit_button = st.form_submit_button(
                        "Cambiar Contraseña",
                        use_container_width=True,
                        type="primary"
                    )
                    
                    if submit_button:
                        if not new_password or not confirm_password:
                            st.error("⚠️ Por favor, complete todos los campos")
                        elif new_password != confirm_password:
                            st.error("⚠️ Las contraseñas no coinciden")
                        elif len(new_password) < 8:
                            st.error("⚠️ La contraseña debe tener al menos 8 caracteres")
                        else:
                            with st.spinner("Cambiando contraseña..."):
                                success = self.complete_new_password_challenge(new_password)
                            
                            if success:
                                st.success("✅ Contraseña cambiada exitosamente. Redirigiendo...")
                                st.rerun()
            else:
                # Normal login form
                st.markdown("### Inicio de Sesión")
                st.markdown("---")

                # Federated login button (Microsoft Entra ID)
                if self.config.enable_federation and self.config.saml_provider_name:
                    oauth2 = OAuth2Handler(self.config)
                    authorize_url, state = oauth2.get_authorize_url(
                        identity_provider=self.config.saml_provider_name,
                        secret_key=self.config.client_secret,
                    )

                    st.link_button(
                        "Iniciar sesion con Microsoft",
                        url=authorize_url,
                        use_container_width=True,
                        type="primary",
                    )
                    st.markdown(
                        "<div style='text-align:center; color:#666; margin:1rem 0;'>"
                        "--- o ---</div>",
                        unsafe_allow_html=True,
                    )

                # Create login form
                with st.form("login_form", clear_on_submit=False):
                    st.markdown("Por favor, ingrese sus credenciales para acceder al sistema.")
                    
                    # Username input
                    username = st.text_input(
                        "Usuario o Correo Electrónico",
                        placeholder="usuario@ejemplo.com",
                        help="Ingrese su nombre de usuario o correo electrónico"
                    )
                    
                    # Password input
                    password = st.text_input(
                        "Contraseña",
                        type="password",
                        placeholder="••••••••",
                        help="Ingrese su contraseña"
                    )
                    
                    # Login button
                    submit_button = st.form_submit_button(
                        "Iniciar Sesión",
                        use_container_width=True,
                        type="primary"
                    )
                    
                    # Handle form submission
                    if submit_button:
                        if not username or not password:
                            st.error("⚠️ Por favor, complete todos los campos")
                        else:
                            # Show loading indicator
                            with st.spinner("Autenticando..."):
                                success = self.login(username, password)
                            
                            if success:
                                st.success("✅ Autenticación exitosa. Redirigiendo...")
                                # Force rerun to refresh the page and show protected content
                                st.rerun()
            
            # Additional information
            st.markdown("---")
            st.markdown(
                """
                <div style='text-align: center; color: #999; font-size: 0.9em;'>
                    <p>Conector INMOGES–el portal de avisos UIF</p>
                    <p>© 2026 Almena Desarrollos. Todos los derechos reservados.</p>
                </div>
                """,
                unsafe_allow_html=True
            )
            
            # Help section (collapsible)
            with st.expander("❓ ¿Necesita ayuda?"):
                st.markdown("""
                **¿Olvidó su contraseña?**
                
                Contacte al administrador del sistema para restablecer su contraseña.
                
                **¿Problemas para iniciar sesión?**
                
                - Verifique que su usuario y contraseña sean correctos
                - Asegúrese de tener una conexión a internet estable
                - Si el problema persiste, contacte al administrador del sistema
                """)


def check_service_health(config: AuthConfig) -> bool:
    """
    Check if Cognito service is reachable.
    
    This function attempts to connect to the Cognito service to verify
    that it's available before attempting authentication.
    
    Args:
        config: AuthConfig with region and user pool settings
        
    Returns:
        True if service is reachable, False otherwise
    """
    try:
        import requests
        jwks_url = (
            f"https://cognito-idp.{config.region}.amazonaws.com/"
            f"{config.user_pool_id}/.well-known/jwks.json"
        )
        response = requests.get(jwks_url, timeout=5)
        return response.status_code < 500
    except Exception as e:
        logger.error(f"Service health check failed: {e}")
        return False


def render_degraded_mode():
    """
    Display message when authentication service is unavailable.
    
    This function shows a user-friendly error message in Spanish when
    the Cognito service cannot be reached.
    """
    st.error("""
    ⚠️ **Servicio de Autenticación No Disponible**
    
    El servicio de autenticación no está disponible temporalmente.
    
    Por favor, intente nuevamente en unos minutos. Si el problema persiste,
    contacte al administrador del sistema.
    
    **Posibles causas:**
    - Problemas de conectividad de red
    - Mantenimiento del servicio
    - Configuración incorrecta
    """)
    st.stop()


def display_error(error: AuthError, user_facing: bool = True):
    """
    Display error with appropriate detail level.
    
    Args:
        error: The error to display
        user_facing: If True, show user-friendly message; if False, show technical details
    """
    if user_facing:
        # User-friendly Spanish messages
        messages = {
            "configuration": "⚠️ Error de configuración. Contacte al administrador.",
            "network": "⚠️ No se puede conectar al servicio de autenticación. Intente más tarde.",
            "authentication": "⚠️ Usuario o contraseña incorrectos.",
            "validation": "⚠️ Sesión inválida. Por favor, inicie sesión nuevamente."
        }
        st.error(messages.get(error.error_type, "⚠️ Error de autenticación."))
    else:
        # Technical details for logging/debugging
        st.error(f"**{error.error_type.upper()}:** {error.message}")
        if error.details:
            with st.expander("Detalles técnicos"):
                st.json(error.details)


def handle_auth_error(error: AuthError, retry_count: int = 0, max_retries: int = 3) -> bool:
    """
    Handle authentication errors with retry logic.
    
    Args:
        error: The error that occurred
        retry_count: Current retry attempt
        max_retries: Maximum number of retries
        
    Returns:
        True if retry should be attempted, False otherwise
    """
    # Log error details (with sensitive data redacted)
    logger.error(
        f"Auth error: {error.error_type} - {error.message}",
        extra={'details': _redact_sensitive_data(error.details)}
    )
    
    # Network errors: retry with exponential backoff
    if error.error_type == "network" and retry_count < max_retries:
        import time
        time.sleep(2 ** retry_count)
        return True  # Indicate retry should be attempted
    
    # Configuration errors: don't retry, need admin intervention
    if error.error_type == "configuration":
        display_error(error, user_facing=True)
        st.stop()
    
    # Authentication/validation errors: show login screen
    if error.error_type in ["authentication", "validation"]:
        display_error(error, user_facing=True)
        return False  # Don't retry, user needs to re-authenticate
    
    return False


def _redact_sensitive_data(data: Dict) -> Dict:
    """
    Redact sensitive information from error details for logging.
    
    Args:
        data: Dictionary that may contain sensitive data
        
    Returns:
        Dictionary with sensitive fields redacted
    """
    if not data:
        return {}
    
    sensitive_keys = ['token', 'password', 'secret', 'refresh_token', 'access_token', 'id_token']
    redacted = data.copy()
    
    for key in redacted:
        if any(sensitive in key.lower() for sensitive in sensitive_keys):
            redacted[key] = '[REDACTED]'
    
    return redacted