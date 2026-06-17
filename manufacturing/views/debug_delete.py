from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.conf import settings
from manufacturing.models import WorkOrder
from manufacturing.views.dashboard import require_company

class DeleteAllWorkOrdersView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    DEBUG VIEW: Delete all WorkOrders for testing
    WARNING: This is destructive! Only use in development.
    """
    def test_func(self):
        return settings.DEBUG and self.request.user.is_superuser

    def get(self, request):
        company = require_company(request.user)
        
        # Count before
        count_before = WorkOrder.objects.filter(company=company).count()
        
        # Get sample data
        samples = list(WorkOrder.objects.filter(company=company).values(
            'id', 'product_name', 'status'
        )[:10])
        
        return JsonResponse({
            'success': True,
            'count': count_before,
            'samples': samples,
            'message': f'Found {count_before} WorkOrders. Use POST to delete them.'
        })
    
    def post(self, request):
        company = require_company(request.user)
        
        # Delete all
        deleted_count, details = WorkOrder.objects.filter(company=company).delete()
        
        return JsonResponse({
            'success': True,
            'deleted': deleted_count,
            'details': details,
            'message': f'Deleted {deleted_count} WorkOrders successfully'
        })
