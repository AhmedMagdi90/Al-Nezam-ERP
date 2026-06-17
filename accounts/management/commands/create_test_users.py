from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from accounts.models import Profile, Role

class Command(BaseCommand):
    help = 'Creates standard test users for development (Planner, Supervisor, Maintenance)'

    def handle(self, *args, **kwargs):
        self.stdout.write("Creating Test Users & Roles...")

        # 1. Define Roles
        roles_data = [
            ("planner", "Planner"),
            ("supervisor", "Supervisor"),
            ("maintenance", "Maintenance"),
            ("worker", "Worker"),
        ]
        
        roles = {}
        for code, name in roles_data:
            role, created = Role.objects.get_or_create(name=code)
            roles[code] = role
            if created:
                self.stdout.write(f"   Created Role: {name}")

        # 2. Create Users
        users_data = [
            # (Username, Role Key)
            ("planner", "planner"),
            ("supervisor", "supervisor"),
            ("maintenance", "maintenance"),
            ("worker", "worker"),
        ]

        for username, role_key in users_data:
            user, created = User.objects.get_or_create(username=username)
            user.set_password("kemet123")
            user.save()
            
            # Link Profile
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.role = roles[role_key]
            profile.save()

            status = "Created" if created else "Updated"
            self.stdout.write(f"   {status} User: {username} (Role: {roles[role_key]})")

        self.stdout.write(self.style.SUCCESS("All test users ready! Password: 'kemet123'"))
