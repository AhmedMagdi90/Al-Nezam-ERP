from django.shortcuts import render, redirect
from django.contrib.auth import login
from django.views import View
from django.contrib import messages
from django.conf import settings
from django.urls import reverse
from manufacturing.models import Company, Machine, Product, ProductionStage, BillOfMaterial, WorkOrder
from manufacturing.forms import CompanyRegistrationForm
from manufacturing.import_catalog import get_bulk_import_catalog
from manufacturing.onboarding_state import get_company_setup_counts
from accounts.models import Profile, Role
from accounts.constants import RoleType
from django.contrib.auth.models import User
from .dashboard import require_company
from tenancy.db import ensure_tenant_database_ready
from tenancy.models import PlatformSettings, Tenant
from tenancy.context import reset_current_tenant_db, set_current_tenant_db
from tenancy.services import TENANT_AUTH_BACKEND, provision_demo_signup
from accounts.views import home_redirect


def resolve_onboarding_tenant_alias(request):
    alias = getattr(request, "tenant_db_alias", None)
    if alias and alias != "default":
        return alias

    tenant_code = request.session.get("tenant_code")
    if not tenant_code:
        return None

    tenant = Tenant.objects.using("default").filter(code=tenant_code, is_active=True).first()
    if not tenant:
        return None
    return ensure_tenant_database_ready(tenant)


def resolve_onboarding_company(request, db_alias):
    ctx_token = set_current_tenant_db(db_alias)
    try:
        company = require_company(request.user)
        if company:
            return company

        company = Company.objects.using(db_alias).order_by("-created_at").first()
        if not company:
            return None

        profile, _ = Profile.objects.using(db_alias).get_or_create(user_id=request.user.id)
        if not profile.company_id:
            profile.company_id = company.id
            profile.save(using=db_alias, update_fields=["company"])
        return company
    finally:
        reset_current_tenant_db(ctx_token)


class LandingPageView(View):
    def get(self, request):
        if request.user.is_authenticated:
            return home_redirect(request)
        platform_settings = PlatformSettings.get_solo()
        return render(
            request,
            'landing.html',
            {
                "subscription_estimator_config": platform_settings.estimator_config,
            },
        )

class RegisterCompanyView(View):
    def get(self, request):
        return redirect(f"{reverse('login')}?tab=register")

    def post(self, request):
        form = CompanyRegistrationForm(request.POST)
        if not form.is_valid():
            return render(request, 'registration/register_company.html', {'form': form})

        data = form.cleaned_data
        try:
            tenant, company, user = provision_demo_signup(
                company_name=data['company_name'],
                company_code=data['company_code'],
                owner_email=data['owner_email'],
                owner_password=data['owner_password'],
                seed_demo_package=False,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(request, 'registration/register_company.html', {'form': form})
        except Exception as exc:
            error_text = str(exc).strip()
            postgres_hint = (
                "Company setup failed while preparing its database. "
                "Check PostgreSQL connection, control database migrations, and tenant DB template settings."
            )
            if settings.DEBUG:
                messages.error(request, f"Registration failed: {error_text or postgres_hint}")
            else:
                messages.error(request, postgres_hint)
            return render(request, 'registration/register_company.html', {'form': form})

        request.session['tenant_code'] = tenant.code
        request.session["first_time_company_setup"] = True
        request.session["reset_planner_workspace_state"] = True
        request.tenant = tenant
        request.tenant_db_alias = tenant.db_alias
        ctx_token = set_current_tenant_db(tenant.db_alias)
        try:
            login(request, user, backend=TENANT_AUTH_BACKEND)
        finally:
            reset_current_tenant_db(ctx_token)
        # Re-assert tenant code because session rotation can drop custom keys.
        request.session['tenant_code'] = tenant.code
        request.session["first_time_company_setup"] = True
        request.session["reset_planner_workspace_state"] = True
        messages.success(
            request,
            f"Welcome to Nezam! Your workspace for {company.name} is ready. Complete the setup wizard before opening planner. Sign-in code: {tenant.code}"
        )
        return home_redirect(request)

class OnboardingDataView(View):
    """Step 2: Add initial Demo Data"""
    def get(self, request):
        db_alias = resolve_onboarding_tenant_alias(request)
        if not db_alias:
            messages.error(request, "Session expired. Please sign in again using your company code.")
            return redirect("login")

        company = resolve_onboarding_company(request, db_alias)
        if not company:
            messages.error(request, "No company found for your account. Please register company again.")
            return redirect("login")

        context = {
            "setup_counts": get_company_setup_counts(company, db_alias=db_alias),
            "active_company_name": company.name,
            "active_tenant_code": request.session.get("tenant_code") or getattr(getattr(request, "tenant", None), "code", ""),
            "upload_counts": {
                **get_company_setup_counts(company, db_alias=db_alias),
                "products": Product.objects.using(db_alias).filter(company=company).count(),
            },
            "import_catalog": [],
        }
        for card in get_bulk_import_catalog():
            count_key = card.get("count_key")
            context["import_catalog"].append(
                {
                    **card,
                    "uploaded_count": int(context["upload_counts"].get(count_key, 0) or 0),
                }
            )
        return render(request, 'registration/onboarding_data.html', context)
        
    def post(self, request):
        db_alias = resolve_onboarding_tenant_alias(request)
        if not db_alias:
            messages.error(request, "Session expired. Please sign in again using your company code.")
            return redirect("login")

        company = resolve_onboarding_company(request, db_alias)
        if not company:
            messages.error(request, "No company found for your account. Please register company again.")
            return redirect("login")

        action = request.POST.get('action')
        if not action and request.POST.get('load_demo'):
            action = 'load_demo'
        
        if action == 'load_demo':
             # Create basic machines
            Machine.objects.using(db_alias).get_or_create(
                company=company,
                code="CNC01",
                defaults={"name": "CNC Lathe 1", "type": "CNC", "status": "operational"},
            )
            Machine.objects.using(db_alias).get_or_create(
                company=company,
                code="ASM01",
                defaults={"name": "Assembly Station A", "type": "Assembly", "status": "operational"},
            )
             
             # Create Products (get_or_create to prevent dupes)
            p1, _ = Product.objects.using(db_alias).get_or_create(company=company, name="Steel Shaft", defaults={'unit': 'pcs'})
             
            messages.success(request, "Demo data loaded!")
            request.session["reset_planner_workspace_state"] = True
            return redirect('onboarding_users')
        return redirect('onboarding_users')

class OnboardingUsersView(View):
    """Step 3: Invite Team"""
    STARTER_PASSWORD = "Password123!"
    TEAM_FIELDS = [
        ("planner", RoleType.PLANNER.value, "manufacturing"),
        ("supervisor", RoleType.SUPERVISOR.value, "manufacturing"),
        ("worker", RoleType.WORKER.value, "manufacturing"),
        ("quality", RoleType.QUALITY.value, "quality"),
        ("maintenance", RoleType.MAINTENANCE.value, "maintenance"),
    ]

    def _split_name(self, full_name, email):
        cleaned = (full_name or "").strip()
        if not cleaned:
            cleaned = (email or "").split("@", 1)[0].replace(".", " ").replace("_", " ").strip()
        first_name, _, last_name = cleaned.partition(" ")
        return first_name or cleaned or email, last_name

    def _create_launch_team(self, request, db_alias, company):
        created_count = 0
        skipped = []
        user_manager = User.objects.db_manager(db_alias)
        role_manager = Role.objects.db_manager(db_alias)
        profile_manager = Profile.objects.db_manager(db_alias)

        for field_key, role_key, app_scope in self.TEAM_FIELDS:
            email = (request.POST.get(f"email_{field_key}") or "").strip().lower()
            full_name = (request.POST.get(f"name_{field_key}") or "").strip()
            password = (request.POST.get(f"password_{field_key}") or "").strip()
            
            if not email and not full_name:
                continue
            if not email:
                skipped.append(field_key.replace("_", " ").title())
                continue

            if not password:
                password = self.STARTER_PASSWORD

            existing_user = user_manager.filter(username=email).first()
            first_name, last_name = self._split_name(full_name, email)
            role, _ = role_manager.get_or_create(name=role_key)

            if existing_user:
                user = existing_user
                update_fields = []
                if not user.email:
                    user.email = email
                    update_fields.append("email")
                if first_name and not user.first_name:
                    user.first_name = first_name
                    update_fields.append("first_name")
                if last_name and not user.last_name:
                    user.last_name = last_name
                    update_fields.append("last_name")
                    
                if password and password != self.STARTER_PASSWORD:
                    user.set_password(password)
                    update_fields.append("password")
                    
                if update_fields:
                    user.save(using=db_alias, update_fields=update_fields)
            else:
                user = user_manager.create_user(
                    username=email,
                    email=email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                )

            profile, _ = profile_manager.get_or_create(user_id=user.id)
            changed = not existing_user
            if profile.company_id != company.id:
                changed = True
            profile.company = company
            if profile.role_id != role.id:
                changed = True
            profile.role = role
            if profile.app_scope != app_scope:
                changed = True
            profile.app_scope = app_scope
            if profile.worker_mode_enabled:
                changed = True
                profile.worker_mode_enabled = False
            profile.save(using=db_alias)
            if changed:
                created_count += 1
            else:
                skipped.append(email)

        return created_count, skipped

    def _team_member_count(self, db_alias, company):
        if not company:
            return 0
        return User.objects.using(db_alias).filter(profile__company=company).distinct().count()

    def _launch_team_snapshot(self, db_alias, company):
        if not company:
            return []
        role_names = [role_key for _field_key, role_key, _scope in self.TEAM_FIELDS]
        return list(
            User.objects.using(db_alias)
            .filter(profile__company=company, profile__role__name__in=role_names)
            .select_related("profile__role")
            .order_by("profile__role__name", "first_name", "username")
            .values(
                "username",
                "email",
                "first_name",
                "last_name",
                "profile__role__name",
                "profile__app_scope",
            )
        )

    def get(self, request):
        db_alias = resolve_onboarding_tenant_alias(request)
        company = resolve_onboarding_company(request, db_alias) if db_alias else None
        setup_counts = get_company_setup_counts(company, db_alias=db_alias) if company and db_alias else {}
        upload_counts = {
            **setup_counts,
            "products": Product.objects.using(db_alias).filter(company=company).count() if company and db_alias else 0,
        }
        launch_team = self._launch_team_snapshot(db_alias, company) if company and db_alias else []
        return render(
            request,
            'registration/onboarding_users.html',
            {
                "active_tenant_code": request.session.get("tenant_code") or getattr(getattr(request, "tenant", None), "code", ""),
                "active_company_name": company.name if company else "",
                "setup_counts": setup_counts,
                "upload_counts": upload_counts,
                "launch_team": launch_team,
                "launch_team_count": len(launch_team),
            },
        )

    def post(self, request):
        db_alias = resolve_onboarding_tenant_alias(request)
        if not db_alias:
            messages.error(request, "Session expired. Please sign in again using your company code.")
            return redirect("login")

        ctx_token = set_current_tenant_db(db_alias)
        try:
            company = resolve_onboarding_company(request, db_alias)
            admin_role, _ = Role.objects.using(db_alias).get_or_create(name='admin')
            profile, _ = Profile.objects.using(db_alias).get_or_create(user_id=request.user.id)
            if not company and not profile.company_id:
                company = Company.objects.using(db_alias).order_by('-created_at').first()
            if company and not profile.company_id:
                profile.company_id = company.id
            if not profile.role_id:
                profile.role_id = admin_role.id
            profile.save(using=db_alias)

            if company:
                created_count, skipped = self._create_launch_team(request, db_alias, company)
                team_member_count = self._team_member_count(db_alias, company)
            else:
                messages.error(request, "No company found for your account. Please register company again.")
                created_count, skipped, team_member_count = 0, [], 0
        finally:
            reset_current_tenant_db(ctx_token)

        if request.POST.get("skip_team_setup"):
            request.session["first_time_company_setup"] = False
            request.session["reset_planner_workspace_state"] = True
            request.session["first_workspace_tour_pending"] = True
            messages.info(request, "Team setup skipped. You can add users later from Settings or Bulk Upload.")
            return redirect('planner_dashboard')

        if team_member_count <= 1:
            request.session["first_time_company_setup"] = True
            messages.warning(
                request,
                "Add at least one launch team member or choose Skip Team Setup before opening planner.",
            )
            if skipped:
                messages.warning(request, "Skipped team entries that were incomplete or already existed: " + ", ".join(skipped))
            return redirect('onboarding_users')

        request.session["first_time_company_setup"] = False
        request.session["reset_planner_workspace_state"] = True
        request.session["first_workspace_tour_pending"] = True
        if created_count:
            messages.success(
                request,
                f"Setup complete. {created_count} launch team member(s) added with default password {self.STARTER_PASSWORD}.",
            )
        else:
            messages.success(request, "Setup complete. You can continue adding data later from Factory Setup or Bulk Import.")
        if skipped:
            messages.warning(request, "Skipped team entries that were incomplete or already existed: " + ", ".join(skipped))
        return redirect('planner_dashboard')
