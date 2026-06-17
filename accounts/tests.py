from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from accounts.models import Profile, Role
from manufacturing.models import Company

class RedirectionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.company = Company.objects.create(name="Redirect Test Co")
        
        # Roles
        self.r_planner, _ = Role.objects.get_or_create(name='planner')
        self.r_supervisor, _ = Role.objects.get_or_create(name='supervisor')
        self.r_worker, _ = Role.objects.get_or_create(name='worker')
        self.r_quality, _ = Role.objects.get_or_create(name='quality')
        self.r_maint, _ = Role.objects.get_or_create(name='maintenance')

        # Users & Profiles
        # Planner
        self.planner = User.objects.create_user(username='planner_user', password='password')
        p1, _ = Profile.objects.get_or_create(user=self.planner)
        p1.role = self.r_planner
        p1.company = self.company
        p1.save()
        self.planner.refresh_from_db()
        
        # Supervisor
        self.supervisor = User.objects.create_user(username='supervisor_user', password='password')
        p2, _ = Profile.objects.get_or_create(user=self.supervisor)
        p2.role = self.r_supervisor
        p2.company = self.company
        p2.save()
        self.supervisor.refresh_from_db()

        # Worker
        self.worker = User.objects.create_user(username='worker_user', password='password')
        p3, _ = Profile.objects.get_or_create(user=self.worker)
        p3.role = self.r_worker
        p3.company = self.company
        p3.save()
        self.worker.refresh_from_db()
        
        # Maintenance
        self.maintenance = User.objects.create_user(username='maint_user', password='password')
        p4, _ = Profile.objects.get_or_create(user=self.maintenance)
        p4.role = self.r_maint
        p4.company = self.company
        p4.save()
        self.maintenance.refresh_from_db()
        
        # Quality
        self.quality = User.objects.create_user(username='qual_user', password='password')
        p5, _ = Profile.objects.get_or_create(user=self.quality)
        p5.role = self.r_quality
        p5.company = self.company
        p5.save()
        self.quality.refresh_from_db()

    def test_planner_redirect(self):
        self.client.force_login(self.planner)
        response = self.client.get('/', follow=True) 
        self.assertRedirects(response, reverse('dashboard'))

    def test_supervisor_redirect(self):
        self.client.force_login(self.supervisor)
        response = self.client.get('/', follow=True)
        self.assertRedirects(response, reverse('supervisor_dashboard'))

    def test_worker_redirect(self):
        self.client.force_login(self.worker)
        response = self.client.get('/', follow=True)
        self.assertRedirects(response, reverse('shop_floor'))

    def test_maintenance_redirect(self):
        self.client.force_login(self.maintenance)
        response = self.client.get('/', follow=True)
        self.assertRedirects(response, reverse('maintenance_dashboard'))

    def test_quality_redirect(self):
        self.client.force_login(self.quality)
        response = self.client.get('/', follow=True)
        self.assertRedirects(response, reverse('quality_check'))

    def test_unauthenticated_redirect(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'landing.html')
