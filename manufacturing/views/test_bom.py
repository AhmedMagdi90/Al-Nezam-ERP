from django.shortcuts import render
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from manufacturing.models import Machine
from .dashboard import require_company

class TestBOMView(LoginRequiredMixin, View):
    def get(self, request):
        company = require_company(request.user)
        
        # Try to get machines with error handling
        try:
            machines = list(Machine.objects.filter(company=company))
            machine_count = len(machines)
            error_msg = None
        except Exception as e:
            machines = []
            machine_count = 0
            error_msg = str(e)
        
        context = {
            'machine_count': machine_count,
            'error_msg': error_msg,
            'test_message': 'Test view loaded successfully'
        }
        
        # Use a minimal template
        return render(request, 'manufacturing/test_bom.html', context)
