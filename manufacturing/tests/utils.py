from django.contrib.auth.models import User

from accounts.models import Role, Profile
from manufacturing.models import Company


def create_company(name="Test Company"):
    return Company.objects.create(name=name)


def create_user_with_role(username, role_name, company, password="password"):
    user = User.objects.create_user(username=username, password=password)
    role, _ = Role.objects.get_or_create(name=role_name)
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.role = role
    profile.company = company
    profile.save()
    user.refresh_from_db()
    # Keep test authentication deterministic across environments. The tenant
    # backend depends on request tenant context; ModelBackend works against the
    # default test DB used by these suites.
    user.backend = "django.contrib.auth.backends.ModelBackend"
    return user
