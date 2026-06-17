from django.db import models

class RoleType(models.TextChoices):
    PLANNER = 'planner', 'Planner'
    SUPERVISOR = 'supervisor', 'Supervisor'
    WORKER = 'worker', 'Worker'
    STORE = 'store', 'Store'
    MAINTENANCE = 'maintenance', 'Maintenance'
    QUALITY = 'quality', 'Quality'
    ADMIN = 'admin', 'Admin'
