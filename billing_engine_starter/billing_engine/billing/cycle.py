"""
BillingCycle — finds due subscriptions, generates invoices, posts ledger DEBITs,
advances the subscription period. Must be IDEMPOTENT (safe to run twice).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from billing_engine.db import (
    Database,
    CustomerRepository, PlanRepository, SubscriptionRepository,
    UsageRecordRepository, InvoiceRepository, InvoiceLineItemRepository,
    LedgerRepository, 
)

from billing_engine.models import Subscription, InvoiceLineItem
from billing_engine.models import LedgerEntry, LedgerDirection
from billing_engine.models import SubscriptionStatus, InvoiceStatus

from billing_engine.billing.pipeline import build_invoice

from billing_engine.billing.proration import compute_proration
from billing_engine.money import Money
from billing_engine.models import (
    Invoice,
    InvoiceLineItem,
    LineItemKind,
)

@dataclass
class BillingResult:
    invoices_created: int
    invoices_skipped_duplicate: int
    trials_activated: int


class BillingCycle:
    """Day-3 deliverable. Day-4 stretch: add `upgrade_subscription(...)`."""

    def __init__(
        self,
        db: Database,
        customer_repo: CustomerRepository,
        plan_repo: PlanRepository,
        subscription_repo: SubscriptionRepository,
        usage_repo: UsageRecordRepository,
        invoice_repo: InvoiceRepository,
        line_item_repo: InvoiceLineItemRepository,
        ledger_repo: LedgerRepository,
        strategy_factory: Callable,    # given a Plan, returns a PricingStrategy
        discount_factory: Callable,    # given a discount_id or None, returns a Discount or None
        tax_factory: Callable,         # given a Customer, returns (TaxCalculator, TaxContext)
    ) -> None:
        self.db = db
        self.customer_repo = customer_repo
        self.plan_repo = plan_repo
        self.subscription_repo = subscription_repo
        self.usage_repo = usage_repo
        self.invoice_repo = invoice_repo
        self.line_item_repo = line_item_repo
        self.ledger_repo = ledger_repo
        self.strategy_factory = strategy_factory
        self.discount_factory = discount_factory
        self.tax_factory = tax_factory

    # --------------------------------------------------------
    def run(self, as_of: date) -> BillingResult:
        invoices_created = 0
        invoices_skipped_duplicate = 0
        trials_activated = 0

        # -------------------------
        # Phase 1: Activate trials
        # -------------------------
        for sub in self.subscription_repo.list_all():
            if (
                sub.status.value == "TRIAL"
                and sub.trial_end
                and sub.trial_end <= as_of
            ):
                self.subscription_repo.update_status(
                    sub.id,
                    "ACTIVE",
                    None
                )
                trials_activated += 1

        # -------------------------
        # Phase 2: Bill due subs
        # -------------------------
        due = self.subscription_repo.get_due_for_billing(as_of)

        for sub in due:
            plan = self.plan_repo.get(sub.plan_id)
            customer = self.customer_repo.get(sub.customer_id)

            strategy = self.strategy_factory(plan)
            discount = self.discount_factory(sub.discount_id)
            tax_calc, tax_context = self.tax_factory(customer)

            usage = self.usage_repo.sum_for_period(
                sub.id,
                "units",
                sub.current_period_start,
                sub.current_period_end,
            )

            invoice_count = self.invoice_repo.count_for_subscription(sub.id)

            invoice = build_invoice(
                subscription=sub,
                plan=plan,
                strategy=strategy,
                discount=discount,
                tax_calc=tax_calc,
                tax_context=tax_context,
                usage_quantity=usage,
                period_start=sub.current_period_start,
                period_end=sub.current_period_end,
                invoice_count_so_far=invoice_count,
            )

            try:
                invoice.status = InvoiceStatus.ISSUED
                saved_invoice = self.invoice_repo.add(invoice)
        
                for li in invoice.line_items:
                    self.line_item_repo.add(
                        InvoiceLineItem(
                            id=None,
                            invoice_id=saved_invoice.id,
                            description=li.description,
                            amount=li.amount,
                            kind=li.kind,
                        )
                    )
                self.ledger_repo.add(
                    LedgerEntry(
                        id=None,
                        invoice_id=saved_invoice.id,
                        customer_id=sub.customer_id,
                        amount=invoice.total,
                        direction=LedgerDirection.DEBIT,
                        reason="INVOICE",
                    )
                )

                new_start = sub.current_period_end
                new_end = sub.current_period_end.replace(
                    month=sub.current_period_end.month + 1 if sub.current_period_end.month < 12 else 1,
                    year=sub.current_period_end.year + (1 if sub.current_period_end.month == 12 else 0)
                )

                self.subscription_repo.update_period(
                    sub.id,
                    new_start,
                    new_end,
                )

                invoices_created += 1

            except Exception as e:
                if "UNIQUE" in str(e):
                    invoices_skipped_duplicate += 1
                else:
                    raise


        return BillingResult(
            invoices_created=invoices_created,
            invoices_skipped_duplicate=invoices_skipped_duplicate,
            trials_activated=trials_activated,
        )
    # --------------------------------------------------------
    def upgrade_subscription(self, subscription_id: int, new_plan_id: int, switch_date: date) -> None:
        """Mid-cycle upgrade — Day 4 stretch."""
        
        subscription = self.subscription_repo.get(subscription_id)

        if subscription is None:
            raise ValueError("Subscription not found")

        old_plan = self.plan_repo.get(subscription.plan_id)
        new_plan = self.plan_repo.get(new_plan_id)

        customer = self.customer_repo.get(subscription.customer_id)

        old_strategy = self.strategy_factory(old_plan)
        new_strategy = self.strategy_factory(new_plan)

        old_price = old_strategy.amount
        new_price = new_strategy.amount

        tax_calc, tax_context = self.tax_factory(customer)

        pr = compute_proration(
            old_plan_price=old_price,
            new_plan_price=new_price,
            period_start=subscription.current_period_start,
            period_end=subscription.current_period_end,
            switch_date=switch_date,
            tax_calc=tax_calc,
            tax_context=tax_context,
        )

        total = (
            pr.charge_amount
            + pr.charge_tax
            - pr.credit_amount
            - pr.credit_tax
        )

        invoice = Invoice(
            id=None,
            subscription_id=subscription.id,
            period_start=switch_date,
            period_end=subscription.current_period_end,
            subtotal=pr.charge_amount - pr.credit_amount,
            discount_total=Money.zero(total.currency),
            tax_total=pr.charge_tax - pr.credit_tax,
            total=total,
            status=InvoiceStatus.ISSUED,
        )

        saved_invoice = self.invoice_repo.add(invoice)

        self.line_item_repo.add(
            InvoiceLineItem(
                id=None,
                invoice_id=saved_invoice.id,
                description="Proration Credit",
                amount=-pr.credit_amount,
                kind=LineItemKind.PRORATION_CREDIT,
            )
        )

        self.line_item_repo.add(
            InvoiceLineItem(
                id=None,
                invoice_id=saved_invoice.id,
                description="Proration Charge",
                amount=pr.charge_amount,
                kind=LineItemKind.PRORATION_CHARGE,
            )
        )

        self.ledger_repo.add(
            LedgerEntry(
                id=None,
                invoice_id=saved_invoice.id,
                customer_id=subscription.customer_id,
                amount=total,
                direction=LedgerDirection.DEBIT,
                reason="PRORATION",
            )
        )

        self.subscription_repo.update_plan(
            subscription_id,
            new_plan_id,
        )