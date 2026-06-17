import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from .views import get_user_company

class ProductionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]
        if isinstance(self.user, AnonymousUser):
            await self.close()
            return
        
        self.company = await self.get_user_company()
        if not self.company:
            await self.close()
            return
        
        # Join company-specific group
        self.group_name = f"company_{self.company.id}"
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        
        await self.accept()
    
    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )
    
    async def receive(self, text_data):
        data = json.loads(text_data)
        message_type = data.get('type')
        
        if message_type == 'ping':
            await self.send(text_data=json.dumps({'type': 'pong'}))
        
        # Handle different message types
        elif message_type == 'work_order_update':
            await self.handle_work_order_update(data)
        elif message_type == 'machine_status_update':
            await self.handle_machine_status_update(data)
    
    async def handle_work_order_update(self, data):
        """Broadcast work order updates to relevant users"""
        await self.channel_layer.group_send(
            self.group_name,
            {
                'type': 'work_order_update',
                'data': data
            }
        )
    
    async def handle_machine_status_update(self, data):
        """Broadcast machine status changes"""
        await self.channel_layer.group_send(
            self.group_name,
            {
                'type': 'machine_status_update',
                'data': data
            }
        )
    
    async def work_order_update(self, event):
        """Send work order update to client"""
        await self.send(text_data=json.dumps({
            'type': 'work_order_update',
            'data': event['data']
        }))
    
    async def machine_status_update(self, event):
        """Send machine status update to client"""
        await self.send(text_data=json.dumps({
            'type': 'machine_status_update',
            'data': event['data']
        }))
    
    async def quality_alert(self, event):
        """Send quality alert to client"""
        await self.send(text_data=json.dumps({
            'type': 'quality_alert',
            'data': event['data']
        }))
    
    async def production_notification(self, event):
        """Send production notification to client"""
        await self.send(text_data=json.dumps({
            'type': 'production_notification',
            'data': event['data']
        }))
    
    @database_sync_to_async
    def get_user_company(self):
        return get_user_company(self.user)

class SupervisorConsumer(AsyncWebsocketConsumer):
    """Specialized consumer for supervisor real-time monitoring"""
    
    async def connect(self):
        self.user = self.scope["user"]
        if isinstance(self.user, AnonymousUser):
            await self.close()
            return
        
        # Check if user is supervisor
        from .views import user_has_role
        if not user_has_role(self.user, ['supervisor', 'admin', 'owner']):
            await self.close()
            return
        
        self.company = await self.get_user_company()
        if not self.company:
            await self.close()
            return
        
        # Join supervisor-specific group
        self.group_name = f"supervisors_{self.company.id}"
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        
        await self.accept()
    
    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )
    
    async def production_update(self, event):
        """Real-time production updates for supervisors"""
        await self.send(text_data=json.dumps({
            'type': 'production_update',
            'data': event['data']
        }))
    
    async def alert_notification(self, event):
        """Critical alerts for supervisors"""
        await self.send(text_data=json.dumps({
            'type': 'alert',
            'data': event['data']
        }))
    
    @database_sync_to_async
    def get_user_company(self):
        return get_user_company(self.user)
