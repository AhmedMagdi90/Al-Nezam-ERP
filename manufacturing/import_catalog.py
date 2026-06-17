IMPORT_CATALOG = (
    {
        "type": "machines",
        "title": "Machines",
        "description": "Import machine codes, status, type, and category.",
        "template": "machines_template.xlsx",
        "sample": "machines_sample.xlsx",
        "count_key": "machines",
        "setup_phase": "required",
        "phase_label": "Required now",
    },
    {
        "type": "products",
        "title": "Products",
        "description": "Import finished goods, raw materials, units, and descriptions.",
        "template": "products_template.xlsx",
        "sample": "products_sample.xlsx",
        "count_key": "products",
        "setup_phase": "required",
        "phase_label": "Required now",
    },
    {
        "type": "stages",
        "title": "Production Stages",
        "description": "Import stage names, machine codes, order, and quality checkpoints.",
        "template": "stages_template.xlsx",
        "sample": "stages_sample.xlsx",
        "count_key": "stages",
        "setup_phase": "required",
        "phase_label": "Required now",
    },
    {
        "type": "bom",
        "title": "Bills of Material",
        "description": "Import products, components, routing stages, and machine mapping.",
        "template": "bom_template.xlsx",
        "sample": "bom_sample.xlsx",
        "count_key": "boms",
        "setup_phase": "required",
        "phase_label": "Required now",
    },
    {
        "type": "work_orders",
        "title": "Work Orders",
        "description": "Import quantities, planned dates, assignees, and work order status.",
        "template": "work_orders_template.xlsx",
        "sample": "work_orders_sample.xlsx",
        "count_key": "work_orders",
        "setup_phase": "recommended",
        "phase_label": "Recommended now",
    },
    {
        "type": "employees",
        "title": "Employees",
        "description": "Import planners, supervisors, workers, quality, and maintenance users.",
        "template": "employees_template.xlsx",
        "sample": "employees_sample.xlsx",
        "count_key": "employees",
        "setup_phase": "optional",
        "phase_label": "Optional later",
    },
)


def get_bulk_import_catalog():
    return [dict(item) for item in IMPORT_CATALOG]


def get_bulk_import_filenames():
    filenames = set()
    for item in IMPORT_CATALOG:
        filenames.add(item["template"])
        filenames.add(item["sample"])
    return sorted(filenames)
