# AWS Upload Guide

## 1) Build upload package
Run from project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prepare_aws_bundle.ps1 -Zip
```

This creates:
- `dist/aws_upload_YYYYMMDD_HHMMSS/`
- `dist/aws_upload_YYYYMMDD_HHMMSS.zip`

The bundle keeps deployment assets that are now checked into the repo:
- `deploy/systemd/smart-erp.service`
- `deploy/nginx/nezam-dev.conf`
- `NEZAM_DEV_ACCOUNT_RUNBOOK.md`

## 2) Configure environment variables
Inside the generated upload folder:

1. Copy `.env.example` to `.env`
2. Set real values for:
- `SECRET_KEY`
- `DEBUG=0`
- `PUBLIC_BASE_URL`
- `ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`
- `DATABASE_URL` (recommended: AWS RDS PostgreSQL)

If you are using only EC2 public IP (no domain/SSL yet):
- `PUBLIC_BASE_URL=http://YOUR_EC2_PUBLIC_IP`
- `ALLOWED_HOSTS=YOUR_EC2_PUBLIC_IP`
- `CSRF_TRUSTED_ORIGINS=http://YOUR_EC2_PUBLIC_IP`
- `SECURE_SSL_REDIRECT=0`
- `SESSION_COOKIE_SECURE=0`
- `CSRF_COOKIE_SECURE=0`

## 3) Upload options

### Option A: Elastic Beanstalk
1. Open AWS Elastic Beanstalk
2. Create Python environment
3. Upload the generated `.zip`
4. Set env vars in Beanstalk configuration
5. Deploy

### Option B: EC2 (Ubuntu)
1. Upload extracted folder to server
2. Install dependencies: `pip install -r requirements.txt`
3. Run migrations: `python manage.py migrate`
4. Collect static: `python manage.py collectstatic --noinput`
5. Install `deploy/systemd/smart-erp.service`
6. Install `deploy/nginx/nezam-dev.conf`
7. Start app with Gunicorn using the systemd unit

For the current `nezam-dev` account, use `NEZAM_DEV_ACCOUNT_RUNBOOK.md` as the exact EC2 checklist.

## Notes
- `gunicorn` and `dj-database-url` were added to `requirements.txt`.
- In production (`DEBUG=0`) secure cookie/HSTS/SSL redirect settings are enabled.
- `openpyxl` is included in `requirements.txt` for bulk import views.

## PostgreSQL Migration (SQLite -> PostgreSQL)

### Control-plane DB (`default`)
1. Create PostgreSQL DB in AWS RDS.
2. Set `DATABASE_URL` in `/opt/smart-erp/.env`.
3. Export existing SQLite data and load into PostgreSQL:

```bash
cd /opt/smart-erp
source .venv/bin/activate
python manage.py dumpdata --database=default --exclude contenttypes --exclude auth.permission > /tmp/default_data.json
python manage.py migrate
python manage.py loaddata /tmp/default_data.json
```

### Tenant DB (per company)
Use the included command to migrate each tenant from SQLite to PostgreSQL:

```bash
python manage.py migrate_tenant_to_postgres \
  --tenant-code al-nour \
  --target-url "postgresql://user:pass@host:5432/tenant_al_nour"
```

After migration, the tenant row is updated to point to PostgreSQL URL.

For new registrations to provision directly on PostgreSQL, set:

```env
TENANT_DB_URL_TEMPLATE=postgresql://user:pass@host:5432/tenant_{code_underscore}
TENANT_PG_ADMIN_URL=postgresql://admin_user:admin_pass@host:5432/postgres
```

If `TENANT_PG_ADMIN_URL` is set, tenant DBs are auto-created before tenant migrations run.
