# 🚀 AWS Deployment Commands

## Quick Deployment (PowerShell)

Copy and paste this command into PowerShell:

```powershell
& $SSH -i $KEY $SERVER 'sudo cp ~/timeline_header_designs.html /opt/smart-erp/templates/manufacturing/partials/timeline_header_bar.html; sudo cp ~/styles/timeline-header.css /opt/smart-erp/staticfiles/css/manufacturing/timeline-header.css; sudo cp ~/styles/timeline-header.css /opt/smart-erp/static/css/manufacturing/timeline-header.css 2>/dev/null || true; sudo cp ~/js/timeline-header.js /opt/smart-erp/staticfiles/js/manufacturing/timeline-header.js; sudo cp ~/js/api-client.js /opt/smart-erp/staticfiles/js/manufacturing/api-client.js; sudo cp ~/js/timeline-header.js /opt/smart-erp/static/js/manufacturing/timeline-header.js 2>/dev/null || true; sudo cp ~/js/api-client.js /opt/smart-erp/static/js/manufacturing/api-client.js 2>/dev/null || true; sudo cp ~/timeline_api.py /opt/smart-erp/manufacturing/timeline_api.py 2>/dev/null || true; sudo chown ubuntu:ubuntu /opt/smart-erp/templates/manufacturing/partials/*.html /opt/smart-erp/staticfiles/css/manufacturing/*.css /opt/smart-erp/staticfiles/js/manufacturing/*.js /opt/smart-erp/manufacturing/*.py 2>/dev/null || true; cd /opt/smart-erp && . .venv/bin/activate && python manage.py collectstatic --noinput 2>/dev/null || true; sudo systemctl restart smart-erp && sudo systemctl status smart-erp --no-pager -l'
```

## Individual Commands

### 1. Copy HTML Templates
```powershell
& $SSH -i $KEY $SERVER 'sudo cp ~/timeline_header_designs.html /opt/smart-erp/templates/manufacturing/partials/timeline_header_bar.html'
```

### 2. Copy CSS Files
```powershell
& $SSH -i $KEY $SERVER 'sudo cp ~/styles/timeline-header.css /opt/smart-erp/staticfiles/css/manufacturing/timeline-header.css'
& $SSH -i $KEY $SERVER 'sudo cp ~/styles/timeline-header.css /opt/smart-erp/static/css/manufacturing/timeline-header.css 2>/dev/null || true'
```

### 3. Copy JavaScript Files
```powershell
& $SSH -i $KEY $SERVER 'sudo cp ~/js/timeline-header.js /opt/smart-erp/staticfiles/js/manufacturing/timeline-header.js'
& $SSH -i $KEY $SERVER 'sudo cp ~/js/api-client.js /opt/smart-erp/staticfiles/js/manufacturing/api-client.js'
& $SSH -i $KEY $SERVER 'sudo cp ~/js/timeline-header.js /opt/smart-erp/static/js/manufacturing/timeline-header.js 2>/dev/null || true'
& $SSH -i $KEY $SERVER 'sudo cp ~/js/api-client.js /opt/smart-erp/static/js/manufacturing/api-client.js 2>/dev/null || true'
```

### 4. Copy Python API Server
```powershell
& $SSH -i $KEY $SERVER 'sudo cp ~/timeline_api.py /opt/smart-erp/manufacturing/timeline_api.py'
```

### 5. Set Permissions
```powershell
& $SSH -i $KEY $SERVER 'sudo chown ubuntu:ubuntu /opt/smart-erp/templates/manufacturing/partials/*.html /opt/smart-erp/staticfiles/css/manufacturing/*.css /opt/smart-erp/staticfiles/js/manufacturing/*.js /opt/smart-erp/manufacturing/*.py'
```

### 6. Collect Static Files
```powershell
& $SSH -i $KEY $SERVER 'cd /opt/smart-erp && . .venv/bin/activate && python manage.py collectstatic --noinput'
```

### 7. Restart Service
```powershell
& $SSH -i $KEY $SERVER 'sudo systemctl restart smart-erp && sudo systemctl status smart-erp --no-pager -l'
```

## 📁 File Structure After Deployment

```
/opt/smart-erp/
├── templates/manufacturing/partials/
│   └── timeline_header_bar.html
├── staticfiles/css/manufacturing/
│   └── timeline-header.css
├── staticfiles/js/manufacturing/
│   ├── timeline-header.js
│   └── api-client.js
├── static/css/manufacturing/
│   └── timeline-header.css
├── static/js/manufacturing/
│   ├── timeline-header.js
│   └── api-client.js
└── manufacturing/
    └── timeline_api.py
```

## 🔧 Variables to Replace

Before running, replace these variables:
- `$KEY` - Your AWS key file path (e.g., `C:\Users\AFRO\my-key.pem`)
- `$SERVER` - Your server address (e.g., `ubuntu@ec2-xx-xx-xx-xx.compute.amazonaws.com`)

## 🎯 Integration Points

### Django Template Integration
Add this to your manufacturing template:
```html
{% include 'manufacturing/partials/timeline_header_bar.html' %}
```

### Static Files Loading
```html
<link rel="stylesheet" href="{% static 'css/manufacturing/timeline-header.css' %}">
<script src="{% static 'js/manufacturing/api-client.js' %}"></script>
<script src="{% static 'js/manufacturing/timeline-header.js' %}"></script>
```

### Python API Integration
Add to your Django views:
```python
from .timeline_api import TimelineAPI
```

## 🚨 Important Notes

1. **Backup First**: Always backup your existing files before deployment
2. **Test Locally**: Ensure files work locally before deploying
3. **Check Paths**: Verify all paths match your Django project structure
4. **Permissions**: Make sure Django has permission to access static files
5. **Service Restart**: Always restart the service after deployment

## 🔍 Troubleshooting

### Files Not Found
```powershell
# Check if files exist on server
& $SSH -i $KEY $SERVER 'ls -la ~/timeline_header_designs.html'
& $SSH -i $KEY $SERVER 'ls -la ~/styles/'
& $SSH -i $KEY $SERVER 'ls -la ~/js/'
```

### Permission Issues
```powershell
# Check file permissions
& $SSH -i $KEY $SERVER 'ls -la /opt/smart-erp/staticfiles/js/manufacturing/'
```

### Service Status
```powershell
# Check Django service
& $SSH -i $KEY $SERVER 'sudo systemctl status smart-erp'
```

## 🎉 Success Indicators

You should see:
- ✅ Files copied successfully
- ✅ Static files collected
- ✅ Service restarted
- ✅ Service status: "active (running)"

After deployment, visit your Django application to see the new timeline header!
