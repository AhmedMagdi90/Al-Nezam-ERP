# AGENTS.md

This file defines the current deployment context for Codex and other AI coding chats working in this repository.

## Project

- Project: Nezam manufacturing ERP
- Stack: Django + Gunicorn + nginx + PostgreSQL
- Repo root: this directory
- Main Django project: `kemet_erp`
- Main app runtime path on servers: `/home/ubuntu/app/Manufacturing`

## AWS Organization Layout

- `nezam-dev`
  - Internal engineering environment
  - URL: `https://dev.alnezam.com`
- `nezam-demo`
  - Customer-facing demo / minimum trial environment
  - URL: `https://demo.alnezam.com`
- `nezam-uat`
  - Planned, not finished yet
- `nezam-prod`
  - Planned, not finished yet

## Domain / DNS

- Registrar and DNS: GoDaddy
- Root domain `alnezam.com` remains on GoDaddy website builder
- Environment subdomains point directly to AWS Elastic IPs

Current records:

- `dev.alnezam.com` -> `51.102.210.45`
- `demo.alnezam.com` -> `18.184.183.151`

## Current Server Definitions

### nezam-dev

- AWS account: `nezam-dev`
- Region: `eu-central-1`
- EC2 name: `nezam-dev-app`
- Public Elastic IP: `51.102.210.45`
- App path: `/home/ubuntu/app/Manufacturing`
- systemd service: `nezam-dev.service`
- nginx site: `/etc/nginx/sites-available/nezam-dev`
- HTTPS handled by Certbot
- PostgreSQL: local on EC2

Current expected `.env` shape on dev:

```env
SECRET_KEY=<real dev secret>
DEBUG=0
ALLOWED_HOSTS=127.0.0.1,localhost,51.102.210.45,dev.alnezam.com
CSRF_TRUSTED_ORIGINS=https://dev.alnezam.com
PUBLIC_BASE_URL=https://dev.alnezam.com
DATABASE_URL=postgresql://erp_admin:...@127.0.0.1:5432/erp_control
TENANT_DB_URL_TEMPLATE=postgresql://erp_admin:...@127.0.0.1:5432/tenant_{code_underscore}
TENANT_PG_ADMIN_URL=postgresql://postgres:...@127.0.0.1:5432/postgres
DB_SSL_REQUIRE=0
TENANCY_ALLOW_TENANT_APPS_ON_DEFAULT=0
```

### nezam-demo

- AWS account: `nezam-demo`
- Region: `eu-central-1`
- EC2 name: `nezam-demo-app`
- Public Elastic IP: `18.184.183.151`
- App path: `/home/ubuntu/app/Manufacturing`
- systemd service: `nezam-demo.service`
- nginx site: `/etc/nginx/sites-available/nezam-demo`
- HTTPS handled by Certbot
- PostgreSQL: local on EC2

Current expected `.env` shape on demo:

```env
SECRET_KEY=<real demo secret>
DEBUG=0
ALLOWED_HOSTS=127.0.0.1,localhost,18.184.183.151,demo.alnezam.com
CSRF_TRUSTED_ORIGINS=https://demo.alnezam.com
PUBLIC_BASE_URL=https://demo.alnezam.com
DATABASE_URL=postgresql://erp_admin:...@127.0.0.1:5432/erp_control
TENANT_DB_URL_TEMPLATE=postgresql://erp_admin:...@127.0.0.1:5432/tenant_{code_underscore}
TENANT_PG_ADMIN_URL=postgresql://postgres:...@127.0.0.1:5432/postgres
DB_SSL_REQUIRE=0
TENANCY_ALLOW_TENANT_APPS_ON_DEFAULT=0
```

## Standard Dev Update Flow

Use this flow when the goal is "update code to dev".

1. Make code changes in this repo.
2. Prepare a clean upload bundle if needed:
   - `scripts/prepare_aws_bundle.ps1`
3. SSH to dev:
   - `ssh -i "C:\Users\AFRO\.ssh\nezam-dev-key.pem" ubuntu@51.102.210.45`
4. Server app path:
   - `/home/ubuntu/app/Manufacturing`
5. Update code on server by uploading archive or syncing files.
6. On the dev server:
   - activate or use `/home/ubuntu/app/Manufacturing/.venv`
   - install any new dependencies
   - run migrations
   - run `python3 manage.py collectstatic --noinput`
7. Restart app:
   - `sudo systemctl restart nezam-dev.service`
8. Verify:
   - `sudo systemctl status nezam-dev.service --no-pager`
   - `sudo systemctl status nginx --no-pager`
   - `sudo nginx -t`
   - open `https://dev.alnezam.com`

## Standard Demo Update Flow

Use this flow when the goal is "update code to demo".

1. Build or archive the updated project from dev/local.
2. Upload to demo server:
   - `ssh -i "C:\Users\AFRO\.ssh\nezam-demo-key.pem" ubuntu@18.184.183.151`
3. Server app path:
   - `/home/ubuntu/app/Manufacturing`
4. On the demo server:
   - install dependencies in `.venv` if needed
   - run migrations
   - run `python3 manage.py collectstatic --noinput`
5. Restart app:
   - `sudo systemctl restart nezam-demo.service`
6. Verify:
   - `sudo systemctl status nezam-demo.service --no-pager`
   - `sudo systemctl status nginx --no-pager`
   - `sudo nginx -t`
   - open `https://demo.alnezam.com`

## Important Safety Rules

- Do not point `demo` or `dev` back to the old IP `3.125.41.189`.
- Do not reuse `dev.alnezam.com` settings in `demo`.
- Keep `SECRET_KEY` different per environment.
- Keep `DEBUG=0` on public internet environments.
- `TENANCY_ALLOW_TENANT_APPS_ON_DEFAULT` may temporarily need `1` only during first-time bootstrap on a fresh server. Set it back to `0` afterward.
- On public servers, expect bot traffic against random URLs. That is normal.

## Existing Runbooks

- `NEZAM_DEV_ACCOUNT_RUNBOOK.md`
- `AWS_UPLOAD_GUIDE.md`
- `AWS_DEPLOYMENT.md`
- `Nezam_AWS_Architecture_Guide.md`

If a future Codex chat needs deployment context, start here first.
