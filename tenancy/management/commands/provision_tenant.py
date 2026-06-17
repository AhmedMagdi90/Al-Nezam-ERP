from django.core.management.base import BaseCommand, CommandError

from tenancy.models import Tenant
from tenancy.services import (
    provision_organization_environments,
    provision_tenant_environment,
    provision_tenant_with_owner,
)


class Command(BaseCommand):
    help = "Provision a dedicated tenant database and seed owner account."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Company name")
        parser.add_argument("--code", required=True, help="Company code (slug), e.g. al-nour")
        parser.add_argument("--email", required=True, help="Owner email/username")
        parser.add_argument("--password", required=True, help="Owner password")
        parser.add_argument(
            "--plan",
            default="free_trial",
            choices=["free_trial", "pro", "enterprise"],
            help="Subscription plan",
        )
        parser.add_argument("--with-demo", action="store_true", help="Also provision a demo environment for this organization.")
        parser.add_argument("--with-test", action="store_true", help="Also provision a test/UAT environment for this organization.")
        parser.add_argument("--with-dev", action="store_true", help="Also provision a dev environment for this organization.")
        parser.add_argument(
            "--environment",
            action="append",
            dest="environments",
            choices=[choice for choice, _label in Tenant.EnvironmentType.choices],
            help="Provision exactly these environment types. Repeat the flag, e.g. --environment demo --environment test --environment dev",
        )
        parser.add_argument(
            "--demo-password",
            default="DemoPass123!",
            help="Shared password for seeded demo role accounts when --with-demo is used.",
        )

    def handle(self, *args, **options):
        try:
            explicit_environments = options.get("environments") or []
            if explicit_environments:
                organization, created = provision_organization_environments(
                    company_name=options["company"],
                    company_code=options["code"],
                    owner_email=options["email"],
                    owner_password=options["password"],
                    environment_types=explicit_environments,
                    subscription_plan=options["plan"],
                    demo_password=options["demo_password"],
                )
                created_environments = [(tenant.environment_type, tenant.code, tenant.hostname) for tenant, _company, _user in created]
                primary_tenant, company, user = next(
                    ((tenant, company, user) for tenant, company, user in created if tenant.environment_type == Tenant.EnvironmentType.LIVE),
                    created[0],
                )
                tenant = primary_tenant
                self.stdout.write(self.style.SUCCESS(f"Organization provisioned successfully: {organization.slug}"))
            else:
                tenant, company, user = provision_tenant_with_owner(
                    company_name=options["company"],
                    company_code=options["code"],
                    owner_email=options["email"],
                    owner_password=options["password"],
                    subscription_plan=options["plan"],
                )
                created_environments = [(tenant.environment_type, tenant.code, tenant.hostname)]

                if options["with_demo"]:
                    demo_tenant, _demo_company, _demo_user = provision_tenant_environment(
                        tenant.organization,
                        Tenant.EnvironmentType.DEMO,
                        owner_password=options["password"],
                        subscription_plan=options["plan"],
                        seed_demo_package=True,
                        demo_password=options["demo_password"],
                    )
                    created_environments.append((demo_tenant.environment_type, demo_tenant.code, demo_tenant.hostname))

                if options["with_test"]:
                    test_tenant, _test_company, _test_user = provision_tenant_environment(
                        tenant.organization,
                        Tenant.EnvironmentType.TEST,
                        owner_password=options["password"],
                        subscription_plan=options["plan"],
                    )
                    created_environments.append((test_tenant.environment_type, test_tenant.code, test_tenant.hostname))

                if options["with_dev"]:
                    dev_tenant, _dev_company, _dev_user = provision_tenant_environment(
                        tenant.organization,
                        Tenant.EnvironmentType.DEV,
                        owner_password=options["password"],
                        subscription_plan=options["plan"],
                    )
                    created_environments.append((dev_tenant.environment_type, dev_tenant.code, dev_tenant.hostname))
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Tenant provisioned successfully."))
        for environment_type, tenant_code, hostname in created_environments:
            self.stdout.write(f"[{environment_type}] tenant code: {tenant_code}")
            if hostname:
                self.stdout.write(f"[{environment_type}] hostname: {hostname}")
        self.stdout.write(f"Primary DB alias: {tenant.db_alias}")
        self.stdout.write(f"Primary DB name: {tenant.db_name}")
        self.stdout.write(f"Company id: {company.id}")
        self.stdout.write(f"Owner id: {user.id}")
