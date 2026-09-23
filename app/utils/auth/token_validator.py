"""
Token validation for AWS Cognito JWT tokens.

This module provides the TokenValidator class for validating JWT tokens
issued by AWS Cognito, including signature verification and claims validation.
"""

import time
import requests
from typing import Dict, Tuple, Optional, Any
from jose import jwt, JWTError
from jose.utils import base64url_decode
import logging

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Error raised when token validation fails."""
    pass


class TokenValidator:
    """
    Validates JWT tokens with AWS Cognito.
    
    This class fetches the JSON Web Key Set (JWKS) from Cognito and uses it
    to verify JWT token signatures. It also validates token claims such as
    audience, issuer, and expiration.
    
    The JWKS is cached for 24 hours to improve performance and reduce
    network requests to Cognito.
    """
    
    # Cache duration: 24 hours in seconds
    JWKS_CACHE_DURATION = 24 * 60 * 60
    
    def __init__(self, config):
        """
        Initialize TokenValidator with Cognito configuration.
        
        Args:
            config: AuthConfig object with user_pool_id, client_id, and region
        """
        self.config = config
        self.jwks = None
        self.jwks_fetch_time = 0
        self._initialize_jwks()
    
    def _initialize_jwks(self) -> None:
        """
        Fetch JWKS from Cognito on initialization.
        
        This method is called during __init__ to pre-fetch the JWKS.
        If fetching fails, it logs a warning but doesn't raise an exception,
        allowing the validator to retry on first token validation.
        """
        try:
            self._fetch_jwks()
        except Exception as e:
            logger.warning(f"Failed to fetch JWKS during initialization: {e}")
            # Don't raise exception - will retry on first validation
    
    def _fetch_jwks(self) -> Dict:
        """
        Fetch JSON Web Key Set from Cognito.
        
        The JWKS contains the public keys used to verify JWT signatures.
        This method caches the JWKS for 24 hours to improve performance.
        
        Returns:
            Dict containing the JWKS
            
        Raises:
            ValidationError: If JWKS cannot be fetched
        """
        # Check if cached JWKS is still valid
        current_time = time.time()
        if self.jwks and (current_time - self.jwks_fetch_time) < self.JWKS_CACHE_DURATION:
            return self.jwks
        
        # Construct JWKS URL
        jwks_url = (
            f"https://cognito-idp.{self.config.region}.amazonaws.com/"
            f"{self.config.user_pool_id}/.well-known/jwks.json"
        )
        
        try:
            logger.info(f"Fetching JWKS from Cognito: {jwks_url}")
            response = requests.get(jwks_url, timeout=10)
            response.raise_for_status()
            
            self.jwks = response.json()
            self.jwks_fetch_time = current_time
            
            logger.info(f"Successfully fetched JWKS with {len(self.jwks.get('keys', []))} keys")
            return self.jwks
            
        except requests.exceptions.RequestException as e:
            error_msg = f"No se pudo obtener las claves públicas de Cognito: {str(e)}"
            logger.error(error_msg)
            raise ValidationError(error_msg)
        except ValueError as e:
            error_msg = f"Respuesta JWKS inválida de Cognito: {str(e)}"
            logger.error(error_msg)
            raise ValidationError(error_msg)
    
    def _get_signing_key(self, token: str) -> Optional[Dict]:
        """
        Get the signing key for a token from JWKS.
        
        Args:
            token: JWT token string
            
        Returns:
            Signing key dict or None if not found
            
        Raises:
            ValidationError: If token header is invalid or JWKS fetch fails
        """
        try:
            # Decode token header to get key ID (kid)
            headers = jwt.get_unverified_headers(token)
            kid = headers.get('kid')
            
            if not kid:
                raise ValidationError("Token no contiene 'kid' en el encabezado")
            
            # Fetch JWKS (uses cache if available)
            jwks = self._fetch_jwks()
            
            # Find matching key
            for key in jwks.get('keys', []):
                if key.get('kid') == kid:
                    return key
            
            logger.warning(f"No se encontró la clave con kid={kid} en JWKS")
            return None
            
        except JWTError as e:
            raise ValidationError(f"Error al decodificar el encabezado del token: {str(e)}")
    
    def validate_token(self, token: str) -> Tuple[bool, Optional[Dict]]:
        """
        Validate JWT token signature and expiration.
        
        This method performs comprehensive token validation:
        1. Verifies the token signature using Cognito's public keys
        2. Validates the token audience (client_id)
        3. Validates the token issuer (Cognito User Pool)
        4. Checks token expiration
        
        Args:
            token: JWT token string
            
        Returns:
            Tuple of (is_valid, decoded_payload)
            - is_valid: True if token is valid, False otherwise
            - decoded_payload: Dict with token claims if valid, None otherwise
        """
        if not token:
            logger.warning("Token validation failed: empty token")
            return False, None
        
        try:
            # Get signing key from JWKS
            signing_key = self._get_signing_key(token)
            if not signing_key:
                logger.warning("Token validation failed: signing key not found")
                return False, None
            
            # Construct expected issuer
            issuer = (
                f"https://cognito-idp.{self.config.region}.amazonaws.com/"
                f"{self.config.user_pool_id}"
            )
            
            # Decode and validate token
            # python-jose will verify signature, expiration, audience, and issuer
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=['RS256'],
                audience=self.config.client_id,
                issuer=issuer,
                options={
                    'verify_signature': True,
                    'verify_exp': True,
                    'verify_aud': True,
                    'verify_iss': True,
                    'verify_at_hash': False,
                }
            )
            
            logger.info(f"Token validation successful for user: {payload.get('cognito:username', 'unknown')}")
            return True, payload
            
        except jwt.ExpiredSignatureError:
            print("[TOKEN-DEBUG] Validation FAILED: token expired", flush=True)
            return False, None
        except jwt.JWTClaimsError as e:
            print(f"[TOKEN-DEBUG] Validation FAILED: invalid claims - {e}", flush=True)
            return False, None
        except JWTError as e:
            print(f"[TOKEN-DEBUG] Validation FAILED: JWT error - {e}", flush=True)
            return False, None
        except ValidationError as e:
            print(f"[TOKEN-DEBUG] Validation FAILED: {e}", flush=True)
            return False, None
        except Exception as e:
            print(f"[TOKEN-DEBUG] Validation FAILED: unexpected - {type(e).__name__}: {e}", flush=True)
            return False, None
    
    def decode_token(self, token: str) -> Dict[str, Any]:
        """
        Decode JWT token without validation (for debugging).
        
        WARNING: This method does NOT verify the token signature or expiration.
        It should only be used for debugging or when you need to inspect
        token contents without validation.
        
        Args:
            token: JWT token string
            
        Returns:
            Decoded token payload as a dictionary
            
        Raises:
            ValidationError: If token cannot be decoded
        """
        if not token:
            raise ValidationError("No se puede decodificar un token vacío")
        
        try:
            # Decode without verification
            payload = jwt.get_unverified_claims(token)
            return payload
        except JWTError as e:
            raise ValidationError(f"Error al decodificar el token: {str(e)}")
    
    def is_token_expired(self, token: str) -> bool:
        """
        Check if token is expired.
        
        This method decodes the token and checks the 'exp' claim against
        the current time. It does NOT verify the token signature.
        
        Args:
            token: JWT token string
            
        Returns:
            True if expired, False otherwise
            
        Raises:
            ValidationError: If token cannot be decoded or has no expiration
        """
        if not token:
            return True
        
        try:
            payload = self.decode_token(token)
            exp = payload.get('exp')
            
            if not exp:
                raise ValidationError("Token no contiene campo de expiración 'exp'")
            
            current_time = time.time()
            is_expired = current_time >= exp
            
            if is_expired:
                logger.info("Token is expired")
            else:
                time_remaining = exp - current_time
                logger.debug(f"Token expires in {time_remaining:.0f} seconds")
            
            return is_expired
            
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError(f"Error al verificar expiración del token: {str(e)}")
    
    def get_time_until_expiry(self, token: str) -> int:
        """
        Get seconds until token expiration.
        
        Args:
            token: JWT token string
            
        Returns:
            Seconds until expiration (0 if already expired)
            
        Raises:
            ValidationError: If token cannot be decoded
        """
        try:
            payload = self.decode_token(token)
            exp = payload.get('exp')
            
            if not exp:
                raise ValidationError("Token no contiene campo de expiración 'exp'")
            
            current_time = time.time()
            time_remaining = max(0, int(exp - current_time))
            
            return time_remaining
            
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError(f"Error al calcular tiempo de expiración: {str(e)}")
