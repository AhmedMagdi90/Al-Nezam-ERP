from django.contrib import admin
from .models import Role, Profile

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'company', 'role', 'phone')
    list_filter = ('role', 'company')
    search_fields = ('user__username', 'user__email')

admin.site.register(Role)
