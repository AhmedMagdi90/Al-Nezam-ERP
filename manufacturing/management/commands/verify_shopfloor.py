from django.core.management.base import BaseCommand
from django.test import RequestFactory
from django.contrib.auth.models import User
from manufacturing.models import WorkOrder, Product, Company
from accounts.models import Profile, Role  # Role is in accounts
from manufacturing.views import ShopFloorUpdateView
import json

class Command(BaseCommand):
    help = 'Verify Shop Floor API (update_work_order)'

    def handle(self, *args, **options):
        # ... (setup code remains same, implied context matching) ...

        # Mock Request
        factory = RequestFactory()
        payload = {'status': 'in_progress'}
        request = factory.post(
            f'/api/work-order/{wo.id}/update/', 
            data=json.dumps(payload), 
            content_type='application/json'
        )
        request.user = user
        
        # Execute View
        try:
            view = ShopFloorUpdateView.as_view()
            response = view(request, wo_id=wo.id)
            self.stdout.write(f"Response: {response.content.decode()}")
            
            wo.refresh_from_db()
            if wo.status == 'in_progress':
                self.stdout.write(self.style.SUCCESS("[OK] basic update logic works (db updated)"))
            else:
                self.stdout.write(self.style.WARNING(f"[WARN] WO status is {wo.status}, expected in_progress."))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"[FAIL] View raised exception: {e}"))
