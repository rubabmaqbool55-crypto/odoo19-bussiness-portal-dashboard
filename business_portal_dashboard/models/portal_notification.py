import logging
from datetime import datetime
from urllib.parse import urljoin

from markupsafe import Markup, escape
from psycopg2 import IntegrityError

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class BusinessPortalNotification(models.Model):
    _name = "business.portal.notification"
    _description = "Business Portal Notification"
    _order = "create_date desc, id desc"

    name = fields.Char(string="Title", required=True)
    message = fields.Text(required=True)
    partner_id = fields.Many2one(
        "res.partner",
        string="Portal company",
        required=True,
        index=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="cascade",
    )
    category = fields.Selection(
        selection=[
            ("sales", "Sales"),
            ("purchase", "Purchase"),
            ("billing", "Billing"),
            ("logistics", "Logistics"),
            ("projects", "Projects"),
            ("tasks", "Tasks"),
            ("system", "System"),
        ],
        required=True,
        default="system",
        index=True,
    )
    notification_type = fields.Selection(
        selection=[
            ("info", "Information"),
            ("success", "Success"),
            ("warning", "Warning"),
            ("critical", "Critical"),
        ],
        required=True,
        default="info",
        index=True,
    )
    requires_action = fields.Boolean(default=False, index=True)
    is_read = fields.Boolean(default=False, index=True)
    read_date = fields.Datetime()
    action_url = fields.Char(default="/my/business-notifications")
    model_name = fields.Char(index=True)
    res_id = fields.Integer(index=True)
    document_reference = fields.Char(index=True)
    event_key = fields.Char(required=True, index=True, copy=False)
    event_date = fields.Datetime(default=fields.Datetime.now, index=True)
    email_status = fields.Selection(
        selection=[
            ("none", "Not requested"),
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
        ],
        default="none",
        required=True,
        copy=False,
    )
    email_sent_at = fields.Datetime(copy=False)
    email_error = fields.Text(copy=False)
    mail_id = fields.Many2one("mail.mail", copy=False, ondelete="set null")

    _event_key_unique = models.Constraint(
        "UNIQUE(event_key)",
        "A portal notification already exists for this event.",
    )

    # ------------------------------------------------------------------
    # Generic notification helpers
    # ------------------------------------------------------------------

    @api.model
    def _portal_users_for_partner(self, partner):
        commercial_partner = partner.commercial_partner_id
        return self.env["res.users"].sudo().search(
            [
                ("active", "=", True),
                ("share", "=", True),
                ("partner_id", "child_of", [commercial_partner.id]),
            ]
        )

    @api.model
    def _recipient_emails(self, partner):
        emails = []
        for user in self._portal_users_for_partner(partner):
            email = (user.partner_id.email or "").strip()
            if email and email.lower() not in {item.lower() for item in emails}:
                emails.append(email)

        commercial_email = (partner.commercial_partner_id.email or "").strip()
        if commercial_email and commercial_email.lower() not in {
            item.lower() for item in emails
        }:
            emails.append(commercial_email)
        return emails

    @api.model
    def _category_is_enabled(self, partner, category):
        commercial_partner = partner.commercial_partner_id.sudo()
        category_field = {
            "sales": "portal_notify_sales",
            "purchase": "portal_notify_purchase",
            "billing": "portal_notify_billing",
            "logistics": "portal_notify_logistics",
            "projects": "portal_notify_projects",
            "tasks": "portal_notify_projects",
            "system": "portal_notification_enabled",
        }.get(category, "portal_notification_enabled")
        return bool(
            commercial_partner.portal_notification_enabled
            and commercial_partner[category_field]
        )

    @api.model
    def _safe_document_url(self, record, fallback):
        method = getattr(record, "get_portal_url", None)
        if callable(method):
            try:
                url = method()
                if url and str(url).startswith("/my/"):
                    return url
            except Exception:
                _logger.debug(
                    "Could not build portal URL for %s,%s",
                    record._name,
                    record.id,
                    exc_info=True,
                )
        return fallback

    @api.model
    def _create_portal_notification(
        self,
        *,
        partner,
        company,
        category,
        notification_type,
        title,
        message,
        event_key,
        action_url,
        document_reference=None,
        model_name=None,
        res_id=None,
        requires_action=False,
        send_email=True,
    ):
        """Create one idempotent portal notification and optionally email it.

        ``event_key`` must describe the exact business event.  The SQL unique
        constraint prevents duplicate notifications when a button or cron is
        executed more than once.
        """
        commercial_partner = partner.commercial_partner_id.sudo()
        company = company.sudo() if company else self.env.company.sudo()

        if not self._category_is_enabled(commercial_partner, category):
            return self.browse()
        if not self._portal_users_for_partner(commercial_partner):
            return self.browse()

        full_event_key = f"{company.id}:{event_key}"
        existing = self.sudo().search(
            [("event_key", "=", full_event_key)], limit=1
        )
        if existing:
            return existing

        values = {
            "name": title,
            "message": message,
            "partner_id": commercial_partner.id,
            "company_id": company.id,
            "category": category,
            "notification_type": notification_type,
            "requires_action": requires_action,
            "action_url": (
                action_url
                if action_url and str(action_url).startswith("/my/")
                else "/my/business-notifications"
            ),
            "model_name": model_name,
            "res_id": res_id or 0,
            "document_reference": document_reference,
            "event_key": full_event_key,
            "event_date": fields.Datetime.now(),
        }

        try:
            with self.env.cr.savepoint():
                notification = self.sudo().create(values)
        except IntegrityError:
            notification = self.sudo().search(
                [("event_key", "=", full_event_key)], limit=1
            )

        if notification and send_email:
            notification._queue_notification_email()
        return notification

    def _queue_notification_email(self):
        """Create/send a transactional email without failing business actions."""
        for notification in self.sudo():
            partner = notification.partner_id.commercial_partner_id.sudo()
            if not partner.portal_email_reminder_enabled:
                notification.write(
                    {
                        "email_status": "skipped",
                        "email_error": _("Email reminders are disabled."),
                    }
                )
                continue

            emails = notification._recipient_emails(partner)
            if not emails:
                notification.write(
                    {
                        "email_status": "skipped",
                        "email_error": _("No portal email address is available."),
                    }
                )
                continue

            base_url = self.env["ir.config_parameter"].sudo().get_param(
                "web.base.url", ""
            )
            full_url = urljoin(
                base_url.rstrip("/") + "/",
                notification.action_url.lstrip("/"),
            )
            company = notification.company_id
            email_from = (
                company.partner_id.email_formatted
                or self.env.user.email_formatted
            )

            safe_title = escape(notification.name or _("Portal notification"))
            safe_message = escape(notification.message or "").replace(
                "\n", Markup("<br/>")
            )
            safe_partner = escape(partner.name or _("Portal user"))
            safe_company = escape(company.name or "")
            safe_url = escape(full_url)

            body_html = Markup(
                """
                <div style="font-family:Arial,sans-serif;font-size:14px;color:#344054;line-height:1.6">
                    <p>Hello <strong>%(partner)s</strong>,</p>
                    <h2 style="font-size:20px;color:#1d2939;margin:0 0 12px">%(title)s</h2>
                    <p>%(message)s</p>
                    <p style="margin:22px 0">
                        <a href="%(url)s" style="background:#714b67;color:#fff;text-decoration:none;padding:10px 16px;border-radius:6px;display:inline-block">
                            View in portal
                        </a>
                    </p>
                    <p style="color:#667085">This notification was sent by %(company)s.</p>
                </div>
                """
            ) % {
                "partner": safe_partner,
                "title": safe_title,
                "message": safe_message,
                "url": safe_url,
                "company": safe_company,
            }

            mail_values = {
                "subject": f"{company.name}: {notification.name}",
                "body_html": body_html,
                "email_to": ",".join(emails),
                "auto_delete": False,
                "model": notification._name,
                "res_id": notification.id,
            }
            if email_from:
                mail_values["email_from"] = email_from

            try:
                mail = self.env["mail.mail"].sudo().create(mail_values)
                notification.write(
                    {
                        "mail_id": mail.id,
                        "email_status": "queued",
                        "email_error": False,
                    }
                )
                # Odoo catches SMTP errors when raise_exception=False.  If no
                # server is configured the email remains visible in the queue.
                mail.send(raise_exception=False)
                if mail.state == "sent":
                    notification.write(
                        {
                            "email_status": "sent",
                            "email_sent_at": fields.Datetime.now(),
                        }
                    )
                elif mail.state == "exception":
                    notification.write(
                        {
                            "email_status": "failed",
                            "email_error": mail.failure_reason
                            or _("Odoo could not send this email."),
                        }
                    )
            except Exception as error:
                _logger.exception(
                    "Unable to send business portal notification %s",
                    notification.id,
                )
                notification.write(
                    {
                        "email_status": "failed",
                        "email_error": str(error),
                    }
                )

    def _mark_as_read(self):
        unread = self.filtered(lambda item: not item.is_read)
        if unread:
            unread.sudo().write(
                {
                    "is_read": True,
                    "read_date": fields.Datetime.now(),
                }
            )
        return True

    # ------------------------------------------------------------------
    # Daily reminders
    # ------------------------------------------------------------------

    @api.model
    def _eligible_commercial_partner_ids(self):
        users = self.env["res.users"].sudo().search(
            [("active", "=", True), ("share", "=", True)]
        )
        return list(set(users.mapped("partner_id.commercial_partner_id").ids))

    @api.model
    def _overdue_milestone(self, days_overdue):
        return days_overdue if days_overdue in (1, 3, 7, 14, 30) else False

    @api.model
    def _cron_invoice_reminders(self, commercial_partner_ids, today):
        if not commercial_partner_ids:
            return 0
        moves = self.env["account.move"].sudo().search(
            [
                ("company_id", "!=", False),
                ("partner_id", "child_of", commercial_partner_ids),
                ("state", "=", "posted"),
                (
                    "move_type",
                    "in",
                    (
                        "out_invoice",
                        "out_receipt",
                        "in_invoice",
                        "in_receipt",
                    ),
                ),
                ("payment_state", "not in", ("paid", "reversed")),
                ("amount_residual", ">", 0),
                ("invoice_date_due", "!=", False),
            ],
            limit=1000,
        )
        count = 0
        for move in moves:
            partner = move.partner_id.commercial_partner_id.sudo()
            if not self._category_is_enabled(partner, "billing"):
                continue
            due_date = move.invoice_date_due
            days_to_due = (due_date - today).days
            reminder_days = max(1, min(partner.portal_invoice_reminder_days or 3, 30))
            reference = move.name or move.display_name
            action_url = self._safe_document_url(move, "/my/invoices")

            if days_to_due == reminder_days:
                title = _("Invoice due soon")
                message = _(
                    "%(reference)s is due in %(days)s day(s) on %(date)s.",
                    reference=reference,
                    days=reminder_days,
                    date=due_date,
                )
                event = f"invoice:{move.id}:due-soon:{due_date}:{reminder_days}"
                level = "warning"
            elif days_to_due == 1 and reminder_days != 1:
                title = _("Invoice due tomorrow")
                message = _(
                    "%(reference)s is due tomorrow on %(date)s.",
                    reference=reference,
                    date=due_date,
                )
                event = f"invoice:{move.id}:due-tomorrow:{due_date}"
                level = "warning"
            elif days_to_due == 0:
                title = _("Invoice due today")
                message = _(
                    "%(reference)s is due today.", reference=reference
                )
                event = f"invoice:{move.id}:due-today:{due_date}"
                level = "warning"
            elif days_to_due < 0 and self._overdue_milestone(-days_to_due):
                overdue_days = -days_to_due
                title = _("Invoice overdue")
                message = _(
                    "%(reference)s is overdue by %(days)s day(s).",
                    reference=reference,
                    days=overdue_days,
                )
                event = f"invoice:{move.id}:overdue:{overdue_days}:{due_date}"
                level = "critical"
            else:
                continue

            if self._create_portal_notification(
                partner=partner,
                company=move.company_id,
                category="billing",
                notification_type=level,
                title=title,
                message=message,
                event_key=event,
                action_url=action_url,
                document_reference=reference,
                model_name=move._name,
                res_id=move.id,
                requires_action=True,
            ):
                count += 1
        return count

    @api.model
    def _cron_quotation_reminders(self, commercial_partner_ids, today):
        if not commercial_partner_ids:
            return 0
        orders = self.env["sale.order"].sudo().search(
            [
                ("company_id", "!=", False),
                ("partner_id", "child_of", commercial_partner_ids),
                ("state", "=", "sent"),
                ("validity_date", "!=", False),
            ],
            limit=1000,
        )
        count = 0
        for order in orders:
            partner = order.partner_id.commercial_partner_id.sudo()
            if not self._category_is_enabled(partner, "sales"):
                continue
            expiry_date = order.validity_date
            days_to_expiry = (expiry_date - today).days
            reminder_days = max(1, min(partner.portal_quote_reminder_days or 3, 30))
            action_url = self._safe_document_url(order, "/my/quotes")

            if days_to_expiry == reminder_days:
                title = _("Quotation expires soon")
                message = _(
                    "%(reference)s expires in %(days)s day(s) on %(date)s.",
                    reference=order.name,
                    days=reminder_days,
                    date=expiry_date,
                )
                event = f"quotation:{order.id}:expires-soon:{expiry_date}:{reminder_days}"
                level = "warning"
            elif days_to_expiry == 1 and reminder_days != 1:
                title = _("Quotation expires tomorrow")
                message = _(
                    "%(reference)s expires tomorrow.", reference=order.name
                )
                event = f"quotation:{order.id}:expires-tomorrow:{expiry_date}"
                level = "warning"
            elif days_to_expiry == 0:
                title = _("Quotation expires today")
                message = _(
                    "%(reference)s expires today.", reference=order.name
                )
                event = f"quotation:{order.id}:expires-today:{expiry_date}"
                level = "critical"
            else:
                continue

            if self._create_portal_notification(
                partner=partner,
                company=order.company_id,
                category="sales",
                notification_type=level,
                title=title,
                message=message,
                event_key=event,
                action_url=action_url,
                document_reference=order.name,
                model_name=order._name,
                res_id=order.id,
                requires_action=True,
            ):
                count += 1
        return count

    @api.model
    def _cron_task_reminders(self, commercial_partner_ids, today):
        if not commercial_partner_ids:
            return 0
        tasks = self.env["project.task"].sudo().search(
            [
                ("active", "=", True),
                ("partner_id", "child_of", commercial_partner_ids),
                ("date_deadline", "!=", False),
                ("stage_id.fold", "=", False),
            ],
            limit=1000,
        )
        count = 0
        for task in tasks:
            partner = task.partner_id.commercial_partner_id.sudo()
            if not self._category_is_enabled(partner, "tasks"):
                continue
            deadline = task.date_deadline
            days_to_deadline = (deadline - today).days
            reminder_days = max(1, min(partner.portal_task_reminder_days or 2, 30))
            action_url = self._safe_document_url(task, "/my/tasks")

            if days_to_deadline == reminder_days:
                title = _("Task deadline approaching")
                message = _(
                    "%(task)s is due in %(days)s day(s) on %(date)s.",
                    task=task.name,
                    days=reminder_days,
                    date=deadline,
                )
                event = f"task:{task.id}:due-soon:{deadline}:{reminder_days}"
                level = "warning"
            elif days_to_deadline == 0:
                title = _("Task due today")
                message = _("%(task)s is due today.", task=task.name)
                event = f"task:{task.id}:due-today:{deadline}"
                level = "warning"
            elif days_to_deadline < 0 and self._overdue_milestone(-days_to_deadline):
                overdue_days = -days_to_deadline
                title = _("Task overdue")
                message = _(
                    "%(task)s is overdue by %(days)s day(s).",
                    task=task.name,
                    days=overdue_days,
                )
                event = f"task:{task.id}:overdue:{overdue_days}:{deadline}"
                level = "critical"
            else:
                continue

            if self._create_portal_notification(
                partner=partner,
                company=task.company_id or self.env.company,
                category="tasks",
                notification_type=level,
                title=title,
                message=message,
                event_key=event,
                action_url=action_url,
                document_reference=task.name,
                model_name=task._name,
                res_id=task.id,
                requires_action=True,
            ):
                count += 1
        return count

    @api.model
    def _cron_delivery_delay_reminders(self, commercial_partner_ids, today):
        if not commercial_partner_ids:
            return 0
        end_of_today = datetime.combine(today, datetime.max.time())
        pickings = self.env["stock.picking"].sudo().search(
            [
                ("company_id", "!=", False),
                ("partner_id", "child_of", commercial_partner_ids),
                ("picking_type_code", "in", ("incoming", "outgoing")),
                ("state", "not in", ("done", "cancel")),
                ("scheduled_date", "<", fields.Datetime.to_string(end_of_today)),
            ],
            limit=1000,
        )
        count = 0
        for picking in pickings:
            partner = picking.partner_id.commercial_partner_id.sudo()
            if not self._category_is_enabled(partner, "logistics"):
                continue
            scheduled_date = fields.Datetime.to_datetime(picking.scheduled_date)
            scheduled_day = scheduled_date.date() if scheduled_date else today
            days_late = (today - scheduled_day).days
            milestone = self._overdue_milestone(days_late)
            if not milestone:
                continue

            transfer_name = (
                _("Delivery")
                if picking.picking_type_code == "outgoing"
                else _("Receipt")
            )
            title = _("%(transfer)s delayed", transfer=transfer_name)
            message = _(
                "%(reference)s is delayed by %(days)s day(s).",
                reference=picking.name,
                days=days_late,
            )
            if self._create_portal_notification(
                partner=partner,
                company=picking.company_id,
                category="logistics",
                notification_type="critical",
                title=title,
                message=message,
                event_key=(
                    f"picking:{picking.id}:delayed:{days_late}:{scheduled_day}"
                ),
                action_url=f"/my/business-deliveries/{picking.id}",
                document_reference=picking.name,
                model_name=picking._name,
                res_id=picking.id,
                requires_action=True,
            ):
                count += 1
        return count

    @api.model
    def _cron_generate_reminders(self):
        """Daily, idempotent reminder generation for portal users."""
        today = fields.Date.context_today(self)
        commercial_partner_ids = self._eligible_commercial_partner_ids()
        total = 0
        total += self._cron_invoice_reminders(commercial_partner_ids, today)
        total += self._cron_quotation_reminders(commercial_partner_ids, today)
        total += self._cron_task_reminders(commercial_partner_ids, today)
        total += self._cron_delivery_delay_reminders(
            commercial_partner_ids, today
        )
        _logger.info("Business portal reminder cron processed %s event(s)", total)
        return total
