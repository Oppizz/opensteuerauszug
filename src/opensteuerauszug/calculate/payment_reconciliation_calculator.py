from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional

from opensteuerauszug.model.critical_warning import CriticalWarningCategory
from opensteuerauszug.model.ech0196 import (
    PaymentTypeOriginal,
    Security,
    SecurityPayment,
    TaxStatement,
)
from opensteuerauszug.model.payment_reconciliation import (
    PaymentReconciliationReport,
    PaymentReconciliationRow,
    TaxValueReconciliationRow,
)
from ..render.translations import DEFAULT_LANGUAGE

logger = logging.getLogger(__name__)


@dataclass
class _BrokerAgg:
    dividend: Decimal = Decimal("0")
    dividend_capital_gain: Decimal = Decimal("0")
    dividend_currency: Optional[str] = None
    withholding: Decimal = Decimal("0")
    withholding_currency: Optional[str] = None
    withholding_entry_text: Optional[str] = None
    allows_broker_above_kursliste: bool = False
    exchange_rate: Optional[Decimal] = None
    wth_correction_late_date: Optional[date] = None
    short_stock: Optional[bool] = None

@dataclass
class _KurslisteAgg:
    dividend_chf: Decimal = Decimal("0")
    withholding_chf: Decimal = Decimal("0")
    exchange_rate: Optional[Decimal] = None
    noncash: bool = False
    allows_broker_above_kursliste: bool = False
    has_capped_payment: bool = False
    has_added_withholding: bool = False
    original_withholding_chf: Optional[Decimal] = None
    kursliste: Optional[bool] = None
    currency: Optional[str] = None


class PaymentReconciliationCalculator:
    _COUNTRIES_WHERE_OVERWITHHOLDING_SUGGESTS_CALCULATION_ISSUE = {
        "GB",  # United Kingdom (0% WHT)
        "NL",  # Netherlands (15% - matches treaty)
        "LU",  # Luxembourg (15% - matches treaty)
        "US",  # USA (15% via W-8BEN)
        "CA",  # Canada (15% via NR301)
        "JP",  # Japan (15% via Relief at Source)
        "FR",  # France (12.8% or 15% via Form 5000/5001)
        "IE",  # Ireland (0% via Form V2)
        "SG",  # Singapore (0% WHT)
        "HK",  # Hong Kong (0% WHT)
        "AU",  # Australia (0% on Franked dividends)
    }

    _BROKER_ABOVE_KURSLISTE_ALLOWLIST_SIGNS = {
        "(H)",
        "(IK)",
        "(KG)",
        "(KR)",
        "KEP",
    }

    _BROKER_ABOVE_KURSLISTE_ALLOWLIST_KEYWORDS = (
        "return of capital",
        "capital gain",
        "kapitaleinlage",
        "kapitalgewinn",
    )

    def __init__(self, tolerance_chf: Decimal = Decimal("0.05"), tolerance_frac: Decimal = Decimal("0.001"), allow_above_treaty_withholding: bool = False):
        self.tolerance_chf = tolerance_chf
        self.tolerance_frac = tolerance_frac
        self.allow_above_treaty_withholding=allow_above_treaty_withholding
        self.skip_broker_payment = False
        self.language=DEFAULT_LANGUAGE
        self.periodFrom: Optional[date] = None
        self.periodTo: Optional[date] = None

    def calculate(self, tax_statement: TaxStatement) -> TaxStatement:
        report = PaymentReconciliationReport()

        if not tax_statement.listOfSecurities or not tax_statement.listOfSecurities.depot:
            tax_statement.payment_reconciliation_report = report
            return tax_statement
        
        self.periodFrom = tax_statement.periodFrom
        self.periodTo = tax_statement.periodTo

        for depot in tax_statement.listOfSecurities.depot:
            for security in depot.security:
                rows = self._reconcile_security(security)
                report.rows.extend(rows)
                tax_value_rows = self._reconcile_tax_value(security)
                report.tax_value_rows.extend(tax_value_rows)

        for row in report.rows:
            if row.status == "match":
                report.match_count += 1
            elif row.status == "expected":
                report.expected_missing_count += 1
            elif row.status == "capped":
                report.capped_count += 1
            else:
                report.mismatch_count += 1

        tax_statement.payment_reconciliation_report = report

        self._dismiss_reconciled_previous_year_exdate_warnings(tax_statement, report)

        return tax_statement

    def _dismiss_reconciled_previous_year_exdate_warnings(
        self,
        tax_statement: TaxStatement,
        report: PaymentReconciliationReport,
    ) -> None:
        """Remove PREVIOUS_YEAR_EXDATE critical warnings whose payment was successfully reconciled."""
        prev_year_warnings = [
            w
            for w in tax_statement.critical_warnings
            if w.category == CriticalWarningCategory.PREVIOUS_YEAR_EXDATE and w.payment_date
        ]
        if not prev_year_warnings:
            return

        matched_dates_by_identifier = defaultdict(set)
        for row in report.rows:
            if row.matched and row.identifier and row.payment_date and row.kursliste:
                matched_dates_by_identifier[row.identifier].add(row.payment_date)

        dismissed = []
        for warning in prev_year_warnings:
            if warning.identifier and warning.payment_date in matched_dates_by_identifier.get(
                warning.identifier, set()
            ):
                dismissed.append(warning)
                logger.info(
                    "Dismissing previous-year ex-date warning for %s on %s "
                    "(reconciliation matched)",
                    warning.identifier,
                    warning.payment_date,
                )

        if dismissed:
            tax_statement.critical_warnings = [
                w for w in tax_statement.critical_warnings if w not in dismissed
            ]

    def _reconcile_security(self, security: Security) -> List[PaymentReconciliationRow]:
        broker_payments = security.broker_payments or [p for p in security.payment if not p.kursliste]
        kursliste_payments = [p for p in security.payment if p.kursliste or self.skip_broker_payment == False]
        security_has_sensitive_overwithholding = (
            security.country in self._COUNTRIES_WHERE_OVERWITHHOLDING_SUGGESTS_CALCULATION_ISSUE
        )

        broker_by_date: Dict[date, _BrokerAgg] = defaultdict(_BrokerAgg)
        kurs_by_date: Dict[date, _KurslisteAgg] = defaultdict(_KurslisteAgg)

        kursliste_payments_amount = [p for p in kursliste_payments if p.kursliste and (p.grossRevenueA or Decimal("0")) + (p.grossRevenueB or Decimal("0")) != Decimal("0")]
        has_kursliste = any(kursliste_payments_amount)

        for payment in broker_payments:
            key_date = payment.paymentDate
            if has_kursliste and (payment.amount or payment.withHoldingTaxClaim or payment.nonRecoverableTaxAmountOriginal) and payment.reportDate and payment.reportDate.year == payment.paymentDate.year and ((payment.paymentDate < payment.reportDate and security.securityCategory == "BOND") or security.securityCategory in ["SHARE", "FUND"]):
                if any(p for p in kursliste_payments_amount if p.paymentDate == payment.paymentDate) == False:
                    if any(p for p in kursliste_payments_amount if p.paymentDate == payment.reportDate):
                        key_date = payment.reportDate
                    else:
                        check_date = payment.reportDate + timedelta(days=1)
                        if check_date.year == payment.paymentDate.year and any(p for p in kursliste_payments_amount if p.paymentDate == check_date):
                            key_date = check_date
                        else:
                            check_date = payment.reportDate - timedelta(days=1)
                            if check_date.year == payment.paymentDate.year and any(p for p in kursliste_payments_amount if p.paymentDate == check_date):
                                key_date = check_date
            agg = broker_by_date[key_date]
            self._accumulate_broker(agg, payment)

        for payment in kursliste_payments:
            key_date = payment.paymentDate
            agg = kurs_by_date[key_date]
            self._accumulate_kursliste(agg, payment)

        all_dates = sorted(set(broker_by_date.keys()) | set(kurs_by_date.keys()))
        rows: List[PaymentReconciliationRow] = []
        security_label = security.securityName
        security_identifier = str(security.isin) if security.isin else None
        country = security.country

        for d in all_dates:
            broker = broker_by_date.get(d, _BrokerAgg())
            kurs = kurs_by_date.get(d, _KurslisteAgg())

            has_broker = d in broker_by_date
            has_kurs = d in kurs_by_date

            broker_dividend_amount_curr=broker.dividend if broker.dividend_currency else None

            broker_div_chf = None
            broker_div_capital_gain_chf = None
            broker_with_chf = None
            if kurs.exchange_rate is not None:
                if broker.dividend_currency is not None and kurs.currency is not None and broker.dividend_currency == kurs.currency:
                    broker_div_chf = broker.dividend * kurs.exchange_rate
                if broker.dividend_currency is not None and kurs.currency is not None and broker.dividend_currency == kurs.currency:
                    broker_div_capital_gain_chf = broker.dividend_capital_gain * kurs.exchange_rate
                if broker.withholding_currency is not None and kurs.currency is not None and broker.withholding_currency == kurs.currency:
                    broker_with_chf = broker.withholding * kurs.exchange_rate
            if broker_div_chf is None and broker.dividend_currency is not None and broker.exchange_rate is not None:
                broker_div_chf = broker.dividend * broker.exchange_rate
            if broker_div_capital_gain_chf is None and broker.dividend_currency is not None and broker.exchange_rate is not None:
                broker_div_capital_gain_chf = broker.dividend_capital_gain * broker.exchange_rate
            if broker_with_chf is None and broker.withholding_currency is not None and broker.exchange_rate is not None:
                broker_with_chf = broker.withholding * broker.exchange_rate

            matched = False
            status = "mismatch"
            note = None
            skip = False
            kursliste_undefined = None

            # Detect capped payments (WithholdingCapCalculator already ran).
            if has_kurs and kurs.has_capped_payment:
                status = "capped"
                matched = True
                original_wht = kurs.original_withholding_chf
                note = (
                    f"Withholding capped to broker level ("
                    f"{kurs.withholding_chf:.2f} CHF)."
                    if kurs.withholding_chf is not None
                    else "Withholding capped to broker level."
                )
                if has_broker and broker.wth_correction_late_date is not None:
                    note += f" Late correction on {broker.wth_correction_late_date}."
            elif has_broker and kurs.has_added_withholding:
                status = "expected" if security.kursliste else "capped"
                matched = False
                original_wht = kurs.original_withholding_chf
                note = (
                    f"Withholding adjusted to broker level ("
                    f"{kurs.withholding_chf:.2f} CHF)."
                    if kurs.withholding_chf is not None
                    else "Withholding adjusted to broker level."
                )
                if has_kurs and security.kursliste:
                    note += " Check in tax software"
            elif has_kurs and not has_broker and kurs.noncash:
                status = "expected"
                if kurs.dividend_chf or kurs.withholding_chf:
                    note = "Accumulating fund payment expected to be absent in broker cash flow."
                if security.stock:
                    corp_actions = [s for s in security.stock if s.mutation and s.corpAction]
                    if any(corp_actions):
                        for payment in kursliste_payments:
                            if payment.paymentDate == d and payment.payment_type_original is not None and payment.payment_type_original != PaymentTypeOriginal.STANDARD:
                                ref_date = payment.exDate if payment.exDate else payment.paymentDate
                                corp_actions_date = [s for s in corp_actions if s.referenceDate == ref_date and s.balanceCurrency == payment.amountCurrency]
                                if len(corp_actions_date) == 0:
                                    corp_actions_date = [s for s in corp_actions if s.referenceDate == ref_date]
                                if any(corp_actions_date):
                                    corp_action = corp_actions_date[0]
                                    if corp_action.name:
                                        note = corp_action.name
                                        break
                if note is None:
                    if any((p for p in kursliste_payments if p.paymentDate == d and (p.payment_type_original not in(PaymentTypeOriginal.OTHER_BENEFIT, PaymentTypeOriginal.FUND_ACCUMULATION) or p.undefined))) == False:
                        status = "match"
            elif has_kurs and has_broker:
                div_note=''
                w_note=''
                allow_broker_dividend_above_kursliste = (
                    kurs.allows_broker_above_kursliste or broker.allows_broker_above_kursliste
                )
                allow_broker_withholding_above_kursliste = (
                    allow_broker_dividend_above_kursliste
                    or self.allow_above_treaty_withholding
                )
                div_diff = self._component_mismatches(
                    kurs_value_chf=kurs.dividend_chf,
                    broker_value_chf=broker_div_chf,
                    broker_capital_gain_chf=broker_div_capital_gain_chf,
                    allow_bidirectional_on_noncash=kurs.noncash,
                    allow_broker_above_kursliste=allow_broker_dividend_above_kursliste,
                )
                w_diff = self._component_mismatches(
                    kurs_value_chf=kurs.withholding_chf,
                    broker_value_chf=broker_with_chf,
                    allow_bidirectional_on_noncash=kurs.noncash,
                    allow_broker_above_kursliste=(
                        allow_broker_withholding_above_kursliste
                        or not security_has_sensitive_overwithholding
                    ),
                )
                if div_diff:
                    broker_dividend_amount_curr = (broker_dividend_amount_curr or Decimal("0")) - broker.dividend_capital_gain
                    div_note = f'Broker dividend differs from Kursliste value beyond tolerance. delta={div_diff} CHF.'
                if w_diff:
                    if broker.wth_correction_late_date is not None:
                        w_note = f"Broker wth is below Kursliste val; late correction on {broker.wth_correction_late_date}."
                    else:
                        if kurs.withholding_chf is None or kurs.withholding_chf == Decimal("0"):
                            w_note = "No wth in Kursliste."
                        else:
                            w_note = f'Broker withholding differs from Kursliste value beyond tolerance. delta={w_diff} CHF.'
                note =' '.join([div_note, w_note])
                if (
                    w_diff
                    and not div_diff
                    and kurs.withholding_chf != 0
                    and abs((broker_with_chf / kurs.withholding_chf) - 2) < 0.01
                    and country == "US"
                ):
                    note = (f'Broker withholding is twice Kursliste value, check that your broker has a valid '
                            f'W8-BEN. delta={w_diff} CHF.')
                matched = not (div_diff or w_diff)
                status = "match" if matched else "mismatch"
                if broker.short_stock and div_diff < Decimal("0") and not kurs.dividend_chf:
                    matched = True
                    status = "expected"
                    note += " Short stock dividend set to 0."
            #elif not has_kurs and has_broker and broker.allows_broker_above_kursliste:
            #    skip = True
            elif not has_kurs and has_broker:
                note = "Broker paym has no Kursliste entry."
                if broker.allows_broker_above_kursliste:
                    status = "match"
                    matched = True
                    if security.kursliste:
                        note = "Tax free paym."
                    else:
                        note = None
            elif has_kurs and not has_broker:
                if kurs.dividend_chf in (None, Decimal("0")):
                    payment_undef = next((p for p in kursliste_payments if p.paymentDate == d and (p.undefined or p.payment_type_original == PaymentTypeOriginal.FUND_ACCUMULATION)), None)
                    if payment_undef:
                        status = "expected"
                        note = f"{payment_undef.name} {payment_undef.sign if payment_undef.sign else ""}"
                        kursliste_undefined = True
                        if payment_undef.remark and len(payment_undef.remark)>0:
                            remark = next((r for r in payment_undef.remark if r.text and r.lang and r.lang.lower() == self.language.lower()), None) or remark[0]
                            if remark and remark.text:
                                note += f": {remark.text}"
                elif (
                    abs(kurs.dividend_chf) < Decimal("0.01")
                    and abs(kurs.withholding_chf) < Decimal("0.01")
                ):
                    status = "match"
                    matched = True
                    note = "Kursliste amounts are negligible; missing broker entry accepted."
                else:
                    note = "Kursliste payment has no broker evidence."
            else:
                status = "match"
                matched = True

            if skip:
                continue

            rows.append(
                PaymentReconciliationRow(
                    country=country,
                    security=security_label,
                    identifier=security_identifier,
                    payment_date=d,
                    kursliste_dividend_chf=kurs.dividend_chf,
                    kursliste_withholding_chf=kurs.original_withholding_chf or kurs.withholding_chf,
                    kursliste_amount_currency=kurs.currency if kurs and kurs.currency else broker.dividend_currency,
                    broker_dividend_amount=broker_dividend_amount_curr,
                    broker_dividend_currency=broker.dividend_currency,
                    broker_withholding_amount=broker.withholding if broker.withholding_currency else None,
                    broker_withholding_currency=broker.withholding_currency,
                    broker_withholding_entry_text=self._remove_symbol_prefix(broker.withholding_entry_text, security),
                    exchange_rate=kurs.exchange_rate,
                    accumulating=kurs.noncash,
                    matched=matched,
                    status=status,
                    note=self._remove_symbol_prefix(note, security),
                    kursliste=has_kurs and kurs.kursliste is not None and kurs.kursliste,
                    kursliste_security=security.kursliste is not None and security.kursliste,
                    kursliste_undefined=kursliste_undefined
                )
            )

        return rows
    # Return Decimal('0') if there is no mismatch, otherwise the CHF delta.
    def _component_mismatches(
        self,
        kurs_value_chf: Decimal,
        broker_value_chf: Optional[Decimal],
        allow_bidirectional_on_noncash: bool,
        allow_broker_above_kursliste: bool,
        broker_capital_gain_chf: Optional[Decimal] = None,
    ) -> Decimal:
        mismatch_precision = Decimal("0.01")
        if broker_value_chf is None:
            if abs(kurs_value_chf) < Decimal("0.01"):
                return Decimal("0")
            else:
                return (-kurs_value_chf).quantize(mismatch_precision)
            
        if kurs_value_chf is None:
            if abs(broker_value_chf) < Decimal("0.01"):
                return Decimal("0")
            else:
                return -broker_value_chf.quantize(mismatch_precision)

        if allow_bidirectional_on_noncash:
            return Decimal("0")

        delta = broker_value_chf - kurs_value_chf
        if abs(delta) <= self.tolerance_chf:
            return Decimal("0")

        if abs(delta) <= self.tolerance_frac * broker_value_chf:
            return Decimal("0")

        if broker_capital_gain_chf is not None: 
            delta = broker_value_chf - broker_capital_gain_chf - kurs_value_chf
            if abs(delta) <= self.tolerance_chf:
                return Decimal("0")
        elif delta > self.tolerance_chf and allow_broker_above_kursliste:
            return Decimal("0")

        return delta.quantize(mismatch_precision)
    
    def _remove_symbol_prefix(self, text: Optional[str], security: Security) -> str:
        if text and security and security.isin and security.symbol:
            text = text.removeprefix(f"{security.symbol}({security.isin})").removeprefix(f"{security.symbol} ({security.isin})").strip()
        return text

    def _accumulate_broker(self, agg: _BrokerAgg, payment: SecurityPayment) -> None:
        if self._is_broker_above_kursliste_allowlisted(payment):
            agg.allows_broker_above_kursliste = True
            agg.dividend_capital_gain += payment.amount or Decimal("0")

        if payment.quantity < Decimal("0"):
            agg.short_stock = True

        non_recoverable_original = payment.nonRecoverableTaxAmountOriginal
        withholding_claim = payment.withHoldingTaxClaim

        if agg.exchange_rate is None and payment.exchangeRate is not None:
            agg.exchange_rate = payment.exchangeRate

        if payment.reportDate > self.periodTo and agg.wth_correction_late_date is None:
            agg.wth_correction_late_date = payment.reportDate

        if withholding_claim is not None and withholding_claim != 0:
            agg.withholding += withholding_claim
            agg.withholding_currency = "CHF"
            agg.withholding_entry_text = payment.name or payment.broker_label_original
            return

        if non_recoverable_original is not None and non_recoverable_original != 0:
            agg.withholding += non_recoverable_original
            agg.withholding_currency = payment.amountCurrency
            agg.withholding_entry_text = payment.name or payment.broker_label_original
            return

        amount = payment.amount or Decimal("0")
        agg.dividend += amount
        agg.dividend_currency = payment.amountCurrency

    def _accumulate_kursliste(self, agg: _KurslisteAgg, payment: SecurityPayment) -> None:
        agg.dividend_chf += (payment.grossRevenueA or Decimal("0")) + (payment.grossRevenueB or Decimal("0"))
        agg.withholding_chf += (
            (payment.withHoldingTaxClaim or Decimal("0"))
            + (payment.nonRecoverableTaxAmount or Decimal("0"))
        )
        if agg.exchange_rate is None and payment.exchangeRate is not None:
            agg.exchange_rate = payment.exchangeRate
        payment_type = payment.payment_type_original
        if payment_type is not None and payment_type != PaymentTypeOriginal.STANDARD and (payment_type != PaymentTypeOriginal.FUND_ACCUMULATION or ((not payment.remark or len(payment.remark) == 0) and not payment.undefined and (payment.sign is None or payment.sign not in ("(I)")))):
            agg.noncash = True
        if agg.currency is None and payment.amountCurrency is not None:
            agg.currency = payment.amountCurrency

        if self._is_broker_above_kursliste_allowlisted(payment):
            agg.allows_broker_above_kursliste = True

        # Detect payments that were already capped by WithholdingCapCalculator.
        if payment.withholding_capped:
            agg.has_capped_payment = True
            agg.original_withholding_chf = payment.withholding_capped_original_wht_chf + (agg.original_withholding_chf if agg.original_withholding_chf is not None else Decimal("0"))
        elif payment.withholding_added and not agg.has_capped_payment:
            agg.has_added_withholding = True
            agg.original_withholding_chf = payment.withholding_added_original_wht_chf + (agg.original_withholding_chf if agg.original_withholding_chf is not None else Decimal("0"))

        agg.kursliste = payment.kursliste

    def _is_broker_above_kursliste_allowlisted(self, payment: SecurityPayment) -> bool:
        if payment.sign is not None and payment.sign in self._BROKER_ABOVE_KURSLISTE_ALLOWLIST_SIGNS:
            return True

        searchable_values = [
            payment.sign,
            payment.name,
            payment.broker_label_original,
        ]
        lowered_values = [value.lower() for value in searchable_values if value]
        return any(
            keyword in value
            for keyword in self._BROKER_ABOVE_KURSLISTE_ALLOWLIST_KEYWORDS
            for value in lowered_values
        )

    def _reconcile_tax_value(self, security: Security) -> List[TaxValueReconciliationRow]:
        rows: List[TaxValueReconciliationRow] = []
        tax_value = security.taxValue
        if not tax_value or not getattr(tax_value, 'quantity', None):
            return rows
        
        status = "match"
        matched = True
        note = ""
        exchange_rate = tax_value.exchangeRate
        if getattr(tax_value, 'kursliste', None):
            ref_date = tax_value.referenceDate
            value_broker = getattr(tax_value, "valueBroker", None)
            exchange_rate_kl = getattr(tax_value, "exchangeRateKursliste", None)
            if exchange_rate_kl:
                exchange_rate = exchange_rate_kl
            if not ref_date or self._component_mismatches(tax_value.value, value_broker, False, False):
                status = "mismatch"        
                matched = False
                if tax_value.value and value_broker:
                    diff_chf = tax_value.value - value_broker
                    decimal_value = Decimal(str(diff_chf)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    formatted = '{:,.2f}'.format(decimal_value).replace(',', "'")
                    if diff_chf > 0:
                        formatted = f"+{formatted}"
                    note = f"Diff CHF {formatted}"
        elif getattr(tax_value, 'undefined', None) and tax_value.balance and abs(tax_value.balance) > Decimal("0.01"):
                status = "mismatch"        
                matched = False

        rows.append(
            TaxValueReconciliationRow(
                country=security.country,
                security=security.securityName,
                kursliste_value_chf=tax_value.value,
                broker_amount=tax_value.balanceBroker,
                broker_amount_currency=tax_value.balanceCurrencyBroker,
                exchange_rate=exchange_rate,
                matched=matched,
                status=status,
                note=note,
                kursliste=getattr(tax_value, 'kursliste', None) or False,
                kursliste_security=security.kursliste is not None and security.kursliste,
                kursliste_undefined=getattr(tax_value, 'undefined', None)
            )
        )

        return rows
    