from .base import BaseCalculator, CalculationMode, CalculationError
from ..model.ech0196 import (
    SecurityStock,
    TaxStatement,
    BankAccount,  # Added BankAccount
    BankAccountTaxValue,
    BankAccountPayment,
    LiabilityAccountTaxValue,
    LiabilityAccountPayment,
    Security,  # Added Security
    SecurityTaxValue,
    SecurityPayment,  # Added SecurityPayment
    PaymentTypeOriginal,
)
from ..core.exchange_rate_provider import ExchangeRateProvider
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from ..core.constants import WITHHOLDING_TAX_RATE
from typing import Dict, Tuple, Optional, List
from datetime import date, timedelta
import logging


INTERNAL_ONLY_SECURITY_PAYMENT_FIELDS = (
    "broker_label_original",
    "nonRecoverableTaxAmountOriginal",
    "payment_type_original",
)


class MinimalTaxValueCalculator(BaseCalculator):
    """
    A minimal implementation of a tax value calculator. This computes only simple
    uncontroversial values. Mainly currenty conversions.
    """
    _CHF_CURRENCY = "CHF"
    _current_account_is_type_A: Optional[bool]
    _current_security_is_type_A: Optional[bool]
    _current_security_country: Optional[str]

    def __init__(self, mode: CalculationMode, exchange_rate_provider: ExchangeRateProvider, keep_existing_payments: bool = False):
        super().__init__(mode)
        self.exchange_rate_provider = exchange_rate_provider
        self.keep_existing_payments = keep_existing_payments
        self.remove_zero_positions = False
        self.reconciliation_active = True
        self.summarize_options = False
        self.remove_offsetting_payments = False
        self._removed_sec_identifiers: List[str] = []
        self._current_account_is_type_A = None
        self._current_security_is_type_A = None
        self._current_security_country = None
        self.logger = logging.getLogger(__name__)
        self.logger.info(
            "MinimalTaxValueCalculator initialized with mode: %s and provider: %s",
            mode.value,
            type(exchange_rate_provider).__name__,
        )

    def _convert_to_chf(self, amount: Optional[Decimal], currency: str, path_prefix_for_rate: str, reference_date: date) -> Tuple[Optional[Decimal], Decimal]:
        """
        Converts an amount to CHF using the exchange rate from the provider.
        Returns the CHF amount and the exchange rate used.
        If amount is None, returns None for CHF amount and the determined exchange rate.
        For CHF currency, the original amount is returned and the rate is 1 (no calculation performed by this method directly, provider handles CHF rate).
        No quantization is performed.
        """
        # Get exchange rate from the provider
        exchange_rate = self.exchange_rate_provider.get_exchange_rate(currency, reference_date, path_prefix_for_rate)
        
        if currency == self._CHF_CURRENCY:
            # For CHF, rate is 1 (as per provider) and amount remains unchanged.
            # This explicit check can remain for clarity or be removed if provider guarantees Decimal("1") for CHF.
            return amount, Decimal("1")

        if amount is None:
            return None, exchange_rate

        # Perform conversion without quantization
        chf_amount = amount * exchange_rate
        return chf_amount, exchange_rate
    
    def _get_sec_identifier(self, security: Security) -> str:
        ident = security.securityName
        if security.isin:
            ident = f"{ident} / {security.isin}"
        return ident

    def calculate(self, tax_statement: TaxStatement) -> TaxStatement:
        """
        Processes the tax statement.
        """
        self._current_account_is_type_A = None  # Reset state at the beginning of a calculation run
        self._current_security_is_type_A = None  # Reset state
        self._current_security_country = None  # Reset state

        if self.summarize_options:
            self._summarize_positions(tax_statement)

        if self.remove_offsetting_payments:
            self._remove_offsetting_payments(tax_statement)

        super().calculate(tax_statement)

        if self.remove_zero_positions:
            self._remove_zero_positions(tax_statement)      

        if self.summarize_options or self.summarize_options:
            self._reorder_pos_idx(tax_statement)

        self.logger.info(
            "MinimalTaxValueCalculator: Finished processing. Errors: %s, Modified fields: %s",
            len(self.errors),
            len(self.modified_fields),
        )
        return tax_statement
    
    def _summarize_positions(self, tax_statement: TaxStatement):
        self.logger.info("Summarizing OPTION positions:")
        if tax_statement.listOfSecurities and tax_statement.listOfSecurities.depot:
            period_end_plus_one = tax_statement.periodTo + timedelta(days=1)
            for d in tax_statement.listOfSecurities.depot:
                original_sec = d.security
                d.security = []
                currency_taxvalue_map: Dict[tuple[str, str], tuple[SecurityStock, SecurityTaxValue]] = {}
                for sec in original_sec:
                    if sec.securityCategory == "OPTION" and not sec.is_rights_issue and not sec.payment and (not sec.stock or any(s.corpAction for s in sec.stock) == False):
                        stock: SecurityStock = None
                        if sec.stock:
                            stock = next((s for s in sec.stock if s.mutation == False and s.referenceDate == tax_statement.periodFrom), None)
                        if (sec.taxValue and sec.taxValue.balance) or (stock and stock.balance):
                            currency = sec.taxValue.balanceCurrency if sec.taxValue else stock.balanceCurrency
                            opening_stock, tax_value = currency_taxvalue_map.get((sec.country, currency), (None, None))
                            if not tax_value:
                                tax_value = SecurityTaxValue(
                                    referenceDate=tax_statement.periodTo,
                                    quotationType=sec.taxValue.quotationType if sec.taxValue else stock.quotationType,
                                    quantity=Decimal("1"),
                                    balanceCurrency=currency,
                                    balance=Decimal("0"),
                                    unitPrice=Decimal("0")
                                )
                                tax_value.balanceCurrencyBroker = tax_value.balanceCurrency
                                
                                opening_stock = SecurityStock(
                                    referenceDate=tax_statement.periodFrom,
                                    mutation=False,
                                    quotationType=tax_value.quotationType,
                                    quantity=Decimal("1"),
                                    balanceCurrency=tax_value.balanceCurrency,
                                    balance=Decimal("0"),
                                    unitPrice=Decimal("0"),
                                    name="Opening balance"
                                )
                                
                                currency_taxvalue_map[(sec.country, currency)] = (opening_stock, tax_value)

                            if sec.taxValue and sec.taxValue.balance:
                                tax_value.balance += sec.taxValue.balance
                                tax_value.unitPrice = tax_value.balance

                            if stock and stock.balance:
                                opening_stock.balance += stock.balance
                                opening_stock.unitPrice = opening_stock.balance
                    else:
                        d.security.append(sec)

                pos_diff = len(original_sec)-len(d.security)

                sec_pos_idx: int = 9900000
                for (country, currency), (opening_stock, tax_value) in currency_taxvalue_map.items():
                    if opening_stock.balance != Decimal("0") or tax_value.balance != Decimal("0"):
                        sec = Security(
                            positionId=sec_pos_idx,
                            currency=opening_stock.balanceCurrency,
                            quotationType=opening_stock.quotationType,
                            securityCategory="OPTION",
                            securityName=f"Interactive Brokers Optionen {currency}",
                            isin=None,
                            valorNumber=None,
                            country=country,
                            stock=[opening_stock],
                            taxValue=tax_value,
                            payment=[]
                        )
                        d.security.append(sec)
                        self._removed_sec_identifiers.append(self._get_sec_identifier(sec))   # add the summarized position to the removed list to avoid kursliste warning, even though it's not technically removed
                        sec_pos_idx += 1
                self.logger.info(f"  - Summarized {pos_diff} positions into {pos_diff-(len(original_sec)-len(d.security))}")

    def _remove_offsetting_payments(self, tax_statement: TaxStatement):
        """Remove payments that cancel each other out on the same day."""
        self.logger.info("Removing offsetting payments:")
        if tax_statement.listOfSecurities and tax_statement.listOfSecurities.depot:
            total_removed = 0
            for d in tax_statement.listOfSecurities.depot:
                for sec in d.security:
                    if sec.payment:
                        payments_by_date = defaultdict(list)
                        for p in sec.payment:
                            payments_by_date[(p.paymentDate,p.exDate,p.payment_type_original or "", p.sign or "", p.amountCurrency or "", p.quantity)].append(p)
                        
                        filtered_payments = []
                        for payment_key, payments in payments_by_date.items():
                            payment_date = payment_key[0]
                            # Group payments by relevant fields to find offsetting pairs
                            if len(payments) > 1:
                                payments_check = payments.copy()
                                for p in payments_check:
                                    if p in payments:
                                        # Look for an offsetting payment
                                        offsetting = next((op for op in payments if op != p and 
                                            op.payment_type_original == p.payment_type_original and
                                            op.sign == p.sign and
                                            op.amountCurrency == p.amountCurrency and
                                            op.quantity == p.quantity and
                                            op.amount == (-p.amount if p.amount is not None else None) and
                                            op.withHoldingTaxClaim == (-p.withHoldingTaxClaim if p.withHoldingTaxClaim is not None else None)
                                        ), None)
                                        if offsetting:
                                            payments.remove(p)
                                            payments.remove(offsetting)
                                if payments:
                                    filtered_payments.extend(payments)
                                
                                cur_removed = len(payments_check) - len(payments)
                                if cur_removed > 0:
                                    total_removed += cur_removed
                                    self.logger.debug(
                                        f"  - Removing {cur_removed} offsetting {sec.securityName} "
                                        f"payment(s) on {payment_date}"
                                    )
                            else:
                                filtered_payments.extend(payments)
                            
                        sec.payment = filtered_payments
            
            self.logger.info(f"  - Removed {total_removed} offsetting payments.")
    
    def _remove_zero_positions(self, tax_statement: TaxStatement):
        self.logger.info("Removing obsolete positions:")
        # Remove bank accounts and securities with zero quantity and zero value to reduce noise in the output.
        if tax_statement.listOfSecurities and tax_statement.listOfSecurities.depot:
            removed_count = 0
            for d in tax_statement.listOfSecurities.depot:
                original_count = len(d.security)
                original_sec = d.security
                d.security = []
                for sec in original_sec:
                    ident = self._get_sec_identifier(sec)
                    do_add = False
                    keeping_info: str = None
                    removal_warning: str = None
                    if sec.stock and any(s.mutation == False for s in sec.stock) and any(s.mutation for s in sec.stock):
                        do_add = True
                        keeping_info = None
                    elif sec.taxValue:
                        if sec.taxValue.balance and sec.taxValue.balance != Decimal(0):
                            do_add = True
                            keeping_info = None
                        elif sec.taxValue.quantity and sec.taxValue.quantity != Decimal(0):
                            if self.reconciliation_active:
                                do_add = True
                                keeping_info = f"  - Keeping {ident} with taxable value = 0 for reconciliation"
                            else:
                                removal_warning = f"  - Removing {ident} with taxable value = 0 and no payments"

                    if do_add == False:
                        if sec.payment:
                            if any(p.grossRevenueA or p.grossRevenueB or p.withHoldingTaxClaim or p.additionalWithHoldingTaxUSA or p.lumpSumTaxCredit or p.payment_type_original != PaymentTypeOriginal.FUND_ACCUMULATION for p in sec.payment):
                                do_add = True
                                keeping_info = None
                            elif sec.broker_payments and any(p.amount for p in sec.broker_payments):
                                do_add = True
                                keeping_info = None
                            else:
                                if self.reconciliation_active:
                                    do_add = True
                                    keeping_info = f"  - Keeping {ident} with only non-taxable or unknown payment for reconciliation"
                                else:
                                    removal_warning = f"  - Removing {ident} with only non-taxable or unknown payment"
                        elif self.reconciliation_active and sec.get_payment_and_broker_nontaxable():
                            do_add = True
                            keeping_info = f"  - Keeping {ident} with zero taxable payments for for reconciliation (capital gains check list)"

                    if do_add:
                        d.security.append(sec)
                        if keeping_info:
                            self.logger.info(keeping_info)
                    else:
                        self._removed_sec_identifiers.append(ident)
                        if removal_warning:
                            self.logger.warning(removal_warning)
                        self.logger.debug(f"  - Removing security with zero payments, quantity and value: {ident}")
                removed_count += original_count - len(d.security)
            self.logger.info(f"  - Removed {removed_count} zero-payment/quantity/value securities from the tax statement.")

        if tax_statement.listOfBankAccounts and len(tax_statement.listOfBankAccounts.bankAccount)>0:
            original_count = len(tax_statement.listOfBankAccounts.bankAccount)
            tax_statement.listOfBankAccounts.bankAccount = [
                ba for ba in tax_statement.listOfBankAccounts.bankAccount
                if (ba.taxValue and ba.taxValue.value is not None and ba.taxValue.value >= Decimal(0.5)) or 
                    (ba.payment and len(ba.payment) > 0 and sum(p.grossRevenueA for p in ba.payment if p.grossRevenueA is not None)+sum(p.grossRevenueB for p in ba.payment if p.grossRevenueB is not None) > Decimal(0.5))
            ]
            removed_count = original_count - len(tax_statement.listOfBankAccounts.bankAccount)
            self.logger.info(f"  - Removed {removed_count} zero-balance bank accounts from the tax statement.")

        if tax_statement.listOfLiabilities and len(tax_statement.listOfLiabilities.liabilityAccount)>0:
            original_count = len(tax_statement.listOfLiabilities.liabilityAccount)
            tax_statement.listOfLiabilities.liabilityAccount = [
                ba for ba in tax_statement.listOfLiabilities.liabilityAccount
                if (ba.taxValue and ba.taxValue.value is not None and ba.taxValue.value >= Decimal(0.5)) or 
                    (ba.payment and len(ba.payment) > 0 and sum(p.grossRevenueB for p in ba.payment if p.grossRevenueB is not None) > Decimal(0.5))
            ]
            removed_count = original_count - len(tax_statement.listOfLiabilities.liabilityAccount)
            self.logger.info(f"  - Removed {removed_count} zero-balance liability accounts from the tax statement.")

    def _reorder_pos_idx(self, tax_statement: TaxStatement):
        sec_pos_idx = 0
        if tax_statement.listOfSecurities and tax_statement.listOfSecurities.depot:
            for d in tax_statement.listOfSecurities.depot:
                for sec in sorted(d.security, key=lambda s: s.positionId or 1):
                    sec_pos_idx += 1
                    sec.positionId = sec_pos_idx

    def _handle_BankAccount(self, bank_account: BankAccount, path_prefix: str) -> None:
        """Sets the type A/B context based on the bank account's institution country code."""
        country_code = bank_account.bankAccountCountry

        if country_code:
            if country_code == "CH":
                self._current_account_is_type_A = True
            else:
                self._current_account_is_type_A = False
        else:
            self._current_account_is_type_A = None
        
        # BaseCalculator does not have a _handle_BankAccount method.

    def _handle_BankAccountTaxValue(self, ba_tax_value: BankAccountTaxValue, path_prefix: str) -> None:
        """Handles BankAccountTaxValue objects during traversal."""
        if ba_tax_value.balanceCurrency: # We need currency to determine/set the rate
            if ba_tax_value.referenceDate is None:
                raise ValueError(f"BankAccountTaxValue at {path_prefix} has balanceCurrency but no referenceDate. Cannot determine exchange rate.")
            
            chf_value, rate = self._convert_to_chf(
                ba_tax_value.balance, # _convert_to_chf handles amount=None
                ba_tax_value.balanceCurrency,
                f"{path_prefix}.exchangeRate", 
                ba_tax_value.referenceDate
            )
            # Set rate regardless of whether balance was present
            self._set_field_value(ba_tax_value, "exchangeRate", rate, path_prefix)
            
            if chf_value is not None: # Only set value if it could be calculated
                self._set_field_value(ba_tax_value, "value", chf_value, path_prefix)
        else:
            raise ValueError(f"BankAccountTaxValue at {path_prefix} has no balanceCurrency. Cannot determine exchange rate.")

    def _handle_BankAccountPayment(self, ba_payment: BankAccountPayment, path_prefix: str) -> None:
        """Handles BankAccountPayment objects during traversal."""
        if ba_payment.amountCurrency:
            if ba_payment.paymentDate is None:
                raise ValueError(f"BankAccountPayment at {path_prefix} has amountCurrency but no paymentDate. Cannot determine exchange rate.")
            
            chf_revenue, rate = self._convert_to_chf(
                ba_payment.amount, 
                ba_payment.amountCurrency,
                f"{path_prefix}.exchangeRate",
                ba_payment.paymentDate
            )
            self._set_field_value(ba_payment, "exchangeRate", rate, path_prefix)

            gross_revenue_a = Decimal(0)
            gross_revenue_b = Decimal(0)
            withholding_tax = Decimal(0)

            if chf_revenue is not None and chf_revenue > 0: # Only process if there's actual revenue
                if self._current_account_is_type_A is True:
                    gross_revenue_a = chf_revenue
                    # Calculate and set withholding tax for Type A revenue
                    withholding_tax = (
                        chf_revenue * WITHHOLDING_TAX_RATE
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                elif self._current_account_is_type_A is False:
                    gross_revenue_b = chf_revenue
                elif self._current_account_is_type_A is None:
                    # If country was not set on parent BankAccount and there's revenue, it's an error.
                    raise ValueError(f"BankAccountPayment at {path_prefix} has revenue, but parent BankAccount has no country specified to determine Type A/B revenue.")

            self._set_field_value(ba_payment, "grossRevenueA", gross_revenue_a, path_prefix)
            self._set_field_value(ba_payment, "grossRevenueB", gross_revenue_b, path_prefix)
            self._set_field_value(ba_payment, "withHoldingTaxClaim", withholding_tax, path_prefix)

    def _handle_LiabilityAccountTaxValue(self, lia_tax_value: LiabilityAccountTaxValue, path_prefix: str) -> None:
        """Handles LiabilityAccountTaxValue objects during traversal."""
        if lia_tax_value.balanceCurrency:
            if lia_tax_value.referenceDate is None:
                raise ValueError(f"LiabilityAccountTaxValue at {path_prefix} has balanceCurrency but no referenceDate. Cannot determine exchange rate.")

            chf_value, rate = self._convert_to_chf(
                lia_tax_value.balance,
                lia_tax_value.balanceCurrency,
                f"{path_prefix}.exchangeRate",
                lia_tax_value.referenceDate
            )
            self._set_field_value(lia_tax_value, "exchangeRate", rate, path_prefix)
            
            if chf_value is not None:
                self._set_field_value(lia_tax_value, "value", chf_value, path_prefix)

    def _handle_LiabilityAccountPayment(self, lia_payment: LiabilityAccountPayment, path_prefix: str) -> None:
        """Handles LiabilityAccountPayment objects during traversal."""
        if lia_payment.amountCurrency:
            if lia_payment.paymentDate is None:
                raise ValueError(f"LiabilityAccountPayment at {path_prefix} has amountCurrency but no paymentDate. Cannot determine exchange rate.")

            chf_amount, rate = self._convert_to_chf(
                lia_payment.amount,
                lia_payment.amountCurrency,
                f"{path_prefix}.exchangeRate",
                lia_payment.paymentDate
            )
            self._set_field_value(lia_payment, "exchangeRate", rate, path_prefix)

            if chf_amount is not None and chf_amount != Decimal(0):
                # Liabilities are considered Type B for revenue purposes
                self._set_field_value(lia_payment, "grossRevenueB", chf_amount, path_prefix)

    def _handle_Security(self, security: Security, path_prefix: str) -> None:
        """Sets the type A/B context based on the security's country of taxation."""
        country_code = security.country
        self._current_security_country = country_code

        if country_code:
            if country_code == "CH":
                self._current_security_is_type_A = True
            else:
                self._current_security_is_type_A = False
        else:
            self._current_security_is_type_A = None

        if security.payment and not security.broker_payments:
            security.broker_payments = [payment.model_copy(deep=True) for payment in security.payment]

        if security.broker_payments:
            for pay in security.broker_payments:
                if (hasattr(pay, "exchangeRate") == False or pay.exchangeRate is None) and pay.amountCurrency and pay.paymentDate:
                    pay.exchangeRate = self.exchange_rate_provider.get_exchange_rate(pay.amountCurrency, pay.paymentDate, path_prefix + ".exchangeRate")
                if pay.exchangeRate and pay.amount:
                    pay.amount_CHF = pay.amount*pay.exchangeRate

        # BaseCalculator does not have a _handle_Security method.

        # After the basic context is set up compute the expected payments
        # from the Kursliste (empty for this minimal calculator).
        self.computePayments(security, path_prefix)

    def _handle_SecurityTaxValue(self, sec_tax_value: SecurityTaxValue, path_prefix: str) -> None:
        """Handles SecurityTaxValue objects for currency conversion."""
        # This calculator converts an existing 'value' (assumed to be in 'balanceCurrency') to CHF 
        # and sets 'exchangeRate'. It does not derive 'value' from quantity/quotation.

        has_balance_currency = hasattr(sec_tax_value, 'balanceCurrency') and sec_tax_value.balanceCurrency

        if has_balance_currency:
            value_to_convert = sec_tax_value.balance
            ref_date = sec_tax_value.referenceDate

            if ref_date is None:
                raise ValueError(f"SecurityTaxValue at {path_prefix} has balanceCurrency but no referenceDate. Cannot determine exchange rate.")

            chf_value, rate = self._convert_to_chf(
                value_to_convert,
                sec_tax_value.balanceCurrency,
                f"{path_prefix}.exchangeRate",
                ref_date
            )
            
            self._set_field_value(sec_tax_value, "exchangeRate", rate, path_prefix)
            
            # Only attempt to set 'value' if the original value was present (chf_value is not None)
            if chf_value is not None:
                self._set_field_value(sec_tax_value, "value", chf_value, path_prefix)
            # If value_to_convert was None, chf_value will be None. 
            # _set_field_value handles VERIFY/FILL/OVERWRITE modes appropriately for None.

        elif sec_tax_value.balance and not has_balance_currency:
            # If there's a value but no currency, this is an error as we cannot process it.
            raise ValueError(f"SecurityTaxValue at {path_prefix} has a 'value' but no 'balanceCurrency'. Cannot perform currency conversion or set exchange rate accurately.")

        self._set_field_value(sec_tax_value, "undefined", True, path_prefix)

    def _handle_SecurityPayment(self, sec_payment: SecurityPayment, path_prefix: str) -> None:
        """Handles SecurityPayment objects for currency conversion and revenue categorization."""
        # In the base implementation all payments will have been cleared (outside of debugging and verify mode)
        # Avoid doing computation here to handle broken inputs on verify + minimal mode.
        if self._current_security_country == "US":
            has_da1_fields = any(
                field is not None
                for field in (
                    sec_payment.lumpSumTaxCreditAmount,
                    sec_payment.lumpSumTaxCreditPercent,
                    sec_payment.nonRecoverableTaxAmount,
                    sec_payment.nonRecoverableTaxPercent,
                    sec_payment.additionalWithHoldingTaxUSA,
                )
            )
            if has_da1_fields and sec_payment.additionalWithHoldingTaxUSA is None:
                self._set_field_value(
                    sec_payment,
                    "additionalWithHoldingTaxUSA",
                    Decimal("0"),
                    path_prefix,
                )

    def computePayments(self, security: Security, path_prefix: str) -> None:
        """Compute and set payments for a security.

        This minimal implementation passes an empty list to ``setKurslistePayments``.
        Subclasses can override to provide actual computation.
        """
        self.setKurslistePayments(security, [], path_prefix)

    def setKurslistePayments(self, security: Security, payments: List[SecurityPayment], path_prefix: str) -> None:
        """Set or verify the list of payments derived from the Kursliste.

        In ``OVERWRITE`` mode the given ``payments`` are written to ``security.payment``.
        In ``VERIFY`` mode the method checks that the payments already present on
        ``security`` are equal to ``payments`` and records a ``CalculationError``
        otherwise. ``FILL`` behaves like ``VERIFY`` but writes the payments if the
        list on the security is empty.
        """

        # If no payments are provided there is nothing to check or set.
        # if payments == None:
        #    return

        field_path = f"{path_prefix}.payment" if path_prefix else "payment"
        current = security.payment

        if self.mode == CalculationMode.OVERWRITE:
            if self.keep_existing_payments:
                payments = current + payments
            security.payment = sorted(payments, key=lambda p: p.paymentDate)
            self.modified_fields.add(field_path)
            return

        if self.mode == CalculationMode.FILL and not current:
            security.payment = sorted(payments, key=lambda p: p.paymentDate)
            self.modified_fields.add(field_path)
            return

        if self.mode not in (CalculationMode.VERIFY, CalculationMode.FILL):
            return

        if self.keep_existing_payments:
            # For debugging we force the list to be the merge even when verifying so
            # we can look at the rendered copy.
            merged = current + payments
            security.payment = sorted(merged, key=lambda p: p.paymentDate)
        
        # Detailed comparison for VERIFY and FILL (with existing payments)
        current_by_date = defaultdict(list)
        for p in current:
            current_by_date[p.paymentDate].append(p)

        expected_by_date = defaultdict(list)
        for p in payments:
            expected_by_date[p.paymentDate].append(p)

        all_dates = sorted(list(set(current_by_date.keys()) | set(expected_by_date.keys())))

        for d in all_dates:
            current_on_date = current_by_date.get(d, [])
            expected_on_date = expected_by_date.get(d, [])

            if not current_on_date:
                for p in expected_on_date:
                    self.errors.append(CalculationError(f"{field_path}.date={d}", p, None))
                continue

            if not expected_on_date:
                for p in current_on_date:
                    self.errors.append(CalculationError(f"{field_path}.date={d}", None, p))
                continue

            # Try to match payments on the same date
            unmatched_current = list(current_on_date)
            remaining_expected = []
            for p_expected in expected_on_date:
                try:
                    unmatched_current.remove(p_expected)
                except ValueError:
                    remaining_expected.append(p_expected)

            if len(unmatched_current) == len(remaining_expected):
                # To provide a stable diff, sort if possible.
                try:
                    unmatched_current.sort()
                    remaining_expected.sort()
                except TypeError:
                    pass  # Not sortable, compare as is.

                for p_curr, p_exp in zip(unmatched_current, remaining_expected):
                    p_curr_vars = vars(p_curr)
                    p_exp_vars = vars(p_exp)
                    all_keys = sorted(list(set(p_curr_vars.keys()) | set(p_exp_vars.keys())))
                    for key in all_keys:
                        if key in INTERNAL_ONLY_SECURITY_PAYMENT_FIELDS:
                            continue
                        v_curr = p_curr_vars.get(key)
                        v_exp = p_exp_vars.get(key)
                        if v_curr != v_exp:
                            # Create one error per differing field.
                            error_path = f"{field_path}.date={d}.{key}"
                            self.errors.append(CalculationError(error_path, v_exp, v_curr))
            else:
                for p in unmatched_current:
                    self.errors.append(CalculationError(f"{field_path}.date={d}", None, p))
                for p in remaining_expected:
                    self.errors.append(CalculationError(f"{field_path}.date={d}", p, None))
