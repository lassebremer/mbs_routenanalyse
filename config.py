import os
from datetime import timedelta

class Config:
    """Basis-Konfiguration für die Flask-App."""
    
    # Flask-Einstellungen
    SECRET_KEY = os.environ.get('SECRET_KEY')
    
    # Google Maps API
    MAPS_API_KEY = os.environ.get('MAPS_API')
    
    # API-Limits
    API_LIMITS = {
    "places": int(os.environ.get('PLACES_API_LIMIT', 1000)),
    "geocoding": int(os.environ.get('GEOCODING_API_LIMIT', 10000))
    }
    
    # Anwendungseinstellungen
    SEARCH_TERMS = [
        "Rewe", "EDEKA", "Markant", "Kaufland", 
        "Getränkemarkt", "Globus", "Trinkgut", "Marktkauf"
    ]
    
    DEFAULT_RADIUS_KM = 40
    MAX_RADIUS_KM = 100
    MIN_RADIUS_KM = 5
    
    # Dateipfade
    USAGE_FILE = "api_usage.json"
    
    # OSM-Einstellungen
    HIGHWAY_VALUES = ["motorway_link", "trunk_link"]
    FALLBACK_HIGHWAY_TYPES = ["motorway", "trunk", "primary", "motorway_junction"]
    ROUTE_BUFFER_M = 2000
    
    # Session-Einstellungen
    PERMANENT_SESSION_LIFETIME = timedelta(hours=2)
    
    # Debugging
    DEBUG = False
    TESTING = False

class DevelopmentConfig(Config):
    """Entwicklungskonfiguration."""
    DEBUG = True
    ENV = 'development'

class ProductionConfig(Config):
    """Produktionskonfiguration."""
    DEBUG = False
    ENV = 'production'
    
    # Erweiterte Sicherheit für Produktion
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

class TestingConfig(Config):
    """Test-Konfiguration."""
    TESTING = True
    DEBUG = True
    WTF_CSRF_ENABLED = False

# Konfigurationsauswahl basierend auf Umgebungsvariable
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
} 