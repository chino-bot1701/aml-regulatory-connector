"""
OAuth2 Authorization Code flow handler for SAML/OIDC federation.

This module handles the OAuth2 Authorization Code flow required for
SAML federation with external identity providers (e.g., Microsoft Entra ID)
through Amazon Cognito.

Flow:
    1. Generate authorize URL with state parameter (CSRF protection)
    2. User is redirected to Cognito -> IdP (Entra ID)
    3. After authentication, Cognito redirects back with authorization code
    4. Exchange code for JWT tokens via Cognito /oauth2/token endpoint
    5. Tokens are stored in SessionManager (same as local login)
"""

import hashlib
import hmac
import logging
import secrets
import time
import urllib.parse
from typing import Dict, Tuple

import requests

from .config import AuthConfig

logger = logging.getLogger(__name__)


class OAuth2Error(Exception):
    """Error during OAuth2 flow."""
    pass


class OAuth2Handler:
    """
    Handles OAuth2 Authorization Code flow for Cognito SAML federation.

    This class generates authorization URLs and exchanges authorization codes
    for JWT tokens. It works with Cognito's OAuth2 endpoints to support
    federated login via SAML identity providers like Microsoft Entra ID.
    """

    def __init__(self, config: AuthConfig):
        self.cognito_domain = config.cognito_domain
        self.client_id = config.client_id
        self.client_secret = config.client_secret
        self.redirect_uri = config.redirect_uri

    def get_authorize_url(self, identity_provider: str = "EntraId", secret_key: str = "") -> Tuple[str, str]:
        """
        Generate Cognito authorization URL for federated login.

        Args:
            identity_provider: Name of the IdP configured in Cognito
            secret_key: Secret key for HMAC-signing the state token

        Returns:
            Tuple of (authorize_url, state_token)
        """
        state = self.generate_state(secret_key or self.client_secret)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "identity_provider": identity_provider,
            "scope": "openid email phone",
            "state": state,
        }
        authorize_url = (
            f"https://{self.cognito_domain}/oauth2/authorize?"
            f"{urllib.parse.urlencode(params)}"
        )
        logger.info(f"Generated authorize URL for provider: {identity_provider}")
        return authorize_url, state

    def exchange_code_for_tokens(self, code: str) -> Dict:
        """
        Exchange authorization code for JWT tokens.

        Posts to Cognito's /oauth2/token endpoint with the authorization code
        and returns the JWT tokens (id_token, access_token, refresh_token).

        Args:
            code: Authorization code received from Cognito callback

        Returns:
            Dict with keys: id_token, access_token, refresh_token,
            token_type, expires_in

        Raises:
            OAuth2Error: If token exchange fails
        """
        token_url = f"https://{self.cognito_domain}/oauth2/token"
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        }

        try:
            response = requests.post(
                token_url,
                data=data,
                auth=(self.client_id, self.client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            response.raise_for_status()
            tokens = response.json()

            if "id_token" not in tokens:
                raise OAuth2Error("Token response missing id_token")

            logger.info("Successfully exchanged authorization code for tokens")
            return tokens

        except requests.exceptions.HTTPError as e:
            error_detail = ""
            try:
                error_detail = e.response.json().get("error", "")
            except Exception:
                error_detail = e.response.text[:200] if e.response else ""
            logger.error(f"Token exchange HTTP error: {e} - {error_detail}")
            raise OAuth2Error(
                f"Error al intercambiar codigo de autorizacion: {error_detail or str(e)}"
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Token exchange network error: {e}")
            raise OAuth2Error(
                f"Error de red al contactar servicio de autenticacion: {e}"
            )

    @staticmethod
    def generate_state(secret_key: str) -> str:
        """
        Generate self-validating HMAC-signed state token for CSRF protection.

        The state is structured as: {random}.{timestamp}.{signature}
        This eliminates the need for server-side storage, which is lost
        when Streamlit session resets after OAuth2 redirect.
        """
        random_part = secrets.token_urlsafe(16)
        timestamp = str(int(time.time()))
        message = f"{random_part}.{timestamp}"
        signature = hmac.new(
            secret_key.encode(), message.encode(), hashlib.sha256
        ).hexdigest()[:16]
        return f"{message}.{signature}"

    @staticmethod
    def validate_state(state: str, secret_key: str, max_age: int = 600) -> bool:
        """
        Validate HMAC-signed state token.

        Verifies the signature and checks the token is not older than max_age seconds.
        No server-side storage needed.
        """
        if not state or not secret_key:
            return False
        parts = state.split(".")
        if len(parts) != 3:
            return False
        random_part, timestamp, signature = parts
        message = f"{random_part}.{timestamp}"
        expected = hmac.new(
            secret_key.encode(), message.encode(), hashlib.sha256
        ).hexdigest()[:16]
        if not hmac.compare_digest(signature, expected):
            return False
        try:
            if int(time.time()) - int(timestamp) > max_age:
                return False
        except ValueError:
            return False
        return True
