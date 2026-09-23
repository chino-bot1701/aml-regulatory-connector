"""
Session management for Cognito authentication.

This module provides the SessionManager class for managing user sessions
and browser cookies to enable Single Sign-On (SSO) across all 4 Streamlit apps.
"""

import time
import logging
from typing import Optional, Dict, Any
import streamlit as st
from streamlit_cookies_manager import EncryptedCookieManager

from .config import AuthConfig


# Configure logging
logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages session state and browser cookies for SSO.
    
    This class handles:
    - Storing JWT tokens in encrypted browser cookies
    - Retrieving tokens from cookies across different app ports/subdomains
    - Managing session expiration and cleanup
    - Proactive token refresh when approaching expiration
    
    Cookie Security:
    - Cookies are encrypted using streamlit-cookies-manager
    - prefix="gfg_" to avoid conflicts with other apps
    - path=/ (accessible from all paths)
    
    IMPORTANT - SSO Configuration:
    streamlit-cookies-manager v0.2.0 does NOT support configuring cookie domain,
    secure, httpOnly, or sameSite attributes from Python.
    
    For SSO to work across subdomains in production/stage:
    1. Configure nginx/proxy to modify Set-Cookie headers
    2. Add domain=.firmasglobales.mx to all gfg_* cookies
    3. Add Secure flag for HTTPS
    4. Add SameSite=Lax for CSRF protection
    
    See: context/07_fixes_correcciones/2026-02-03_fix_sso_cookie_domain.md
    """
    
    def __init__(self, config: AuthConfig):
        """
        Initialize session manager with authentication configuration.
        
        Args:
            config: AuthConfig object with cookie and session settings
        """
        self.config = config
        self.cookie_name_token = "auth_token"
        self.cookie_name_expiry = "token_expiry"
        self.cookie_name_refresh = "refresh_token"
        
        # Initialize encrypted cookie manager
        # The prefix "gfg_" will be added to all cookie names
        self.cookies = EncryptedCookieManager(
            prefix="gfg_",
            password=config.cookie_encryption_key
        )
        
        # Wait for cookie manager to be ready
        # This is required by streamlit-cookies-manager
        if not self.cookies.ready():
            st.stop()
    
    def get_session_token(self) -> Optional[str]:
        """
        Retrieve JWT token from browser cookie or Streamlit session.
        
        This method checks both the encrypted browser cookie and Streamlit's
        session state. Browser cookies enable SSO across apps, while session
        state provides in-memory caching for performance.
        
        Returns:
            JWT token string or None if not found or expired
        """
        # First check Streamlit session state (faster, in-memory)
        if 'auth_token' in st.session_state:
            token = st.session_state['auth_token']
            expiry = st.session_state.get('token_expiry', 0)
            
            # Check if token is still valid
            if time.time() < expiry:
                logger.debug("Retrieved valid token from session state")
                return token
            else:
                logger.debug("Token in session state has expired")
                # Clear expired token from session state
                self._clear_session_state()
        
        # Check browser cookie (enables SSO across apps)
        token = self.cookies.get(self.cookie_name_token)
        expiry_str = self.cookies.get(self.cookie_name_expiry)
        
        if token and expiry_str:
            try:
                expiry = float(expiry_str)
                
                # Check if token is still valid
                if time.time() < expiry:
                    logger.debug("Retrieved valid token from cookie")
                    
                    # Cache in session state for performance
                    st.session_state['auth_token'] = token
                    st.session_state['token_expiry'] = expiry
                    
                    # Also retrieve refresh token if available
                    refresh_token = self.cookies.get(self.cookie_name_refresh)
                    if refresh_token:
                        st.session_state['refresh_token'] = refresh_token
                    
                    return token
                else:
                    logger.debug("Token in cookie has expired")
                    # Clear expired token
                    self.clear_session()
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid token expiry format in cookie: {e}")
                self.clear_session()
        
        logger.debug("No valid token found in session or cookies")
        return None
    
    def set_session_token(self, token: str, expires_in: int, refresh_token: Optional[str] = None) -> None:
        """
        Store JWT token in browser cookie and Streamlit session.
        
        This method stores the token in both locations:
        - Browser cookie: Enables SSO across all 4 apps
        - Streamlit session state: Provides fast in-memory access
        
        Args:
            token: JWT token string (ID token from Cognito)
            expires_in: Token expiration time in seconds from now
            refresh_token: Optional refresh token for token renewal
        """
        # Calculate expiration timestamp
        expiry_timestamp = time.time() + expires_in
        
        # Store in browser cookie (enables SSO)
        self.cookies[self.cookie_name_token] = token
        self.cookies[self.cookie_name_expiry] = str(expiry_timestamp)
        
        if refresh_token:
            self.cookies[self.cookie_name_refresh] = refresh_token
        
        # Save cookies to browser
        self.cookies.save()
        
        # Also store in Streamlit session state (performance)
        st.session_state['auth_token'] = token
        st.session_state['token_expiry'] = expiry_timestamp
        
        if refresh_token:
            st.session_state['refresh_token'] = refresh_token
        
        logger.info(f"Session token stored successfully, expires in {expires_in} seconds")
    
    def clear_session(self) -> None:
        """
        Remove all session data and cookies.
        
        This method clears authentication data from both browser cookies
        and Streamlit session state. When called from any app, it effectively
        logs out the user from all 4 apps due to shared cookie domain.
        """
        # Clear browser cookies
        self.cookies[self.cookie_name_token] = ""
        self.cookies[self.cookie_name_expiry] = ""
        self.cookies[self.cookie_name_refresh] = ""
        self.cookies.save()
        
        # Clear Streamlit session state
        self._clear_session_state()
        
        logger.info("Session cleared successfully")
    
    def _clear_session_state(self) -> None:
        """
        Clear authentication data from Streamlit session state.
        
        This is an internal helper method that removes auth-related keys
        from st.session_state without affecting other application state.
        """
        keys_to_clear = ['auth_token', 'token_expiry', 'refresh_token', 'user_info']
        for key in keys_to_clear:
            if key in st.session_state:
                del st.session_state[key]
    
    def refresh_token_if_needed(self, token: str) -> Optional[str]:
        """
        Refresh token if close to expiration.
        
        This method implements proactive token refresh to maintain active
        sessions without interrupting the user. If the token is within the
        refresh threshold (default 5 minutes) of expiration, it attempts
        to refresh the token using the stored refresh token.
        
        Args:
            token: Current JWT token
            
        Returns:
            New token if refreshed, original token if still valid, None if refresh failed
        """
        # Get token expiry from session state or cookie
        expiry = st.session_state.get('token_expiry')
        
        if not expiry:
            # Try to get from cookie
            expiry_str = self.cookies.get(self.cookie_name_expiry)
            if expiry_str:
                try:
                    expiry = float(expiry_str)
                except (ValueError, TypeError):
                    logger.warning("Invalid token expiry format")
                    return None
            else:
                logger.warning("No token expiry found")
                return None
        
        # Calculate time until expiration
        time_until_expiry = expiry - time.time()
        
        # Check if token is already expired
        if time_until_expiry <= 0:
            logger.info("Token has already expired")
            return None
        
        # Check if token is within refresh threshold
        if time_until_expiry < self.config.refresh_threshold:
            logger.info(f"Token expires in {time_until_expiry:.0f} seconds, attempting refresh")
            
            # Get refresh token
            refresh_token = st.session_state.get('refresh_token')
            if not refresh_token:
                refresh_token = self.cookies.get(self.cookie_name_refresh)
            
            if not refresh_token:
                logger.warning("No refresh token available for token refresh")
                return None
            
            # Attempt to refresh token
            # Note: The actual Cognito refresh logic will be implemented in CognitoAuth class
            # This method just checks if refresh is needed and returns the appropriate value
            # For now, we return the original token and let CognitoAuth handle the refresh
            logger.debug("Token refresh needed, returning original token for CognitoAuth to handle")
            return token
        
        # Token is still valid and not close to expiration
        logger.debug(f"Token is valid for {time_until_expiry:.0f} more seconds, no refresh needed")
        return token
    
    def get_refresh_token(self) -> Optional[str]:
        """
        Retrieve refresh token from session or cookies.
        
        Returns:
            Refresh token string or None if not found
        """
        # Check session state first
        refresh_token = st.session_state.get('refresh_token')
        if refresh_token:
            return refresh_token
        
        # Check cookie
        refresh_token = self.cookies.get(self.cookie_name_refresh)
        if refresh_token:
            # Cache in session state
            st.session_state['refresh_token'] = refresh_token
            return refresh_token
        
        return None
    
    def get_time_until_expiry(self) -> Optional[int]:
        """
        Get seconds until token expiration.
        
        Returns:
            Seconds until expiration, or None if no valid token
        """
        expiry = st.session_state.get('token_expiry')
        
        if not expiry:
            # Try to get from cookie
            expiry_str = self.cookies.get(self.cookie_name_expiry)
            if expiry_str:
                try:
                    expiry = float(expiry_str)
                except (ValueError, TypeError):
                    return None
            else:
                return None
        
        time_remaining = expiry - time.time()
        return max(0, int(time_remaining))
    
    def is_token_expiring_soon(self) -> bool:
        """
        Check if token is within refresh threshold of expiration.
        
        Returns:
            True if token should be refreshed, False otherwise
        """
        time_remaining = self.get_time_until_expiry()
        if time_remaining is None:
            return False
        
        return time_remaining < self.config.refresh_threshold
