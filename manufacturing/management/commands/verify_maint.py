from django.core.management.base import BaseCommand
from django.test import RequestFactory
from django.contrib.auth.models import User
from manufacturing.models import Machine, Company
from manufacturing.views import maintenance_dashboard
from accounts.models import Profile, Role

class Command(BaseCommand):
    help = 'Verify Maintenance View'

    def handle(self, *args, **options):
        # Setup
        user, _ = User.objects.get_or_create(username='maint_tech')
        company, _ = Company.objects.get_or_create(name='Test Corp Maint')
        role, _ = Role.objects.get_or_create(name='Maintenance')
        
        if not hasattr(user, 'profile'):
            Profile.objects.create(user=user, company=company, role=role)
        else:
            user.profile.company = company
            user.profile.role = role
            user.profile.save()
            
        user.refresh_from_db()

        # Create Machine
        machine, _ = Machine.objects.get_or_create(
            name='Test Lathe', code='L-99', company=company
        )
        
        self.stdout.write(f"Testing Machine update: {machine.name} ({machine.status})")

        # Mock POST Request (Break the machine)
        factory = RequestFactory()
        payload = {
            'machine_id': machine.id,
            'status': 'broken',
            'note': 'Motor overheating'
        }
        
        request = factory.post('/maintenance/', data=payload)
        request.user = user
        
        # Add messages middleware mock
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.contrib.messages.middleware import MessageMiddleware
        middleware = SessionMiddleware(lambda x: None)
        middleware.process_request(request)
        request.session.save()
        middleware = MessageMiddleware(lambda x: None)
        middleware.process_request(request)

        # Execute View
        try:
            response = maintenance_dashboard(request)
            self.stdout.write(f"Response Status: {response.status_code}")
            
            # Verify DB
            machine.refresh_from_db()
            if machine.status == 'broken':
                 self.stdout.write(self.style.SUCCESS(f"[OK] Machine status updated to: {machine.status}"))
            else:
                 self.stdout.write(self.style.ERROR(f"[FAIL] Machine status is {machine.status}"))
                 
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"[FAIL] Exception: {e}"))
