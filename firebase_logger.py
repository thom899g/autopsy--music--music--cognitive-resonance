"""
Firebase State Manager for AEMP - Fixed Version
Handles all Firebase interactions for logging, state persistence, and real-time monitoring

ARCHITECTURAL DECISIONS:
1. Singleton pattern ensures single Firebase app instance across the ecosystem
2. Lazy initialization prevents Firebase connection until actually needed
3. Circuit breaker pattern prevents cascading failures during Firebase outages
4. Automatic retry with exponential backoff for transient network failures
5. Connection health monitoring with automatic reconnection
6. Type-safe document serialization/deserialization
7. Batch operations for performance optimization
8. Comprehensive telemetry for monitoring Firebase performance
"""

import json
import logging
import time
import threading
from typing import Dict, Any, Optional, List, Union, Callable
from datetime import datetime, timedelta
from dataclasses import asdict, is_dataclass
from enum import Enum
from contextlib import contextmanager
import firebase_admin
from firebase_admin import credentials, firestore, exceptions
from firebase_admin.firestore import Client as FirestoreClient
from google.cloud.firestore_v1.base_query import FieldFilter

# Import config safely with fallback
try:
    from config import config
except ImportError:
    # Fallback configuration for testing
    class FallbackConfig:
        class FirebaseConfig:
            credentials_path = "firebase-credentials.json"
            project_id = "aemp-project"
            database_url = "https://aemp-project.firebaseio.com"
            collection_name = "strategies"
            timeout_seconds = 30
            max_retries = 3
            circuit_breaker_threshold = 5
            circuit_breaker_timeout = 60
            
        firebase = FirebaseConfig()
        
    config = FallbackConfig()


class FirebaseConnectionState(Enum):
    """Firebase connection state machine"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degrated"  # Typo preserved for backward compatibility
    FAILED = "failed"


class FirebaseManager:
    """
    Manages Firebase Firestore connections and operations with:
    - Singleton pattern for single app instance
    - Automatic reconnection on failure
    - Circuit breaker to prevent cascade failures
    - Connection health monitoring
    - Batch operation support
    """
    
    _instance = None
    _lock = threading.RLock()
    
    def __new__(cls):
        """Singleton pattern implementation"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
                cls._instance._state = FirebaseConnectionState.DISCONNECTED
                cls._instance._failure_count = 0
                cls._instance._last_failure_time = None
                cls._instance._connection_attempts = 0
                cls._instance._db = None
                cls._instance._strategies_collection_ref = None
                cls._instance._metrics_collection_ref = None
                cls._instance._app = None
            return cls._instance
    
    @property
    def state(self) -> FirebaseConnectionState:
        """Get current connection state"""
        return self._state
    
    @property
    def is_healthy(self) -> bool:
        """Check if connection is healthy"""
        return self._state in [FirebaseConnectionState.CONNECTED, FirebaseConnectionState.DEGRADED]
    
    def _check_circuit_breaker(self) -> bool:
        """
        Circuit breaker pattern: if too many failures in quick succession,
        block further attempts for a cooldown period.
        
        Returns:
            bool: True if circuit is closed (allow operations), False if open (block)
        """
        if self._state == FirebaseConnectionState.FAILED:
            # Check if we should reset based on timeout
            if self._last_failure_time:
                time_since_failure = time.time() - self._last_failure_time
                if time_since_failure > config.firebase.circuit_breaker_timeout:
                    logging.info("Circuit breaker timeout expired, attempting reset")
                    self._state = FirebaseConnectionState.DISCONNECTED
                    self._failure_count = 0
                    return True
            return False
        
        if self._failure_count >= config.firebase.circuit_breaker_threshold:
            self._state = FirebaseConnectionState.FAILED
            self._last_failure_time = time.time()
            logging.error(f"Circuit breaker OPEN - too many failures: {self._failure_count}")
            return False
        
        return True
    
    def _record_success(self):
        """Record successful operation to reset failure counter"""
        self._failure_count = 0
        if self._state == FirebaseConnectionState.FAILED:
            self._state = FirebaseConnectionState.DISCONNECTED
    
    def _record_failure(self, error: Exception):
        """Record failed operation and update circuit breaker"""
        self._failure_count += 1
        self._last_failure_time = time.time()
        logging.error(f"Firebase operation failed (count: {self._failure_count}): {error}")
        
        if self._failure_count >= config.firebase.circuit_breaker_threshold:
            self._state = FirebaseConnectionState.FAILED
    
    @contextmanager
    def _operation_context(self, operation_name: str):
        """
        Context manager for Firebase operations with:
        - Circuit breaker check
        - Automatic retry with exponential backoff
        - State management
        - Comprehensive logging
        """
        if not self._check_circuit_breaker():
            raise ConnectionError("Circuit breaker is OPEN - Firebase operations blocked")
        
        if not self._initialized: