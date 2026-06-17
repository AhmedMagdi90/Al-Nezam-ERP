
from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.conf import settings
from manufacturing.models import WorkOrder, Machine
from django.utils import timezone

class DebugWOView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return settings.DEBUG and self.request.user.is_superuser

    def get(self, request):
        # 1. Last 10 WOs
        wos = WorkOrder.objects.all().order_by('-id')[:10]
        wos_data = []
        for wo in wos:
            d = {
                'id': wo.id,
                'product': wo.product_name,
                'status': wo.status,
                'machine_id': wo.machine.id if wo.machine else None,
                'machine_name': wo.machine.name if wo.machine else None,
                'start_date': wo.start_date.isoformat() if wo.start_date else None,
                'end_date': wo.end_date.isoformat() if wo.end_date else None,
                'start_tz': str(wo.start_date.tzinfo) if wo.start_date else None,
            }
            wos_data.append(d)
            
        # 2. Machine Occupancy (CNC)
        cnc_machines = Machine.objects.filter(name__icontains="CNC")
        cnc_data = []
        for m in cnc_machines:
            occupants = WorkOrder.objects.filter(machine=m, status__in=['pending', 'in_progress']).order_by('start_date')
            occ_data = []
            for o in occupants:
                occ_data.append({
                    'id': o.id,
                    'start': o.start_date.isoformat() if o.start_date else None,
                    'end': o.end_date.isoformat() if o.end_date else None
                })
            cnc_data.append({
                'id': m.id,
                'name': m.name,
                'occupants': occ_data
            })
            
        return JsonResponse({
            'now': timezone.now().isoformat(),
            'recent_work_orders': wos_data,
            'cnc_status': cnc_data
        })
