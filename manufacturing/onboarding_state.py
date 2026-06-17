from django.contrib.auth import get_user_model

from manufacturing.models import BillOfMaterial, Machine, ProductionStage, WorkOrder


def get_company_setup_counts(company, db_alias="default"):
    if not company:
        return {
            "machines": 0,
            "stages": 0,
            "boms": 0,
            "work_orders": 0,
            "employees": 0,
        }

    alias = db_alias or "default"
    user_model = get_user_model()
    return {
        "machines": Machine.objects.using(alias).filter(company=company).count(),
        "stages": ProductionStage.objects.using(alias).filter(machine__company=company).distinct().count(),
        "boms": BillOfMaterial.objects.using(alias).filter(product__company=company).count(),
        "work_orders": WorkOrder.objects.using(alias).filter(company=company).count(),
        "employees": user_model.objects.using(alias).filter(profile__company=company).distinct().count(),
    }


def company_requires_onboarding(company, db_alias="default"):
    counts = get_company_setup_counts(company, db_alias=db_alias)
    return any(
        counts[key] <= 0
        for key in ("machines", "stages", "boms", "work_orders")
    )
