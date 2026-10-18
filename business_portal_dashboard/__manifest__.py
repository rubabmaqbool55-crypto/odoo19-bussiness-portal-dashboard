{
    "name": "Business Portal Dashboard",
    "version": "19.0.4.0.0",
    "category": "Website/Portal",
    "summary": "Professional portal dashboard for sales, purchases, invoices, deliveries and projects",
    "description": """
A professional Odoo 19 website portal dashboard. It shows role-aware business
cards and opens Odoo's standard portal document pages instead of backend apps.
A secure custom delivery/receipt portal, account statement, CSV export, and invoice aging are included.
    """,
    "author": "Custom Development",
    "website": "https://example.com",
    "license": "LGPL-3",
    "depends": [
        "portal",
        "mail",
        "website",
        "sale_management",
        "purchase",
        "purchase_stock",
        "account",
        "sale_stock",
        "project",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/portal_notification_cron.xml",
        "views/res_partner_views.xml",
        "views/portal_templates.xml",
        "views/dashboard_chart_views.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "business_portal_dashboard/static/src/js/dashboard_charts.js",
"business_portal_dashboard/static/src/scss/portal_dashboard.scss",
            "business_portal_dashboard/static/src/scss/dashboard_charts.scss",
        ],
    },
    "application": False,
    "installable": True,
}
