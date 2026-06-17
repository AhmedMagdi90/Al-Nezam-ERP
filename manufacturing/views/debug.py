from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.conf import settings
from manufacturing.models import SystemSettings

class ShiftConfigDebugView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Debug view to check shift configuration"""
    def test_func(self):
        return settings.DEBUG and self.request.user.is_superuser

    def get(self, request):
        try:
            system_settings = SystemSettings.objects.first()
            if system_settings:
                return JsonResponse({
                    'success': True,
                    'shift_configuration': system_settings.shift_configuration,
                    'message': 'Shift configuration loaded from database'
                }, json_dumps_params={'indent': 2})
            else:
                return JsonResponse({
                    'success': False,
                    'message': 'No SystemSettings found in database'
                })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': str(e)
            })
