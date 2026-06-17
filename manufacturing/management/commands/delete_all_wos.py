from django.core.management.base import BaseCommand
from manufacturing.models import WorkOrder, ProductionLog
from django.db import transaction

class Command(BaseCommand):
    help = 'Deletes ALL Work Orders and related production logs from the database.'

    def handle(self, *args, **options):
        self.stdout.write("⚠️  WARNING: This will delete ALL Work Orders for ALL users.")
        # Check confirmation if interactive? 
        # For a double-click script, we usually skip confirmation or assume the user knows.
        # But for safety, the script itself can just run.
        
        total_count = WorkOrder.objects.count()
        if total_count == 0:
            self.stdout.write(self.style.WARNING("No Work Orders found."))
            return

        parent_count = WorkOrder.objects.filter(parent__isnull=True).count()
        child_count = WorkOrder.objects.filter(parent__isnull=False).count()
        self.stdout.write(f"Found {parent_count} parent work orders and {child_count} stage tasks.")

        with transaction.atomic():
            # Production logs should cascade, but let's be thorough if needed
            # dependent objects on CASCADE:
            # - ProductionLog
            # - WorkOrderStage
            # - MachineFault (if linked? no)
            # - QualityCheck
            
            # Using the custom delete method if defined on model, or queryset delete
            WorkOrder.objects.all().delete()
            
        self.stdout.write(self.style.SUCCESS(f"✅ Successfully deleted {total_count} Work Orders."))
