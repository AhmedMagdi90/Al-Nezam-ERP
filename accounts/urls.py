from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='logout'),
    path('bootstrap/', views.organization_bootstrap, name='organization_bootstrap'),
    path('control-center/', views.platform_control_center, name='platform_control_center'),
    path('portal/', views.environment_portal, name='environment_portal'),
    path('access/<slug:tenant_code>/<uidb64>/<token>/', views.environment_access_setup, name='environment_access_setup'),
]
