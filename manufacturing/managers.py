from django.db import models

class TenantManager(models.Manager):
    """
    Custom Manager to enforce Company Tenant Isolation.
    Usage: WorkOrder.objects.for_company(company)
    """
    def for_company(self, company):
        if not company:
            return self.none()
        return self.get_queryset().filter(company=company)
