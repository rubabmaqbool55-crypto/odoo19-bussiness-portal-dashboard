from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    portal_dashboard_role = fields.Selection(
        selection=[
            ("auto", "Automatic"),
            ("customer", "Customer"),
            ("vendor", "Vendor"),
            ("both", "Customer and Vendor"),
        ],
        string="Portal dashboard role",
        default="auto",
        help=(
            "Controls which business cards appear in the website portal. "
            "Automatic uses the contact's customer/vendor ranks."
        ),
    )
    portal_dashboard_show_logistics = fields.Boolean(
        string="Show deliveries and receipts",
        default=True,
    )
    portal_dashboard_show_projects = fields.Boolean(
        string="Show projects",
        default=True,
    )
    portal_dashboard_show_tasks = fields.Boolean(
        string="Show tasks",
        default=True,
    )
    portal_dashboard_show_empty_cards = fields.Boolean(
        string="Show cards with zero records",
        default=True,
    )
    portal_dashboard_default_period = fields.Selection(
        selection=[
            ("7d", "Last 7 days"),
            ("30d", "Last 30 days"),
            ("90d", "Last 90 days"),
            ("month", "This month"),
            ("year", "This year"),
            ("all", "All time"),
        ],
        string="Default dashboard period",
        default="30d",
    )

    # Portal notifications and email reminders are stored on the commercial
    # company contact so all portal users of the same company share one policy.
    portal_notification_enabled = fields.Boolean(
        string="Enable portal notifications",
        default=True,
    )
    portal_email_reminder_enabled = fields.Boolean(
        string="Send notification emails",
        default=True,
    )
    portal_notify_sales = fields.Boolean(
        string="Sales notifications",
        default=True,
    )
    portal_notify_purchase = fields.Boolean(
        string="Purchase notifications",
        default=True,
    )
    portal_notify_billing = fields.Boolean(
        string="Billing notifications",
        default=True,
    )
    portal_notify_logistics = fields.Boolean(
        string="Logistics notifications",
        default=True,
    )
    portal_notify_projects = fields.Boolean(
        string="Project and task notifications",
        default=True,
    )
    portal_invoice_reminder_days = fields.Integer(
        string="Invoice reminder days",
        default=3,
        help="Create the first invoice reminder this many days before the due date.",
    )
    portal_quote_reminder_days = fields.Integer(
        string="Quotation reminder days",
        default=3,
        help="Create the first quotation reminder this many days before expiry.",
    )
    portal_task_reminder_days = fields.Integer(
        string="Task reminder days",
        default=2,
        help="Create the first task reminder this many days before the deadline.",
    )
