from datetime import datetime, time, timedelta
import csv
import io
from urllib.parse import urlencode
import pytz
from werkzeug.exceptions import NotFound
from dateutil.relativedelta import relativedelta

from odoo import _, fields, http
from odoo.http import request
from odoo.osv import expression
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager


class BusinessPortalDashboard(CustomerPortal):
    """Website portal dashboard.

    Standard Odoo portal routes are reused for Sales, Purchase, Accounting,
    Projects and Tasks. Stock pickings use custom routes because standard Stock
    does not expose a customer delivery portal page.
    """

    _period_labels = {
        "7d": _("Last 7 days"),
        "30d": _("Last 30 days"),
        "90d": _("Last 90 days"),
        "month": _("This month"),
        "year": _("This year"),
        "all": _("All time"),
    }

    _delivery_state_labels = {
        "draft": _("Preparing"),
        "waiting": _("Waiting for another operation"),
        "confirmed": _("Waiting for stock"),
        "assigned": _("Ready"),
        "done": _("Completed"),
        "cancel": _("Cancelled"),
    }

    # -------------------------------------------------------------------------
    # Common helpers
    # -------------------------------------------------------------------------

    def _commercial_partner(self):
        return request.env.user.partner_id.commercial_partner_id

    def _dashboard_settings(self):
        # Reading configuration from the authenticated user's own commercial
        # partner is safe. sudo is used only because fields are configured by
        # administrators and portal users may not have field-level visibility.
        partner = self._commercial_partner().sudo()
        role = partner.portal_dashboard_role or "auto"
        if role == "auto":
            is_customer = bool(partner.customer_rank)
            is_vendor = bool(partner.supplier_rank)
            if is_customer and is_vendor:
                role = "both"
            elif is_vendor:
                role = "vendor"
            else:
                # Customer is a sensible default for a new portal contact.
                role = "customer"

        return {
            "role": role,
            "show_logistics": partner.portal_dashboard_show_logistics,
            "show_projects": partner.portal_dashboard_show_projects,
            "show_tasks": partner.portal_dashboard_show_tasks,
            "show_empty_cards": partner.portal_dashboard_show_empty_cards,
            "default_period": partner.portal_dashboard_default_period or "30d",
        }

    def _normalise_period(self, period, default_period="30d"):
        return period if period in self._period_labels else default_period

    def _period_dates(self, period):
        today = fields.Date.context_today(request.env.user)
        if period == "7d":
            return today - timedelta(days=6), today
        if period == "30d":
            return today - timedelta(days=29), today
        if period == "90d":
            return today - timedelta(days=89), today
        if period == "month":
            return today.replace(day=1), today
        if period == "year":
            return today.replace(month=1, day=1), today
        return None, None

    def _period_domain(self, period, field_name="create_date", is_datetime=True):
        date_begin, date_end = self._period_dates(period)
        if not date_begin:
            return []
        if is_datetime:
            begin = datetime.combine(date_begin, time.min)
            end = datetime.combine(date_end + timedelta(days=1), time.min)
            return [
                (field_name, ">=", fields.Datetime.to_string(begin)),
                (field_name, "<", fields.Datetime.to_string(end)),
            ]
        return [
            (field_name, ">=", fields.Date.to_string(date_begin)),
            (field_name, "<=", fields.Date.to_string(date_end)),
        ]

    def _standard_portal_url(self, base_url, period, **params):
        date_begin, date_end = self._period_dates(period)
        query = dict(params)
        if date_begin and date_end:
            query.update(
                date_begin=fields.Date.to_string(date_begin),
                date_end=fields.Date.to_string(date_end),
            )
        return f"{base_url}?{urlencode(query)}" if query else base_url

    def _model_can_read(self, model_name):
        return request.env[model_name].has_access("read")

    def _partner_domain(self):
        partner = self._commercial_partner()
        return [("partner_id", "child_of", [partner.id])]

    def _company_domain(self):
        return [("company_id", "=", request.env.company.id)]

    def _visible_card(self, card, show_empty):
        if not card.get("visible"):
            return False
        if show_empty:
            return True
        return any(metric.get("count", 0) for metric in card.get("metrics", []))


    def _company_currency(self):
        return request.env.company.currency_id

    def _sum_company_currency(
        self,
        records,
        amount_field,
        currency_field="currency_id",
        date_field=None,
    ):
        """Return a safe total in the current company's currency.

        The dashboard can contain documents in different currencies.  Each
        document is converted before totals are added so the displayed amount
        remains meaningful.
        """
        company = request.env.company
        company_currency = company.currency_id
        today = fields.Date.context_today(request.env.user)
        total = 0.0

        for record in records:
            amount = record[amount_field] or 0.0
            currency = record[currency_field] or company_currency
            conversion_date = today

            if date_field and record[date_field]:
                raw_date = record[date_field]
                conversion_date = raw_date.date() if isinstance(raw_date, datetime) else raw_date

            total += currency._convert(
                amount,
                company_currency,
                company,
                conversion_date,
            )

        return company_currency.round(total)

    def _safe_portal_url(self, record, fallback_url):
        get_portal_url = getattr(record, "get_portal_url", None)
        if callable(get_portal_url):
            try:
                return get_portal_url()
            except Exception:
                # The fallback always remains inside the authenticated portal.
                pass
        return fallback_url

    def _picking_progress_data(self, picking):
        moves = picking.move_ids.filtered(lambda move: move.state != "cancel")
        planned_qty = sum(moves.mapped("product_uom_qty"))
        processed_qty = sum(moves.mapped("quantity"))

        if picking.state == "done":
            progress = 100
        elif planned_qty:
            progress = min(100, max(0, round((processed_qty / planned_qty) * 100)))
        else:
            progress = 0

        now = fields.Datetime.now()
        late_days = 0
        if (
            picking.scheduled_date
            and picking.state not in ("done", "cancel")
            and picking.scheduled_date < now
        ):
            late_days = max(1, (now - picking.scheduled_date).days)

        outgoing = picking.picking_type_code == "outgoing"
        step_labels = (
            [
                _("Order confirmed"),
                _("Preparing"),
                _("Ready to ship"),
                _("Delivered"),
            ]
            if outgoing
            else [
                _("Order confirmed"),
                _("In preparation"),
                _("Ready to receive"),
                _("Received"),
            ]
        )

        state_index = {
            "draft": 0,
            "waiting": 1,
            "confirmed": 1,
            "assigned": 2,
            "done": 3,
            "cancel": -1,
        }.get(picking.state, 1)

        steps = []
        for index, label in enumerate(step_labels):
            steps.append(
                {
                    "label": label,
                    "done": picking.state == "done" or state_index > index,
                    "current": picking.state != "done" and state_index == index,
                }
            )

        return {
            "planned_qty": planned_qty,
            "processed_qty": processed_qty,
            "progress": progress,
            "late_days": late_days,
            "steps": steps,
        }

    def _activity_date_bounds(self, activity_date=None):
        """Return selected portal date and its UTC datetime domain.

        The selected date is interpreted using the logged-in user's timezone.
        Odoo datetime fields are stored in UTC, so local midnight boundaries
        are converted back to UTC before creating the domain.
        """

        today = fields.Date.context_today(request.env.user)

        try:
            selected_date = (
                fields.Date.to_date(activity_date)
                if activity_date
                else today
            )
        except (TypeError, ValueError):
            selected_date = today

        user_timezone = pytz.timezone(
            request.env.user.tz or "UTC"
        )

        local_start = user_timezone.localize(
            datetime.combine(selected_date, time.min),
            is_dst=False,
        )

        local_end = local_start + timedelta(days=1)

        utc_start = (
            local_start
            .astimezone(pytz.UTC)
            .replace(tzinfo=None)
        )

        utc_end = (
            local_end
            .astimezone(pytz.UTC)
            .replace(tzinfo=None)
        )

        domain = [
            (
                "write_date",
                ">=",
                fields.Datetime.to_string(utc_start),
            ),
            (
                "write_date",
                "<",
                fields.Datetime.to_string(utc_end),
            ),
        ]

        return selected_date, domain

    def _sale_activity_status(self, order):
        labels = {
            "draft": _("Draft quotation"),
            "sent": _("Quotation sent"),
            "sale": _("Confirmed"),
            "cancel": _("Cancelled"),
        }

        return labels.get(
            order.state,
            _("Updated"),
        )

    def _purchase_activity_status(self, order):
        labels = {
            "draft": _("Draft RFQ"),
            "sent": _("RFQ sent"),
            "to approve": _("Waiting approval"),
            "purchase": _("Confirmed"),
            "done": _("Locked"),
            "cancel": _("Cancelled"),
        }

        return labels.get(
            order.state,
            _("Updated"),
        )

    def _invoice_activity_status(self, invoice):
        if invoice.state == "cancel":
            return _("Cancelled")

        if invoice.payment_state == "paid":
            return _("Paid")

        if invoice.payment_state == "partial":
            return _("Partially paid")

        if invoice.payment_state == "in_payment":
            return _("Payment processing")

        if invoice.state == "posted":
            return _("Posted")

        return _("Updated")

    def _transfer_activity_status(self, picking):
        labels = {
            "draft": _("Preparing"),
            "waiting": _("Waiting"),
            "confirmed": _("Waiting for stock"),
            "assigned": _("Ready"),
            "done": _("Completed"),
            "cancel": _("Cancelled"),
        }

        return labels.get(
            picking.state,
            _("Updated"),
        )

    def _activity_category_info(self, category, role, show_logistics):
        """Return metadata for one visible activity category."""
        definitions = {
            "sales": {
                "key": "sales",
                "title": _("Sales"),
                "icon": "fa-shopping-cart",
                "tone": "sales",
                "document_url": "/my/orders",
                "visible": (
                    role in ("customer", "both")
                    and self._model_can_read("sale.order")
                ),
            },
            "purchase": {
                "key": "purchase",
                "title": _("Purchase"),
                "icon": "fa-shopping-bag",
                "tone": "purchase",
                "document_url": "/my/purchase",
                "visible": (
                    role in ("vendor", "both")
                    and self._model_can_read("purchase.order")
                ),
            },
            "billing": {
                "key": "billing",
                "title": _("Billing"),
                "icon": "fa-file-text-o",
                "tone": "billing",
                "document_url": "/my/invoices",
                "visible": self._model_can_read("account.move"),
            },
            "logistics": {
                "key": "logistics",
                "title": _("Logistics"),
                "icon": "fa-truck",
                "tone": "logistics",
                "document_url": "/my/business-deliveries",
                "visible": bool(
                    self._allowed_delivery_directions(
                        role,
                        show_logistics,
                    )
                ),
            },
        }
        info = definitions.get(category)
        return info if info and info["visible"] else None

    def _available_activity_categories(self, role, show_logistics):
        """Return activity categories available to the logged-in portal user."""
        categories = []
        for category in ("sales", "purchase", "billing", "logistics"):
            info = self._activity_category_info(
                category,
                role,
                show_logistics,
            )
            if info:
                categories.append(info)
        return categories

    def _activity_all_url(self, category, selected_date, **params):
        query = {
            "category": category,
            "activity_date": fields.Date.to_string(selected_date),
        }
        query.update({key: value for key, value in params.items() if value})
        return f"/my/business-activity?{urlencode(query)}"

    def _activity_search_domain(self, category, search):
        if not search:
            return []
        if category == "sales":
            return [
                "|",
                ("name", "ilike", search),
                ("client_order_ref", "ilike", search),
            ]
        if category == "purchase":
            return [
                "|",
                ("name", "ilike", search),
                ("partner_ref", "ilike", search),
            ]
        if category == "billing":
            return [
                "|",
                "|",
                ("name", "ilike", search),
                ("ref", "ilike", search),
                ("payment_reference", "ilike", search),
            ]
        if category == "logistics":
            return [
                "|",
                ("name", "ilike", search),
                ("origin", "ilike", search),
            ]
        return []

    def _activity_group(
        self,
        category,
        selected_date,
        date_domain,
        role,
        show_logistics,
        limit=3,
        offset=0,
        search=None,
    ):
        """Build one category group for dashboard or View All page.

        Activity is based on ``write_date``. The status shown is the record's
        current status; this is not a full historical state-transition log.
        """
        info = self._activity_category_info(
            category,
            role,
            show_logistics,
        )
        if not info:
            return None

        search = (search or "").strip()[:100]
        records = request.env["res.partner"].browse()
        domain = []

        if category == "sales":
            Model = request.env["sale.order"]
            domain = (
                self._partner_domain()
                + self._company_domain()
                + date_domain
                + [("state", "in", ("sent", "sale", "cancel"))]
            )
        elif category == "purchase":
            Model = request.env["purchase.order"]
            domain = (
                self._partner_domain()
                + self._company_domain()
                + date_domain
                + [
                    (
                        "state",
                        "in",
                        ("sent", "to approve", "purchase", "done", "cancel"),
                    )
                ]
            )
        elif category == "billing":
            Model = request.env["account.move"]
            if role == "vendor":
                move_types = ("in_invoice", "in_refund", "in_receipt")
            elif role == "customer":
                move_types = ("out_invoice", "out_refund", "out_receipt")
            else:
                move_types = (
                    "out_invoice",
                    "out_refund",
                    "out_receipt",
                    "in_invoice",
                    "in_refund",
                    "in_receipt",
                )
            domain = (
                self._partner_domain()
                + self._company_domain()
                + date_domain
                + [
                    ("move_type", "in", move_types),
                    ("state", "in", ("posted", "cancel")),
                ]
            )
        else:
            Model = request.env["stock.picking"].sudo()
            allowed_directions = self._allowed_delivery_directions(
                role,
                show_logistics,
            )
            direction_domain = expression.OR(
                [
                    self._delivery_domain(direction, "all")
                    for direction in allowed_directions
                ]
            )
            domain = expression.AND([direction_domain, date_domain])

        search_domain = self._activity_search_domain(category, search)
        if search_domain:
            domain = expression.AND([domain, search_domain])

        total = Model.search_count(domain)
        records = Model.search(
            domain,
            order="write_date desc, id desc",
            limit=limit,
            offset=offset,
        )

        items = []
        for record in records:
            if category == "sales":
                item = {
                    "key": f"sales-{record.id}",
                    "title": record.name,
                    "status": self._sale_activity_status(record),
                    "description": _("Sales document updated"),
                    "date": record.write_date,
                    "url": self._safe_portal_url(record, "/my/orders"),
                }
            elif category == "purchase":
                item = {
                    "key": f"purchase-{record.id}",
                    "title": record.name,
                    "status": self._purchase_activity_status(record),
                    "description": _("Purchase document updated"),
                    "date": record.write_date,
                    "url": self._safe_portal_url(record, "/my/purchase"),
                }
            elif category == "billing":
                item = {
                    "key": f"billing-{record.id}",
                    "title": record.name or record.display_name,
                    "status": self._invoice_activity_status(record),
                    "description": _("Invoice or bill updated"),
                    "date": record.write_date,
                    "url": self._safe_portal_url(record, "/my/invoices"),
                }
            else:
                transfer_type = (
                    _("Delivery")
                    if record.picking_type_code == "outgoing"
                    else _("Receipt")
                )
                item = {
                    "key": f"logistics-{record.id}",
                    "title": record.name,
                    "status": self._transfer_activity_status(record),
                    "description": _(
                        "%(type)s updated",
                        type=transfer_type,
                    ),
                    "date": record.write_date,
                    "url": f"/my/business-deliveries/{record.id}",
                }
            items.append(item)

        return {
            **info,
            "items": items,
            "total": total,
            "all_url": self._activity_all_url(
                category,
                selected_date,
            ),
        }

    def _datewise_recent_activity(
        self,
        activity_date,
        role,
        show_logistics,
        activity_category="all",
    ):
        """Return date-wise dashboard groups and the active category filter."""
        selected_date, date_domain = self._activity_date_bounds(activity_date)
        available_categories = self._available_activity_categories(
            role,
            show_logistics,
        )

        allowed_keys = {category["key"] for category in available_categories}
        if activity_category != "all" and activity_category not in allowed_keys:
            activity_category = "all"

        category_keys = (
            [category["key"] for category in available_categories]
            if activity_category == "all"
            else [activity_category]
        )

        groups = []
        for category in category_keys:
            group = self._activity_group(
                category=category,
                selected_date=selected_date,
                date_domain=date_domain,
                role=role,
                show_logistics=show_logistics,
                limit=3,
            )
            if group:
                groups.append(group)

        return (
            selected_date,
            groups,
            available_categories,
            activity_category,
        )


    # -------------------------------------------------------------------------
    # Account statement and aging helpers
    # -------------------------------------------------------------------------

    def _allowed_statement_types(self, role):
        types = []
        if role in ("customer", "both"):
            types.append(
                {
                    "key": "customer",
                    "label": _("Customer statement"),
                    "account_type": "asset_receivable",
                }
            )
        if role in ("vendor", "both"):
            types.append(
                {
                    "key": "vendor",
                    "label": _("Vendor statement"),
                    "account_type": "liability_payable",
                }
            )
        return types

    def _normalise_statement_type(self, statement_type, role):
        allowed = self._allowed_statement_types(role)
        if not allowed:
            return None, []
        allowed_keys = {item["key"] for item in allowed}
        if statement_type not in allowed_keys:
            statement_type = allowed[0]["key"]
        return statement_type, allowed

    def _statement_dates(self, date_from=None, date_to=None):
        today = fields.Date.context_today(request.env.user)
        default_from = today.replace(month=1, day=1)

        try:
            parsed_from = fields.Date.to_date(date_from) if date_from else default_from
        except (TypeError, ValueError):
            parsed_from = default_from

        try:
            parsed_to = fields.Date.to_date(date_to) if date_to else today
        except (TypeError, ValueError):
            parsed_to = today

        parsed_to = min(parsed_to, today)
        if parsed_from > parsed_to:
            parsed_from, parsed_to = parsed_to, parsed_from

        return parsed_from, parsed_to

    def _statement_line_domain(self, statement_type):
        partner = self._commercial_partner()
        account_type = (
            "asset_receivable"
            if statement_type == "customer"
            else "liability_payable"
        )
        return [
            ("partner_id", "child_of", [partner.id]),
            ("company_id", "=", request.env.company.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", account_type),
        ]

    def _statement_multiplier(self, statement_type):
        # Receivable balances are positive; payable balances are normally
        # negative. Normalise both statement types so an amount due is positive.
        return 1 if statement_type == "customer" else -1

    def _statement_group_sums(self, model, domain):
        grouped = model.read_group(
            domain,
            ["debit:sum", "credit:sum", "balance:sum"],
            [],
        )
        if not grouped:
            return {"debit": 0.0, "credit": 0.0, "balance": 0.0}
        row = grouped[0]
        return {
            "debit": row.get("debit", 0.0) or 0.0,
            "credit": row.get("credit", 0.0) or 0.0,
            "balance": row.get("balance", 0.0) or 0.0,
        }

    def _statement_status(self, line):
        move = line.move_id
        if move.move_type == "entry":
            if line.reconciled:
                return _("Settled")
            if line.payment_id:
                return _("Payment")
            return _("Open")

        if move.state == "cancel":
            return _("Cancelled")

        labels = {
            "paid": _("Paid"),
            "partial": _("Partially paid"),
            "in_payment": _("In payment"),
            "not_paid": _("Unpaid"),
            "reversed": _("Reversed"),
        }
        return labels.get(move.payment_state, _("Posted"))

    def _statement_document_type(self, line, statement_type):
        move_type = line.move_id.move_type
        labels = {
            "out_invoice": _("Invoice"),
            "out_refund": _("Credit note"),
            "out_receipt": _("Receipt"),
            "in_invoice": _("Vendor bill"),
            "in_refund": _("Vendor credit"),
            "in_receipt": _("Purchase receipt"),
        }
        if move_type in labels:
            return labels[move_type]
        if line.payment_id:
            return _("Customer payment") if statement_type == "customer" else _("Vendor payment")
        return _("Journal entry")

    def _statement_row(self, line, statement_type, running_balance):
        multiplier = self._statement_multiplier(statement_type)
        if statement_type == "customer":
            charge = line.debit
            payment = line.credit
        else:
            charge = line.credit
            payment = line.debit

        move = line.move_id
        url = False
        if move.move_type != "entry":
            url = self._safe_portal_url(move, "/my/invoices")

        description = (
            line.name
            if line.name and line.name != "/"
            else move.ref or move.name or line.journal_id.display_name
        )

        return {
            "id": line.id,
            "date": line.date,
            "due_date": line.date_maturity,
            "reference": move.name or move.ref or _("Journal entry"),
            "description": description,
            "document_type": self._statement_document_type(line, statement_type),
            "status": self._statement_status(line),
            "charge": charge,
            "payment": payment,
            "net": line.balance * multiplier,
            "running_balance": running_balance,
            "url": url,
        }

    def _statement_aging(self, statement_type):
        partner = self._commercial_partner()
        today = fields.Date.context_today(request.env.user)
        currency = self._company_currency()
        multiplier = self._statement_multiplier(statement_type)

        move_types = (
            ("out_invoice", "out_refund", "out_receipt")
            if statement_type == "customer"
            else ("in_invoice", "in_refund", "in_receipt")
        )
        domain = [
            ("partner_id", "child_of", [partner.id]),
            ("company_id", "=", request.env.company.id),
            ("state", "=", "posted"),
            ("move_type", "in", move_types),
            ("payment_state", "not in", ("paid", "reversed")),
        ]
        moves = request.env["account.move"].sudo().search(domain)

        buckets = {
            "not_due": {
                "key": "not_due",
                "label": _("Not due"),
                "amount": 0.0,
                "count": 0,
            },
            "1_30": {
                "key": "1_30",
                "label": _("1–30 days"),
                "amount": 0.0,
                "count": 0,
            },
            "31_60": {
                "key": "31_60",
                "label": _("31–60 days"),
                "amount": 0.0,
                "count": 0,
            },
            "61_90": {
                "key": "61_90",
                "label": _("61–90 days"),
                "amount": 0.0,
                "count": 0,
            },
            "90_plus": {
                "key": "90_plus",
                "label": _("Over 90 days"),
                "amount": 0.0,
                "count": 0,
            },
        }

        for move in moves:
            amount = (move.amount_residual_signed or 0.0) * multiplier
            if currency.is_zero(amount):
                continue

            due_date = move.invoice_date_due or move.invoice_date or today
            overdue_days = (today - due_date).days
            if overdue_days <= 0:
                key = "not_due"
            elif overdue_days <= 30:
                key = "1_30"
            elif overdue_days <= 60:
                key = "31_60"
            elif overdue_days <= 90:
                key = "61_90"
            else:
                key = "90_plus"

            buckets[key]["amount"] += amount
            buckets[key]["count"] += 1

        ordered = [
            buckets["not_due"],
            buckets["1_30"],
            buckets["31_60"],
            buckets["61_90"],
            buckets["90_plus"],
        ]
        for bucket in ordered:
            bucket["amount"] = currency.round(bucket["amount"])

        return {
            "buckets": ordered,
            "total": currency.round(sum(bucket["amount"] for bucket in ordered)),
            "date": today,
        }

    def _statement_data(self, statement_type, date_from, date_to, page=1):
        Line = request.env["account.move.line"].sudo()
        currency = self._company_currency()
        multiplier = self._statement_multiplier(statement_type)
        base_domain = self._statement_line_domain(statement_type)

        opening_domain = base_domain + [("date", "<", fields.Date.to_string(date_from))]
        period_domain = base_domain + [
            ("date", ">=", fields.Date.to_string(date_from)),
            ("date", "<=", fields.Date.to_string(date_to)),
        ]

        opening_sums = self._statement_group_sums(Line, opening_domain)
        period_sums = self._statement_group_sums(Line, period_domain)
        opening_balance = currency.round(opening_sums["balance"] * multiplier)

        if statement_type == "customer":
            period_charges = period_sums["debit"]
            period_payments = period_sums["credit"]
        else:
            period_charges = period_sums["credit"]
            period_payments = period_sums["debit"]

        period_charges = currency.round(period_charges)
        period_payments = currency.round(period_payments)
        closing_balance = currency.round(
            opening_balance + (period_sums["balance"] * multiplier)
        )

        total = Line.search_count(period_domain)
        pager = portal_pager(
            url="/my/account-statement",
            url_args={
                "statement_type": statement_type,
                "date_from": fields.Date.to_string(date_from),
                "date_to": fields.Date.to_string(date_to),
            },
            total=total,
            page=page,
            step=self._items_per_page,
        )

        order = "date asc, id asc"
        prior_balance = 0.0
        if pager["offset"]:
            previous_lines = Line.search(
                period_domain,
                order=order,
                limit=pager["offset"],
            )
            prior_balance = sum(previous_lines.mapped("balance")) * multiplier

        running_balance = currency.round(opening_balance + prior_balance)
        lines = Line.search(
            period_domain,
            order=order,
            limit=self._items_per_page,
            offset=pager["offset"],
        )

        rows = []
        for line in lines:
            running_balance = currency.round(
                running_balance + (line.balance * multiplier)
            )
            rows.append(
                self._statement_row(
                    line,
                    statement_type,
                    running_balance,
                )
            )

        return {
            "rows": rows,
            "pager": pager,
            "total": total,
            "currency": currency,
            "opening_balance": opening_balance,
            "period_charges": period_charges,
            "period_payments": period_payments,
            "closing_balance": closing_balance,
            "aging": self._statement_aging(statement_type),
        }

    # -------------------------------------------------------------------------
    # Dashboard card builders
    # -------------------------------------------------------------------------

    def _sales_card(self, period, role):
        visible = role in ("customer", "both") and self._model_can_read("sale.order")
        card = {
            "key": "sales",
            "title": _("Sale Orders"),
            "subtitle": _("Orders and quotations"),
            "icon": "fa-line-chart",
            "visible": visible,
            "url": self._standard_portal_url("/my/orders", period),
            "metrics": [],
            "summary": [],
        }
        if not visible:
            return card

        SaleOrder = request.env["sale.order"]
        base = self._partner_domain() + self._company_domain() + self._period_domain(period)
        confirmed_domain = base + [("state", "=", "sale")]
        quotation_domain = base + [("state", "=", "sent")]
        to_invoice_domain = confirmed_domain + [("invoice_status", "=", "to invoice")]

        confirmed_orders = SaleOrder.search(confirmed_domain)
        quotations = SaleOrder.search_count(quotation_domain)
        to_invoice_orders = SaleOrder.search(to_invoice_domain)

        card["metrics"] = [
            {
                "label": _("Confirmed orders"),
                "count": len(confirmed_orders),
                "url": self._standard_portal_url("/my/orders", period),
            },
            {
                "label": _("Sent quotations"),
                "count": quotations,
                "url": self._standard_portal_url("/my/quotes", period),
            },
            {
                "label": _("Orders to invoice"),
                "count": len(to_invoice_orders),
                "url": self._standard_portal_url("/my/orders", period),
            },
        ]
        card["summary"] = [
            {
                "label": _("Order value"),
                "value": self._sum_company_currency(
                    confirmed_orders,
                    "amount_total",
                    date_field="date_order",
                ),
                "currency": self._company_currency(),
            },
            {
                "label": _("To invoice value"),
                "value": self._sum_company_currency(
                    to_invoice_orders,
                    "amount_total",
                    date_field="date_order",
                ),
                "currency": self._company_currency(),
            },
        ]
        return card


    def _purchase_card(self, period, role):
        visible = role in ("vendor", "both") and self._model_can_read("purchase.order")
        card = {
            "key": "purchase",
            "title": _("Purchase Orders"),
            "subtitle": _("RFQs and purchase orders"),
            "icon": "fa-shopping-bag",
            "visible": visible,
            "url": self._standard_portal_url("/my/purchase", period),
            "metrics": [],
            "summary": [],
        }
        if not visible:
            return card

        PurchaseOrder = request.env["purchase.order"]
        base = self._partner_domain() + self._company_domain() + self._period_domain(period)
        purchase_domain = base + [("state", "=", "purchase")]
        rfq_domain = base + [("state", "=", "sent")]
        cancel_domain = base + [("state", "=", "cancel")]

        purchase_orders = PurchaseOrder.search(purchase_domain)
        rfqs = PurchaseOrder.search_count(rfq_domain)
        cancelled = PurchaseOrder.search_count(cancel_domain)

        card["metrics"] = [
            {
                "label": _("Purchase orders"),
                "count": len(purchase_orders),
                "url": self._standard_portal_url("/my/purchase", period, filterby="purchase"),
            },
            {
                "label": _("Sent RFQs"),
                "count": rfqs,
                "url": self._standard_portal_url("/my/rfq", period),
            },
            {
                "label": _("Cancelled orders"),
                "count": cancelled,
                "url": self._standard_portal_url("/my/purchase", period, filterby="cancel"),
            },
        ]
        card["summary"] = [
            {
                "label": _("Purchase value"),
                "value": self._sum_company_currency(
                    purchase_orders,
                    "amount_total",
                    date_field="date_order",
                ),
                "currency": self._company_currency(),
            },
        ]
        return card


    def _accounting_card(self, period, role):
        visible = self._model_can_read("account.move")
        card = {
            "key": "accounting",
            "title": _("Bills & Invoices"),
            "subtitle": _("Invoices, bills and due documents"),
            "icon": "fa-file-text-o",
            "visible": visible,
            "url": self._standard_portal_url("/my/invoices", period),
            "metrics": [],
            "summary": [],
            "statement_url": "/my/account-statement",
        }
        if not visible:
            return card

        Move = request.env["account.move"]
        base = (
            self._partner_domain()
            + self._company_domain()
            + self._period_domain(period)
            + [("state", "=", "posted")]
        )
        customer_domain = base + [
            ("move_type", "in", ("out_invoice", "out_refund", "out_receipt"))
        ]
        vendor_domain = base + [
            ("move_type", "in", ("in_invoice", "in_refund", "in_receipt"))
        ]
        unpaid_customer_domain = customer_domain + [
            ("payment_state", "in", ("not_paid", "partial", "in_payment"))
        ]
        unpaid_vendor_domain = vendor_domain + [
            ("payment_state", "in", ("not_paid", "partial", "in_payment"))
        ]
        overdue_domain = customer_domain + [
            ("payment_state", "not in", ("paid", "reversed", "in_payment", "blocked")),
            ("invoice_date_due", "<", fields.Date.context_today(request.env.user)),
        ]

        customer_count = Move.search_count(customer_domain)
        vendor_count = Move.search_count(vendor_domain)
        unpaid_customer = Move.search(unpaid_customer_domain)
        unpaid_vendor = Move.search(unpaid_vendor_domain)
        overdue_invoices = Move.search(overdue_domain)

        customer_outstanding = self._sum_company_currency(
            unpaid_customer,
            "amount_residual",
            date_field="invoice_date",
        )
        vendor_outstanding = self._sum_company_currency(
            unpaid_vendor,
            "amount_residual",
            date_field="invoice_date",
        )
        overdue_amount = self._sum_company_currency(
            overdue_invoices,
            "amount_residual",
            date_field="invoice_date_due",
        )

        if role == "vendor":
            card["metrics"] = [
                {
                    "label": _("Vendor bills"),
                    "count": vendor_count,
                    "url": self._standard_portal_url("/my/invoices", period, filterby="bills"),
                },
                {
                    "label": _("Unpaid bills"),
                    "count": len(unpaid_vendor),
                    "url": self._standard_portal_url("/my/invoices", period, filterby="bills"),
                },
            ]
            card["summary"] = [
                {
                    "label": _("Bills outstanding"),
                    "value": vendor_outstanding,
                    "currency": self._company_currency(),
                },
            ]
        elif role == "both":
            card["metrics"] = [
                {
                    "label": _("Customer invoices"),
                    "count": customer_count,
                    "url": self._standard_portal_url("/my/invoices", period, filterby="invoices"),
                },
                {
                    "label": _("Vendor bills"),
                    "count": vendor_count,
                    "url": self._standard_portal_url("/my/invoices", period, filterby="bills"),
                },
                {
                    "label": _("Overdue invoices"),
                    "count": len(overdue_invoices),
                    "url": self._standard_portal_url(
                        "/my/invoices", period, filterby="overdue_invoices"
                    ),
                    "danger": bool(overdue_invoices),
                },
            ]
            card["summary"] = [
                {
                    "label": _("Customer outstanding"),
                    "value": customer_outstanding,
                    "currency": self._company_currency(),
                },
                {
                    "label": _("Vendor outstanding"),
                    "value": vendor_outstanding,
                    "currency": self._company_currency(),
                },
            ]
        else:
            card["metrics"] = [
                {
                    "label": _("Customer invoices"),
                    "count": customer_count,
                    "url": self._standard_portal_url("/my/invoices", period, filterby="invoices"),
                },
                {
                    "label": _("Unpaid invoices"),
                    "count": len(unpaid_customer),
                    "url": self._standard_portal_url("/my/invoices", period, filterby="invoices"),
                },
                {
                    "label": _("Overdue invoices"),
                    "count": len(overdue_invoices),
                    "url": self._standard_portal_url(
                        "/my/invoices", period, filterby="overdue_invoices"
                    ),
                    "danger": bool(overdue_invoices),
                },
            ]
            card["summary"] = [
                {
                    "label": _("Outstanding balance"),
                    "value": customer_outstanding,
                    "currency": self._company_currency(),
                },
                {
                    "label": _("Overdue amount"),
                    "value": overdue_amount,
                    "currency": self._company_currency(),
                    "danger": bool(overdue_amount),
                },
            ]
        return card


    def _allowed_delivery_directions(self, role, show_logistics=True):
        if not show_logistics:
            return []
        directions = []
        if role in ("customer", "both"):
            directions.append("outgoing")
        if role in ("vendor", "both"):
            directions.append("incoming")
        return directions

    def _delivery_domain(self, direction, period="all"):
        partner = self._commercial_partner()
        common = self._company_domain() + self._period_domain(period)

        outgoing_domain = common + [
            ("picking_type_code", "=", "outgoing"),
            ("sale_id.partner_id", "child_of", [partner.id]),
        ]
        incoming_domain = common + [
            ("picking_type_code", "=", "incoming"),
            ("purchase_id.partner_id", "child_of", [partner.id]),
        ]

        if direction == "outgoing":
            return outgoing_domain
        if direction == "incoming":
            return incoming_domain
        if direction == "all":
            return expression.OR([outgoing_domain, incoming_domain])
        return expression.FALSE_DOMAIN

    def _logistics_card(self, period, role, show_logistics):
        directions = self._allowed_delivery_directions(role, show_logistics)
        card = {
            "key": "logistics",
            "title": _("Deliveries & Receipts"),
            "subtitle": _("Deliveries and receipts"),
            "icon": "fa-truck",
            "visible": bool(directions),
            "url": "/my/business-deliveries",
            "metrics": [],
        }
        if not directions:
            return card

        Picking = request.env["stock.picking"].sudo()
        now = fields.Datetime.now()

        def counts(direction):
            base = self._delivery_domain(direction, period)
            pending = Picking.search_count(base + [("state", "not in", ("done", "cancel"))])
            ready = Picking.search_count(base + [("state", "=", "assigned")])
            late = Picking.search_count(
                base
                + [
                    ("scheduled_date", "<", now),
                    ("state", "not in", ("done", "cancel")),
                ]
            )
            return pending, ready, late

        if directions == ["outgoing"]:
            pending, ready, late = counts("outgoing")
            card["url"] = f"/my/business-deliveries?direction=outgoing&period={period}"
            card["metrics"] = [
                {
                    "label": _("Pending deliveries"),
                    "count": pending,
                    "url": f"/my/business-deliveries?direction=outgoing&status=pending&period={period}",
                },
                {
                    "label": _("Ready to ship"),
                    "count": ready,
                    "url": f"/my/business-deliveries?direction=outgoing&status=ready&period={period}",
                },
                {
                    "label": _("Late deliveries"),
                    "count": late,
                    "url": f"/my/business-deliveries?direction=outgoing&status=late&period={period}",
                    "danger": bool(late),
                },
            ]
        elif directions == ["incoming"]:
            pending, ready, late = counts("incoming")
            card["url"] = f"/my/business-deliveries?direction=incoming&period={period}"
            card["metrics"] = [
                {
                    "label": _("Pending receipts"),
                    "count": pending,
                    "url": f"/my/business-deliveries?direction=incoming&status=pending&period={period}",
                },
                {
                    "label": _("Ready receipts"),
                    "count": ready,
                    "url": f"/my/business-deliveries?direction=incoming&status=ready&period={period}",
                },
                {
                    "label": _("Late receipts"),
                    "count": late,
                    "url": f"/my/business-deliveries?direction=incoming&status=late&period={period}",
                    "danger": bool(late),
                },
            ]
        else:
            out_pending, _out_ready, out_late = counts("outgoing")
            in_pending, _in_ready, in_late = counts("incoming")
            total_late = out_late + in_late

            card["url"] = f"/my/business-deliveries?direction=all&period={period}"
            card["metrics"] = [
                {
                    "label": _("Pending deliveries"),
                    "count": out_pending,
                    "url": f"/my/business-deliveries?direction=outgoing&status=pending&period={period}",
                },
                {
                    "label": _("Pending receipts"),
                    "count": in_pending,
                    "url": f"/my/business-deliveries?direction=incoming&status=pending&period={period}",
                },
                {
                    "label": _("Late transfers"),
                    "count": total_late,
                    "url": f"/my/business-deliveries?direction=all&status=late&period={period}",
                    "danger": bool(total_late),
                },
            ]
        return card

    def _projects_card(self, period, show_projects):
        visible = show_projects and self._model_can_read("project.project")
        card = {
            "key": "projects",
            "title": _("Projects"),
            "subtitle": _("Shared customer projects"),
            "icon": "fa-folder-open-o",
            "visible": visible,
            "url": self._standard_portal_url("/my/projects", period),
            "metrics": [],
        }
        if visible:
            Project = request.env["project.project"]
            base = (
                [("is_template", "=", False)]
                + self._period_domain(period)
                + ["|", ("company_id", "=", False), ("company_id", "=", request.env.company.id)]
            )
            active = Project.search_count(base + [("active", "=", True)])
            card["metrics"] = [
                {
                    "label": _("Active projects"),
                    "count": active,
                    "url": self._standard_portal_url("/my/projects", period),
                },
            ]
        return card

    def _tasks_card(self, period, show_tasks):
        visible = show_tasks and self._model_can_read("project.task")
        card = {
            "key": "tasks",
            "title": _("Tasks"),
            "subtitle": _("Work, deadlines and progress"),
            "icon": "fa-check-square-o",
            "visible": visible,
            "url": self._standard_portal_url("/my/tasks", period),
            "metrics": [],
        }
        if not visible:
            return card

        Task = request.env["project.task"]
        today = fields.Date.context_today(request.env.user)
        week_end = today + relativedelta(days=7)
        base = (
            [("project_id", "!=", False), ("is_template", "=", False)]
            + self._period_domain(period)
            + ["|", ("company_id", "=", False), ("company_id", "=", request.env.company.id)]
        )
        open_domain = base + [("stage_id.fold", "=", False)]
        open_tasks = Task.search_count(open_domain)
        due_soon = Task.search_count(
            open_domain
            + [
                ("date_deadline", ">=", fields.Date.to_string(today)),
                ("date_deadline", "<=", fields.Date.to_string(week_end)),
            ]
        )
        overdue = Task.search_count(
            open_domain + [("date_deadline", "<", fields.Date.to_string(today))]
        )
        card["metrics"] = [
            {
                "label": _("Open tasks"),
                "count": open_tasks,
                "url": self._standard_portal_url("/my/tasks", period),
            },
            {
                "label": _("Due in 7 days"),
                "count": due_soon,
                "url": self._standard_portal_url("/my/tasks", period),
            },
            {
                "label": _("Overdue tasks"),
                "count": overdue,
                "url": self._standard_portal_url("/my/tasks", period),
                "danger": bool(overdue),
            },
        ]
        return card

    def _dashboard_alerts(self, role, show_logistics, show_tasks=True):
        alerts = []
        today = fields.Date.context_today(request.env.user)

        if role in ("customer", "both") and self._model_can_read("account.move"):
            Move = request.env["account.move"]
            overdue = Move.search(
                self._partner_domain()
                + self._company_domain()
                + [
                    ("state", "=", "posted"),
                    ("move_type", "in", ("out_invoice", "out_receipt")),
                    ("payment_state", "not in", ("paid", "reversed", "in_payment", "blocked")),
                    ("invoice_date_due", "<", today),
                ],
                order="invoice_date_due asc",
                limit=4,
            )
            for invoice in overdue:
                days = (today - invoice.invoice_date_due).days if invoice.invoice_date_due else 0
                alerts.append(
                    {
                        "priority": 1,
                        "level": "danger",
                        "icon": "fa-exclamation-circle",
                        "title": _("Invoice overdue"),
                        "text": _(
                            "%(invoice)s is overdue by %(days)s day(s).",
                            invoice=invoice.name,
                            days=days,
                        ),
                        "url": invoice.get_portal_url(),
                    }
                )

        if role in ("customer", "both") and self._model_can_read("sale.order"):
            SaleOrder = request.env["sale.order"]
            expiring = SaleOrder.search(
                self._partner_domain()
                + self._company_domain()
                + [
                    ("state", "=", "sent"),
                    ("validity_date", ">=", today),
                    ("validity_date", "<=", today + timedelta(days=3)),
                ],
                order="validity_date asc",
                limit=3,
            )
            for order in expiring:
                alerts.append(
                    {
                        "priority": 2,
                        "level": "warning",
                        "icon": "fa-clock-o",
                        "title": _("Quotation expiring"),
                        "text": _(
                            "%(order)s expires on %(date)s.",
                            order=order.name,
                            date=order.validity_date,
                        ),
                        "url": order.get_portal_url(),
                    }
                )

        directions = self._allowed_delivery_directions(role, show_logistics)
        if directions:
            Picking = request.env["stock.picking"].sudo()
            direction_domain = expression.OR(
                [self._delivery_domain(direction, "all") for direction in directions]
            )
            late_pickings = Picking.search(
                expression.AND(
                    [
                        direction_domain,
                        [
                            ("scheduled_date", "<", fields.Datetime.now()),
                            ("state", "not in", ("done", "cancel")),
                        ],
                    ]
                ),
                order="scheduled_date asc",
                limit=4,
            )
            for picking in late_pickings:
                progress = self._picking_progress_data(picking)
                alerts.append(
                    {
                        "priority": 1,
                        "level": "danger" if progress["late_days"] > 3 else "warning",
                        "icon": "fa-truck",
                        "title": _("Transfer delayed"),
                        "text": _(
                            "%(transfer)s is late by %(days)s day(s).",
                            transfer=picking.name,
                            days=progress["late_days"],
                        ),
                        "url": f"/my/business-deliveries/{picking.id}",
                    }
                )

        if show_tasks and self._model_can_read("project.task"):
            Task = request.env["project.task"]
            overdue_tasks = Task.search(
                [
                    ("project_id", "!=", False),
                    ("is_template", "=", False),
                    ("stage_id.fold", "=", False),
                    ("date_deadline", "<", today),
                    "|",
                    ("company_id", "=", False),
                    ("company_id", "=", request.env.company.id),
                ],
                order="date_deadline asc",
                limit=3,
            )
            for task in overdue_tasks:
                alerts.append(
                    {
                        "priority": 1,
                        "level": "danger",
                        "icon": "fa-check-square-o",
                        "title": _("Task overdue"),
                        "text": _(
                            "%(task)s was due on %(date)s.",
                            task=task.name,
                            date=task.date_deadline,
                        ),
                        "url": self._safe_portal_url(task, "/my/tasks"),
                    }
                )

        alerts.sort(key=lambda alert: alert.get("priority", 99))
        return alerts[:6]



    def _prepare_home_portal_values(self, counters):
        """Add the same role-aware cards to Odoo's normal portal home.

        The normal portal counters are still prepared by Odoo.  Our template
        hides the duplicate standard document cards and renders these grouped
        business cards instead.
        """
        values = super()._prepare_home_portal_values(counters)
        settings = self._dashboard_settings()
        period = settings["default_period"]
        role = settings["role"]

        cards = [
            self._sales_card(period, role),
            self._purchase_card(period, role),
            self._accounting_card(period, role),
            self._logistics_card(period, role, settings["show_logistics"]),
            self._projects_card(period, settings["show_projects"]),
            self._tasks_card(period, settings["show_tasks"]),
        ]
        cards = [
            card
            for card in cards
            if self._visible_card(card, settings["show_empty_cards"])
        ]

        card_map = {card["key"]: card for card in cards}
        tiles = []

        sales = card_map.get("sales")
        if sales:
            quotation_url = next(
                (
                    metric["url"]
                    for metric in sales.get("metrics", [])
                    if "quotation" in str(metric.get("label", "")).lower()
                ),
                self._standard_portal_url("/my/quotes", period),
            )
            tiles.extend(
                [
                    {
                        "key": "quotations",
                        "title": _("Quotations to review"),
                        "icon": "fa-file-text-o",
                        "url": quotation_url,
                    },
                    {
                        "key": "sales",
                        "title": _("Sales Orders"),
                        "icon": sales["icon"],
                        "url": sales["url"],
                    },
                ]
            )

        accounting = card_map.get("accounting")
        if accounting:
            accounting_title = {
                "customer": _("Your Invoices"),
                "vendor": _("Vendor Bills"),
                "both": _("Bills & Invoices"),
            }.get(role, _("Bills & Invoices"))
            tiles.append(
                {
                    "key": "accounting",
                    "title": accounting_title,
                    "icon": accounting["icon"],
                    "url": accounting["url"],
                }
            )

        for key in ("purchase", "logistics", "projects", "tasks"):
            card = card_map.get(key)
            if card:
                tiles.append(
                    {
                        "key": key,
                        "title": card["title"],
                        "icon": card["icon"],
                        "url": card["url"],
                    }
                )

        notification_summary = self._notification_summary(limit=0)
        tiles.insert(
            0,
            {
                "key": "notifications",
                "title": _("Notifications"),
                "icon": "fa-bell-o",
                "url": "/my/business-notifications",
                "count": notification_summary["unread_count"],
            },
        )

        values.update(
            {
                "bpd_home_tiles": tiles,
                "bpd_home_period": period,
                "bpd_home_period_label": self._period_labels[period],
            }
        )
        return values


    # -------------------------------------------------------------------------
    # Portal notifications and preferences
    # -------------------------------------------------------------------------

    _notification_category_meta = {
        "sales": {"label": _("Sales"), "icon": "fa-shopping-cart"},
        "purchase": {"label": _("Purchase"), "icon": "fa-shopping-bag"},
        "billing": {"label": _("Billing"), "icon": "fa-file-text-o"},
        "logistics": {"label": _("Logistics"), "icon": "fa-truck"},
        "projects": {"label": _("Projects"), "icon": "fa-folder-open-o"},
        "tasks": {"label": _("Tasks"), "icon": "fa-check-square-o"},
        "system": {"label": _("System"), "icon": "fa-info-circle"},
    }

    _notification_type_meta = {
        "info": {"label": _("Information"), "icon": "fa-info-circle"},
        "success": {"label": _("Success"), "icon": "fa-check-circle"},
        "warning": {"label": _("Warning"), "icon": "fa-clock-o"},
        "critical": {"label": _("Critical"), "icon": "fa-exclamation-circle"},
    }

    def _portal_notification_domain(self):
        return [
            ("partner_id", "=", self._commercial_partner().id),
            ("company_id", "=", request.env.company.id),
        ]

    def _notification_summary(self, limit=4):
        Notification = request.env["business.portal.notification"].sudo()
        domain = self._portal_notification_domain()
        return {
            "unread_count": Notification.search_count(
                domain + [("is_read", "=", False)]
            ),
            "action_count": Notification.search_count(
                domain
                + [
                    ("requires_action", "=", True),
                    ("is_read", "=", False),
                ]
            ),
            "recent": (
                Notification.search(
                    domain,
                    order="create_date desc, id desc",
                    limit=limit,
                )
                if limit
                else Notification.browse()
            ),
        }

    def _notification_filters(self):
        filters = [
            {"key": "all", "label": _("All"), "icon": "fa-bell-o"},
            {"key": "unread", "label": _("Unread"), "icon": "fa-circle"},
            {
                "key": "action",
                "label": _("Action required"),
                "icon": "fa-exclamation-circle",
            },
        ]
        for key in (
            "sales",
            "purchase",
            "billing",
            "logistics",
            "projects",
            "tasks",
        ):
            meta = self._notification_category_meta[key]
            filters.append(
                {"key": key, "label": meta["label"], "icon": meta["icon"]}
            )
        return filters

    @http.route(
        [
            "/my/business-notifications",
            "/my/business-notifications/page/<int:page>",
        ],
        type="http",
        auth="user",
        website=True,
    )
    def portal_business_notifications(self, page=1, filterby="all", **kwargs):
        filters = self._notification_filters()
        allowed_filters = {item["key"] for item in filters}
        if filterby not in allowed_filters:
            filterby = "all"

        domain = self._portal_notification_domain()
        if filterby == "unread":
            domain.append(("is_read", "=", False))
        elif filterby == "action":
            domain.append(("requires_action", "=", True))
        elif filterby in self._notification_category_meta:
            domain.append(("category", "=", filterby))

        Notification = request.env["business.portal.notification"].sudo()
        total = Notification.search_count(domain)
        pager = portal_pager(
            url="/my/business-notifications",
            url_args={"filterby": filterby},
            total=total,
            page=page,
            step=20,
        )
        notifications = Notification.search(
            domain,
            order="create_date desc, id desc",
            limit=20,
            offset=pager["offset"],
        )
        summary = self._notification_summary(limit=0)

        values = self._prepare_portal_layout_values()
        values.update(
            {
                "page_name": "business_notifications",
                "notifications": notifications,
                "notification_filters": filters,
                "selected_notification_filter": filterby,
                "notification_total": total,
                "notification_unread_count": summary["unread_count"],
                "notification_action_count": summary["action_count"],
                "notification_category_meta": self._notification_category_meta,
                "notification_type_meta": self._notification_type_meta,
                "pager": pager,
            }
        )
        return request.render(
            "business_portal_dashboard.portal_business_notifications",
            values,
        )

    @http.route(
        "/my/business-notifications/<int:notification_id>/open",
        type="http",
        auth="user",
        website=True,
    )
    def portal_business_notification_open(self, notification_id, **kwargs):
        notification = (
            request.env["business.portal.notification"].sudo().browse(notification_id)
        )
        notification = notification.exists()
        if (
            not notification
            or notification.partner_id.id != self._commercial_partner().id
            or notification.company_id.id != request.env.company.id
        ):
            raise NotFound()

        notification._mark_as_read()
        target = notification.action_url or "/my/business-notifications"
        if not target.startswith("/my/"):
            target = "/my/business-notifications"
        return request.redirect(target)

    @http.route(
        "/my/business-notifications/mark-all-read",
        type="http",
        auth="user",
        methods=["POST"],
        website=True,
    )
    def portal_business_notifications_mark_all_read(self, **kwargs):
        Notification = request.env["business.portal.notification"].sudo()
        Notification.search(
            self._portal_notification_domain() + [("is_read", "=", False)]
        )._mark_as_read()
        return request.redirect("/my/business-notifications")

    @http.route(
        "/my/business-notification-preferences",
        type="http",
        auth="user",
        methods=["GET", "POST"],
        website=True,
    )
    def portal_business_notification_preferences(self, **kwargs):
        partner = self._commercial_partner().sudo()
        if request.httprequest.method == "POST":
            def checkbox(name):
                return kwargs.get(name) in ("1", "true", "True", "on", "yes")

            def reminder_days(name, default):
                try:
                    return max(1, min(int(kwargs.get(name, default)), 30))
                except (TypeError, ValueError):
                    return default

            partner.write(
                {
                    "portal_notification_enabled": checkbox(
                        "portal_notification_enabled"
                    ),
                    "portal_email_reminder_enabled": checkbox(
                        "portal_email_reminder_enabled"
                    ),
                    "portal_notify_sales": checkbox("portal_notify_sales"),
                    "portal_notify_purchase": checkbox("portal_notify_purchase"),
                    "portal_notify_billing": checkbox("portal_notify_billing"),
                    "portal_notify_logistics": checkbox(
                        "portal_notify_logistics"
                    ),
                    "portal_notify_projects": checkbox("portal_notify_projects"),
                    "portal_invoice_reminder_days": reminder_days(
                        "portal_invoice_reminder_days", 3
                    ),
                    "portal_quote_reminder_days": reminder_days(
                        "portal_quote_reminder_days", 3
                    ),
                    "portal_task_reminder_days": reminder_days(
                        "portal_task_reminder_days", 2
                    ),
                }
            )
            return request.redirect(
                "/my/business-notification-preferences?saved=1"
            )

        values = self._prepare_portal_layout_values()
        values.update(
            {
                "page_name": "business_notification_preferences",
                "notification_partner": partner,
                "notification_preferences_saved": kwargs.get("saved") == "1",
            }
        )
        return request.render(
            "business_portal_dashboard.portal_business_notification_preferences",
            values,
        )


    # -------------------------------------------------------------------------
    # Global portal search
    # -------------------------------------------------------------------------

    def _search_date_text(self, value):
        """Return a safe display string for a Date or Datetime value."""
        if not value:
            return ""
        if isinstance(value, datetime):
            return fields.Datetime.to_string(value)
        return fields.Date.to_string(value)

    def _global_search_categories(
        self,
        role,
        show_logistics=True,
        show_projects=True,
        show_tasks=True,
    ):
        """Return search categories available to the logged-in portal user."""
        categories = [{"key": "all", "label": _("All Documents")}]

        if role in ("customer", "both") and self._model_can_read("sale.order"):
            categories.append({"key": "sales", "label": _("Sales Orders")})

        if role in ("vendor", "both") and self._model_can_read("purchase.order"):
            categories.append({"key": "purchase", "label": _("Purchase Orders")})

        if self._model_can_read("account.move"):
            categories.append({"key": "billing", "label": _("Invoices & Bills")})

        if self._allowed_delivery_directions(role, show_logistics):
            categories.append(
                {"key": "logistics", "label": _("Deliveries & Receipts")}
            )

        if show_projects and self._model_can_read("project.project"):
            categories.append({"key": "projects", "label": _("Projects")})

        if show_tasks and self._model_can_read("project.task"):
            categories.append({"key": "tasks", "label": _("Tasks")})

        return categories

    def _global_search_groups(
        self,
        query,
        category,
        role,
        show_logistics=True,
        show_projects=True,
        show_tasks=True,
        result_limit=20,
    ):
        """Find or browse portal-accessible business documents.

        Behaviour:
        * ``category == 'all'`` requires a keyword and searches all categories.
        * A specific category with an empty keyword enters Browse mode and
          shows the latest records from that category.
        * A specific category with a keyword searches only that category.
        * Today's Activity is not touched; it remains date based.
        """
        query = (query or "").strip()[:100]
        has_query = len(query) >= 2

        available_categories = self._global_search_categories(
            role=role,
            show_logistics=show_logistics,
            show_projects=show_projects,
            show_tasks=show_tasks,
        )
        allowed_keys = {item["key"] for item in available_categories}
        if category not in allowed_keys:
            category = "all"

        # Browsing is allowed only after selecting a specific category.
        browse_mode = category != "all" and not query

        if category == "all" and not has_query:
            return []

        def category_enabled(key):
            return category in ("all", key)

        def with_search(base_domain, search_domain):
            if has_query:
                return expression.AND([base_domain, search_domain])
            return base_domain

        groups = []

        def add_group(key, title, icon, tone, total, items, module_url):
            if not total:
                return
            groups.append(
                {
                    "key": key,
                    "title": title,
                    "icon": icon,
                    "tone": tone,
                    "total": total,
                    "items": items,
                    "module_url": module_url,
                    "browse_mode": browse_mode,
                }
            )

        # ------------------------------------------------------------------
        # Sales orders and quotations
        # ------------------------------------------------------------------
        if (
            category_enabled("sales")
            and role in ("customer", "both")
            and self._model_can_read("sale.order")
        ):
            SaleOrder = request.env["sale.order"]
            base_domain = (
                self._partner_domain()
                + self._company_domain()
                + [("state", "in", ("sent", "sale", "cancel"))]
            )
            search_domain = [
                "|", "|", "|",
                ("name", "ilike", query),
                ("client_order_ref", "ilike", query),
                ("origin", "ilike", query),
                ("order_line.name", "ilike", query),
            ]
            domain = with_search(base_domain, search_domain)
            total = SaleOrder.search_count(domain)
            records = SaleOrder.search(
                domain,
                order="date_order desc, id desc",
                limit=result_limit,
            )
            items = []
            for order in records:
                matching_lines = (
                    order.order_line.filtered(
                        lambda line: query.lower() in (line.name or "").lower()
                    )
                    if has_query
                    else order.order_line[:1]
                )
                description = (
                    order.client_order_ref
                    or order.origin
                    or (matching_lines[:1].name if matching_lines else False)
                    or _("Sales order or quotation")
                )
                items.append(
                    {
                        "key": f"sale-{order.id}",
                        "title": order.name,
                        "description": description,
                        "status": self._sale_activity_status(order),
                        "date": self._search_date_text(order.date_order),
                        "url": self._safe_portal_url(order, "/my/orders"),
                        "amount": order.amount_total,
                        "currency": order.currency_id,
                    }
                )
            add_group(
                "sales",
                _("Sales"),
                "fa-shopping-cart",
                "sales",
                total,
                items,
                "/my/orders",
            )

        # ------------------------------------------------------------------
        # Purchase orders and RFQs
        # ------------------------------------------------------------------
        if (
            category_enabled("purchase")
            and role in ("vendor", "both")
            and self._model_can_read("purchase.order")
        ):
            PurchaseOrder = request.env["purchase.order"]
            base_domain = (
                self._partner_domain()
                + self._company_domain()
                + [
                    (
                        "state",
                        "in",
                        ("sent", "to approve", "purchase", "done", "cancel"),
                    )
                ]
            )
            search_domain = [
                "|", "|", "|",
                ("name", "ilike", query),
                ("partner_ref", "ilike", query),
                ("origin", "ilike", query),
                ("order_line.name", "ilike", query),
            ]
            domain = with_search(base_domain, search_domain)
            total = PurchaseOrder.search_count(domain)
            records = PurchaseOrder.search(
                domain,
                order="date_order desc, id desc",
                limit=result_limit,
            )
            items = []
            for order in records:
                matching_lines = (
                    order.order_line.filtered(
                        lambda line: query.lower() in (line.name or "").lower()
                    )
                    if has_query
                    else order.order_line[:1]
                )
                description = (
                    order.partner_ref
                    or order.origin
                    or (matching_lines[:1].name if matching_lines else False)
                    or _("Purchase order or RFQ")
                )
                items.append(
                    {
                        "key": f"purchase-{order.id}",
                        "title": order.name,
                        "description": description,
                        "status": self._purchase_activity_status(order),
                        "date": self._search_date_text(order.date_order),
                        "url": self._safe_portal_url(order, "/my/purchase"),
                        "amount": order.amount_total,
                        "currency": order.currency_id,
                    }
                )
            add_group(
                "purchase",
                _("Purchase"),
                "fa-shopping-bag",
                "purchase",
                total,
                items,
                "/my/purchase",
            )

        # ------------------------------------------------------------------
        # Invoices and vendor bills
        # ------------------------------------------------------------------
        if category_enabled("billing") and self._model_can_read("account.move"):
            Move = request.env["account.move"]
            if role == "customer":
                move_types = ("out_invoice", "out_refund", "out_receipt")
            elif role == "vendor":
                move_types = ("in_invoice", "in_refund", "in_receipt")
            else:
                move_types = (
                    "out_invoice", "out_refund", "out_receipt",
                    "in_invoice", "in_refund", "in_receipt",
                )

            base_domain = (
                self._partner_domain()
                + self._company_domain()
                + [("state", "=", "posted"), ("move_type", "in", move_types)]
            )
            search_domain = [
                "|", "|", "|", "|",
                ("name", "ilike", query),
                ("ref", "ilike", query),
                ("payment_reference", "ilike", query),
                ("invoice_origin", "ilike", query),
                ("invoice_line_ids.name", "ilike", query),
            ]
            domain = with_search(base_domain, search_domain)
            total = Move.search_count(domain)
            records = Move.search(
                domain,
                order="invoice_date desc, id desc",
                limit=result_limit,
            )
            items = []
            for move in records:
                matching_lines = (
                    move.invoice_line_ids.filtered(
                        lambda line: query.lower() in (line.name or "").lower()
                    )
                    if has_query
                    else move.invoice_line_ids[:1]
                )
                description = (
                    move.ref
                    or move.invoice_origin
                    or (matching_lines[:1].name if matching_lines else False)
                    or _("Invoice or bill")
                )
                items.append(
                    {
                        "key": f"billing-{move.id}",
                        "title": move.name or move.display_name,
                        "description": description,
                        "status": self._invoice_activity_status(move),
                        "date": self._search_date_text(move.invoice_date or move.date),
                        "url": self._safe_portal_url(move, "/my/invoices"),
                        "amount": move.amount_total,
                        "currency": move.currency_id,
                    }
                )
            add_group(
                "billing",
                _("Billing"),
                "fa-file-text-o",
                "billing",
                total,
                items,
                "/my/invoices",
            )

        # ------------------------------------------------------------------
        # Deliveries and receipts
        # ------------------------------------------------------------------
        directions = self._allowed_delivery_directions(role, show_logistics)
        if category_enabled("logistics") and directions:
            Picking = request.env["stock.picking"].sudo()
            direction_domain = expression.OR(
                [self._delivery_domain(direction, "all") for direction in directions]
            )
            search_domain = [
                "|", "|",
                ("name", "ilike", query),
                ("origin", "ilike", query),
                ("move_ids.description_picking", "ilike", query),
            ]
            domain = with_search(direction_domain, search_domain)
            total = Picking.search_count(domain)
            records = Picking.search(
                domain,
                order="scheduled_date desc, id desc",
                limit=result_limit,
            )
            items = []
            for picking in records:
                transfer_type = (
                    _("Delivery")
                    if picking.picking_type_code == "outgoing"
                    else _("Receipt")
                )
                matching_moves = (
                    picking.move_ids.filtered(
                        lambda move: query.lower()
                        in (move.description_picking or "").lower()
                    )
                    if has_query
                    else picking.move_ids[:1]
                )
                description = (
                    picking.origin
                    or (
                        matching_moves[:1].description_picking
                        if matching_moves
                        else False
                    )
                    or transfer_type
                )
                items.append(
                    {
                        "key": f"logistics-{picking.id}",
                        "title": picking.name,
                        "description": description,
                        "status": self._transfer_activity_status(picking),
                        "date": self._search_date_text(picking.scheduled_date),
                        "url": f"/my/business-deliveries/{picking.id}",
                    }
                )
            add_group(
                "logistics",
                _("Logistics"),
                "fa-truck",
                "logistics",
                total,
                items,
                "/my/business-deliveries",
            )

        # ------------------------------------------------------------------
        # Shared projects
        # ------------------------------------------------------------------
        if (
            category_enabled("projects")
            and show_projects
            and self._model_can_read("project.project")
        ):
            Project = request.env["project.project"]
            base_domain = [
                ("is_template", "=", False),
                "|",
                ("company_id", "=", False),
                ("company_id", "=", request.env.company.id),
            ]
            search_domain = [
                "|",
                ("name", "ilike", query),
                ("partner_id.name", "ilike", query),
            ]
            domain = with_search(base_domain, search_domain)
            total = Project.search_count(domain)
            records = Project.search(
                domain,
                order="write_date desc, id desc",
                limit=result_limit,
            )
            items = [
                {
                    "key": f"project-{project.id}",
                    "title": project.name,
                    "description": project.partner_id.display_name or _("Shared project"),
                    "status": _("Active") if project.active else _("Archived"),
                    "date": self._search_date_text(project.write_date),
                    "url": self._safe_portal_url(project, "/my/projects"),
                }
                for project in records
            ]
            add_group(
                "projects",
                _("Projects"),
                "fa-folder-open-o",
                "projects",
                total,
                items,
                "/my/projects",
            )

        # ------------------------------------------------------------------
        # Shared tasks
        # ------------------------------------------------------------------
        if (
            category_enabled("tasks")
            and show_tasks
            and self._model_can_read("project.task")
        ):
            Task = request.env["project.task"]
            base_domain = [
                ("project_id", "!=", False),
                ("is_template", "=", False),
                "|",
                ("company_id", "=", False),
                ("company_id", "=", request.env.company.id),
            ]
            search_domain = [
                "|", "|",
                ("name", "ilike", query),
                ("project_id.name", "ilike", query),
                ("description", "ilike", query),
            ]
            domain = with_search(base_domain, search_domain)
            total = Task.search_count(domain)
            records = Task.search(
                domain,
                order="write_date desc, id desc",
                limit=result_limit,
            )
            items = [
                {
                    "key": f"task-{task.id}",
                    "title": task.name,
                    "description": task.project_id.name or _("Project task"),
                    "status": task.stage_id.name or _("Updated"),
                    "date": self._search_date_text(task.write_date),
                    "url": self._safe_portal_url(task, "/my/tasks"),
                }
                for task in records
            ]
            add_group(
                "tasks",
                _("Tasks"),
                "fa-check-square-o",
                "tasks",
                total,
                items,
                "/my/tasks",
            )

        return groups

    @http.route(
        "/my/business-search",
        type="http",
        auth="user",
        website=True,
    )
    def portal_business_search(self, q=None, category="all", **kwargs):
        query = (q or "").strip()[:100]
        settings = self._dashboard_settings()

        search_categories = self._global_search_categories(
            role=settings["role"],
            show_logistics=settings["show_logistics"],
            show_projects=settings["show_projects"],
            show_tasks=settings["show_tasks"],
        )
        allowed_keys = {item["key"] for item in search_categories}
        if category not in allowed_keys:
            category = "all"

        search_performed = q is not None or category != "all"
        browse_mode = category != "all" and not query
        search_too_short = bool(query) and len(query) < 2
        browse_category_required = (
            search_performed and category == "all" and not query
        )

        search_groups = []
        if (
            search_performed
            and not search_too_short
            and not browse_category_required
        ):
            search_groups = self._global_search_groups(
                query=query,
                category=category,
                role=settings["role"],
                show_logistics=settings["show_logistics"],
                show_projects=settings["show_projects"],
                show_tasks=settings["show_tasks"],
                result_limit=20 if category != "all" else 6,
            )

        selected_category = next(
            (item for item in search_categories if item["key"] == category),
            search_categories[0],
        )

        values = self._prepare_portal_layout_values()
        values.update(
            {
                "page_name": "business_search",
                "search_query": query,
                "search_category": category,
                "search_category_label": selected_category["label"],
                "search_categories": search_categories,
                "search_groups": search_groups,
                "search_total": sum(group["total"] for group in search_groups),
                "search_performed": search_performed,
                "search_too_short": search_too_short,
                "browse_mode": browse_mode,
                "browse_category_required": browse_category_required,
            }
        )
        return request.render(
            "business_portal_dashboard.portal_business_search",
            values,
        )

    # -------------------------------------------------------------------------
    # Main dashboard
    # -------------------------------------------------------------------------

    @http.route("/my/business-dashboard", type="http", auth="user", website=True)
    def portal_business_dashboard(
        self,
        period=None,
        activity_date=None,
        activity_category="all",
        **kwargs,
    ):
        settings = self._dashboard_settings()
        period = self._normalise_period(period, settings["default_period"])
        role = settings["role"]

        (
            selected_activity_date,
            activity_groups,
            activity_categories,
            activity_category,
        ) = self._datewise_recent_activity(
            activity_date=activity_date,
            role=role,
            show_logistics=settings["show_logistics"],
            activity_category=activity_category,
        )

        cards = [
            self._sales_card(period, role),
            self._accounting_card(period, role),
            self._purchase_card(period, role),
            self._logistics_card(period, role, settings["show_logistics"]),
            self._projects_card(period, settings["show_projects"]),
            self._tasks_card(period, settings["show_tasks"]),
        ]
        cards = [
            card
            for card in cards
            if self._visible_card(card, settings["show_empty_cards"])
        ]

        global_search_categories = self._global_search_categories(
            role=role,
            show_logistics=settings["show_logistics"],
            show_projects=settings["show_projects"],
            show_tasks=settings["show_tasks"],
        )

        notification_summary = self._notification_summary(limit=0)
        today = fields.Date.context_today(request.env.user)
        values = self._prepare_portal_layout_values()
        values.update(
            {
                "page_name": "business_dashboard",
                "cards": cards,
                "alerts": self._dashboard_alerts(
                    role,
                    settings["show_logistics"],
                ),
                "selected_period": period,
                "period_labels": self._period_labels,
                "partner": self._commercial_partner(),
                "last_updated": fields.Datetime.now(),
                "activity_groups": activity_groups,
                "activity_categories": activity_categories,
                "activity_category": activity_category,
                "activity_date": fields.Date.to_string(selected_activity_date),
                "activity_date_display": selected_activity_date,
                "activity_today": fields.Date.to_string(today),
                "activity_is_today": selected_activity_date == today,
                "global_search_categories": global_search_categories,
                "notification_unread_count": notification_summary["unread_count"],
            }
        )
        return request.render(
            "business_portal_dashboard.portal_business_dashboard",
            values,
        )


    # -------------------------------------------------------------------------
    # Account statement and aging portal
    # -------------------------------------------------------------------------

    @http.route(
        [
            "/my/account-statement",
            "/my/account-statement/page/<int:page>",
        ],
        type="http",
        auth="user",
        website=True,
    )
    def portal_account_statement(
        self,
        page=1,
        statement_type=None,
        date_from=None,
        date_to=None,
        **kwargs,
    ):
        settings = self._dashboard_settings()
        statement_type, statement_type_options = self._normalise_statement_type(
            statement_type,
            settings["role"],
        )
        if not statement_type:
            return request.redirect("/my/business-dashboard")

        date_from, date_to = self._statement_dates(date_from, date_to)
        statement = self._statement_data(
            statement_type,
            date_from,
            date_to,
            page=page,
        )

        statement_label = next(
            item["label"]
            for item in statement_type_options
            if item["key"] == statement_type
        )
        export_query = urlencode(
            {
                "statement_type": statement_type,
                "date_from": fields.Date.to_string(date_from),
                "date_to": fields.Date.to_string(date_to),
            }
        )

        values = self._prepare_portal_layout_values()
        values.update(
            {
                "page_name": "account_statement",
                "statement_type": statement_type,
                "statement_type_options": statement_type_options,
                "statement_label": statement_label,
                "date_from": fields.Date.to_string(date_from),
                "date_to": fields.Date.to_string(date_to),
                "today": fields.Date.to_string(
                    fields.Date.context_today(request.env.user)
                ),
                "statement_rows": statement["rows"],
                "statement_total": statement["total"],
                "statement_currency": statement["currency"],
                "opening_balance": statement["opening_balance"],
                "period_charges": statement["period_charges"],
                "period_payments": statement["period_payments"],
                "closing_balance": statement["closing_balance"],
                "aging": statement["aging"],
                "pager": statement["pager"],
                "export_url": f"/my/account-statement/csv?{export_query}",
            }
        )
        return request.render(
            "business_portal_dashboard.portal_account_statement",
            values,
        )

    @http.route(
        "/my/account-statement/csv",
        type="http",
        auth="user",
        website=True,
    )
    def portal_account_statement_csv(
        self,
        statement_type=None,
        date_from=None,
        date_to=None,
        **kwargs,
    ):
        settings = self._dashboard_settings()
        statement_type, _options = self._normalise_statement_type(
            statement_type,
            settings["role"],
        )
        if not statement_type:
            return request.not_found()

        date_from, date_to = self._statement_dates(date_from, date_to)
        Line = request.env["account.move.line"].sudo()
        currency = self._company_currency()
        multiplier = self._statement_multiplier(statement_type)
        base_domain = self._statement_line_domain(statement_type)
        opening_domain = base_domain + [
            ("date", "<", fields.Date.to_string(date_from))
        ]
        period_domain = base_domain + [
            ("date", ">=", fields.Date.to_string(date_from)),
            ("date", "<=", fields.Date.to_string(date_to)),
        ]

        opening = self._statement_group_sums(Line, opening_domain)["balance"] * multiplier
        running = currency.round(opening)
        lines = Line.search(period_domain, order="date asc, id asc", limit=5000)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                _("Date"),
                _("Reference"),
                _("Document type"),
                _("Description"),
                _("Due date"),
                _("Status"),
                _("Charges"),
                _("Payments / Credits"),
                _("Balance"),
                _("Currency"),
            ]
        )
        for line in lines:
            running = currency.round(running + (line.balance * multiplier))
            row = self._statement_row(line, statement_type, running)
            writer.writerow(
                [
                    fields.Date.to_string(row["date"]),
                    row["reference"],
                    row["document_type"],
                    row["description"],
                    fields.Date.to_string(row["due_date"]) if row["due_date"] else "",
                    row["status"],
                    row["charge"],
                    row["payment"],
                    row["running_balance"],
                    currency.name,
                ]
            )

        filename = (
            f"{statement_type}_statement_"
            f"{fields.Date.to_string(date_from)}_"
            f"{fields.Date.to_string(date_to)}.csv"
        )
        content = "\ufeff" + output.getvalue()
        return request.make_response(
            content,
            headers=[
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition", f'attachment; filename="{filename}"'),
            ],
        )

    # -------------------------------------------------------------------------
    # Date-wise activity portal
    # -------------------------------------------------------------------------

    @http.route(
        [
            "/my/business-activity",
            "/my/business-activity/page/<int:page>",
        ],
        type="http",
        auth="user",
        website=True,
    )
    def portal_business_activity(
        self,
        page=1,
        category="sales",
        activity_date=None,
        search=None,
        **kwargs,
    ):
        settings = self._dashboard_settings()
        role = settings["role"]
        show_logistics = settings["show_logistics"]
        selected_date, date_domain = self._activity_date_bounds(activity_date)

        available_categories = []
        for key in ("sales", "purchase", "billing", "logistics"):
            info = self._activity_category_info(key, role, show_logistics)
            if info:
                available_categories.append(info)

        if not available_categories:
            return request.redirect("/my/business-dashboard")

        allowed_keys = {item["key"] for item in available_categories}
        if category not in allowed_keys:
            category = available_categories[0]["key"]

        search = (search or "").strip()[:100]
        first_group = self._activity_group(
            category=category,
            selected_date=selected_date,
            date_domain=date_domain,
            role=role,
            show_logistics=show_logistics,
            limit=1,
            search=search,
        )
        total = first_group["total"] if first_group else 0

        pager = portal_pager(
            url="/my/business-activity",
            url_args={
                "category": category,
                "activity_date": fields.Date.to_string(selected_date),
                "search": search,
            },
            total=total,
            page=page,
            step=self._items_per_page,
        )

        group = self._activity_group(
            category=category,
            selected_date=selected_date,
            date_domain=date_domain,
            role=role,
            show_logistics=show_logistics,
            limit=self._items_per_page,
            offset=pager["offset"],
            search=search,
        )

        values = self._prepare_portal_layout_values()
        values.update(
            {
                "page_name": "business_activity",
                "activity_group": group,
                "activity_category": category,
                "activity_date": fields.Date.to_string(selected_date),
                "activity_date_display": selected_date,
                "activity_today": fields.Date.to_string(
                    fields.Date.context_today(request.env.user)
                ),
                "search": search,
                "pager": pager,
            }
        )
        return request.render(
            "business_portal_dashboard.portal_business_activity",
            values,
        )


    # -------------------------------------------------------------------------
    # Deliveries / receipts portal
    # -------------------------------------------------------------------------

    def _delivery_status_domain(self, status):
        if status == "pending":
            return [("state", "not in", ("done", "cancel"))]
        if status == "ready":
            return [("state", "=", "assigned")]
        if status == "late":
            return [
                ("scheduled_date", "<", fields.Datetime.now()),
                ("state", "not in", ("done", "cancel")),
            ]
        if status == "done":
            return [("state", "=", "done")]
        return []

    @http.route(
        ["/my/business-deliveries", "/my/business-deliveries/page/<int:page>"],
        type="http",
        auth="user",
        website=True,
    )
    def portal_business_deliveries(
        self,
        page=1,
        direction=None,
        status="all",
        period=None,
        search=None,
        **kwargs,
    ):
        settings = self._dashboard_settings()
        allowed_directions = self._allowed_delivery_directions(
            settings["role"], settings["show_logistics"]
        )
        if not allowed_directions:
            return request.redirect("/my/business-dashboard")

        selectable_directions = list(allowed_directions)
        if len(allowed_directions) > 1:
            selectable_directions.insert(0, "all")

        if direction not in selectable_directions:
            direction = (
                "all"
                if len(allowed_directions) > 1
                else allowed_directions[0]
            )
        period = self._normalise_period(period, settings["default_period"])
        if status not in ("all", "pending", "ready", "late", "done"):
            status = "all"

        domain = self._delivery_domain(direction, period) + self._delivery_status_domain(status)
        search = (search or "").strip()[:100]
        if search:
            domain += ["|", ("name", "ilike", search), ("origin", "ilike", search)]

        Picking = request.env["stock.picking"].sudo()
        total = Picking.search_count(domain)
        pager = portal_pager(
            url="/my/business-deliveries",
            url_args={
                "direction": direction,
                "status": status,
                "period": period,
                "search": search,
            },
            total=total,
            page=page,
            step=self._items_per_page,
        )
        pickings = Picking.search(
            domain,
            order="scheduled_date desc, id desc",
            limit=self._items_per_page,
            offset=pager["offset"],
        )
        picking_rows = [
            {
                "picking": picking,
                **self._picking_progress_data(picking),
            }
            for picking in pickings
        ]

        values = self._prepare_portal_layout_values()
        values.update(
            {
                "page_name": "business_deliveries",
                "pickings": pickings,
                "picking_rows": picking_rows,
                "pager": pager,
                "direction": direction,
                "allowed_directions": allowed_directions,
                "selectable_directions": selectable_directions,
                "status": status,
                "selected_period": period,
                "period_labels": self._period_labels,
                "search": search,
                "delivery_state_labels": self._delivery_state_labels,
            }
        )
        return request.render("business_portal_dashboard.portal_business_deliveries", values)

    @http.route(
        "/my/business-deliveries/<int:picking_id>",
        type="http",
        auth="user",
        website=True,
    )
    def portal_business_delivery_detail(self, picking_id, **kwargs):
        settings = self._dashboard_settings()
        directions = self._allowed_delivery_directions(
            settings["role"], settings["show_logistics"]
        )
        if not directions:
            return request.not_found()

        allowed_domain = expression.OR(
            [self._delivery_domain(direction, "all") for direction in directions]
        )
        domain = expression.AND([[('id', '=', picking_id)], allowed_domain])
        picking = request.env["stock.picking"].sudo().search(domain, limit=1)
        if not picking:
            return request.not_found()

        moves = picking.move_ids.filtered(lambda move: move.state != "cancel")
        values = self._prepare_portal_layout_values()
        values.update(
            {
                "page_name": "business_delivery_detail",
                "picking": picking,
                "moves": moves,
                "delivery_state_labels": self._delivery_state_labels,
                "delivery_progress": self._picking_progress_data(picking),
            }
        )
        return request.render("business_portal_dashboard.portal_business_delivery_detail", values)
