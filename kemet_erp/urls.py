from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import path , include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from accounts.views import home_redirect
from manufacturing.views import LandingPageView
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('', LandingPageView.as_view(), name='home'),
    path('admin/', admin.site.urls),
    
    # API Documentation
    path('api/schema/', staff_member_required(SpectacularAPIView.as_view()), name='schema'),
    path('api/docs/', staff_member_required(SpectacularSwaggerView.as_view(url_name='schema')), name='swagger-ui'),
    
    # API URLs (Moved to manufacturing app for /manufacturing/api/v1/ prefix)
    # path('api/', include('manufacturing.api_urls')),
    
    # App URLs
    path('manufacturing/', include('manufacturing.urls')),
    path('accounts/', include('accounts.urls')),
    path('i18n/', include('django.conf.urls.i18n')),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

