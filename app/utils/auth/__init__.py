"""
Authentication module for GFG Real Estate Investment Analysis System.

This module provides centralized authentication using Amazon Cognito
for all 4 Streamlit applications (ports 8501, 8502, 8503, 8504).

Features:
- Single Sign-On (SSO) across all apps
- JWT token-based session management
- Shared browser cookies for cross-app authentication
- Secure token validation
- User-friendly Spanish language interface

Main Components:
- CognitoAuth: Main authentication class
- SessionManager: Session and cookie management
- TokenValidator: JWT token validation
- AuthConfig: Configuration management

Usage:
    from utils.auth.cognito_auth import CognitoAuth
    from utils.auth.config import AuthConfig
    
    config = AuthConfig.from_env()
    auth = CognitoAuth(config)
    
    if not auth.is_authenticated():
        auth.render_login_screen()
        st.stop()
"""

__version__ = "1.0.0"
__author__ = "GFG Development Team"

# Package initialization
# Import main components for easy access
from .cognito_auth import (
    CognitoAuth,
    AuthError,
    NetworkError,
    AuthenticationError,
    check_service_health,
    render_degraded_mode,
    display_error,
    handle_auth_error
)
from .config import AuthConfig, ConfigurationError
from .session_manager import SessionManager
from .token_validator import TokenValidator, ValidationError
from .oauth2_handler import OAuth2Handler, OAuth2Error

__all__ = [
    'CognitoAuth',
    'AuthConfig',
    'SessionManager',
    'TokenValidator',
    'AuthError',
    'NetworkError',
    'AuthenticationError',
    'ConfigurationError',
    'ValidationError',
    'check_service_health',
    'render_degraded_mode',
    'display_error',
    'handle_auth_error',
    'OAuth2Handler',
    'OAuth2Error',
]
