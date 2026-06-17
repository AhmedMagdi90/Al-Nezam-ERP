from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from datetime import datetime, timedelta
from django.utils import timezone
from .views import get_user_company, user_has_role
from .analytics import AnalyticsService
import json

@login_required
def analytics_dashboard(request):
    """Enhanced analytics dashboard"""
    company = get_user_company(request.user)
    if not company:
        return redirect('landing_page')
    
    # Check permissions
    if not user_has_role(request.user, ['admin', 'owner', 'supervisor', 'planner']):
        return redirect('dashboard')
    
    # Get date range from request (default to 30 days)
    days = int(request.GET.get('days', 30))
    
    # Get analytics data
    analytics_data = AnalyticsService.production_dashboard(company, days)
    
    context = {
        'analytics_data': analytics_data,
        'days': days,
        'company': company,
    }
    
    return render(request, 'manufacturing/analytics_dashboard.html', context)

@login_required
@require_GET
def analytics_api(request):
    """API endpoint for analytics data"""
    company = get_user_company(request.user)
    if not company:
        return JsonResponse({'error': 'No company assigned'}, status=400)
    
    # Check permissions
    if not user_has_role(request.user, ['admin', 'owner', 'supervisor', 'planner']):
        return JsonResponse({'error': 'Permission denied'}, status=403)
    
    # Get parameters
    metric_type = request.GET.get('type', 'dashboard')
    days = int(request.GET.get('days', 30))
    
    try:
        if metric_type == 'efficiency':
            data = AnalyticsService.production_efficiency(company, days)
        elif metric_type == 'bottlenecks':
            data = AnalyticsService.bottleneck_analysis(company, days)
        elif metric_type == 'quality':
            data = AnalyticsService.quality_trends(company, days)
        elif metric_type == 'costs':
            data = AnalyticsService.cost_analysis(company, days)
        elif metric_type == 'dashboard':
            data = AnalyticsService.production_dashboard(company, days)
        else:
            return JsonResponse({'error': 'Invalid metric type'}, status=400)
        
        return JsonResponse({'success': True, 'data': data})
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@login_required
@require_GET
def real_time_metrics(request):
    """Get real-time production metrics"""
    company = get_user_company(request.user)
    if not company:
        return JsonResponse({'error': 'No company assigned'}, status=400)
    
    # Get current active work orders
    from .models import WorkOrder, Machine, ProductionLog
    
    active_orders = WorkOrder.objects.filter(
        company=company,
        status='in_progress'
    ).select_related('machine', 'current_stage', 'assigned_to')
    
    # Get machine status
    machines = Machine.objects.filter(company=company)
    machine_status = []
    for machine in machines:
        active_count = active_orders.filter(machine=machine).count()
        machine_status.append({
            'id': machine.id,
            'name': machine.name,
            'display_name': machine.display_label,
            'display_name': machine.display_label,
            'code': machine.code,
            'status': machine.status,
            'active_orders': active_count
        })
    
    # Get recent production activity (last hour)
    one_hour_ago = timezone.now() - timedelta(hours=1)
    recent_logs = ProductionLog.objects.filter(
        work_order__company=company,
        created_at__gte=one_hour_ago
    ).select_related('work_order', 'worker').order_by('-created_at')[:10]
    
    recent_activity = []
    for log in recent_logs:
        recent_activity.append({
            'id': log.id,
            'work_order_id': log.work_order.id,
            'product_name': log.work_order.product_name,
            'quantity': log.quantity,
            'worker': log.worker.username,
            'status': log.status,
            'timestamp': log.created_at.isoformat()
        })
    
    return JsonResponse({
        'active_orders_count': active_orders.count(),
        'machine_status': machine_status,
        'recent_activity': recent_activity,
        'timestamp': timezone.now().isoformat()
    })

@login_required
def production_timeline(request):
    """Production timeline view"""
    company = get_user_company(request.user)
    if not company:
        return redirect('landing_page')
    
    # Get date range
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    
    if not start_date:
        start_date = (timezone.now() - timedelta(days=7)).date()
    else:
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    
    if not end_date:
        end_date = timezone.now().date()
    else:
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
    
    # Get work orders in date range
    from .models import WorkOrder
    
    work_orders = WorkOrder.objects.filter(
        company=company,
        start_date__date__gte=start_date,
        start_date__date__lte=end_date
    ).select_related('machine', 'assigned_to', 'bom__product')
    
    timeline_data = []
    for wo in work_orders:
        timeline_data.append({
            'id': wo.id,
            'title': f"WO#{wo.id} - {wo.product_name}",
            'start': wo.start_date.isoformat(),
            'end': wo.end_date.isoformat() if wo.end_date else None,
            'status': wo.status,
            'progress': float(wo.progress),
            'machine': wo.machine.display_label if wo.machine else None,
            'assigned_to': wo.assigned_to.username if wo.assigned_to else None,
            'priority': wo.priority,
            'color': get_status_color(wo.status)
        })
    
    context = {
        'timeline_data': json.dumps(timeline_data),
        'start_date': start_date,
        'end_date': end_date,
        'company': company,
    }
    
    return render(request, 'manufacturing/production_timeline.html', context)

def get_status_color(status):
    """Get color for work order status"""
    colors = {
        'pending': '#6B7280',
        'in_progress': '#3B82F6',
        'completed': '#10B981',
        'hold': '#F59E0B',
    }
    return colors.get(status, '#6B7280')
