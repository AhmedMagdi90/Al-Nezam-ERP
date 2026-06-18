# Phase 2 Cleanup Report - 2026-06-18

## Scope

Phase 2 cleanup was limited to development artifacts, generated caches, temporary folders, old deployment bundles, and documentation organization. No business logic, database models, migrations, views, services, templates, or deployment runtime configuration were changed.

## Deleted Artifacts

- Removed Python cache artifacts:
  - All removable `__pycache__/` directories.
  - Tracked and untracked `*.pyc` files under app packages, migrations, tests, and project modules.
- Removed temporary/generated directories:
  - `.codex_tmp/`
  - `.tmp_debug_access/`
  - `.tmp_environment_access_setup_tests/`
  - `.tmp_onboarding_planner_tests/`
  - `.tmp_tenant_provision_tests/`
  - `.tmp_tenant_schema_tests/`
  - `.tmp_test_runtime/`
  - `.tmp_test_tenant_translations/`
  - `.tmp_xlsx/`
  - `.venv_excel/`
  - `dist/`
  - root `__pycache__/`
- Removed root archive/bundle artifacts:
  - `actual-close-time-fix.tgz`
  - `material_readiness_button_fix.tgz`
  - `nezam-accessibility-responsive-ux.tar.gz`
  - `nezam-audit-reporting-ux.tar.gz`
  - `nezam-bom-builder-ux.tar.gz`
  - `nezam-material-readiness-ux.tar.gz`
  - `nezam-planner-action-ux.tar.gz`
  - `nezam-planner-notification-ux.tar.gz`
  - `nezam-supervisor-approval-scope.tar.gz`
  - `nezam-timeline-split-ux.tar.gz`
  - `reports_sprint11_bundle.tgz`
  - `reports_sprint12_material_readiness_bundle.tgz`
  - `sprint13_production_start_gate.tgz`
  - `store-shell-ux-fix.tgz`
  - `us06-team-tab-redesign.tgz`
  - `us07-team-delete-member.tgz`
  - `us07-team-delete-member.zip`
  - `us09-main-wo-split-quantity-sync.tgz`
  - `us09-split-quantity-display.tgz`
  - `us16-stage-machine-separation.tgz`
  - `us17-material-readiness-percent.tgz`
- Removed root scratch scripts and outputs:
  - `check.py`
  - `dump_backlog.py`
  - `test_pw.py`
  - `read_excel.py`
  - `test_out.txt`
  - `test_out2.txt`
  - `scp_log.txt`
  - `all_sheets.json`
  - `sheet_names.json`

Initial deletion command summary: 315 directories and 30 root files were removed. After verification tests regenerated cache/temp artifacts, a final artifact-only cleanup pass removed the regenerated `__pycache__/` and `.tmp_*` directories that were not locked.

## Files Moved

Moved Phase 1 documentation to `docs/phase1/`:

- `PHASE1_CHANGES_QUICK_REFERENCE.md`
- `PHASE1_FINAL_REPORT.md`
- `PHASE1_IMPLEMENTATION_CHECKLIST.txt`
- `PHASE1_IMPLEMENTATION_SUMMARY.md`

Cleanup report created under `docs/cleanup/`.

## .gitignore Changes

Created root `.gitignore` with cleanup protections for:

- `__pycache__/`
- `**pycache**/`
- `**/__pycache__/`
- `*.pyc`
- `.pytest_cache/`
- `.codex_tmp/`
- `.tmp_*/`
- `.venv*/`
- `dist/`
- `*.zip`
- `*.tgz`
- `*.tar.gz`
- `*.bak`
- `*.backup`
- `*.broken_*`

## Risky Files Skipped

- `.tmp_test_catalog/` was not fully removed because Windows denied access to a child temp directory. It remains ignored by `.gitignore`.
- `.env` and `.env.example` were not deleted.
- `db.sqlite3` was not deleted.
- `staticfiles/`, `outputs/`, `qa/`, and `translations/` were not deleted.
- Root runbooks, architecture documents, audit reports, Excel files, and possible project documentation were not deleted.
- No uploaded media or AWS deployment configuration was deleted.

## Verification

- `python manage.py check` - passed.
- `python manage.py test manufacturing.tests.test_phase1_security --noinput` - passed, 4 tests.
- `python manage.py test manufacturing.tests.test_bom_save_api --noinput` - passed, 24 tests.
- `python manage.py test accounts tenancy --noinput` - passed, 82 tests, 1 skipped.

## Recommendation

READY FOR AWS DEV DEPLOY.
