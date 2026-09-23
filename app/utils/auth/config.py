"""
Configuration management for Cognito authentication.

This module provides the AuthConfig class for loading and validating
authentication configuration from environment variables.
"""

import os
from dataclasses import dataclass
from typing import Optional


class ConfigurationError(Exception):
    """Error raised when configuration is missing or invalid."""
    pass


@dataclass
class AuthConfig:
    """
    Configuration for Cognito authentication.
    Loaded from environment variables.
    
    Attributes:
        user_pool_id: AWS Cognito User Pool ID
        client_id: AWS Cognito App Client ID
        client_secret: AWS Cognito App Client Secret
        region: AWS region where User Pool is located
        cookie_domain: Domain for session cookies (default: localhost)
        cookie_secure: Whether to use secure cookies (HTTPS only)
        cookie_encryption_key: Encryption key for cookie manager
        session_timeout: Session timeout in seconds (default: 3600 = 1 hour)
        refresh_threshold: Time before expiry to refresh token in seconds (default: 300 = 5 min)
    """
    user_pool_id: str
    client_id: str
    client_secret: str
    region: str
    cookie_domain: str = "localhost"
    cookie_secure: bool = False
    cookie_encryption_key: str = ""
    session_timeout: int = 3600
    refresh_threshold: int = 300

    # Federation SAML fields (optional - for Entra ID integration)
    cognito_domain: str = ""
    redirect_uri: str = ""
    saml_provider_name: str = ""
    enable_federation: bool = False
    
    @classmethod
    def from_env(cls) -> 'AuthConfig':
        """
        Load configuration from environment variables.
        
        Required environment variables:
            - COGNITO_USER_POOL_ID: User Pool ID
            - COGNITO_CLIENT_ID: App Client ID
            - COGNITO_CLIENT_SECRET: App Client Secret
            - AWS_REGION: AWS region
        
        Optional environment variables:
            - COOKIE_DOMAIN: Cookie domain (default: localhost)
            - COOKIE_SECURE: Use secure cookies (default: false)
            - COOKIE_ENCRYPTION_KEY: Cookie encryption key (default: auto-generated)
            - SESSION_TIMEOUT: Session timeout in seconds (default: 3600)
            - REFRESH_THRESHOLD: Token refresh threshold in seconds (default: 300)
        
        Returns:
            AuthConfig instance with loaded configuration
            
        Raises:
            ConfigurationError: If required variables are missing or invalid
        """
        # Load required variables
        user_pool_id = os.getenv('COGNITO_USER_POOL_ID')
        client_id = os.getenv('COGNITO_CLIENT_ID')
        client_secret = os.getenv('COGNITO_CLIENT_SECRET')
        region = os.getenv('AWS_REGION')
        
        # Check for missing required variables
        missing_vars = []
        if not user_pool_id:
            missing_vars.append('COGNITO_USER_POOL_ID')
        if not client_id:
            missing_vars.append('COGNITO_CLIENT_ID')
        if not client_secret:
            missing_vars.append('COGNITO_CLIENT_SECRET')
        if not region:
            missing_vars.append('AWS_REGION')
        
        if missing_vars:
            raise ConfigurationError(
                f"Faltan las siguientes variables de entorno requeridas: {', '.join(missing_vars)}. "
                f"Por favor, configure estas variables en el archivo .env"
            )
        
        # Load optional variables with defaults
        cookie_domain = os.getenv('COOKIE_DOMAIN', 'localhost')
        cookie_secure = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
        cookie_encryption_key = os.getenv('COOKIE_ENCRYPTION_KEY', 'default-key-change-in-prod')
        
        # Parse integer values with error handling
        try:
            session_timeout = int(os.getenv('SESSION_TIMEOUT', '3600'))
        except ValueError:
            raise ConfigurationError(
                "La variable SESSION_TIMEOUT debe ser un número entero válido"
            )
        
        try:
            refresh_threshold = int(os.getenv('REFRESH_THRESHOLD', '300'))
        except ValueError:
            raise ConfigurationError(
                "La variable REFRESH_THRESHOLD debe ser un número entero válido"
            )

        # Load federation variables (optional)
        cognito_domain = os.getenv('COGNITO_DOMAIN', '')
        saml_provider_name = os.getenv('SAML_PROVIDER_NAME', '')
        enable_federation = os.getenv('ENABLE_FEDERATION', 'false').lower() == 'true'
        # redirect_uri is set per-app, not from global .env

        # Create config instance
        config = cls(
            user_pool_id=user_pool_id,
            client_id=client_id,
            client_secret=client_secret,
            region=region,
            cookie_domain=cookie_domain,
            cookie_secure=cookie_secure,
            cookie_encryption_key=cookie_encryption_key,
            session_timeout=session_timeout,
            refresh_threshold=refresh_threshold,
            cognito_domain=cognito_domain,
            saml_provider_name=saml_provider_name,
            enable_federation=enable_federation,
        )
        
        # Validate configuration
        config.validate()
        
        return config
    
    def validate(self) -> None:
        """
        Validate configuration values.
        
        Raises:
            ConfigurationError: If configuration is invalid
        """
        # Validate User Pool ID format (should be like us-east-1_XXXXXXXXX)
        if not self.user_pool_id or '_' not in self.user_pool_id:
            raise ConfigurationError(
                "COGNITO_USER_POOL_ID tiene un formato inválido. "
                "Debe tener el formato: region_XXXXXXXXX (ejemplo: us-east-1_abc123def)"
            )
        
        # Validate Client ID (should be alphanumeric, typically 26 characters)
        if not self.client_id or len(self.client_id) < 10:
            raise ConfigurationError(
                "COGNITO_CLIENT_ID tiene un formato inválido. "
                "Debe ser un identificador alfanumérico válido"
            )
        
        # Validate Client Secret (should be non-empty)
        if not self.client_secret or len(self.client_secret) < 10:
            raise ConfigurationError(
                "COGNITO_CLIENT_SECRET tiene un formato inválido. "
                "Debe ser una cadena de texto válida"
            )
        
        # Validate AWS region format
        valid_region_prefixes = ['us-', 'eu-', 'ap-', 'sa-', 'ca-', 'me-', 'af-']
        if not any(self.region.startswith(prefix) for prefix in valid_region_prefixes):
            raise ConfigurationError(
                f"AWS_REGION '{self.region}' no es una región válida. "
                f"Debe ser una región de AWS válida (ejemplo: us-east-1, eu-west-1)"
            )
        
        # Validate session timeout (should be positive and reasonable)
        if self.session_timeout <= 0:
            raise ConfigurationError(
                "SESSION_TIMEOUT debe ser un número positivo"
            )
        
        if self.session_timeout > 86400:  # 24 hours
            raise ConfigurationError(
                "SESSION_TIMEOUT no puede ser mayor a 86400 segundos (24 horas)"
            )
        
        # Validate refresh threshold
        if self.refresh_threshold <= 0:
            raise ConfigurationError(
                "REFRESH_THRESHOLD debe ser un número positivo"
            )
        
        if self.refresh_threshold >= self.session_timeout:
            raise ConfigurationError(
                "REFRESH_THRESHOLD debe ser menor que SESSION_TIMEOUT"
            )
        
        # Warn if using default encryption key (security risk) - check this first
        if self.cookie_encryption_key == 'default-key-change-in-prod':
            raise ConfigurationError(
                "COOKIE_ENCRYPTION_KEY está usando el valor por defecto. "
                "Por favor, configure una clave de encriptación segura en producción"
            )
        
        # Validate cookie encryption key length (should be at least 32 characters for security)
        if len(self.cookie_encryption_key) < 32:
            raise ConfigurationError(
                "COOKIE_ENCRYPTION_KEY debe tener al menos 32 caracteres para seguridad. "
                "Por favor, genere una clave segura"
            )

        # Validate federation configuration (only when enabled)
        if self.enable_federation:
            if not self.cognito_domain:
                raise ConfigurationError(
                    "COGNITO_DOMAIN es requerido cuando ENABLE_FEDERATION=true"
                )
            if not self.saml_provider_name:
                raise ConfigurationError(
                    "SAML_PROVIDER_NAME es requerido cuando ENABLE_FEDERATION=true"
                )
