import json
import requests
from datetime import datetime, timedelta
from django.conf import settings
from django.utils import timezone
from decimal import Decimal
from .models import Company, WorkOrder, ProductionLog

class IntegrationService:
    """Base class for all integrations"""
    
    def __init__(self, company):
        self.company = company
        self.config = self.get_integration_config()
    
    def get_integration_config(self):
        """Get integration configuration for the company"""
        # This would typically come from a CompanyIntegration model
        # For now, using a simple approach
        return getattr(self.company, 'integration_config', {})

class AccountingIntegration(IntegrationService):
    """Integration with accounting systems (QuickBooks, Xero, etc.)"""
    
    def sync_work_order_costs(self, work_order):
        """Sync work order costs to accounting system"""
        if not self.config.get('accounting_enabled'):
            return {'success': False, 'error': 'Accounting integration not enabled'}
        
        try:
            # Calculate total cost
            from .services import BOMService
            if work_order.bom:
                cost_result = BOMService.simulate_run(work_order.bom, work_order.quantity)
                total_cost = cost_result.get('estimated_cost', 0)
            else:
                total_cost = 0
            
            # Prepare data for accounting system
            accounting_data = {
                'transaction_type': 'work_order_cost',
                'work_order_id': work_order.id,
                'product_name': work_order.product_name,
                'quantity': work_order.quantity,
                'total_cost': float(total_cost),
                'date': work_order.start_date.isoformat(),
                'reference': f"WO#{work_order.id}"
            }
            
            # Send to accounting system (example implementation)
            response = self.send_to_accounting_system(accounting_data)
            
            return response
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def send_to_accounting_system(self, data):
        """Send data to accounting system API"""
        api_url = self.config.get('accounting_api_url')
        api_key = self.config.get('accounting_api_key')
        
        if not api_url or not api_key:
            return {'success': False, 'error': 'Missing accounting configuration'}
        
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        
        try:
            response = requests.post(api_url, json=data, headers=headers, timeout=30)
            response.raise_for_status()
            return {'success': True, 'data': response.json()}
        except requests.RequestException as e:
            return {'success': False, 'error': str(e)}

class CRMIntegration(IntegrationService):
    """Integration with CRM systems (Salesforce, HubSpot, etc.)"""
    
    def sync_customer_orders(self, work_order):
        """Sync work order information to CRM"""
        if not self.config.get('crm_enabled'):
            return {'success': False, 'error': 'CRM integration not enabled'}
        
        try:
            crm_data = {
                'work_order_id': work_order.id,
                'product_name': work_order.product_name,
                'quantity': work_order.quantity,
                'status': work_order.status,
                'priority': work_order.priority,
                'start_date': work_order.start_date.isoformat(),
                'assigned_to': work_order.assigned_to.username if work_order.assigned_to else None,
                'progress': float(work_order.progress),
                'company_name': self.company.name
            }
            
            response = self.send_to_crm_system(crm_data)
            return response
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def send_to_crm_system(self, data):
        """Send data to CRM system API"""
        api_url = self.config.get('crm_api_url')
        api_key = self.config.get('crm_api_key')
        
        if not api_url or not api_key:
            return {'success': False, 'error': 'Missing CRM configuration'}
        
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        
        try:
            response = requests.post(api_url, json=data, headers=headers, timeout=30)
            response.raise_for_status()
            return {'success': True, 'data': response.json()}
        except requests.RequestException as e:
            return {'success': False, 'error': str(e)}

class TimeTrackingIntegration(IntegrationService):
    """Integration with time tracking systems"""
    
    def sync_production_time(self, production_log):
        """Sync production log time to time tracking system"""
        if not self.config.get('time_tracking_enabled'):
            return {'success': False, 'error': 'Time tracking integration not enabled'}
        
        try:
            # Calculate work hours (simplified)
            work_hours = 8  # Default 8 hours per log entry
            
            time_data = {
                'employee_id': production_log.worker.id,
                'employee_name': production_log.worker.username,
                'work_order_id': production_log.work_order.id,
                'date': production_log.date.isoformat(),
                'hours': work_hours,
                'quantity_produced': production_log.quantity,
                'shift': production_log.shift,
                'notes': production_log.note or '',
                'project_code': f"WO#{production_log.work_order.id}"
            }
            
            response = self.send_to_time_tracking_system(time_data)
            return response
            
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def send_to_time_tracking_system(self, data):
        """Send data to time tracking system API"""
        api_url = self.config.get('time_tracking_api_url')
        api_key = self.config.get('time_tracking_api_key')
        
        if not api_url or not api_key:
            return {'success': False, 'error': 'Missing time tracking configuration'}
        
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
        
        try:
            response = requests.post(api_url, json=data, headers=headers, timeout=30)
            response.raise_for_status()
            return {'success': True, 'data': response.json()}
        except requests.RequestException as e:
            return {'success': False, 'error': str(e)}

class EmailNotificationService:
    """Service for sending email notifications"""
    
    @staticmethod
    def send_work_order_notification(work_order, notification_type):
        """Send work order notifications"""
        from django.core.mail import send_mail
        from django.conf import settings
        
        subject = f"Work Order #{work_order.id} Update"
        
        if notification_type == 'created':
            message = f"Work Order #{work_order.id} for {work_order.product_name} has been created."
        elif notification_type == 'completed':
            message = f"Work Order #{work_order.id} for {work_order.product_name} has been completed."
        elif notification_type == 'delayed':
            message = f"Work Order #{work_order.id} for {work_order.product_name} is delayed."
        else:
            message = f"Work Order #{work_order.id} status updated to {work_order.get_status_display()}."
        
        # Send to assigned user and supervisor
        recipients = []
        if work_order.assigned_to:
            recipients.append(work_order.assigned_to.email)
        
        # Add supervisor emails
        from .views import user_has_role
        supervisors = work_order.company.users.filter(
            profile__role__name__in=['supervisor', 'admin', 'owner']
        )
        recipients.extend([supervisor.email for supervisor in supervisors if supervisor.email])
        
        if recipients:
            try:
                send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    list(set(recipients)),  # Remove duplicates
                    fail_silently=False
                )
                return {'success': True}
            except Exception as e:
                return {'success': False, 'error': str(e)}
        
        return {'success': True, 'message': 'No recipients to notify'}

class WebhookService:
    """Service for handling webhooks"""
    
    @staticmethod
    def trigger_webhook(company, event_type, data):
        """Trigger webhook for external systems"""
        webhooks = getattr(company, 'webhooks', [])
        
        for webhook in webhooks:
            if webhook['event_type'] == event_type:
                try:
                    payload = {
                        'event': event_type,
                        'timestamp': timezone.now().isoformat(),
                        'company': company.name,
                        'data': data
                    }
                    
                    response = requests.post(
                        webhook['url'],
                        json=payload,
                        headers={'Content-Type': 'application/json'},
                        timeout=30
                    )
                    response.raise_for_status()
                    
                except Exception as e:
                    # Log webhook failure but don't stop processing
                    print(f"Webhook failed: {e}")

# Integration manager to coordinate all integrations
class IntegrationManager:
    """Main integration coordinator"""
    
    def __init__(self, company):
        self.company = company
        self.accounting = AccountingIntegration(company)
        self.crm = CRMIntegration(company)
        self.time_tracking = TimeTrackingIntegration(company)
    
    def sync_work_order(self, work_order, event_type='created'):
        """Sync work order across all enabled integrations"""
        results = {}
        
        # Sync to accounting
        if event_type in ['created', 'completed']:
            results['accounting'] = self.accounting.sync_work_order_costs(work_order)
        
        # Sync to CRM
        results['crm'] = self.crm.sync_customer_orders(work_order)
        
        # Send notifications
        results['email'] = EmailNotificationService.send_work_order_notification(
            work_order, event_type
        )
        
        # Trigger webhooks
        WebhookService.trigger_webhook(
            self.company, 
            f'work_order_{event_type}',
            {
                'work_order_id': work_order.id,
                'product_name': work_order.product_name,
                'status': work_order.status
            }
        )
        
        return results
    
    def sync_production_log(self, production_log):
        """Sync production log across integrations"""
        results = {}
        
        # Sync to time tracking
        results['time_tracking'] = self.time_tracking.sync_production_time(production_log)
        
        # Trigger webhook
        WebhookService.trigger_webhook(
            self.company,
            'production_logged',
            {
                'log_id': production_log.id,
                'work_order_id': production_log.work_order.id,
                'quantity': production_log.quantity,
                'worker': production_log.worker.username
            }
        )
        
        return results
