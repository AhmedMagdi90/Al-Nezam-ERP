from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.contrib.auth.decorators import login_required
from manufacturing.models import Product
from .dashboard import require_company


@login_required
@require_GET
def search_materials(request):
    """
    API endpoint to search for materials (raw materials/components) by name.
    Returns matching products that can be used as BOM components.
    """
    query = request.GET.get('q', '').strip()
    
    # Require at least 1 character to search
    if len(query) < 1:
        return JsonResponse([], safe=False)
    
    company = require_company(request.user)
    if not company:
        return JsonResponse({'error': 'No company found'}, status=400)
    
    # Search for products (materials) matching the query
    # Filter by raw materials or all products depending on your needs
    materials = Product.objects.filter(
        company=company,
        name__icontains=query
    ).values(
        'id',
        'name',
        'unit'
    )[:10]  # Limit to 10 results
    
    # Convert QuerySet to list and format response
    results = [{
        'id': m['id'],
        'name': m['name'],
        'unit_cost': 0.0,  # Default to 0 since Product doesn't have unit_cost
        'unit': m['unit'] or 'pcs'
    } for m in materials]
    
    return JsonResponse(results, safe=False)
