from django.core.management.base import BaseCommand
from django.test import RequestFactory
from django.contrib.auth.models import User
from manufacturing.models import WorkOrder, QualityCheck, Company
from manufacturing.views import quality_check
from accounts.models import Profile, Role

class Command(BaseCommand):
    help = 'Verify QA View'

    def handle(self, *args, **options):
        # Setup
        user, _ = User.objects.get_or_create(username='qa_officer')
        company, _ = Company.objects.get_or_create(name='Test Corp QA')
        role, _ = Role.objects.get_or_create(name='Quality Officer')

        if not hasattr(user, 'profile'):
            Profile.objects.create(user=user, company=company, role=role)
        else:
            user.profile.company = company
            user.profile.role = role
            user.profile.save()
        
        user.refresh_from_db() # Ensure profile is attached

        # Create Completed WO
        wo = WorkOrder.objects.create(
            product_name='Widget Q', 
            quantity=100, 
            status='completed', 
            company=company
        )
        
        self.stdout.write(f"Created Completed WO #{wo.id}")

        # Mock POST Request
        factory = RequestFactory()
        payload = {
            'work_order': wo.id,
            'good_quantity': 90,
            'repair_quantity': 5,
            'faulty_quantity': 5,
            'notes': 'Verified.'
        }
        
        request = factory.post('/quality/', data=payload)
        request.user = user
        
        # Add messages middleware mock
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.contrib.messages.middleware import MessageMiddleware
        middleware = SessionMiddleware(lambda x: None)
        middleware.process_request(request)
        request.session.save()
        middleware = MessageMiddleware(lambda x: None)
        middleware.process_request(request)

        # Debug User State
        try:
            self.stdout.write(f"User: {user.username}")
            self.stdout.write(f"Profile: {user.profile}")
            self.stdout.write(f"Company: {user.profile.company}")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Debug Error: {e}"))

        # Execute View
        try:
            response = quality_check(request)
            self.stdout.write(f"Response Status: {response.status_code}")
            
            # Verify DB
            qc = QualityCheck.objects.filter(work_order=wo).first()
            if qc:
                 self.stdout.write(self.style.SUCCESS(f"[OK] QualityCheck created: Good={qc.good_quantity}"))
            else:
                 self.stdout.write(self.style.ERROR("[FAIL] No QualityCheck record found!"))
                 
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"[FAIL] Exception: {e}"))
