from datetime import datetime, time, timedelta
import logging

import pytz
from dateutil.relativedelta import relativedelta

from odoo import _, fields, http
from odoo.http import request

from odoo.addons.business_portal_dashboard.controllers.portal import (
    BusinessPortalDashboard,
)


_logger = logging.getLogger(__name__)


class BusinessPortalDashboardCharts(BusinessPortalDashboard):
    """Secure chart data endpoints for the website portal dashboard."""

    _chart_period_labels = {
        "7d": _("Last 7 days"),
        "30d": _("Last 30 days"),
        "90d": _("Last 90 days"),
        "month": _("Current month"),
        "year": _("Current year"),
        "12m": _("Last 12 months"),
        "all": _("All time"),
    }

    def _normalise_chart_period(self, period):
        return (
            period
            if period in self._chart_period_labels
            else "12m"
        )
    # ---------------------------------------------------------------------
    # Date buckets and formatting helpers
    # ---------------------------------------------------------------------

    def _chart_daily_buckets(self, date_from, date_to):
        buckets = []
        cursor = date_from
        while cursor <= date_to:
            buckets.append(
                {
                    "start": cursor,
                    "end": cursor,
                    "label": cursor.strftime("%d %b"),
                }
            )
            cursor += timedelta(days=1)
        return buckets

    def _chart_chunk_buckets(self, date_from, date_to, days):
        buckets = []
        cursor = date_from
        while cursor <= date_to:
            bucket_end = min(cursor + timedelta(days=days - 1), date_to)
            label = (
                cursor.strftime("%d %b")
                if cursor == bucket_end
                else f"{cursor.strftime('%d %b')}–{bucket_end.strftime('%d %b')}"
            )
            buckets.append({"start": cursor, "end": bucket_end, "label": label})
            cursor = bucket_end + timedelta(days=1)
        return buckets

    def _chart_monthly_buckets(self, date_from, date_to, show_year=False):
        buckets = []
        cursor = date_from.replace(day=1)
        while cursor <= date_to:
            next_month = cursor + relativedelta(months=1)
            bucket_end = min(next_month - timedelta(days=1), date_to)
            buckets.append(
                {
                    "start": max(cursor, date_from),
                    "end": bucket_end,
                    "label": cursor.strftime("%b %Y" if show_year else "%b"),
                }
            )
            cursor = next_month
        return buckets

    def _chart_yearly_buckets(self, date_from, date_to):
        buckets = []

        cursor = date_from.replace(
            month=1,
            day=1,
        )

        while cursor <= date_to:
            next_year = cursor + relativedelta(years=1)

            buckets.append({
                "start": max(cursor, date_from),
                "end": min(
                    next_year - timedelta(days=1),
                    date_to,
                ),
                "label": str(cursor.year),
            })

            cursor = next_year

        return buckets

    def _chart_all_time_start(self):
        """Find earliest accessible business record for this portal user."""

        settings = self._dashboard_settings()
        role = settings["role"]
        dates = []

        # Sales orders
        if (
                role in ("customer", "both")
                and self._model_can_read("sale.order")
        ):
            order = request.env["sale.order"].search(
                self._partner_domain()
                + self._company_domain()
                + [
                    ("state", "=", "sale"),
                    ("date_order", "!=", False),
                ],
                order="date_order asc",
                limit=1,
            )

            if order:
                dates.append(
                    self._chart_record_date(
                        order,
                        "date_order",
                    )
                )

        # Purchase orders
        if (
                role == "vendor"
                and self._model_can_read("purchase.order")
        ):
            purchase = request.env["purchase.order"].search(
                self._partner_domain()
                + self._company_domain()
                + [
                    ("state", "=", "purchase"),
                    ("date_order", "!=", False),
                ],
                order="date_order asc",
                limit=1,
            )

            if purchase:
                dates.append(
                    self._chart_record_date(
                        purchase,
                        "date_order",
                    )
                )

        # Invoices or vendor bills
        if self._model_can_read("account.move"):
            document_side = self._chart_document_side(role)

            move_types = (
                ("in_invoice", "in_receipt")
                if document_side == "vendor"
                else ("out_invoice", "out_receipt")
            )

            move = request.env["account.move"].search(
                self._partner_domain()
                + self._company_domain()
                + [
                    ("state", "=", "posted"),
                    ("move_type", "in", move_types),
                    ("invoice_date", "!=", False),
                ],
                order="invoice_date asc",
                limit=1,
            )

            if move:
                dates.append(move.invoice_date)

        # Deliveries
        directions = self._allowed_delivery_directions(
            role,
            settings["show_logistics"],
        )

        for direction in directions:
            picking = (
                request.env["stock.picking"]
                .sudo()
                .search(
                    self._delivery_domain(
                        direction,
                        "all",
                    )
                    + [
                        ("state", "=", "done"),
                        ("date_done", "!=", False),
                    ],
                    order="date_done asc",
                    limit=1,
                )
            )

            if picking:
                dates.append(
                    self._chart_record_date(
                        picking,
                        "date_done",
                    )
                )

        valid_dates = [
            value
            for value in dates
            if value
        ]

        return (
            min(valid_dates)
            if valid_dates
            else fields.Date.context_today(
                request.env.user
            )
        )

    def _chart_buckets(self, period):
        today = fields.Date.context_today(
            request.env.user
        )

        if period == "7d":
            return self._chart_daily_buckets(
                today - timedelta(days=6),
                today,
            )

        if period == "30d":
            return self._chart_chunk_buckets(
                today - timedelta(days=29),
                today,
                5,
            )

        if period == "90d":
            return self._chart_chunk_buckets(
                today - timedelta(days=89),
                today,
                7,
            )

        if period == "month":
            return self._chart_daily_buckets(
                today.replace(day=1),
                today,
            )

        if period == "year":
            return self._chart_monthly_buckets(
                today.replace(month=1, day=1),
                today,
                show_year=False,
            )

        if period == "12m":
            first_month = (
                    today - relativedelta(months=11)
            ).replace(day=1)

            return self._chart_monthly_buckets(
                first_month,
                today,
                show_year=True,
            )

        # Actual All Time
        first_date = self._chart_all_time_start()

        total_months = (
                (today.year - first_date.year) * 12
                + today.month
                - first_date.month
                + 1
        )

        # Up to three years: monthly chart
        if total_months <= 36:
            return self._chart_monthly_buckets(
                first_date.replace(day=1),
                today,
                show_year=True,
            )

        # More than three years: yearly chart
        return self._chart_yearly_buckets(
            first_date,
            today,
        )

    def _chart_datetime_domain(self, field_name, date_from, date_to):
        """Build UTC boundaries from the portal user's local timezone."""
        timezone = pytz.timezone(request.env.user.tz or "UTC")
        local_start = timezone.localize(datetime.combine(date_from, time.min))
        local_end = timezone.localize(
            datetime.combine(date_to + timedelta(days=1), time.min)
        )
        utc_start = local_start.astimezone(pytz.UTC).replace(tzinfo=None)
        utc_end = local_end.astimezone(pytz.UTC).replace(tzinfo=None)
        return [
            (field_name, ">=", fields.Datetime.to_string(utc_start)),
            (field_name, "<", fields.Datetime.to_string(utc_end)),
        ]

    def _chart_date_domain(self, field_name, date_from, date_to):
        return [
            (field_name, ">=", fields.Date.to_string(date_from)),
            (field_name, "<=", fields.Date.to_string(date_to)),
        ]

    def _chart_record_date(self, record, field_name):
        value = record[field_name]
        if not value:
            return False
        if isinstance(value, datetime):
            return fields.Datetime.context_timestamp(request.env.user, value).date()
        return value

    def _chart_bucket_index(self, value, buckets):
        for index, bucket in enumerate(buckets):
            if bucket["start"] <= value <= bucket["end"]:
                return index
        return False

    def _chart_convert_amount(self, record, amount, conversion_date=None):
        company = request.env.company
        company_currency = company.currency_id
        currency = record.currency_id or company_currency
        conversion_date = conversion_date or fields.Date.context_today(request.env.user)
        if isinstance(conversion_date, datetime):
            conversion_date = conversion_date.date()
        return currency._convert(amount or 0.0, company_currency, company, conversion_date)

    def _chart_currency_data(self):
        currency = request.env.company.currency_id
        return {
            "code": currency.name,
            "symbol": currency.symbol,
            "position": currency.position,
            "decimal_places": currency.decimal_places,
        }

    def _chart_document_side(self, role):
        # A dual customer/vendor contact sees customer analytics by default,
        # matching the most common B2B portal use case.
        return "vendor" if role == "vendor" else "customer"

    # ---------------------------------------------------------------------
    # Chart builders
    # ---------------------------------------------------------------------

    def _business_trend_chart(self, period, role):
        buckets = self._chart_buckets(period)
        labels = [bucket["label"] for bucket in buckets]
        date_from = buckets[0]["start"]
        date_to = buckets[-1]["end"]
        ordered = [0.0] * len(buckets)
        invoiced = [0.0] * len(buckets)
        paid = [0.0] * len(buckets)

        order_label = _("Ordered")
        order_url = "/my/orders"
        order_visible = False

        if role == "vendor" and self._model_can_read("purchase.order"):
            Order = request.env["purchase.order"]
            order_domain = (
                self._partner_domain()
                + self._company_domain()
                + self._chart_datetime_domain("date_order", date_from, date_to)
                + [("state", "=", "purchase")]
            )
            orders = Order.search(order_domain)
            order_label = _("Purchased")
            order_url = "/my/purchase"
            order_visible = True
        elif role in ("customer", "both") and self._model_can_read("sale.order"):
            Order = request.env["sale.order"]
            order_domain = (
                self._partner_domain()
                + self._company_domain()
                + self._chart_datetime_domain("date_order", date_from, date_to)
                + [("state", "=", "sale")]
            )
            orders = Order.search(order_domain)
            order_visible = True
        else:
            orders = request.env["sale.order"].browse()

        for order in orders:
            order_date = self._chart_record_date(order, "date_order")
            bucket_index = self._chart_bucket_index(order_date, buckets)
            if bucket_index is False:
                continue
            ordered[bucket_index] += self._chart_convert_amount(
                order, order.amount_total, order_date
            )

        invoice_visible = self._model_can_read("account.move")
        document_side = self._chart_document_side(role)
        move_types = (
            ("in_invoice", "in_receipt")
            if document_side == "vendor"
            else ("out_invoice", "out_receipt")
        )
        invoice_label = _("Billed") if document_side == "vendor" else _("Invoiced")

        if invoice_visible:
            Move = request.env["account.move"]
            invoice_domain = (
                self._partner_domain()
                + self._company_domain()
                + self._chart_date_domain("invoice_date", date_from, date_to)
                + [
                    ("state", "=", "posted"),
                    ("move_type", "in", move_types),
                    ("payment_state", "!=", "reversed"),
                ]
            )
            invoices = Move.search(invoice_domain)
        else:
            invoices = request.env["account.move"].browse()

        for move in invoices:
            invoice_date = move.invoice_date or fields.Date.context_today(request.env.user)
            bucket_index = self._chart_bucket_index(invoice_date, buckets)
            if bucket_index is False:
                continue
            total = self._chart_convert_amount(move, move.amount_total, invoice_date)
            residual = self._chart_convert_amount(
                move, move.amount_residual, invoice_date
            )
            invoiced[bucket_index] += total
            # "Paid" is the settled portion of invoices grouped by invoice date.
            paid[bucket_index] += max(total - residual, 0.0)

        currency = request.env.company.currency_id
        ordered = [currency.round(value) for value in ordered]
        invoiced = [currency.round(value) for value in invoiced]
        paid = [currency.round(value) for value in paid]

        return {
            "visible": order_visible or invoice_visible,
            "labels": labels,
            "series": [
                {
                    "key": "ordered",
                    "label": order_label,
                    "values": ordered,
                    "url": order_url,
                },
                {
                    "key": "invoiced",
                    "label": invoice_label,
                    "values": invoiced,
                    "url": "/my/invoices",
                },
                {
                    "key": "paid",
                    "label": _("Paid"),
                    "values": paid,
                    "url": "/my/invoices",
                },
            ],
            "totals": {
                "ordered": currency.round(sum(ordered)),
                "invoiced": currency.round(sum(invoiced)),
                "paid": currency.round(sum(paid)),
            },
            "summary_labels": {
                "ordered": order_label,
                "invoiced": invoice_label,
                "paid": _("Paid"),
            },
        }

    def _invoice_status_chart(self, period, role):
        if not self._model_can_read("account.move"):
            return {"visible": False, "items": []}

        document_side = self._chart_document_side(role)
        move_types = (
            ("in_invoice", "in_receipt")
            if document_side == "vendor"
            else ("out_invoice", "out_receipt")
        )
        if period == "all":
            period_domain = []
        else:
            buckets = self._chart_buckets(period)

            period_domain = self._chart_date_domain(
                "invoice_date",
                buckets[0]["start"],
                buckets[-1]["end"],
            )

        domain = (
            self._partner_domain()
            + self._company_domain()
            + period_domain
            + [
                ("state", "=", "posted"),
                ("move_type", "in", move_types),
                ("payment_state", "!=", "reversed"),
            ]
        )
        moves = request.env["account.move"].search(domain)
        today = fields.Date.context_today(request.env.user)
        counts = {"paid": 0, "partial": 0, "due": 0, "overdue": 0}

        for move in moves:
            if move.payment_state == "paid" or not move.amount_residual:
                counts["paid"] += 1
                continue
            due_date = move.invoice_date_due or move.invoice_date or today
            if due_date < today:
                counts["overdue"] += 1
            elif move.payment_state in ("partial", "in_payment") or (
                move.amount_residual < move.amount_total
            ):
                counts["partial"] += 1
            else:
                counts["due"] += 1

        base_url = "/my/invoices"
        return {
            "visible": True,
            "title": _("Bill payment status")
            if document_side == "vendor"
            else _("Invoice payment status"),
            "items": [
                {
                    "key": "paid",
                    "label": _("Paid"),
                    "value": counts["paid"],
                    "url": f"{base_url}?filterby=paid",
                },
                {
                    "key": "partial",
                    "label": _("Partially paid"),
                    "value": counts["partial"],
                    "url": base_url,
                },
                {
                    "key": "due",
                    "label": _("Payment due"),
                    "value": counts["due"],
                    "url": base_url,
                },
                {
                    "key": "overdue",
                    "label": _("Overdue"),
                    "value": counts["overdue"],
                    "url": f"{base_url}?filterby=overdue_invoices",
                },
            ],
        }

    def _aging_chart(self, role):
        if not self._model_can_read("account.move"):
            return {
                "visible": False,
                "items": [],
                "total": 0.0,
                "overdue": 0.0,
                "overdue_count": 0,
            }

        statement_type = self._chart_document_side(role)
        aging = self._statement_aging(statement_type)
        items = []
        for bucket in aging["buckets"]:
            items.append(
                {
                    "key": bucket["key"],
                    "label": bucket["label"],
                    "value": bucket["amount"],
                    "count": bucket["count"],
                    "url": f"/my/account-statement?statement_type={statement_type}",
                }
            )

        overdue_items = [item for item in items if item["key"] != "not_due"]
        return {
            "visible": self._model_can_read("account.move"),
            "items": items,
            "total": aging["total"],
            "overdue": request.env.company.currency_id.round(
                sum(item["value"] for item in overdue_items)
            ),
            "overdue_count": sum(item["count"] for item in overdue_items),
        }

    def _delivery_performance_chart(self, period, role, show_logistics):
        directions = self._allowed_delivery_directions(role, show_logistics)
        if not directions:
            return {"visible": False, "labels": [], "series": []}

        buckets = self._chart_buckets(period)
        date_from = buckets[0]["start"]
        date_to = buckets[-1]["end"]
        labels = [bucket["label"] for bucket in buckets]
        on_time = [0] * len(buckets)
        delayed = [0] * len(buckets)

        direction = directions[0] if len(directions) == 1 else "all"
        domain = (
            self._delivery_domain(direction, "all")
            + [("state", "=", "done"), ("date_done", "!=", False)]
            + self._chart_datetime_domain("date_done", date_from, date_to)
        )
        pickings = request.env["stock.picking"].sudo().search(domain)

        for picking in pickings:
            completion_date = self._chart_record_date(picking, "date_done")
            bucket_index = self._chart_bucket_index(completion_date, buckets)
            if bucket_index is False:
                continue
            if not picking.scheduled_date or picking.date_done <= picking.scheduled_date:
                on_time[bucket_index] += 1
            else:
                delayed[bucket_index] += 1

        total = sum(on_time) + sum(delayed)
        rate = round((sum(on_time) / total) * 100) if total else 0
        return {
            "visible": True,
            "labels": labels,
            "series": [
                {"key": "on_time", "label": _("On time"), "values": on_time},
                {"key": "delayed", "label": _("Delayed"), "values": delayed},
            ],
            "on_time_rate": rate,
            "url": f"/my/business-deliveries?direction={direction}&period={period}",
        }

    def _project_progress_chart(self, show_projects):
        if not show_projects or not self._model_can_read("project.project"):
            return {"visible": False, "items": []}
        if not self._model_can_read("project.task"):
            return {"visible": False, "items": []}

        Project = request.env["project.project"]
        Task = request.env["project.task"]
        project_domain = [
            ("is_template", "=", False),
            ("active", "=", True),
            "|",
            ("company_id", "=", False),
            ("company_id", "=", request.env.company.id),
        ]
        projects = Project.search(project_domain, order="write_date desc, id desc", limit=6)
        items = []

        for project in projects:
            task_domain = [
                ("project_id", "=", project.id),
                ("is_template", "=", False),
            ]
            total = Task.search_count(task_domain)
            completed = Task.search_count(task_domain + [("stage_id.fold", "=", True)])
            progress = round((completed / total) * 100) if total else 0
            items.append(
                {
                    "id": project.id,
                    "label": project.display_name,
                    "value": progress,
                    "completed": completed,
                    "total": total,
                    "url": self._safe_portal_url(project, "/my/projects"),
                }
            )

        return {"visible": True, "items": items}

    def _task_status_chart(self, show_tasks):
        if not show_tasks or not self._model_can_read("project.task"):
            return {"visible": False, "items": []}

        Task = request.env["project.task"]
        today = fields.Date.context_today(request.env.user)
        due_soon_date = today + timedelta(days=7)
        base = [
            ("project_id", "!=", False),
            ("is_template", "=", False),
            "|",
            ("company_id", "=", False),
            ("company_id", "=", request.env.company.id),
        ]
        completed = Task.search_count(base + [("stage_id.fold", "=", True)])
        open_base = base + [("stage_id.fold", "=", False)]
        overdue = Task.search_count(
            open_base + [("date_deadline", "<", fields.Date.to_string(today))]
        )
        due_soon = Task.search_count(
            open_base
            + [
                ("date_deadline", ">=", fields.Date.to_string(today)),
                ("date_deadline", "<=", fields.Date.to_string(due_soon_date)),
            ]
        )
        total_open = Task.search_count(open_base)
        open_other = max(total_open - overdue - due_soon, 0)

        return {
            "visible": True,
            "items": [
                {
                    "key": "open",
                    "label": _("Open"),
                    "value": open_other,
                    "url": "/my/tasks",
                },
                {
                    "key": "due_soon",
                    "label": _("Due in 7 days"),
                    "value": due_soon,
                    "url": "/my/tasks",
                },
                {
                    "key": "overdue",
                    "label": _("Overdue"),
                    "value": overdue,
                    "url": "/my/tasks",
                },
                {
                    "key": "completed",
                    "label": _("Completed"),
                    "value": completed,
                    "url": "/my/tasks",
                },
            ],
        }

    # ---------------------------------------------------------------------
    # JSON-RPC endpoint
    # ---------------------------------------------------------------------

    def _safe_chart_result(self, chart_name, callback, fallback, errors):
        """Keep the complete dashboard usable if one optional chart fails."""
        try:
            return callback()
        except Exception:
            _logger.exception("Business portal chart failed: %s", chart_name)
            errors.append(chart_name)
            return fallback

    # ---------------------------------------------------------------------
    # JSON-RPC endpoint
    # ---------------------------------------------------------------------

    @http.route(
        "/my/business-dashboard/charts/data",
        type="jsonrpc",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def portal_business_dashboard_chart_data(self, period=None, **kwargs):
        settings = self._dashboard_settings()
        period = self._normalise_chart_period(period)
        role = settings["role"]
        errors = []

        buckets = self._chart_buckets(period)
        empty_labels = [bucket["label"] for bucket in buckets]
        customer_side = self._chart_document_side(role) != "vendor"
        ordered_label = _("Ordered") if customer_side else _("Purchased")
        invoiced_label = _("Invoiced") if customer_side else _("Billed")

        trend_fallback = {
            "visible": False,
            "labels": empty_labels,
            "series": [
                {"key": "ordered", "label": ordered_label, "values": [0.0] * len(empty_labels), "url": "/my/orders"},
                {"key": "invoiced", "label": invoiced_label, "values": [0.0] * len(empty_labels), "url": "/my/invoices"},
                {"key": "paid", "label": _("Paid"), "values": [0.0] * len(empty_labels), "url": "/my/invoices"},
            ],
            "totals": {"ordered": 0.0, "invoiced": 0.0, "paid": 0.0},
            "summary_labels": {
                "ordered": ordered_label,
                "invoiced": invoiced_label,
                "paid": _("Paid"),
            },
        }
        invoice_status_fallback = {
            "visible": False,
            "title": _("Invoice payment status"),
            "items": [],
        }
        aging_fallback = {
            "visible": False,
            "items": [],
            "total": 0.0,
            "overdue": 0.0,
            "overdue_count": 0,
        }
        delivery_fallback = {
            "visible": False,
            "labels": empty_labels,
            "series": [
                {"key": "on_time", "label": _("On time"), "values": [0] * len(empty_labels)},
                {"key": "delayed", "label": _("Delayed"), "values": [0] * len(empty_labels)},
            ],
            "on_time_rate": 0,
            "url": "/my/business-deliveries",
        }

        trend = self._safe_chart_result(
            "trend",
            lambda: self._business_trend_chart(period, role),
            trend_fallback,
            errors,
        )
        invoice_status = self._safe_chart_result(
            "invoice_status",
            lambda: self._invoice_status_chart(period, role),
            invoice_status_fallback,
            errors,
        )
        aging = self._safe_chart_result(
            "aging",
            lambda: self._aging_chart(role),
            aging_fallback,
            errors,
        )
        delivery = self._safe_chart_result(
            "delivery",
            lambda: self._delivery_performance_chart(
                period, role, settings["show_logistics"]
            ),
            delivery_fallback,
            errors,
        )
        projects = self._safe_chart_result(
            "projects",
            lambda: self._project_progress_chart(settings["show_projects"]),
            {"visible": False, "items": []},
            errors,
        )
        tasks = self._safe_chart_result(
            "tasks",
            lambda: self._task_status_chart(settings["show_tasks"]),
            {"visible": False, "items": []},
            errors,
        )

        return {
            "period": period,
            "period_label": self._chart_period_labels[period],
            "currency": self._chart_currency_data(),
            "partial_failure": bool(errors),
            "failed_charts": errors,
            "summary": {
                "ordered": {
                    "label": trend["summary_labels"]["ordered"],
                    "value": trend["totals"]["ordered"],
                },
                "invoiced": {
                    "label": trend["summary_labels"]["invoiced"],
                    "value": trend["totals"]["invoiced"],
                },
                "paid": {
                    "label": trend["summary_labels"]["paid"],
                    "value": trend["totals"]["paid"],
                },
                "outstanding": {
                    "label": _("Outstanding"),
                    "value": aging["total"],
                },
                "overdue": {
                    "label": _("Overdue"),
                    "value": aging["overdue"],
                    "count": aging["overdue_count"],
                },
            },
            "charts": {
                "trend": trend,
                "invoice_status": invoice_status,
                "aging": aging,
                "delivery": delivery,
                "projects": projects,
                "tasks": tasks,
            },
        }
