# Business Portal Dashboard — Odoo 19

An original website portal dashboard for Odoo 19. It does **not** open backend
applications. Sales, invoices, purchases, projects and tasks open Odoo's normal
secure portal pages. Deliveries and receipts use a custom secure portal page.

## Included logic

- Customer, vendor, both, or automatic portal role
- Sales, billing, purchase, logistics, projects and tasks cards
- Period filter: 7/30/90 days, current month/year, all time
- Overdue invoice, expiring quotation and late transfer alerts
- Custom delivery/receipt list with status, period, search and pagination
- Custom transfer detail page with product quantities
- Current-company filtering
- Option to hide zero-value cards
- Responsive original design

## Installation

1. Copy `business_portal_dashboard` into an Odoo addons directory.
2. Restart Odoo.
3. Update Apps List.
4. Install **Business Portal Dashboard**.
5. Open a company/contact as an administrator and configure the **Portal Dashboard** tab.
6. Grant the contact portal access and open `/my/business-dashboard`.

CLI example:

```bash
./odoo-bin -d YOUR_DATABASE -u business_portal_dashboard --stop-after-init
```

## Security notes

Standard Odoo portal ACLs and record rules are used for Sales, Purchase,
Accounting, Projects and Tasks. Stock pickings normally have no generic portal
page, so the custom logistics routes use `sudo()` only after applying strict
server-side domains tied to the authenticated user's commercial partner,
current company, and the related Sale/Purchase order.

Do not weaken those delivery domains or accept a partner ID from query-string
parameters.

## Before production

This starter module is syntax-checked, but it must be tested on your exact Odoo
19 Community/Enterprise build, localization, installed addons, multi-company
rules, and data model customizations. Add automated portal security tests before
production deployment.

## Account Statement & Aging

The Billing & Invoices dashboard card includes a Statement link. The statement page provides opening and closing balances, charges, payments/credits, a current aging breakdown, paginated ledger lines, date filters, customer/vendor statement selection, and Excel-compatible CSV export.

## Portal notifications and email reminders

Version 19.0.4.0.0 adds:

- unread notification bell and latest notifications on the business dashboard;
- a paginated `/my/business-notifications` page with category, unread, and action-required filters;
- mark-all-read and secure document links;
- portal notification preferences and email on/off controls;
- immediate notifications for posted invoices/bills, confirmed/cancelled sales and purchase orders, and transfer state changes;
- a daily scheduled action for invoice due/overdue, quotation expiry, task deadline, and delayed transfer reminders;
- duplicate protection through a unique business event key.

Email delivery uses Odoo's configured outgoing mail server. Administrators can run
`Business Portal: Notification and Email Reminders` manually from Scheduled Actions
to test date-based reminders.
