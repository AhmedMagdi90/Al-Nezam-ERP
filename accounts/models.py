from django.db import models
from django.contrib.auth.models import User


class Role(models.Model):
    """Predefined roles like Planner, Supervisor, Worker, etc."""
    ROLE_CHOICES = [
        ("admin", "Admin"),
        ("planner", "Planner"),
        ("supervisor", "Supervisor"),
        ("worker", "Worker"),
        ("store", "Store"),
        ("quality", "Quality Officer"),
        ("maintenance", "Maintenance"),
    ]
    name = models.CharField(max_length=50, choices=ROLE_CHOICES, unique=True)

    def __str__(self):
        return self.get_name_display()


class Profile(models.Model):
    """Extend Django User with a role."""
    APP_SCOPE_CHOICES = [
        ("manufacturing", "Manufacturing"),
        ("store", "Store"),
        ("quality", "Quality"),
        ("maintenance", "Maintenance"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.SET_NULL)
    company = models.ForeignKey('manufacturing.Company', on_delete=models.CASCADE, related_name="users", null=True, blank=True)
    app_scope = models.CharField(max_length=20, choices=APP_SCOPE_CHOICES, default="manufacturing")
    department = models.CharField(max_length=500, blank=True, null=True)
    shift = models.CharField(max_length=30, blank=True, null=True)
    planned_shift = models.CharField(max_length=30, blank=True, null=True)
    planned_shift_start_date = models.DateField(blank=True, null=True)
    worker_mode_enabled = models.BooleanField(default=False)

    phone = models.CharField(max_length=20, blank=True, null=True)
    profile_image = models.ImageField(upload_to='profiles/', blank=True, null=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"
