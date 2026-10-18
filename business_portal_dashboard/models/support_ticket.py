import re

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext


class BusinessPortalTicket(models.Model):
    _name = "business.portal.ticket"
    _description = "Business Portal Support Ticket"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"

    name = fields.Char(
        string="Ticket Number",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _("New"),
        tracking=True,
        index=True,
    )
    subject = fields.Char(required=True, tracking=True, index=True)
    description = fields.Text(required=True)
    partner_id = fields.Many2one(
        "res.partner",
        string="Portal Company",
        required=True,
        index=True,
        ondelete="cascade",
        tracking=True,
    )
    contact_id = fields.Many2one(
        "res.partner",
        string="Portal Contact",
        required=True,
        index=True,
        ondelete="restrict",
        tracking=True,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="cascade",
    )
    assigned_user_id = fields.Many2one(
        "res.users",
        string="Assigned To",
        domain=[("share", "=", False)],
        tracking=True,
        index=True,
    )
    category = fields.Selection(
        selection=[
            ("general", "General"),
            ("sales", "Sales"),
            ("purchase", "Purchase"),
            ("billing", "Billing"),
            ("delivery", "Delivery / Receipt"),
            ("product", "Product Issue"),
            ("return", "Return / Refund"),
            ("project", "Project / Task"),
        ],
        required=True,
        default="general",
        tracking=True,
        index=True,
    )
    priority = fields.Selection(
        selection=[
            ("0", "Low"),
            ("1", "Normal"),
            ("2", "High"),
            ("3", "Urgent"),
        ],
        required=True,
        default="1",
        tracking=True,
        index=True,
    )
    state = fields.Selection(
        selection=[
            ("new", "New"),
            ("in_progress", "In Progress"),
            ("waiting_customer", "Waiting for Customer"),
            ("resolved", "Resolved"),
            ("closed", "Closed"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="new",
        tracking=True,
        index=True,
    )
    date_opened = fields.Datetime(
        default=fields.Datetime.now,
        required=True,
        readonly=True,
    )
    date_resolved = fields.Datetime(readonly=True, copy=False)
    last_customer_reply_at = fields.Datetime(readonly=True, copy=False)
    last_support_reply_at = fields.Datetime(readonly=True, copy=False)
    last_auto_reply_at = fields.Datetime(readonly=True, copy=False)
    auto_reply_count = fields.Integer(readonly=True, copy=False)

    expected_reply_type = fields.Selection(
        selection=[
            ("none", "No Specific Information"),
            ("address", "Complete Delivery Address"),
            ("contact", "Contact Number"),
            ("payment_reference", "Payment Reference / Receipt"),
            ("damage_evidence", "Damage Photos / Evidence"),
            ("document_details", "Document Details"),
            ("general_details", "More Issue Details"),
        ],
        string="Information Requested",
        default="none",
        tracking=True,
        copy=False,
    )
    customer_reply_status = fields.Selection(
        selection=[
            ("none", "No Reply Expected"),
            ("pending", "Waiting for Customer"),
            ("invalid", "Incomplete Reply"),
            ("valid", "Reply Received"),
        ],
        default="none",
        string="Customer Reply Check",
        tracking=True,
        copy=False,
        index=True,
    )
    customer_reply_pending_review = fields.Boolean(
        string="Customer Reply Needs Review",
        default=False,
        tracking=True,
        copy=False,
        index=True,
    )

    related_model = fields.Selection(
        selection=[
            ("sale.order", "Sales Order / Quotation"),
            ("purchase.order", "Purchase Order / RFQ"),
            ("account.move", "Invoice / Bill"),
            ("stock.picking", "Delivery / Receipt"),
            ("project.project", "Project"),
            ("project.task", "Task"),
        ],
        string="Related Document Type",
        index=True,
    )
    related_res_id = fields.Integer(string="Related Record ID", index=True)
    related_reference = fields.Char(string="Related Document", index=True)
    related_url = fields.Char(string="Portal Link")

    # Resolution and fulfilment tracking. These fields make the final customer
    # communication specific instead of sending a generic "ticket closed" reply.
    resolution_type = fields.Selection(
        selection=[
            ("none", "Not Decided"),
            ("replacement", "Replacement"),
            ("refund", "Refund"),
            ("repair", "Repair"),
            ("credit_note", "Credit Note"),
            ("rejected", "Request Rejected"),
            ("other", "Other Resolution"),
        ],
        string="Resolution Type",
        default="none",
        tracking=True,
        copy=False,
        index=True,
    )
    resolution_note = fields.Text(
        string="Resolution Note",
        tracking=True,
        copy=False,
        help="Customer-facing explanation of the approved solution.",
    )
    resolution_expected_date = fields.Date(
        string="Expected Completion Date",
        tracking=True,
        copy=False,
    )
    resolution_completed_date = fields.Date(
        string="Delivered / Completed Date",
        tracking=True,
        copy=False,
    )
    replacement_picking_id = fields.Many2one(
        "stock.picking",
        string="Replacement Delivery",
        tracking=True,
        copy=False,
        ondelete="set null",
        domain=[("picking_type_code", "=", "outgoing")],
    )
    replacement_tracking_reference = fields.Char(
        string="Tracking Reference",
        tracking=True,
        copy=False,
    )
    refund_amount = fields.Monetary(
        string="Refund Amount",
        currency_field="currency_id",
        tracking=True,
        copy=False,
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        store=True,
        readonly=True,
    )
    credit_note_id = fields.Many2one(
        "account.move",
        string="Credit Note",
        tracking=True,
        copy=False,
        ondelete="set null",
        domain=[
            ("move_type", "in", ("out_refund", "in_refund")),
            ("state", "=", "posted"),
        ],
    )

    is_open = fields.Boolean(compute="_compute_is_open", store=True, index=True)

    @api.depends("state")
    def _compute_is_open(self):
        for ticket in self:
            ticket.is_open = ticket.state in (
                "new",
                "in_progress",
                "waiting_customer",
            )

    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env["ir.sequence"]
        for values in vals_list:
            if values.get("name", _("New")) == _("New"):
                values["name"] = (
                    sequence.next_by_code("business.portal.ticket")
                    or _("New")
                )
            values.setdefault("date_opened", fields.Datetime.now())

        tickets = super().create(vals_list)
        for ticket in tickets:
            partner_ids = [
                partner_id
                for partner_id in {
                    ticket.partner_id.id,
                    ticket.contact_id.id,
                }
                if partner_id
            ]
            if partner_ids:
                ticket.message_subscribe(partner_ids=partner_ids)
        return tickets

    def write(self, values):
        old_states = {ticket.id: ticket.state for ticket in self}
        values = dict(values)

        if values.get("state") == "resolved":
            values.setdefault("date_resolved", fields.Datetime.now())
        elif "state" in values and values["state"] not in ("resolved", "closed"):
            values.setdefault("date_resolved", False)

        result = super().write(values)

        if (
            "state" in values
            and not self.env.context.get("skip_portal_ticket_notification")
            and not self.env.user.share
        ):
            for ticket in self:
                if old_states.get(ticket.id) != ticket.state:
                    ticket._notify_portal_state_change()
        return result

    def message_post(self, **kwargs):
        message = super().message_post(**kwargs)

        if (
            len(self) == 1
            and message
            and message.message_type == "comment"
            and not self.env.context.get("skip_portal_ticket_notification")
        ):
            ticket = self[0]
            portal_company = ticket.partner_id.commercial_partner_id
            author_company = message.author_id.commercial_partner_id

            if author_company != portal_company:
                ticket.with_context(
                    skip_portal_ticket_notification=True
                ).sudo().write(
                    {"last_support_reply_at": fields.Datetime.now()}
                )
                ticket._notify_portal_support_reply(message)
            else:
                ticket.with_context(
                    skip_portal_ticket_notification=True
                ).sudo().write(
                    {"last_customer_reply_at": fields.Datetime.now()}
                )
        return message

    # ------------------------------------------------------------------
    # Automatic customer communication
    # ------------------------------------------------------------------

    def _automatic_author_partner(self):
        self.ensure_one()
        return self.company_id.partner_id or self.env.company.partner_id

    def _post_automatic_reply(self, text):
        self.ensure_one()
        clean_text = (text or "").strip()
        if not clean_text:
            return self.env["mail.message"]

        body = Markup("<p>%s</p>") % escape(clean_text)
        body = body.replace("\n", Markup("<br/>"))
        message = self.sudo().message_post(
            author_id=self._automatic_author_partner().id,
            body=body,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
        )
        self.with_context(skip_portal_ticket_notification=True).sudo().write(
            {
                "last_auto_reply_at": fields.Datetime.now(),
                "auto_reply_count": self.auto_reply_count + 1,
            }
        )
        return message

    def _default_expected_reply_type(self):
        self.ensure_one()
        return {
            "delivery": "address",
            "billing": "payment_reference",
            "product": "damage_evidence",
            "return": "damage_evidence",
            "sales": "document_details",
            "purchase": "document_details",
            "project": "general_details",
            "general": "general_details",
        }.get(self.category, "general_details")

    def _request_message_for_type(self, reply_type):
        self.ensure_one()
        reference = self.related_reference or self.name
        messages = {
            "address": _(
                "Please provide your complete delivery address for %(reference)s. "
                "Include house/building number, street or area, city, and a contact number.",
                reference=reference,
            ),
            "contact": _(
                "Please provide a valid contact number including the area or country code."
            ),
            "payment_reference": _(
                "Please provide the payment reference or transaction number for %(reference)s. "
                "You may also attach the payment receipt.",
                reference=reference,
            ),
            "damage_evidence": _(
                "Please attach clear photos of the damaged product and packaging, "
                "and include the product or serial reference in your reply."
            ),
            "document_details": _(
                "Please provide the exact change or issue for %(reference)s, "
                "including any relevant product, quantity, or date details.",
                reference=reference,
            ),
            "general_details": _(
                "Please provide more details about the issue, including what happened "
                "and what result you expect."
            ),
        }
        return messages.get(reply_type, messages["general_details"])

    @api.model
    def _normalise_customer_text(self, text):
        return re.sub(r"\s+", " ", (text or "").strip())

    @api.model
    def _looks_like_junk(self, text):
        normalised = self._normalise_customer_text(text).lower()
        compact = re.sub(r"[^a-z0-9]", "", normalised)
        blocked = {
            "ok",
            "okay",
            "yes",
            "no",
            "abc",
            "abcd",
            "test",
            "testing",
            "same",
            "sameaddress",
            "dontknow",
            "idk",
            "na",
            "none",
        }
        if compact in blocked:
            return True
        if compact and re.fullmatch(r"(.)\1{4,}", compact):
            return True
        if len(set(compact)) <= 2 and len(compact) >= 5:
            return True
        return False

    @api.model
    def _attachment_is_image(self, attachment):
        mimetype = (attachment.mimetype or "").lower()
        name = (attachment.name or "").lower()
        return mimetype.startswith("image/") or name.endswith(
            (".png", ".jpg", ".jpeg", ".webp")
        )

    def _validate_customer_reply(self, text, attachments=None):
        self.ensure_one()
        attachments = attachments or self.env["ir.attachment"]
        clean = self._normalise_customer_text(text)
        reply_type = self.expected_reply_type or "none"
        words = re.findall(r"[A-Za-zÀ-ÿ]{2,}", clean)
        digits = re.sub(r"\D", "", clean)

        if not clean and not attachments:
            return {
                "valid": False,
                "reply": _("Please enter a message or attach the requested file."),
            }

        if clean and self._looks_like_junk(clean):
            return {
                "valid": False,
                "reply": _(
                    "Your reply appears incomplete. Please provide clear and meaningful information."
                ),
            }

        if reply_type == "address":
            has_location_separator = any(
                token in clean.lower()
                for token in (
                    ",",
                    " street ",
                    " road ",
                    " area ",
                    " block ",
                    " sector ",
                    " city ",
                    " house ",
                    " flat ",
                    " building ",
                )
            )
            if (
                len(clean) < 20
                or len(words) < 4
                or not re.search(r"\d", clean)
                or not has_location_separator
            ):
                return {
                    "valid": False,
                    "reply": _(
                        "The address appears incomplete. Please include house/building number, "
                        "street or area, city, and a contact number. "
                        "Example: House 12, Street 4, Model Town, Lahore, 03001234567."
                    ),
                }

        elif reply_type == "contact":
            if len(digits) < 7 or len(digits) > 15:
                return {
                    "valid": False,
                    "reply": _(
                        "Please provide a valid contact number containing 7 to 15 digits."
                    ),
                }

        elif reply_type == "payment_reference":
            has_reference = bool(re.search(r"[A-Za-z0-9][A-Za-z0-9\-/]{5,}", clean))
            if not attachments and not has_reference:
                return {
                    "valid": False,
                    "reply": _(
                        "Please provide a payment or transaction reference of at least six characters, "
                        "or attach the payment receipt."
                    ),
                }

        elif reply_type == "damage_evidence":
            has_image = any(
                self._attachment_is_image(attachment)
                for attachment in attachments
            )
            if not has_image:
                return {
                    "valid": False,
                    "reply": _(
                        "Please attach at least one clear image of the damaged product or packaging."
                    ),
                }
            if clean and len(clean) < 8:
                return {
                    "valid": False,
                    "reply": _(
                        "The image was received. Please also add a short description of the damage."
                    ),
                }

        elif reply_type in ("document_details", "general_details"):
            if not attachments and (len(clean) < 15 or len(words) < 3):
                return {
                    "valid": False,
                    "reply": _(
                        "Please provide more detail so the support team can understand and process the request."
                    ),
                }

        elif clean and len(clean) < 5 and not attachments:
            return {
                "valid": False,
                "reply": _("Please provide a little more detail about your request."),
            }

        return {"valid": True}

    def _valid_reply_acknowledgement(self):
        self.ensure_one()
        messages = {
            "address": _(
                "Thank you. Your delivery address has been received and is ready for support review."
            ),
            "contact": _(
                "Thank you. Your contact number has been received and is ready for support review."
            ),
            "payment_reference": _(
                "Thank you. Your payment information has been received and will be verified by support."
            ),
            "damage_evidence": _(
                "Thank you. Your damage evidence has been received and will be reviewed by support."
            ),
            "document_details": _(
                "Thank you. The requested document details have been received and are ready for review."
            ),
            "general_details": _(
                "Thank you. The additional information has been received and is ready for support review."
            ),
            "none": _(
                "Thank you for your message. It has been recorded and the support team will review it."
            ),
        }
        return messages.get(self.expected_reply_type or "none", messages["none"])

    def _schedule_customer_reply_activity(self):
        self.ensure_one()
        if not self.assigned_user_id:
            return

        summary = _("Review customer reply: %(ticket)s", ticket=self.name)
        existing = self.activity_ids.filtered(
            lambda activity: activity.user_id == self.assigned_user_id
            and activity.summary == summary
        )
        if existing:
            return

        self.activity_schedule(
            "mail.mail_activity_data_todo",
            user_id=self.assigned_user_id.id,
            summary=summary,
            note=_(
                "The customer submitted a validated reply. Review the message and change the ticket stage as appropriate."
            ),
        )

    def _complete_customer_reply_activities(self):
        for ticket in self:
            activities = ticket.activity_ids.filtered(
                lambda activity: (activity.summary or "").startswith(
                    _("Review customer reply:")
                )
            )
            if activities:
                activities.action_feedback(
                    feedback=_("Customer reply reviewed through the ticket workflow.")
                )

    def _process_portal_customer_reply(self, text, attachments=None):
        self.ensure_one()
        attachments = attachments or self.env["ir.attachment"]
        validation = self._validate_customer_reply(text, attachments)
        now = fields.Datetime.now()

        values = {
            "last_customer_reply_at": now,
            "customer_reply_status": "valid" if validation["valid"] else "invalid",
            "customer_reply_pending_review": bool(validation["valid"]),
        }
        self.with_context(skip_portal_ticket_notification=True).sudo().write(values)

        if validation["valid"]:
            self._schedule_customer_reply_activity()
            automatic_reply = self._valid_reply_acknowledgement()
            result = "valid"
        else:
            automatic_reply = validation["reply"]
            result = "invalid"

        self._post_automatic_reply(automatic_reply)
        return {"result": result, "message": automatic_reply}

    def _initial_automatic_acknowledgement(self):
        self.ensure_one()
        reference_text = (
            _(" related to %(reference)s", reference=self.related_reference)
            if self.related_reference
            else ""
        )
        return _(
            "Thank you. Support ticket %(ticket)s%(reference)s has been created. "
            "The support team will review it and update the stage.",
            ticket=self.name,
            reference=reference_text,
        )

    # ------------------------------------------------------------------
    # Resolution validation and customer-facing completion messages
    # ------------------------------------------------------------------

    @api.onchange("resolution_type")
    def _onchange_resolution_type(self):
        for ticket in self:
            if ticket.resolution_type != "replacement":
                ticket.replacement_picking_id = False
                ticket.replacement_tracking_reference = False
            if ticket.resolution_type != "refund":
                ticket.refund_amount = 0.0
            if ticket.resolution_type != "credit_note":
                ticket.credit_note_id = False

    def _date_text(self, value):
        self.ensure_one()
        return fields.Date.to_string(value) if value else ""

    def _amount_text(self):
        self.ensure_one()
        currency = self.currency_id
        amount = self.refund_amount or 0.0
        precision = getattr(currency, "decimal_places", 2) or 2
        number = f"{amount:,.{precision}f}"
        symbol = currency.symbol or currency.name or ""
        if not symbol:
            return number
        return f"{symbol} {number}" if currency.position == "before" else f"{number} {symbol}"

    def _validate_resolution_for_resolve(self):
        self.ensure_one()
        if self.resolution_type == "none":
            raise UserError(_("Select a resolution type before resolving the ticket."))
        if not (self.resolution_note or "").strip():
            raise UserError(_("Enter a customer-facing resolution note before resolving the ticket."))

        if self.resolution_type == "replacement":
            if not self.replacement_picking_id:
                raise UserError(_("Select the replacement delivery before resolving this ticket."))
            if self.replacement_picking_id.picking_type_code != "outgoing":
                raise UserError(_("The replacement document must be an outgoing delivery."))
            if self.replacement_picking_id.company_id != self.company_id:
                raise UserError(_("The replacement delivery must belong to the same company as the ticket."))
            picking_partner = self.replacement_picking_id.partner_id.commercial_partner_id
            if picking_partner and picking_partner != self.partner_id.commercial_partner_id:
                raise UserError(_("The replacement delivery must belong to the same portal customer."))
            if not self.resolution_expected_date:
                raise UserError(_("Enter the expected replacement delivery date."))

        elif self.resolution_type == "refund":
            if self.refund_amount <= 0:
                raise UserError(_("Enter a refund amount greater than zero."))
            if not self.resolution_expected_date:
                raise UserError(_("Enter the expected refund completion date."))

        elif self.resolution_type == "repair":
            if not self.resolution_expected_date:
                raise UserError(_("Enter the expected repair completion date."))

        elif self.resolution_type == "credit_note":
            if not self.credit_note_id:
                raise UserError(_("Select the posted credit note before resolving this ticket."))
            if self.credit_note_id.state != "posted":
                raise UserError(_("The selected credit note must be posted."))
            if self.credit_note_id.company_id != self.company_id:
                raise UserError(_("The credit note must belong to the same company as the ticket."))
            move_partner = self.credit_note_id.partner_id.commercial_partner_id
            if move_partner and move_partner != self.partner_id.commercial_partner_id:
                raise UserError(_("The credit note must belong to the same portal customer."))

    def _validate_resolution_for_close(self):
        """Block closure until the approved fulfilment is verifiably complete.

        A manually entered date is not accepted as proof for a replacement or
        refund. Replacement completion is verified from the stock transfer,
        while refund completion is verified from a posted, fully paid credit
        note belonging to the same customer and company.
        """
        self.ensure_one()
        if self.state != "resolved":
            raise UserError(_("Resolve the ticket before closing it."))

        if self.resolution_type == "replacement":
            picking = self.replacement_picking_id
            if not picking:
                raise UserError(_("Select the replacement delivery before closing the ticket."))
            if picking.state != "done" or not picking.date_done:
                raise UserError(_(
                    "The replacement delivery has not been completed yet. "
                    "Validate the delivery before closing this ticket."
                ))
            self.with_context(skip_portal_ticket_notification=True).write({
                "resolution_completed_date": fields.Date.to_date(picking.date_done),
            })

        elif self.resolution_type == "refund":
            credit_note = self.credit_note_id
            if not credit_note:
                raise UserError(_(
                    "The refund has not been completed yet. Select the posted "
                    "refund credit note before closing this ticket."
                ))
            if credit_note.move_type not in ("out_refund", "in_refund"):
                raise UserError(_("The selected accounting document must be a credit note."))
            if credit_note.state != "posted":
                raise UserError(_("Post the refund credit note before closing this ticket."))
            if credit_note.company_id != self.company_id:
                raise UserError(_("The refund credit note must belong to the same company as the ticket."))

            move_partner = credit_note.partner_id.commercial_partner_id
            ticket_partner = self.partner_id.commercial_partner_id
            if move_partner and move_partner != ticket_partner:
                raise UserError(_("The refund credit note must belong to the same portal customer."))

            if credit_note.currency_id != self.currency_id:
                raise UserError(_("The refund credit note must use the same currency as the ticket."))
            if not self.currency_id.is_zero(credit_note.amount_total - self.refund_amount):
                raise UserError(_(
                    "The refund credit note amount does not match the approved refund amount."
                ))

            if credit_note.payment_state != "paid":
                raise UserError(_(
                    "The refund has not been paid yet, so this ticket cannot be closed. "
                    "Register and reconcile the refund payment first."
                ))

            paid_dates = []
            if "reconciled_payment_ids" in credit_note._fields:
                paid_dates = credit_note.reconciled_payment_ids.filtered(
                    lambda payment: payment.state == "paid"
                ).mapped("date")
            completed_date = max(paid_dates) if paid_dates else fields.Date.context_today(self)
            self.with_context(skip_portal_ticket_notification=True).write({
                "resolution_completed_date": completed_date,
            })

        elif self.resolution_type == "repair":
            if not self.resolution_completed_date:
                raise UserError(_(
                    "The repair has not been completed yet. Enter the completed "
                    "date before closing this ticket."
                ))

        elif self.resolution_type == "credit_note":
            if not self.credit_note_id or self.credit_note_id.state != "posted":
                raise UserError(_("A posted credit note is required before closing this ticket."))
            if not self.resolution_completed_date:
                self.with_context(skip_portal_ticket_notification=True).write({
                    "resolution_completed_date": fields.Date.context_today(self),
                })

        elif not self.resolution_completed_date:
            self.with_context(skip_portal_ticket_notification=True).write({
                "resolution_completed_date": fields.Date.context_today(self),
            })

    def _resolution_automatic_message(self):
        self.ensure_one()
        reference = self.related_reference or self.name
        note = (self.resolution_note or "").strip()

        if self.resolution_type == "replacement":
            lines = [
                _("Your replacement request for %(reference)s has been approved.", reference=reference),
                _("Replacement delivery: %(delivery)s", delivery=self.replacement_picking_id.name),
                _("Expected delivery date: %(date)s", date=self._date_text(self.resolution_expected_date)),
            ]
            if self.replacement_tracking_reference:
                lines.append(_("Tracking reference: %(tracking)s", tracking=self.replacement_tracking_reference))
            lines.append(note)
            lines.append(_("The ticket will be closed after the replacement delivery is completed."))
            return "\n".join(filter(None, lines))

        if self.resolution_type == "refund":
            lines = [
                _("Your refund request for %(reference)s has been approved.", reference=reference),
                _("Refund amount: %(amount)s", amount=self._amount_text()),
                _("Expected completion date: %(date)s", date=self._date_text(self.resolution_expected_date)),
            ]
            if self.credit_note_id:
                lines.append(_(
                    "Refund credit note: %(credit_note)s",
                    credit_note=self.credit_note_id.name or self.credit_note_id.display_name,
                ))
            lines.extend([
                note,
                _("The ticket will be closed only after Odoo confirms the refund as paid."),
            ])
            return "\n".join(filter(None, lines))

        if self.resolution_type == "repair":
            return "\n".join([
                _("Your repair request for %(reference)s has been approved.", reference=reference),
                _("Expected completion date: %(date)s", date=self._date_text(self.resolution_expected_date)),
                note,
                _("The ticket will be closed after the repair is completed."),
            ])

        if self.resolution_type == "credit_note":
            return "\n".join([
                _("A credit note has been issued for %(reference)s.", reference=reference),
                _("Credit note: %(credit_note)s", credit_note=self.credit_note_id.name or self.credit_note_id.display_name),
                note,
            ])

        if self.resolution_type == "rejected":
            return "\n".join([
                _("Your request for %(reference)s has been reviewed and could not be approved.", reference=reference),
                _("Reason: %(reason)s", reason=note),
                _("Reply from the portal if you need further clarification."),
            ])

        return "\n".join([
            _("Ticket %(ticket)s has been resolved.", ticket=self.name),
            note,
            _("Reply from the portal if the issue still needs attention."),
        ])

    def _closure_automatic_message(self):
        self.ensure_one()
        completed = self._date_text(self.resolution_completed_date)

        if self.resolution_type == "replacement":
            return "\n".join([
                _("Your replacement product has been delivered successfully."),
                _("Delivery: %(delivery)s", delivery=self.replacement_picking_id.name),
                _("Delivered on: %(date)s", date=completed),
                _("Ticket %(ticket)s is now closed. Reply from the portal to reopen it if the issue remains unresolved.", ticket=self.name),
            ])

        if self.resolution_type == "refund":
            return "\n".join([
                _("Your refund has been completed and verified."),
                _("Refund amount: %(amount)s", amount=self._amount_text()),
                _(
                    "Refund credit note: %(credit_note)s",
                    credit_note=self.credit_note_id.name or self.credit_note_id.display_name,
                ),
                _("Completed on: %(date)s", date=completed),
                _("Ticket %(ticket)s is now closed.", ticket=self.name),
            ])

        if self.resolution_type == "repair":
            return "\n".join([
                _("The repair has been completed."),
                _("Completed on: %(date)s", date=completed),
                _("Ticket %(ticket)s is now closed.", ticket=self.name),
            ])

        if self.resolution_type == "credit_note":
            return "\n".join([
                _("Credit note %(credit_note)s has been issued and the request is complete.", credit_note=self.credit_note_id.name or self.credit_note_id.display_name),
                _("Ticket %(ticket)s is now closed.", ticket=self.name),
            ])

        if self.resolution_type == "rejected":
            return "\n".join([
                _("The request has been reviewed and closed."),
                _("Reason: %(reason)s", reason=(self.resolution_note or "").strip()),
                _("Reply from the portal to reopen ticket %(ticket)s if further review is required.", ticket=self.name),
            ])

        return _("Ticket %(ticket)s has been completed and closed. Reply from the portal if further assistance is required.", ticket=self.name)

    # ------------------------------------------------------------------
    # Portal notifications
    # ------------------------------------------------------------------

    def _notify_portal_state_change(self):
        Notification = self.env["business.portal.notification"]
        state_label = dict(self._fields["state"].selection).get(
            self.state,
            self.state,
        )
        notification_type = {
            "waiting_customer": "warning",
            "resolved": "success",
            "closed": "success",
            "cancelled": "warning",
        }.get(self.state, "info")

        Notification._create_portal_notification(
            partner=self.partner_id,
            company=self.company_id,
            category="support",
            notification_type=notification_type,
            title=_("Support ticket updated"),
            message=_(
                "%(ticket)s is now %(state)s.",
                ticket=self.name,
                state=state_label,
            ),
            event_key=f"support-ticket:{self.id}:state:{self.state}:{fields.Datetime.now()}",
            action_url=f"/my/support-tickets/{self.id}",
            document_reference=self.name,
            model_name=self._name,
            res_id=self.id,
            requires_action=self.state == "waiting_customer",
            send_email=True,
        )

    def _notify_portal_support_reply(self, message):
        body_text = html2plaintext(message.body or "").strip()
        if len(body_text) > 240:
            body_text = f"{body_text[:237]}..."

        is_complete = self.state in ("resolved", "closed")
        title = {
            "resolved": _("Support ticket resolved"),
            "closed": _("Support ticket closed"),
        }.get(self.state, _("New support reply"))
        notification_type = "success" if is_complete else "info"

        self.env["business.portal.notification"]._create_portal_notification(
            partner=self.partner_id,
            company=self.company_id,
            category="support",
            notification_type=notification_type,
            title=title,
            message=_(
                "Support updated %(ticket)s: %(message)s",
                ticket=self.name,
                message=body_text or _("Open the ticket to view the update."),
            ),
            event_key=f"support-ticket:{self.id}:message:{message.id}",
            action_url=f"/my/support-tickets/{self.id}",
            document_reference=self.name,
            model_name=self._name,
            res_id=self.id,
            requires_action=(self.state == "waiting_customer"),
            send_email=True,
        )

    # ------------------------------------------------------------------
    # Stage actions: staff only changes the stage; system communicates
    # ------------------------------------------------------------------

    def action_set_in_progress(self):
        for ticket in self:
            ticket._complete_customer_reply_activities()
            ticket.with_context(skip_portal_ticket_notification=True).write(
                {
                    "state": "in_progress",
                    "expected_reply_type": "none",
                    "customer_reply_status": "none",
                    "customer_reply_pending_review": False,
                }
            )
            ticket._post_automatic_reply(
                _(
                    "Your ticket %(ticket)s is now in progress. The support team is reviewing the information provided.",
                    ticket=ticket.name,
                )
            )
        return True

    def action_waiting_customer(self):
        for ticket in self:
            reply_type = ticket._default_expected_reply_type()
            ticket.with_context(skip_portal_ticket_notification=True).write(
                {
                    "state": "waiting_customer",
                    "expected_reply_type": reply_type,
                    "customer_reply_status": "pending",
                    "customer_reply_pending_review": False,
                }
            )
            ticket._post_automatic_reply(
                ticket._request_message_for_type(reply_type)
            )
        return True

    def action_resolve(self):
        for ticket in self:
            ticket._validate_resolution_for_resolve()
            ticket._complete_customer_reply_activities()
            ticket.with_context(skip_portal_ticket_notification=True).write(
                {
                    "state": "resolved",
                    "expected_reply_type": "none",
                    "customer_reply_status": "none",
                    "customer_reply_pending_review": False,
                    "resolution_completed_date": False,
                }
            )
            ticket._post_automatic_reply(ticket._resolution_automatic_message())
        return True

    def action_close(self):
        for ticket in self:
            ticket._validate_resolution_for_close()
            ticket._complete_customer_reply_activities()
            ticket.with_context(skip_portal_ticket_notification=True).write(
                {
                    "state": "closed",
                    "expected_reply_type": "none",
                    "customer_reply_status": "none",
                    "customer_reply_pending_review": False,
                }
            )
            ticket._post_automatic_reply(ticket._closure_automatic_message())
        return True

    def action_reopen(self):
        for ticket in self:
            ticket.with_context(skip_portal_ticket_notification=True).write(
                {
                    "state": "new",
                    "date_resolved": False,
                    "resolution_completed_date": False,
                    "expected_reply_type": "none",
                    "customer_reply_status": "none",
                    "customer_reply_pending_review": False,
                }
            )
            ticket._post_automatic_reply(
                _(
                    "Ticket %(ticket)s has been reopened and will be reviewed again.",
                    ticket=ticket.name,
                )
            )
        return True

    def _portal_initial_message_body(self):
        self.ensure_one()
        return Markup("<p>%s</p>") % escape(self.description or "")