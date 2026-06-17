from django.db import migrations, models
import django.db.models.deletion


def backfill_bomcomponent_product(apps, schema_editor):
    BOMComponent = apps.get_model("manufacturing", "BOMComponent")
    Company = apps.get_model("manufacturing", "Company")
    Product = apps.get_model("manufacturing", "Product")
    db_alias = schema_editor.connection.alias

    for component in (
        BOMComponent.objects.using(db_alias)
        .filter(product__isnull=True)
        .select_related("bom", "bom__product")
    ):
        material_name = (component.material_name or "").strip()
        if not material_name:
            continue

        company_id = None
        if component.bom_id and getattr(component.bom, "product_id", None):
            bom_product = component.bom.product
            company_id = getattr(bom_product, "company_id", None)
            if company_id and not Company.objects.using(db_alias).filter(id=company_id).exists():
                company_id = None

        product_qs = Product.objects.using(db_alias).filter(name__iexact=material_name)
        if company_id:
            product_qs = product_qs.filter(company_id=company_id)
        elif component.bom_id and getattr(component.bom, "product_id", None):
            product_qs = product_qs.filter(company__isnull=True)

        product = product_qs.first()
        if not product:
            product = Product.objects.using(db_alias).create(
                company_id=company_id,
                name=material_name,
                unit=component.unit or "pcs",
                material_type="raw",
                description="Auto-created while linking BOM components to products.",
            )

        component.product_id = product.id
        component.save(using=db_alias, update_fields=["product"])


class Migration(migrations.Migration):

    dependencies = [
        ("manufacturing", "0061_qualitycheck_scrap_compensated_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="bomcomponent",
            name="product",
            field=models.ForeignKey(
                blank=True,
                help_text="Master material/product linked to this BOM component.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="bom_components",
                to="manufacturing.product",
            ),
        ),
        migrations.RunPython(backfill_bomcomponent_product, migrations.RunPython.noop),
    ]
