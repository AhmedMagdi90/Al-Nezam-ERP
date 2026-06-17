from django.core.management.base import BaseCommand
from manufacturing.models import WorkOrder, WorkOrderStage, Machine
from django.utils import timezone

class Command(BaseCommand):
    help = 'Cleans up ghost reservations and orphan records.'

    def add_arguments(self, parser):
        parser.add_argument('--machine', type=str, help='Specific machine name to clear schedules for')
        parser.add_argument('--clear-future', action='store_true', help='Clear all future schedules for the specified machine')

    def handle(self, *args, **options):
        self.stdout.write("Running Schedule Cleanup...")
        
        # 1. Fix "Ghost" Reservations (Canceled/Draft orders holding slots)
        # We explicitly clear start/end dates for canceled orders so they don't block logic
        ghosts = WorkOrder.objects.filter(status='canceled', start_date__isnull=False)
        count = ghosts.count()
        if count > 0:
            self.stdout.write(f"Found {count} canceled orders holding machine slots. Clearing dates...")
            ghosts.update(start_date=None, end_date=None)
            self.stdout.write(self.style.SUCCESS(f"Cleared {count} ghost reservations."))
        else:
            self.stdout.write("No 'canceled' status ghost reservations found.")

        # 2. Check for Orphan Subtasks (Parent deleted but child remains - unlikely with CASCADE but possible)
        orphans = WorkOrder.objects.filter(parent__isnull=False).exclude(parent__in=WorkOrder.objects.all())
        orphan_count = orphans.count()
        if orphan_count > 0:
             self.stdout.write(f"Found {orphan_count} orphan subtasks. Deleting...")
             orphans.delete()
        
        # 3. Check for Orphan Stages
        orphan_stages = WorkOrderStage.objects.filter(work_order__isnull=True)
        if orphan_stages.exists():
            cnt = orphan_stages.count()
            orphan_stages.delete()
            self.stdout.write(self.style.SUCCESS(f"Deleted {cnt} orphan WorkOrderStage records."))

        # 4. Clear Future Schedules (if requested)
        if options['machine'] and options['clear_future']:
            machine_name = options['machine']
            try:
                # Approximate fuzzy match
                machine = Machine.objects.filter(name__icontains=machine_name).first()
                if not machine:
                     self.stdout.write(self.style.ERROR(f"Machine '{machine_name}' not found."))
                     return
                
                self.stdout.write(f"Clearing future schedule for {machine.name}...")
                
                future_wos = WorkOrder.objects.filter(
                    machine=machine, 
                    start_date__gt=timezone.now()
                )
                
                # We don't delete the WOs, just unschedule them by clearing dates and resetting status to pending
                updated = future_wos.update(start_date=None, end_date=None, status='pending')
                self.stdout.write(self.style.SUCCESS(f"Unscheduled {updated} future work orders for {machine.name}."))
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error: {str(e)}"))

        self.stdout.write(self.style.SUCCESS("Cleanup Complete."))
