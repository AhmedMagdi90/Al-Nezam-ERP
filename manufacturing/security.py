from django.contrib.auth.models import User
from django.db import models
from rest_framework.permissions import BasePermission

class AuditLog(models.Model):
    """Track all important actions for security and compliance"""
    ACTION_CHOICES = [
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('view', 'View'),
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('export', 'Export Data'),
        ('import', 'Import Data'),
        ('approve', 'Approve'),
        ('reject', 'Reject'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    model_name = models.CharField(max_length=50)  # e.g., 'WorkOrder', 'Machine'
    object_id = models.PositiveIntegerField(null=True, blank=True)
    object_repr = models.CharField(max_length=200, blank=True)  # String representation
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    company = models.ForeignKey('manufacturing.Company', on_delete=models.CASCADE, null=True)
    details = models.JSONField(default=dict, blank=True)  # Additional context
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['user', 'timestamp']),
            models.Index(fields=['company', 'timestamp']),
            models.Index(fields=['model_name', 'timestamp']),
        ]
    
    def __str__(self):
        return f"{self.user} - {self.action} - {self.model_name} at {self.timestamp}"

class SecurityEvent(models.Model):
    """Track security-related events"""
    EVENT_TYPES = [
        ('failed_login', 'Failed Login'),
        ('permission_denied', 'Permission Denied'),
        ('suspicious_activity', 'Suspicious Activity'),
        ('data_breach_attempt', 'Data Breach Attempt'),
        ('unauthorized_access', 'Unauthorized Access'),
    ]
    
    event_type = models.CharField(max_length=30, choices=EVENT_TYPES)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    ip_address = models.GenericIPAddressField()
    timestamp = models.DateTimeField(auto_now_add=True)
    details = models.JSONField(default=dict)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, 
                                   related_name='resolved_events')
    resolved_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-timestamp']

class IsCompanyMember(BasePermission):
    """Custom permission to ensure user can only access their company's data"""
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers can access everything
        if request.user.is_superuser:
            return True
        
        # Check if user has a company
        from manufacturing.views.dashboard import require_company
        company = require_company(request.user)
        return company is not None
    
    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers can access everything
        if request.user.is_superuser:
            return True
        
        from manufacturing.views.dashboard import require_company
        company = require_company(request.user)
        if not company:
            return False
        
        # Check various ways objects can be linked to company
        if hasattr(obj, 'company'):
            return obj.company == company
        elif hasattr(obj, 'product') and hasattr(obj.product, 'company'):
            return obj.product.company == company
        elif hasattr(obj, 'work_order') and hasattr(obj.work_order, 'company'):
            return obj.work_order.company == company
        elif hasattr(obj, 'machine') and hasattr(obj.machine, 'company'):
            return obj.machine.company == company
        
        return False

class RoleBasedPermission(BasePermission):
    """Permission based on user roles"""
    
    def __init__(self, allowed_roles=None):
        self.allowed_roles = allowed_roles or []
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers can access everything
        if request.user.is_superuser:
            return True
        
        from manufacturing.views.dashboard import user_has_role
        return user_has_role(request.user, self.allowed_roles)

def log_security_event(event_type, user=None, ip_address=None, details=None):
    """Log security events for monitoring"""
    SecurityEvent.objects.create(
        event_type=event_type,
        user=user,
        ip_address=ip_address,
        details=details or {}
    )

def audit_log_action(user, action, model_name, object_id=None, object_repr='', 
                    ip_address=None, user_agent='', company=None, details=None):
    """Create audit log entry"""
    AuditLog.objects.create(
        user=user,
        action=action,
        model_name=model_name,
        object_id=object_id,
        object_repr=object_repr,
        ip_address=ip_address,
        user_agent=user_agent,
        company=company,
        details=details or {}
    )


def audit_request_action(
    request,
    action,
    target=None,
    *,
    model_name=None,
    object_id=None,
    object_repr='',
    company=None,
    details=None,
):
    """Create an audit entry using request metadata and an optional target object."""
    resolved_company = company
    if resolved_company is None and target is not None:
        if hasattr(target, 'company') and getattr(target, 'company', None) is not None:
            resolved_company = target.company
        elif hasattr(target, 'work_order') and getattr(target, 'work_order', None) is not None:
            resolved_company = getattr(target.work_order, 'company', None)
        elif hasattr(target, 'machine') and getattr(target, 'machine', None) is not None:
            resolved_company = getattr(target.machine, 'company', None)

    if resolved_company is None and getattr(request, 'user', None):
        from manufacturing.views.dashboard import require_company
        resolved_company = require_company(request.user)

    resolved_model_name = model_name or (target._meta.object_name if target is not None else '')
    resolved_object_id = object_id if object_id is not None else getattr(target, 'pk', None)
    resolved_object_repr = object_repr or (str(target) if target is not None else '')

    audit_log_action(
        user=getattr(request, 'user', None),
        action=action,
        model_name=resolved_model_name,
        object_id=resolved_object_id,
        object_repr=resolved_object_repr,
        ip_address=getattr(request, 'client_ip', None) or get_client_ip(request),
        user_agent=getattr(request, 'user_agent', '') or request.META.get('HTTP_USER_AGENT', ''),
        company=resolved_company,
        details=details or {},
    )

class SecurityMiddleware:
    """Middleware to enhance security"""
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        # Get client IP
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip_address = x_forwarded_for.split(',')[0]
        else:
            ip_address = request.META.get('REMOTE_ADDR')
        
        # Store IP and user agent in request for later use
        request.client_ip = ip_address
        request.user_agent = request.META.get('HTTP_USER_AGENT', '')
        
        response = self.get_response(request)
        
        # Log security events for failed attempts
        if response.status_code == 403 and request.user.is_authenticated:
            log_security_event(
                'permission_denied',
                user=request.user,
                ip_address=ip_address,
                details={
                    'path': request.path,
                    'method': request.method,
                    'user_agent': request.user_agent
                }
            )
        
        return response

def get_client_ip(request):
    """Get client IP address from request"""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip
