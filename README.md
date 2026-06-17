# Nezam Manufacturing ERP

Multi-tenant Django manufacturing system with:

- control-plane tenancy in `tenancy`
- tenant-scoped apps in `accounts`, `manufacturing`, and `dashboard`
- WSGI entrypoint for Gunicorn at `kemet_erp.wsgi`
- WhiteNoise static serving
- PostgreSQL-ready configuration through environment variables

## Local Run

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Production Shape

Current runtime is structured for:

- `gunicorn kemet_erp.wsgi:application`
- Nginx reverse proxy
- PostgreSQL for control DB
- PostgreSQL tenant DB provisioning via:
  - `DATABASE_URL`
  - `TENANT_DB_URL_TEMPLATE`
  - `TENANT_PG_ADMIN_URL`
- Optional generated tenant hostnames via `TENANT_BASE_DOMAIN`

Static files are handled by WhiteNoise. Media is still local-disk by default and should move to S3 for AWS production.

Checked-in deployment assets for the current EC2-based dev account:

- `deploy/systemd/smart-erp.service`
- `deploy/nginx/nezam-dev.conf`
- `scripts/prepare_aws_bundle.ps1`
- `NEZAM_DEV_ACCOUNT_RUNBOOK.md`

## Notes

- `.env.example` is the reference environment file.
- `Procfile` matches the current WSGI deployment path.
- `kemet_erp/asgi.py` exists, but Channels is not currently enabled in `settings.py`, so WebSocket deployment is not part of the active production structure yet.
