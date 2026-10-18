from odoo import _, models


class AccountMove(models.Model):
    _inherit = "account.move"

    def action_post(self):
        draft_moves = self.filtered(lambda move: move.state == "draft")
        result = super().action_post()
        Notification = self.env["business.portal.notification"]
        for move in draft_moves.filtered(
            lambda item: item.state == "posted"
            and item.move_type
            in (
                "out_invoice",
                "out_refund",
                "out_receipt",
                "in_invoice",
                "in_refund",
                "in_receipt",
            )
            and item.partner_id
        ):
            is_customer_document = move.move_type.startswith("out_")
            document_type = {
                "out_invoice": _("Invoice"),
                "out_receipt": _("Invoice"),
                "out_refund": _("Credit note"),
                "in_invoice": _("Vendor bill"),
                "in_receipt": _("Vendor bill"),
                "in_refund": _("Vendor credit note"),
            }.get(move.move_type, _("Accounting document"))
            reference = move.name or move.display_name
            Notification._create_portal_notification(
                partner=move.partner_id,
                company=move.company_id,
                category="billing",
                notification_type="info",
                title=_("New %(document)s available", document=document_type),
                message=_(
                    "%(document)s %(reference)s has been posted and is available in your portal.",
                    document=document_type,
                    reference=reference,
                ),
                event_key=f"move:{move.id}:posted",
                action_url=Notification._safe_document_url(move, "/my/invoices"),
                document_reference=reference,
                model_name=move._name,
                res_id=move.id,
                requires_action=is_customer_document and move.amount_residual > 0,
            )
        return result


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_confirm(self):
        candidates = self.filtered(lambda order: order.state in ("draft", "sent"))
        result = super().action_confirm()
        Notification = self.env["business.portal.notification"]
        for order in candidates.filtered(
            lambda item: item.state in ("sale", "done") and item.partner_id
        ):
            Notification._create_portal_notification(
                partner=order.partner_id,
                company=order.company_id,
                category="sales",
                notification_type="success",
                title=_("Sales order confirmed"),
                message=_(
                    "Your sales order %(reference)s has been confirmed.",
                    reference=order.name,
                ),
                event_key=f"sale:{order.id}:confirmed",
                action_url=Notification._safe_document_url(order, "/my/orders"),
                document_reference=order.name,
                model_name=order._name,
                res_id=order.id,
            )
        return result

    def action_cancel(self):
        candidates = self.filtered(lambda order: order.state != "cancel")
        result = super().action_cancel()
        Notification = self.env["business.portal.notification"]
        for order in candidates.filtered(
            lambda item: item.state == "cancel" and item.partner_id
        ):
            Notification._create_portal_notification(
                partner=order.partner_id,
                company=order.company_id,
                category="sales",
                notification_type="critical",
                title=_("Sales document cancelled"),
                message=_(
                    "Sales document %(reference)s has been cancelled.",
                    reference=order.name,
                ),
                event_key=f"sale:{order.id}:cancelled",
                action_url=Notification._safe_document_url(order, "/my/orders"),
                document_reference=order.name,
                model_name=order._name,
                res_id=order.id,
            )
        return result


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    def button_approve(self, force=False):
        candidates = self.filtered(
            lambda order: order.state in ("draft", "sent", "to approve")
        )
        result = super().button_approve(force=force)
        Notification = self.env["business.portal.notification"]
        for order in candidates.filtered(
            lambda item: item.state in ("purchase", "done") and item.partner_id
        ):
            Notification._create_portal_notification(
                partner=order.partner_id,
                company=order.company_id,
                category="purchase",
                notification_type="success",
                title=_("Purchase order confirmed"),
                message=_(
                    "Purchase order %(reference)s has been confirmed.",
                    reference=order.name,
                ),
                event_key=f"purchase:{order.id}:confirmed",
                action_url=Notification._safe_document_url(order, "/my/purchase"),
                document_reference=order.name,
                model_name=order._name,
                res_id=order.id,
            )
        return result

    def button_cancel(self):
        candidates = self.filtered(lambda order: order.state != "cancel")
        result = super().button_cancel()
        Notification = self.env["business.portal.notification"]
        for order in candidates.filtered(
            lambda item: item.state == "cancel" and item.partner_id
        ):
            Notification._create_portal_notification(
                partner=order.partner_id,
                company=order.company_id,
                category="purchase",
                notification_type="critical",
                title=_("Purchase document cancelled"),
                message=_(
                    "Purchase document %(reference)s has been cancelled.",
                    reference=order.name,
                ),
                event_key=f"purchase:{order.id}:cancelled",
                action_url=Notification._safe_document_url(order, "/my/purchase"),
                document_reference=order.name,
                model_name=order._name,
                res_id=order.id,
            )
        return result


class StockPicking(models.Model):
    _inherit = "stock.picking"

    def write(self, vals):
        old_states = {picking.id: picking.state for picking in self}
        result = super().write(vals)
        if "state" not in vals:
            return result

        Notification = self.env["business.portal.notification"]
        state_configuration = {
            "assigned": ("warning", _("Transfer ready"), _("ready"), False),
            "done": ("success", _("Transfer completed"), _("completed"), False),
            "cancel": ("critical", _("Transfer cancelled"), _("cancelled"), False),
        }
        for picking in self:
            if (
                not picking.partner_id
                or picking.picking_type_code not in ("incoming", "outgoing")
                or old_states.get(picking.id) == picking.state
                or picking.state not in state_configuration
            ):
                continue
            level, title, state_label, requires_action = state_configuration[
                picking.state
            ]
            direction = (
                _("Delivery")
                if picking.picking_type_code == "outgoing"
                else _("Receipt")
            )
            Notification._create_portal_notification(
                partner=picking.partner_id,
                company=picking.company_id,
                category="logistics",
                notification_type=level,
                title=title,
                message=_(
                    "%(direction)s %(reference)s is now %(state)s.",
                    direction=direction,
                    reference=picking.name,
                    state=state_label,
                ),
                event_key=f"picking:{picking.id}:state:{picking.state}",
                action_url=f"/my/business-deliveries/{picking.id}",
                document_reference=picking.name,
                model_name=picking._name,
                res_id=picking.id,
                requires_action=requires_action,
            )
        return result
