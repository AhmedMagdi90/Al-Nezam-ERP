from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.http import JsonResponse, HttpResponseForbidden
from django.template.loader import render_to_string
from django.utils.text import slugify
from django.db.models import Q
import json
import base64
from decimal import Decimal, InvalidOperation
import re
from django.core.files.base import ContentFile

from manufacturing.bom_attachments import save_bom_attachment, serialize_bom_attachment
from manufacturing.models import BillOfMaterial, Product, Machine, ProductionStage, BOMComponent, BOMOperation, BOMAcceptanceCriteria, Notification, WorkOrder
from manufacturing.services import flag_bom_change_impact
from manufacturing.utils import normalize_operation_time_minutes
from .dashboard import require_company, user_has_role

_BOM_VERSION_STEP = Decimal("0.1")


def _parse_bom_version_value(version):
    version_text = str(version or "").strip().lower()
    if version_text.startswith("v"):
        version_text = version_text[1:]
    match = re.search(r"\d+(?:\.\d+)?", version_text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _next_bom_version(product):
    versions = list(BillOfMaterial.objects.filter(product=product).values_list("version", flat=True))
    parsed_versions = [value for value in (_parse_bom_version_value(v) for v in versions) if value is not None]
    if not parsed_versions:
        fallback_major = max(len(versions) + 1, 1)
        return f"v{fallback_major}.0"
    next_value = max(parsed_versions) + _BOM_VERSION_STEP
    return f"v{next_value:.1f}"

class BOMBuilderView(LoginRequiredMixin, View):
    def get(self, request, bom_id=None):
        if not user_has_role(request.user, 'ui.bom.manage'):
            return HttpResponseForbidden("Unauthorized")

        print(f"\n{'='*60}")
        print(f"BOMBuilderView.get() called - BOM ID: {bom_id}")
        print(f"{'='*60}")
        
        company = require_company(request.user)
        if not company: 
            print("No company found, redirecting to onboarding")
            return redirect('onboarding_data')
        
        print(f"Company: {company.name}")

        bom = None
        if bom_id:
            print(f"Loading BOM #{bom_id}...")
            # Use filter instead of get_object_or_404 to avoid crashes from broken FKs
            try:
                bom = BillOfMaterial.objects.filter(
                    pk=bom_id, 
                    product__company=company
                ).select_related('product').first()
                
                if not bom:
                    print("BOM not found")
                    return JsonResponse({'error': 'BOM not found'}, status=404)
                print(f"BOM loaded: {bom.product.name}")
            except Exception as e:
                print(f"ERROR loading BOM: {e}")
                return JsonResponse({'error': f'Error loading BOM: {str(e)}'}, status=500)

        bom_data = {}
        if bom:
            print("Serializing BOM data...")
            try:
                # Serialize BOM for Frontend (Alpine.js)
                components = []
                for c in bom.components.all():
                    wastage_percent = float(c.wastage_percent or 0)
                    wastage_qty = float(c.wastage_quantity or 0)
                    use_percent_mode = wastage_percent > 0 or (wastage_percent == 0 and wastage_qty == 0)
                    components.append({
                        "id": c.id,
                        "client_id": f"cmp-{c.id}",
                        "name": c.material_name,
                        "qty": float(c.quantity),
                        "unit": c.unit,
                        "cost": float(c.cost_per_unit),
                        "wastage_percent": wastage_percent,
                        "wastage_qty": wastage_qty,
                        "wastage_basis": "percent" if use_percent_mode else "qty",
                        "wastage": wastage_percent if use_percent_mode else wastage_qty,
                        "wastage_mode": "percent" if use_percent_mode else "qty",
                        "scrap": float(c.scrap_value_per_unit or 0)
                    })
                print(f"  Components: {len(components)}")

                operations = []
                for op in bom.operations.all().order_by('order'):
                    try:
                        machine_id = None
                        machine_type = op.machine_type or "Generic"
                        stage_id = None
                        stage_name = None
                        stage_machine_id = None
                        is_qc = False

                        if op.machine_id:
                            try:
                                machine_id = op.machine.id
                                machine_type = op.machine.category or op.machine.type or machine_type
                            except Machine.DoesNotExist:
                                pass

                        if op.stage_id:
                            try:
                                stage_id = op.stage.id
                                stage_name = op.stage.name
                                stage_machine_id = op.stage.machine_id
                                is_qc = bool(op.stage.is_quality_check)
                                if not machine_type or machine_type == "Generic":
                                    machine_type = (
                                        (op.stage.category or "").strip()
                                        or (op.stage.machine.category if op.stage.machine else "")
                                        or (op.stage.machine.type if op.stage.machine else "")
                                        or "General"
                                    )
                            except ProductionStage.DoesNotExist:
                                pass

                        material_component_ids = list(
                            op.material_links.values_list("component_id", flat=True)
                        )
                        operations.append({
                            "id": op.id,
                            "machine_id": machine_id,
                            "default_machine_id": stage_machine_id,
                            "stage_id": stage_id,
                            "stage_name": stage_name,
                            "name": stage_name or op.description or f"Op {op.order}",
                            "type": machine_type,
                            "setup_time": float(op.setup_time),
                            "setup_time_unit": "min",
                            "run_time": float(op.run_time),
                            "run_time_unit": "min",
                            "unit": "pcs",
                            "quality_check": is_qc,
                            "material_client_ids": [f"cmp-{cid}" for cid in material_component_ids],
                        })
                    except Exception as e:
                        print(f"WARNING: Skipping broken operation {op.id}: {e}")
                        continue

                print(f"  Operations: {len(operations)}")

                # Reconstruct Quality Checks from Criteria
                quality_checks = [{
                    "name": c.parameter,
                    "type": "pass_fail" if c.pass_fail else "measure",
                    "criteria": c.method,
                    "frequency": "100%",
                    "operation_id": None
                } for c in bom.acceptance_criteria.all()]
                print(f"  Quality Checks: {len(quality_checks)}")

                bom_data = {
                    "id": bom.id,
                    "productName": bom.product.name,
                    "status": bom.status,
                    "batchSize": float(bom.base_quantity),
                    "batchType": bom.uom or "pcs",
                    "attachment": serialize_bom_attachment(bom, request),
                    "components": components,
                    "operations": operations,
                    "qualityChecks": quality_checks
                }
                print("BOM data serialized successfully")
            except Exception as e:
                print(f"ERROR serializing BOM data: {e}")
                # Return empty data structure so page can still load
                bom_data = {
                    "id": bom.id if bom else None,
                    "productName": bom.product.name if bom else "",
                    "status": bom.status if bom else "draft",
                    "batchSize": 1,
                    "batchType": "pcs",
                    "components": [],
                    "operations": [],
                    "qualityChecks": []
                }

        print("Preparing context...")
        try:
            stage_qs = (
                ProductionStage.objects.filter(
                    Q(machine__company=company) |
                    Q(bomoperation__bom__product__company=company)
                )
                .select_related("machine")
                .distinct()
                .order_by("order", "name", "id")
            )
            existing_stage_names = []
            stage_library = []
            stage_catalog = []
            for stage in stage_qs:
                stage_name = (stage.name or "").strip()
                if not stage_name:
                    continue
                existing_stage_names.append(stage_name)
                stage_type = (
                    (stage.category or "").strip()
                    or (stage.machine.category if stage.machine else "")
                    or (stage.machine.type if stage.machine else "")
                    or "General"
                )
                stage_library.append({
                    "id": f"stage-{stage.id}",
                    "ref_id": str(stage.id),
                    "name": stage_name,
                    "type": stage_type,
                    "machine_id": "",
                    "default_machine_id": str(stage.machine_id) if stage.machine_id else "",
                    "source": "stage",
                    "icon": "ph-git-branch",
                    "is_quality_check": bool(stage.is_quality_check),
                })
                stage_catalog.append({
                    "id": str(stage.id),
                    "ref_id": str(stage.id),
                    "name": stage_name,
                    "type": stage_type,
                    "machine_id": "",
                    "default_machine_id": str(stage.machine_id) if stage.machine_id else "",
                    "is_quality_check": bool(stage.is_quality_check),
                })
            existing_stage_names = sorted(set(existing_stage_names), key=str.lower)
            print(f"  Existing stages: {existing_stage_names}")
        except Exception as e:
            print(f"ERROR loading stages: {e}")
            existing_stage_names = []
            stage_library = []
            stage_catalog = []
        
        # 🔒 Company Isolation: Only show machines from THIS company
        # Each company has its own machines and stages
        machine_qs = Machine.objects.filter(company=company, is_active=True)
        machine_categories = []
        seen_categories = set()
        for machine in machine_qs.order_by("category", "type", "name"):
            # Use category first (if defined), fallback to type or name
            category = (machine.category or machine.type or machine.name or "").strip()
            if not category:
                continue
            key = category.lower()
            if key in seen_categories:
                continue
            seen_categories.add(key)
            slug = slugify(category) or f"category-{len(seen_categories)}"
            machine_categories.append({
                "id": f"cat-{slug}",
                "ref_id": "",
                "name": category,
                "type": category,
                "machine_id": "",
                "source": "machine_category",
                "icon": "ph-gear-six",
                "is_quality_check": False,
            })
        if not machine_categories:
            # Fallback to machines if no categories are defined yet
            machine_categories = [
                {
                    "id": str(machine.id),
                    "ref_id": str(machine.id),
                    "name": machine.name,
                    "display_name": machine.display_label,
                    "type": (machine.category or machine.type or "Generic"),
                    "machine_id": str(machine.id),
                    "source": "machine",
                    "icon": "ph-gear-six",
                    "is_quality_check": False,
                }
                for machine in machine_qs.order_by("name")
            ]

        # Show only unique categories/types in the library (no verbose stage names like "Op 10: ...")
        operation_library = []
        seen_types = set()
        for item in (stage_library + machine_categories):
            display_type = (item.get("type") or item.get("name") or "").strip()
            if not display_type:
                continue
            key = display_type.lower()
            if key in seen_types:
                continue
            seen_types.add(key)

            normalized = dict(item)
            normalized["name"] = display_type
            normalized["type"] = display_type
            operation_library.append(normalized)

        print("Building context dictionary...")
        context = {
            'products': Product.objects.filter(company=company),
            'machines': operation_library,
            'existing_stages': existing_stage_names,
            'stage_catalog_json': json.dumps(stage_catalog),
            'bom': bom,
            'bom_data_json': json.dumps(bom_data)
        }
        print(f"Context prepared. Rendering template...")
        
        try:
            result = render(request, 'manufacturing/bom_builder.html', context)
            print("Template rendered successfully!")
            print(f"{'='*60}\n")
            return result
        except Exception as e:
            print(f"ERROR rendering template: {e}")
            import traceback
            traceback.print_exc()
            raise

class BOMSaveAPI(LoginRequiredMixin, View):
    def post(self, request):
        try:
            if not user_has_role(request.user, 'ui.bom.manage'):
                return JsonResponse({"status": "error", "message": "Unauthorized"}, status=403)

            data = json.loads(request.body)
            user = request.user
            company = require_company(user)
            valid_uoms = {choice[0] for choice in BillOfMaterial._meta.get_field('uom').choices}
            
            # 1. Parse Basic Information
            product_id = data.get('product_id')
            product_name = data.get('product_name', '').strip()
            base_qty = float(data.get('base_qty') or data.get('base_quantity') or data.get('batch') or 1)
            batch_type = (data.get('uom') or data.get('batch_type') or 'pcs').strip().lower()
            bom_status = data.get('status', 'draft')
            # Always start new BOMs as draft (activation is a separate step)
            if not data.get('bom_id'):
                bom_status = 'draft'
            valid_statuses = dict(BillOfMaterial.STATUS_CHOICES)
            if bom_status not in valid_statuses:
                return JsonResponse({"status": "error", "message": "Invalid BOM status"}, status=400)
            if batch_type not in valid_uoms:
                return JsonResponse({"status": "error", "message": "Invalid batch type"}, status=400)

            # HYBRID PRODUCT STRATEGY: Find by ID, or Find/Create by Name
            if product_id:
                product = get_object_or_404(Product, pk=product_id, company=company)
            elif product_name:
                product = Product.objects.filter(company=company, name__iexact=product_name).first()
                if not product:
                    product = Product.objects.create(
                        company=company,
                        name=product_name,
                        material_type='finished',  # Default to finished for BOM parent
                        unit='pcs',
                        description='Auto-created via BOM Wizard'
                    )
            else:
                return JsonResponse({"status": "error", "message": "Product Name is required"}, status=400)
            
            # 1B. Handle Product Image (Base64)
            product_image_b64 = data.get('product_image')
            if product_image_b64:
                try:
                    # Expect: data:image/png;base64,.....
                    if ';base64,' in product_image_b64:
                        format, imgstr = product_image_b64.split(';base64,') 
                        ext = format.split('/')[-1] 
                        file_data = ContentFile(base64.b64decode(imgstr), name=f"{product.name.replace(' ', '_')}_image.{ext}")
                        product.image = file_data
                        product.save()
                except Exception as e:
                    print(f"Error saving image: {e}")
            
            # 2. Create or Update BOM Head
            bom_id = data.get('bom_id')
            version_created_from = None
            if bom_id:
                existing_bom = get_object_or_404(BillOfMaterial, pk=bom_id, product__company=company)
                bom_is_used = WorkOrder.objects.filter(bom=existing_bom).exists()
                if existing_bom.status == 'draft' and not bom_is_used:
                    bom = existing_bom
                    bom.base_quantity = base_qty
                    bom.uom = batch_type
                    bom.status = bom_status
                    bom.save()
                else:
                    version_created_from = existing_bom.id
                    bom = BillOfMaterial.objects.create(
                        product=product,
                        base_quantity=base_qty,
                        uom=batch_type,
                        version=_next_bom_version(product),
                        status='draft',
                        created_by=user
                    )
            else:
                # CREATE NEW: Force DRAFT first to allow component additions
                bom = BillOfMaterial.objects.create(
                    product=product,
                    base_quantity=base_qty,
                    uom=batch_type,
                    status='draft', # FORCE DRAFT initially
                    created_by=user
                )

            attachment_data = data.get("attachment_data")
            if attachment_data:
                try:
                    header, encoded = attachment_data.split(";base64,", 1)
                    content_type = header.replace("data:", "", 1)
                    save_bom_attachment(
                        bom,
                        base64.b64decode(encoded),
                        file_name=data.get("attachment_name"),
                        content_type=content_type,
                    )
                except ValueError as exc:
                    return JsonResponse({"status": "error", "message": str(exc)}, status=400)
                except Exception:
                    return JsonResponse({"status": "error", "message": "Could not save BOM attachment."}, status=400)

            # 3. Process Materials (Full Replace Strategy)
            # Only allow modifying components if BOM is DRAFT (which it is now for new ones)
            if bom.status == 'draft':
                bom.components.all().delete()
                for idx, comp in enumerate(data.get('components', []), start=1):
                    # HYBRID APPROACH: Ensure Master Item Exists
                    mat_name = (comp.get('name') or '').strip()
                    if not mat_name:
                        return JsonResponse(
                            {"status": "error", "message": f"Material row {idx} is missing a material name."},
                            status=400
                        )

                    try:
                        qty_value = float(comp.get('qty'))
                    except (TypeError, ValueError):
                        return JsonResponse(
                            {"status": "error", "message": f"Material '{mat_name}' must have a valid quantity."},
                            status=400
                        )

                    if qty_value <= 0:
                        return JsonResponse(
                            {"status": "error", "message": f"Material '{mat_name}' must have a quantity greater than 0."},
                            status=400
                        )

                    product = None
                    if mat_name:
                        # Try to find or create the Product (Raw Material) scoped to current company
                        product = Product.objects.filter(company=company, name__iexact=mat_name).first()
                        if not product:
                            product = Product.objects.create(
                                company=company,
                                name=mat_name,
                                unit=comp['unit'],
                                material_type='raw',
                                description='Auto-created via BOM Builder'
                            )
                    
                    BOMComponent.objects.create(
                        bom=bom,
                        product=product,
                        material_name=mat_name,
                        quantity=qty_value,
                        unit=comp['unit'],
                        cost_per_unit=comp['cost'],
                        wastage_quantity=comp.get('scrap_qty', 0),
                        scrap_value_per_unit=comp.get('scrap_price', 0),
                        scrap_type=comp.get('scrap_type', 'sell_as_scrap')
                    )

                # 4. Acceptance Criteria (CREATE FIRST to allow linking)
                bom.acceptance_criteria.all().delete()
                created_criteria = []
                for crit in data.get('criteria', []):
                    c = BOMAcceptanceCriteria.objects.create(
                        bom=bom,
                        parameter=crit['parameter'],
                        method=crit['method'],
                        criteria_min=crit.get('min'),
                        criteria_max=crit.get('max'),
                        pass_fail=crit.get('pass_fail', False),
                        target_value=crit.get('target_value'),
                        tolerance=crit.get('tolerance'),
                        is_critical=crit.get('is_critical', False)
                    )
                    created_criteria.append(c)

                # 5. Process Operations (Link to Criteria if needed)
                bom.operations.all().delete()
                for idx, op in enumerate(data.get('operations', [])):
                    machine = None
                    machine_type_str = op.get('type') # 🆕 Get type from frontend

                    if op.get('machine_id') and str(op['machine_id']).isdigit(): # Handle specific machine if passed
                        try:
                             machine = Machine.objects.get(pk=op['machine_id'], company=company)
                        except: pass
                    
                    # Find or Create Stage generic
                    stage_name = op.get('stage_name', f"Stage {idx+1}")
                    is_qc = bool(op.get('quality_check')) or stage_name == "Quality Check"
                    
                    stage, _ = ProductionStage.objects.get_or_create(
                        name=stage_name,
                        defaults={'machine': machine, 'is_quality_check': is_qc}
                    )
                    if stage.is_quality_check != is_qc:
                        stage.is_quality_check = is_qc
                        stage.save(update_fields=['is_quality_check'])
                    
                    setup = normalize_operation_time_minutes(
                        op.get('setup_time', 0),
                        op.get('setup_time_unit') or op.get('setup_unit') or 'min',
                    )
                    run = normalize_operation_time_minutes(
                        op.get('run_time', 0),
                        op.get('run_time_unit') or op.get('run_unit') or 'min',
                    )
                    description = op.get('description', '')

                    # LINK QUALITY CHECK (Virtual Link via Description)
                    qc_idx = op.get('quality_check_index')
                    if qc_idx is not None and str(qc_idx) != "":
                        try:
                            qc_idx = int(qc_idx)
                            if 0 <= qc_idx < len(created_criteria):
                                crit = created_criteria[qc_idx]
                                # Create a standard formatted instruction
                                qc_text = f"\n\n🛑 QC REQUIRED: {crit.parameter} ({crit.method})"
                                if crit.target_value: qc_text += f"\nTarget: {crit.target_value}"
                                if crit.tolerance: qc_text += f" (±{crit.tolerance})"
                                description += qc_text
                        except (ValueError, IndexError):
                            pass

                    BOMOperation.objects.create(
                        bom=bom,
                        machine=machine, 
                        machine_type=machine_type_str, # 🆕 Save the type requirement
                        stage=stage,
                        order=idx + 1,
                        setup_time=setup,
                        run_time=run,
                        duration_minutes=max(int(round(float(setup + (run * base_qty)))), 0),
                        description=description
                    )

            # FINAL STEP: Activate if requested (now safe because components are added)
            if bom_status == 'active' and bom.status != 'active':
                bom.status = 'active'
                bom.save()    
                flag_bom_change_impact(bom, actor=user)
            
            response_payload = {
                "status": "success",
                "bom_id": bom.id,
                "message": "BOM Saved Successfully!",
            }
            if version_created_from:
                Notification.objects.create(
                    recipient=user,
                    title="BOM version created",
                    message=(
                        f"{bom.product.name} was saved as {bom.version}. "
                        "Existing work orders keep their original BOM snapshot."
                    ),
                    link=f"/manufacturing/bom-builder/{bom.id}/",
                )
                response_payload.update({
                    "version_created": True,
                    "previous_bom_id": version_created_from,
                    "bom_version": bom.version,
                    "message": "A new BOM version was created. Existing work orders keep their original BOM snapshot.",
                })
            return JsonResponse(response_payload)

        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)}, status=500)


class BOMDetailsView(LoginRequiredMixin, View):
    def get(self, request, bom_id):
        try:
            company = require_company(request.user)
            bom = BillOfMaterial.objects.filter(product__company=company).select_related("product", "created_by").prefetch_related("components").get(id=bom_id)
            total_cost = sum(c.total_cost() for c in bom.components.all())

            html = render_to_string("manufacturing/partials/bom_details.html", {
                "bom": bom,
                "components": bom.components.all(),
                "total_cost": total_cost,
            })
            return JsonResponse({"success": True, "html": html})
        except BillOfMaterial.DoesNotExist:
            return JsonResponse({"success": False, "error": "BOM not found"})

class BOMJsonView(LoginRequiredMixin, View):
    def get(self, request, bom_id):
        try:
            company = require_company(request.user)
            bom = BillOfMaterial.objects.filter(product__company=company).get(id=bom_id)
            
            components = [{
                "material_name": c.material_name,
                "quantity": float(c.quantity),
                "unit": c.unit,
                "cost_per_unit": float(c.cost_per_unit),
                "sub_bom_id": c.sub_bom.id if c.sub_bom else None
            } for c in bom.components.all()]

            operations = [{
                "id": op.id,
                "order": op.order,
                "duration": op.duration_minutes,
                "machine_id": op.machine.id if op.machine else None,
                "stage_name": op.stage.name if op.stage else None,
            } for op in bom.operations.all()]

            data = {
                "id": bom.id,
                "product_name": bom.product.name if bom.product else "Unknown",
                "version": bom.version,
                "base_quantity": float(bom.base_quantity),
                "uom": bom.uom or "pcs",
                "attachment": serialize_bom_attachment(bom, request),
                "components": components,
                "operations": operations,
                "stages": list({op.stage.name: {"id": op.stage.id, "name": op.stage.name} for op in bom.operations.all() if op.stage}.values()),
                "all_machines": [
                    {
                        "id": machine.id,
                        "name": machine.name,
                        "display_name": machine.display_label,
                        "code": machine.code,
                        "hourly_rate": float(machine.hourly_rate or 0),
                        "status": machine.status,
                        "type": machine.type,
                        "category": machine.category,
                    }
                    for machine in Machine.objects.filter(company=company).order_by("id")
                ],
            }
            return JsonResponse({"success": True, "bom": data})
        except: return JsonResponse({"success": False, "error": "Error"})


class BOMLifecycleView(LoginRequiredMixin, View):
    def post(self, request):
        try:
            if not user_has_role(request.user, 'ui.bom.manage'):
                return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

            bom_id = request.POST.get("bom_id")
            new_status = request.POST.get("status")
            company = require_company(request.user)
            bom = BillOfMaterial.objects.filter(product__company=company).get(id=bom_id)
            
            # Simple State Machine
            if new_status in ['draft', 'test', 'active', 'archived']:
                bom.status = new_status
                bom.save()
                impacted_count = flag_bom_change_impact(bom, actor=request.user) if new_status == "active" else 0
                message = f"Moved to {new_status}"
                if impacted_count:
                    message += f". {impacted_count} open work order(s) need BOM change action."
                return JsonResponse({"success": True, "message": message, "impacted_work_orders": impacted_count})
            
            return JsonResponse({"success": False, "error": "Invalid status"})
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})

# Legacy Create BOM (Form Post) - Optional to keep or refactor to utilize SaveAPI logic
# For brevity, I'll omit the legacy 'create_bom' function unless critical, as BOMBuilder replaces it.


