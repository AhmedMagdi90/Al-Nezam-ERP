
from pathlib import Path
import os
import sys
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured
try:
    import dj_database_url
except ImportError:
    dj_database_url = None

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')
SQLITE_TIMEOUT_SECONDS = int(os.getenv('SQLITE_TIMEOUT_SECONDS', '30'))
SQLITE_ENABLE_WAL = os.getenv('SQLITE_ENABLE_WAL', '1') == '1'

DEFAULT_SECRET_KEY = 'dev-secret-key-change-me'
SECRET_KEY = os.getenv('SECRET_KEY', DEFAULT_SECRET_KEY)
DEBUG = os.getenv('DEBUG', '1') == '1'
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv('ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
    if host.strip()
]
if DEBUG or 'test' in sys.argv:
    for host in ('127.0.0.1', 'localhost', 'testserver'):
        if host not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(host)

if not DEBUG and SECRET_KEY == DEFAULT_SECRET_KEY:
    raise ImproperlyConfigured('Set a unique SECRET_KEY when DEBUG=0.')

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        'CSRF_TRUSTED_ORIGINS',
        'http://localhost:8000,http://127.0.0.1:8000'
    ).split(',')
    if origin.strip()
]
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '').strip().lower()
PUBLIC_HTTPS = PUBLIC_BASE_URL.startswith('https://')
# Use cookie-backed CSRF by default for browser compatibility.
CSRF_USE_SESSIONS = os.getenv('CSRF_USE_SESSIONS', '0') == '1'
CSRF_COOKIE_HTTPONLY = False
CSRF_FAILURE_VIEW = 'kemet_erp.csrf.csrf_failure'

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'drf_spectacular',
    # 'channels',  # For WebSocket support (Disabled for now)
    # Project apps
    'tenancy',
    'accounts',
    'manufacturing',
    'dashboard',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    "whitenoise.middleware.WhiteNoiseMiddleware",
    'django.contrib.sessions.middleware.SessionMiddleware',
    'tenancy.middleware.TenantContextMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'manufacturing.security.SecurityMiddleware',  # Custom security middleware
]

ROOT_URLCONF = 'kemet_erp.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.debug',
        'django.template.context_processors.request',
        'django.template.context_processors.csrf',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
        'django.template.context_processors.i18n',
    ]},
}]

WSGI_APPLICATION = 'kemet_erp.wsgi.application'

# Database: SQLite for quick start; we'll move to Postgres when adding tenants.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        'ATOMIC_REQUESTS': False,
        'AUTOCOMMIT': True,
        'CONN_MAX_AGE': 0,
        'CONN_HEALTH_CHECKS': False,
        'TIME_ZONE': None,
        'OPTIONS': {
            'timeout': SQLITE_TIMEOUT_SECONDS,
        },
    }
}

DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
if DATABASE_URL and dj_database_url:
    DATABASES['default'] = dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=int(os.getenv('DB_CONN_MAX_AGE', '600')),
        ssl_require=os.getenv('DB_SSL_REQUIRE', '1') == '1',
    )
    DATABASES['default'].setdefault('ATOMIC_REQUESTS', False)
    DATABASES['default'].setdefault('AUTOCOMMIT', True)
    DATABASES['default'].setdefault('CONN_MAX_AGE', int(os.getenv('DB_CONN_MAX_AGE', '600')))
    DATABASES['default'].setdefault('CONN_HEALTH_CHECKS', False)
    DATABASES['default'].setdefault('TIME_ZONE', None)
    DATABASES['default'].setdefault('OPTIONS', {})

# Tenant routing (Phase 1: infrastructure)
DATABASE_ROUTERS = ['tenancy.router.TenantDatabaseRouter']
TENANCY_SHARED_APPS = ['tenancy']
TENANCY_TENANT_APPS = ['auth', 'contenttypes', 'accounts', 'manufacturing', 'dashboard']
if 'test' in sys.argv:
    TENANCY_ALLOW_TENANT_APPS_ON_DEFAULT = True
else:
    TENANCY_ALLOW_TENANT_APPS_ON_DEFAULT = os.getenv(
        'TENANCY_ALLOW_TENANT_APPS_ON_DEFAULT',
        '0'
    ) == '1'

if 'test' in sys.argv:
    AUTHENTICATION_BACKENDS = [
        'django.contrib.auth.backends.ModelBackend',
        'tenancy.auth_backend.TenantModelBackend',
    ]
else:
    AUTHENTICATION_BACKENDS = [
        'tenancy.auth_backend.TenantModelBackend',
        'django.contrib.auth.backends.ModelBackend',
    ]


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = 'en'
LANGUAGES = [('en', 'English'), ('ar', 'العربية')]
LOCALE_PATHS = [BASE_DIR / 'locale']

TIME_ZONE = 'Africa/Cairo'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Media files (uploads)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Kemet ERP API',
    'VERSION': '1.0.0',
    'DESCRIPTION': 'Manufacturing ERP API for small and medium businesses',
    'SERVE_INCLUDE_SCHEMA': False,
}

# Channels configuration for WebSockets
# ASGI_APPLICATION = 'kemet_erp.asgi.application'

# CHANNEL_LAYERS = {
#     'default': {
#         'BACKEND': 'channels.layers.InMemoryChannelLayer'
#     }
# }

# Email configuration for notifications
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')  # Configure as needed
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@kemeterp.com')
try:
    DEMO_TENANT_LIFETIME_DAYS = max(1, int(os.getenv('DEMO_TENANT_LIFETIME_DAYS', '14')))
except ValueError:
    DEMO_TENANT_LIFETIME_DAYS = 14

LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'
LOGIN_URL = 'login'

# Session/cookie hardening.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax')
CSRF_COOKIE_SAMESITE = os.getenv('CSRF_COOKIE_SAMESITE', 'Lax')
SESSION_COOKIE_AGE = int(os.getenv('SESSION_COOKIE_AGE', '1209600'))  # 14 days.

# Browser hardening headers.
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = os.getenv('SECURE_REFERRER_POLICY', 'same-origin')
SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'

# Login brute-force protection.
LOGIN_MAX_ATTEMPTS = int(os.getenv('LOGIN_MAX_ATTEMPTS', '5'))
LOGIN_MAX_ATTEMPTS_PER_IP = int(os.getenv('LOGIN_MAX_ATTEMPTS_PER_IP', '20'))
LOGIN_ATTEMPT_WINDOW_SECONDS = int(os.getenv('LOGIN_ATTEMPT_WINDOW_SECONDS', '900'))
LOGIN_LOCKOUT_SECONDS = int(os.getenv('LOGIN_LOCKOUT_SECONDS', '900'))

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', '1' if PUBLIC_HTTPS else '0') == '1'
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', '1' if PUBLIC_HTTPS else '0') == '1'
    CSRF_COOKIE_SECURE = os.getenv('CSRF_COOKIE_SECURE', '1' if PUBLIC_HTTPS else '0') == '1'
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000' if PUBLIC_HTTPS else '0'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

