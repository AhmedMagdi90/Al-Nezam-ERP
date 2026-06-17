import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.conf import settings
from django.db import connections
from django.test import TransactionTestCase

from accounts.models import Profile
from manufacturing.models import Company, SystemSettings
from tenancy.db import _TENANT_SCHEMA_READY_ALIASES, ensure_tenant_database_ready


TEST_TENANT_ROOT = Path.cwd() / ".tmp_tenant_schema_tests"
TEST_TENANT_DB_PATH = TEST_TENANT_ROOT / "legacy_schema.sqlite3"
TEST_TENANT_ROOT.mkdir(parents=True, exist_ok=True)

if "tenant_legacy_schema_test" not in settings.DATABASES:
    settings.DATABASES["tenant_legacy_schema_test"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(TEST_TENANT_DB_PATH),
        "OPTIONS": {"timeout": 30},
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {
            "NAME": str(TEST_TENANT_DB_PATH),
            "MIRROR": None,
            "MIGRATE": True,
            "SERIALIZE": False,
            "DEPENDENCIES": [],
        },
    }
    connections.databases["tenant_legacy_schema_test"] = settings.DATABASES["tenant_legacy_schema_test"]


class LegacyTenantSchemaUpgradeTests(TransactionTestCase):
    TENANT_ALIAS = "tenant_legacy_schema_test"
    databases = {"default", TENANT_ALIAS}

    def setUp(self):
        source = Path.cwd() / "tenant_dbs" / "al-nour.sqlite3"
        if not source.exists():
            self.skipTest(f"Legacy tenant fixture not found: {source}")
        self.temp_root = TEST_TENANT_ROOT
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.db_path = TEST_TENANT_DB_PATH
        shutil.copy2(source, self.db_path)
        self.alias = self.TENANT_ALIAS

    def tearDown(self):
        if self.alias in connections.databases:
            connections[self.alias].close()
        _TENANT_SCHEMA_READY_ALIASES.discard(self.alias)
        shutil.rmtree(self.temp_root, ignore_errors=True)
        super().tearDown()

    def _table_count(self, table_name):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            return cursor.fetchone()[0]

    def _table_columns(self, table_name):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({table_name})")
            return [row[1] for row in cursor.fetchall()]

    def test_ensure_tenant_database_ready_upgrades_legacy_sqlite_schema_without_losing_rows(self):
        counts_before = {
            "auth_user": self._table_count("auth_user"),
            "accounts_profile": self._table_count("accounts_profile"),
            "manufacturing_company": self._table_count("manufacturing_company"),
            "manufacturing_systemsettings": self._table_count("manufacturing_systemsettings"),
        }

        tenant = SimpleNamespace(
            code="legacy-schema-test",
            db_alias=self.alias,
            db_name=str(self.db_path),
        )
        alias = ensure_tenant_database_ready(tenant)

        User = get_user_model()
        self.assertEqual(User.objects.using(alias).count(), counts_before["auth_user"])
        self.assertEqual(Profile.objects.using(alias).count(), counts_before["accounts_profile"])
        self.assertEqual(Company.objects.using(alias).count(), counts_before["manufacturing_company"])
        self.assertEqual(SystemSettings.objects.using(alias).count(), counts_before["manufacturing_systemsettings"])

        profile_columns = self._table_columns("accounts_profile")
        self.assertIn("department", profile_columns)
        self.assertIn("shift", profile_columns)
        self.assertIn("worker_mode_enabled", profile_columns)

        settings_columns = self._table_columns("manufacturing_systemsettings")
        self.assertIn("department_catalog", settings_columns)
        self.assertIn("translation_overrides", settings_columns)
        self.assertIn("default_operation_flow_mode", settings_columns)
