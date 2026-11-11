"""Django settings for the ZyDwell rental management system."""
import os
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    'ZYDWELL_SECRET_KEY',
    'django-insecure-dev-only-change-me-in-production-0x9f3a',
)
DEBUG = os.environ.get(
    'ZYDWELL_DEBUG', '0' if os.environ.get('RENDER') else '1',
) == '1'
ALLOWED_HOSTS = [h for h in os.environ.get('ZYDWELL_ALLOWED_HOSTS', '*').split(',') if h]
# Django 4+ rejects HTTPS form posts unless the origin is trusted.
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get(
    'ZYDWELL_CSRF_TRUSTED_ORIGINS', 'https://zydwell.hostlanetechnolab.com',
).split(',') if o]
# The site sits behind an HTTPS reverse proxy (nginx / cPanel).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'core',
    'accounts',
    'properties',
    'guests',
    'billing',
    'accounting',
    'messaging',
    'reports',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'core.middleware.CurrentRequestMiddleware',
    'core.middleware.TenantMiddleware',
    'core.middleware.PasswordChangeRequiredMiddleware',
    'core.middleware.SubscriptionLockMiddleware',
]

ROOT_URLCONF = 'zydwell.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.i18n',
                'core.context_processors.branding',
            ],
        },
    },
]

WSGI_APPLICATION = 'zydwell.wsgi.application'

DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL'),
        conn_max_age=600,
        ssl_require=True
    )
}

AUTH_USER_MODEL = 'accounts.User'
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'reports:dashboard'
LOGOUT_REDIRECT_URL = 'accounts:login'

LANGUAGE_CODE = 'en'
LANGUAGES = [('en', 'English'), ('ar', 'العربية')]
LOCALE_PATHS = [BASE_DIR / 'locale']
TIME_ZONE = os.environ.get('ZYDWELL_TIME_ZONE', 'Asia/Dubai')
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

MESSAGE_STORAGE = 'django.contrib.messages.storage.session.SessionStorage'

# --- ZyDwell platform defaults (super admin can override per tenant) ---
ZYDWELL = {
    'DEFAULT_CURRENCY': os.environ.get('ZYDWELL_CURRENCY', 'AED'),
    'RECEIPT_PREFIX': 'RCP',
    'INVOICE_PREFIX': 'INV',
    'BACKUP_DIR': BASE_DIR / 'backups',
    # How long a receipt link shared with a guest keeps working.
    'RECEIPT_LINK_DAYS': int(os.environ.get('ZYDWELL_RECEIPT_LINK_DAYS', '30')),
    # WhatsApp log entries older than this are deleted (waiting ones are kept).
    'MESSAGE_LOG_DAYS': int(os.environ.get('ZYDWELL_MESSAGE_LOG_DAYS', '30')),
}

WHATSAPP = {
    'ENABLED': os.environ.get('WHATSAPP_ENABLED', '0') == '1',
    'API_URL': os.environ.get('WHATSAPP_API_URL', 'https://graph.facebook.com/v21.0'),
    'PHONE_NUMBER_ID': os.environ.get('WHATSAPP_PHONE_NUMBER_ID', ''),
    'ACCESS_TOKEN': os.environ.get('WHATSAPP_ACCESS_TOKEN', ''),
}
