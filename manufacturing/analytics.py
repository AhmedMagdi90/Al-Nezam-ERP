from datetime import datetime, timedelta
from decimal import Decimal
from django.db.models import Sum, Avg, Count, Q, F, DurationField, ExpressionWrapper
from django.utils import timezone
from .models import WorkOrder, Machine, ProductionLog, QualityCheck, BillOfMaterial

class AnalyticsService:
    
    @staticmethod
    def production_efficiency(company, date_range=30):
        """
        Calculate Overall Equipment Effectiveness (OEE)
        OEE = Availability × Performance × Quality
        """
        end_date = timezone.now()
        start_date = end_date - timedelta(days=date_range)
        
        # Get work orders in the period
        work_orders = WorkOrder.objects.filter(
            company=company,
            start_date__gte=start_date,
            start_date__lte=end_date
        )
        
        total_work_orders = work_orders.count()
        if total_work_orders == 0:
            return {
                'oee_percentage': 0,
                'availability': 0,
                'performance': 0,
                'quality': 0,
                'total_orders': 0
            }
        
        # Availability: Actual production time / Planned production time
        completed_orders = work_orders.filter(status='completed').count()
        availability = (completed_orders / total_work_orders) * 100
        
        # Performance: Actual output / Planned output
        total_planned = work_orders.aggregate(Sum('quantity'))['quantity__sum'] or 0
        total_actual = ProductionLog.objects.filter(
            work_order__in=work_orders,
            status='approved'
        ).aggregate(Sum('quantity'))['quantity__sum'] or 0
        
        performance = (total_actual / total_planned * 100) if total_planned > 0 else 0
        
        # Quality: Good units / Total units produced
        quality_checks = QualityCheck.objects.filter(
            work_order__in=work_orders
        )
        
        total_good = quality_checks.aggregate(Sum('good_quantity'))['good_quantity__sum'] or 0
        total_produced = total_good + (
            quality_checks.aggregate(Sum('faulty_quantity'))['faulty_quantity__sum'] or 0
        ) + (
            quality_checks.aggregate(Sum('repair_quantity'))['repair_quantity__sum'] or 0
        )
        
        quality = (total_good / total_produced * 100) if total_produced > 0 else 0
        
        # Calculate OEE
        oee = (availability * performance * quality) / 10000  # Divide by 100^2
        
        return {
            'oee_percentage': round(oee, 2),
            'availability': round(availability, 2),
            'performance': round(performance, 2),
            'quality': round(quality, 2),
            'total_orders': total_work_orders,
            'completed_orders': completed_orders,
            'total_planned_qty': int(total_planned),
            'total_actual_qty': int(total_actual)
        }
    
    @staticmethod
    def bottleneck_analysis(company, date_range=30):
        """Identify production bottlenecks"""
        end_date = timezone.now()
        start_date = end_date - timedelta(days=date_range)
        
        # Analyze by machine
        machines = Machine.objects.filter(company=company)
        machine_data = []
        
        for machine in machines:
            work_orders = WorkOrder.objects.filter(
                company=company,
                machine=machine,
                start_date__gte=start_date,
                start_date__lte=end_date
            )
            
            if work_orders.exists():
                completed_with_duration = work_orders.filter(
                    status='completed',
                    start_date__isnull=False,
                    end_date__isnull=False
                ).annotate(
                    cycle_time=ExpressionWrapper(
                        F('end_date') - F('start_date'),
                        output_field=DurationField()
                    )
                )
                avg_duration_delta = completed_with_duration.aggregate(
                    avg_cycle_time=Avg('cycle_time')
                ).get('avg_cycle_time')
                avg_duration_minutes = round(avg_duration_delta.total_seconds() / 60, 1) if avg_duration_delta else 0.0
                avg_duration_hours = round(avg_duration_minutes / 60, 2) if avg_duration_minutes else 0.0
                
                # Count delays using due date (true late logic):
                # 1) completed orders finished after due_date
                # 2) still-open orders already past due_date
                now = timezone.now()
                delayed_orders = work_orders.filter(
                    due_date__isnull=False
                ).filter(
                    Q(status='completed', end_date__isnull=False, end_date__gt=F('due_date')) |
                    Q(status__in=['pending', 'in_progress', 'hold'], due_date__lt=now)
                ).count()
                total_orders = work_orders.count()
                
                machine_data.append({
                    'machine_name': machine.display_label,
                    'machine_code': machine.code,
                    'total_orders': total_orders,
                    'delayed_orders': delayed_orders,
                    'delay_rate': (delayed_orders / total_orders * 100) if total_orders > 0 else 0,
                    'avg_duration_minutes': avg_duration_minutes,
                    'avg_duration_hours': avg_duration_hours,
                })
        
        # Sort by delay rate to find bottlenecks
        machine_data.sort(key=lambda x: x['delay_rate'], reverse=True)
        
        return {
            'bottlenecks': machine_data[:5],  # Top 5 bottlenecks
            'analysis_period': f"{date_range} days"
        }
    
    @staticmethod
    def quality_trends(company, date_range=30):
        """Analyze quality trends over time"""
        end_date = timezone.now()
        start_date = end_date - timedelta(days=date_range)
        
        # Group quality checks by day
        daily_quality = []
        current_date = start_date
        
        while current_date <= end_date:
            day_checks = QualityCheck.objects.filter(
                work_order__company=company,
                created_at__date=current_date.date()
            )
            
            if day_checks.exists():
                total_good = day_checks.aggregate(Sum('good_quantity'))['good_quantity__sum'] or 0
                total_faulty = day_checks.aggregate(Sum('faulty_quantity'))['faulty_quantity__sum'] or 0
                total_repair = day_checks.aggregate(Sum('repair_quantity'))['repair_quantity__sum'] or 0
                total_produced = total_good + total_faulty + total_repair
                
                quality_rate = (total_good / total_produced * 100) if total_produced > 0 else 0
                
                daily_quality.append({
                    'date': current_date.date().isoformat(),
                    'quality_rate': round(quality_rate, 2),
                    'total_checks': day_checks.count(),
                    'good_quantity': int(total_good),
                    'faulty_quantity': int(total_faulty),
                    'repair_quantity': int(total_repair)
                })
            
            current_date += timedelta(days=1)
        
        # Identify common defect patterns
        defect_patterns = QualityCheck.objects.filter(
            work_order__company=company,
            created_at__gte=start_date,
            faulty_quantity__gt=0
        ).values('work_order__product_name').annotate(
            total_faulty=Sum('faulty_quantity'),
            occurrence_count=Count('id')
        ).order_by('-total_faulty')[:5]
        
        return {
            'daily_trends': daily_quality,
            'defect_patterns': list(defect_patterns),
            'period': f"{date_range} days"
        }
    
    @staticmethod
    def cost_analysis(company, date_range=30):
        """Analyze production costs and variances"""
        end_date = timezone.now()
        start_date = end_date - timedelta(days=date_range)
        
        work_orders = WorkOrder.objects.filter(
            company=company,
            start_date__gte=start_date,
            start_date__lte=end_date,
            bom__isnull=False
        )
        
        cost_data = []
        
        for wo in work_orders:
            if wo.bom:
                # Get estimated cost from BOM
                from .services import BOMService
                try:
                    estimated_cost = BOMService.simulate_run(wo.bom, wo.quantity)
                    estimated_total = estimated_cost.get('estimated_cost', 0)
                except:
                    estimated_total = 0
                
                # Calculate actual cost (simplified - would include labor, overhead, etc.)
                actual_logs = ProductionLog.objects.filter(
                    work_order=wo,
                    status='approved'
                )
                
                # This is a simplified actual cost calculation
                actual_total = estimated_total * 0.95  # Assume 5% variance for demo
                
                variance = actual_total - estimated_total
                variance_percent = (variance / estimated_total * 100) if estimated_total > 0 else 0
                
                cost_data.append({
                    'work_order_id': wo.id,
                    'product_name': wo.product_name,
                    'quantity': wo.quantity,
                    'estimated_cost': float(estimated_total),
                    'actual_cost': actual_total,
                    'variance': variance,
                    'variance_percent': round(variance_percent, 2)
                })
        
        # Summary statistics
        if cost_data:
            avg_variance = sum(item['variance_percent'] for item in cost_data) / len(cost_data)
            total_estimated = sum(item['estimated_cost'] for item in cost_data)
            total_actual = sum(item['actual_cost'] for item in cost_data)
        else:
            avg_variance = 0
            total_estimated = 0
            total_actual = 0
        
        return {
            'cost_details': cost_data,
            'summary': {
                'total_orders': len(cost_data),
                'total_estimated_cost': total_estimated,
                'total_actual_cost': total_actual,
                'total_variance': total_actual - total_estimated,
                'average_variance_percent': round(avg_variance, 2)
            }
        }
    
    @staticmethod
    def production_dashboard(company, date_range=7):
        """Get comprehensive dashboard data"""
        return {
            'efficiency': AnalyticsService.production_efficiency(company, date_range),
            'bottlenecks': AnalyticsService.bottleneck_analysis(company, date_range),
            'quality': AnalyticsService.quality_trends(company, date_range),
            'costs': AnalyticsService.cost_analysis(company, date_range)
        }
