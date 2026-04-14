from decimal import Decimal
from datetime import date

from typing import Optional, List, Set
import logging

from .kursliste_tax_value_calculator import KurslisteTaxValueCalculator
from .base import CalculationMode
from ..core.exchange_rate_provider import ExchangeRateProvider
from ..core.position_reconciler import PositionReconciler
from ..core.constants import NON_TAXABLE_SIGNS, WITHHOLDING_TAX_RATE
from ..model.kursliste import SecurityGroupESTV
from ..model.ech0196 import SecurityPayment, Security, PaymentTypeOriginal, SecurityTaxValue
from ..util.converters import security_tax_value_to_stock


from ..core.flag_override_provider import FlagOverrideProvider

logger = logging.getLogger(__name__)

KNOWN_SIGN_TYPES: Set[str] = {
    "(B)",  # Bonus
    "(E)",  # Ex-date related
    "(G)",  # Withholding tax free capital gains
    "(H)",  # Investment fund with direct real estate
    "(I)",  # Taxable earnings not yet determined
    "(IK)",  # Non-taxable KEP distribution not yet determined
    "(IM)",  # Reinvestment of retained earnings
    "KEP",  # Return of capital contributions - SKIP PAYMENT
    "(KG)",  # Capital gain - SKIP PAYMENT
    "(KR)",  # Return of Capital - SKIP PAYMENT
    "(L)",  # No withholding tax deduction
    "(M)",  # Re-investment fund (Switzerland)
    "(MV)",  # Distribution notification procedure
    "(N)",  # Re-investment fund (abroad)
    "(P)",  # Foreign earnings subject to withholding tax
    "PRO",  # Provisional
    "(Q)",  # With foreign withholding tax - SPECIAL HANDLING
    "(V)",  # Distribution in form of shares - NOT IMPLEMENTED
    "(Y)",  # Purchasing own shares
    "(Z)",  # Without withholding tax
}

class BrokerFillInTaxValueCalculator(KurslisteTaxValueCalculator):
    """
    Calculator that fills in missing values based on other available data,
    potentially after Kursliste and minimal calculations have been performed.
    """
    def __init__(self, mode: CalculationMode, exchange_rate_provider: ExchangeRateProvider, flag_override_provider: Optional[FlagOverrideProvider] = None, keep_existing_payments: bool = False):
        super().__init__(mode, exchange_rate_provider, flag_override_provider=flag_override_provider, keep_existing_payments=keep_existing_payments)

        self.use_broker_exch_rate = False

        logger.info(
            "BrokerFillInTaxValueCalculator initialized with mode: %s and provider: %s",
            mode.value,
            type(exchange_rate_provider).__name__,
        )

    def _handle_SecurityTaxValue(self, sec_tax_value: SecurityTaxValue, path_prefix: str) -> None:
        has_balance_currency = hasattr(sec_tax_value, 'balanceCurrency') and sec_tax_value.balanceCurrency
        ref_date: date = None
        if has_balance_currency and hasattr(sec_tax_value, 'referenceDate') and sec_tax_value.referenceDate:
            ref_date = sec_tax_value.referenceDate

        if self._current_kursliste_security or not has_balance_currency or not ref_date:
            super()._handle_SecurityTaxValue(sec_tax_value, path_prefix)
            return
        
        value_to_convert = sec_tax_value.balance
        if value_to_convert is not None:# and sec_tax_value.unitPrice is None:
            price = value_to_convert / sec_tax_value.quantity if sec_tax_value.quantity and sec_tax_value.quantity != 0 else None
            self._set_field_value(sec_tax_value, "unitPrice", price, path_prefix)

        chf_value, rate = self._convert_to_chf(
            value_to_convert,
            sec_tax_value.balanceCurrency,
            f"{path_prefix}.exchangeRate",
            ref_date
        )
        
        self._set_field_value(sec_tax_value, "exchangeRate", rate, path_prefix)
        #self._set_field_value(sec_tax_value, "exchangeRateOrig", rate, path_prefix)

        # Only attempt to set 'value' if the original value was present (chf_value is not None)
        if chf_value is not None:
            self._set_field_value(sec_tax_value, "value", chf_value, path_prefix)
            #price = chf_value / sec_tax_value.quantity if sec_tax_value.quantity and sec_tax_value.quantity != 0 else None
            #self._set_field_value(sec_tax_value, "unitPrice", price, path_prefix)
            #self._set_field_value(sec_tax_value, "exchangeRate", Decimal("1"), path_prefix)
            #self._set_field_value(sec_tax_value, "balanceCurrency", "CHF", path_prefix)     
        elif value_to_convert is None:       
            self._set_field_value(sec_tax_value, "exchangeRate", Decimal("1"), path_prefix)
            self._set_field_value(sec_tax_value, "balanceCurrency", "CHF", path_prefix)     

    def computePayments(self, security: Security, path_prefix: str) -> None:
        payment_list = security.payment
        result: List[SecurityPayment] = []
        if self._current_kursliste_security:
            super().computePayments(security, path_prefix)
            if (payment_list and len(payment_list)>0 and any(p for p in payment_list if p.sign is None or p.sign not in NON_TAXABLE_SIGNS) and
                   (security.payment is None or len(security.payment) == 0 or 
                        any(p for p in security.payment if p.kursliste and (
                            (p.amount and p.amount != Decimal("0")) or 
                              (not p.undefined and p.payment_type_original != PaymentTypeOriginal.FUND_ACCUMULATION) or not p.remark or len(p.remark) == 0) == False))):
                payment_list = security.broker_payments
                if security.payment:
                    result = list(security.payment)
                logger.warning(f"Security {security.isin or security.securityName} has Kursliste payments but they are all zero amount or undefined with no remark. Falling back to broker payments for tax value calculation.")
            else:
                return

        #result: List[SecurityPayment] = []

        stock = list(security.stock)
        if security.taxValue:
            stock.append(security_tax_value_to_stock(security.taxValue))

        reconciler = PositionReconciler(stock, identifier=f"{security.isin or 'SEC'}-payments")

        ref_year = (
            security.taxValue.referenceDate.year
            if security.taxValue and security.taxValue.referenceDate
            else security.stock[-1].referenceDate.year
        )
        accessor = self.kursliste_manager.get_kurslisten_for_year(ref_year)

        tax_payments: List[SecurityPayment] = []
        wht_payments: List[SecurityPayment] = []

        for pay in payment_list:
            if not pay.paymentDate:
                continue

            # Skip payments with no amount, as they do not impact tax calculations
            if pay.amount is None or pay.amount == Decimal("0"):
                continue

            # witholding position is for reconciliation only
            if (pay.withHoldingTaxClaim or pay.nonRecoverableTaxAmountOriginal) and -pay.amount == (pay.withHoldingTaxClaim or pay.nonRecoverableTaxAmountOriginal):
                wht_payments.append(pay)
            elif not hasattr(pay, "capitalGain") or not pay.capitalGain:
                tax_payments.append(pay)

        for pay in tax_payments:

            reconciliation_date = pay.exDate or pay.paymentDate

            # Warn if exDate is in the previous year (before the tax period)
            if pay.exDate and security.taxValue and security.taxValue.referenceDate:
                tax_year = security.taxValue.referenceDate.year
                if pay.exDate.year < tax_year:
                    sec_ident = security.isin or security.securityName
                    warning_msg = (
                        f"Payment '{pay.paymentDate}' for security "
                        f"'{sec_ident}' has an ex-date "
                        f"({pay.exDate}) in the previous year. "
                        f"The dividend amount is based on the "
                        f"opening position of the period because "
                        f"mutations from the previous year are not "
                        f"processed. Please double-check the amount."
                    )
                    logger.warning(warning_msg)
                    self._previous_year_exdate_warnings.append(
                        {
                            "message": warning_msg,
                            "identifier": sec_ident,
                            "payment_date": pay.paymentDate,
                        }
                    )

            pos = reconciler.synthesize_position_at_date(reconciliation_date, assume_zero_if_no_balances=True, security=security)
            if pos is None:
                raise ValueError(
                    f"No position found for {security.isin or security.securityName} on date {reconciliation_date}"
                )

            quantity = pos.quantity

            logger.debug("quantity %s found for date %s", quantity, reconciliation_date)
            if quantity == 0:
                # Skip payment generation if the quantity of outstanding securities is zero
                continue
            
            # Validate sign type if present
            current_sign = pay.sign if hasattr(pay, "sign") else None
            if current_sign is not None and current_sign not in KNOWN_SIGN_TYPES:
                raise ValueError(
                    f"Unknown sign type '{current_sign}' for payment on {pay.paymentDate} "
                    f"for {security.isin or security.securityName}. "
                    f"Please add handling for this sign type."
                )

            # Skip non-taxable payments (return of capital, capital gains)
            if current_sign in NON_TAXABLE_SIGNS:
                logger.debug(
                    "Skipping non-taxable payment with sign '%s' on %s for %s",
                    current_sign,
                    pay.paymentDate,
                    security.isin or security.securityName,
                )
                continue

            payment_name = f"KL:{security.securityName}"
            security_type = security.securityType
            security_group: SecurityGroupESTV = SecurityGroupESTV(security.securityCategory) if security.securityCategory else None
            if security.securityCategory == "SHARE":
                payment_name = "Dividende"
                if security_type is None:
                    security_type = "SHARE.NOMINAL"
                #security_group = SecurityGroupESTV.SHARE
            elif security.securityCategory == "FUND":
                payment_name = "Dividende"
                if security_type is None:
                    security_type = "FUND.ACCUMULATION"
                #security_group = SecurityGroupESTV.SHARE
            elif security.securityCategory == "BOND":
                payment_name = "Zinszahlung"
                if security_type is None:
                    security_type = "BOND.BOND"
                #security_group = SecurityGroupESTV.BOND
            elif security.securityCategory is None or security_type is None:
                raise ValueError(f"Security Category not supported {security.securityCategory} ({security.securityName})")

            # Preserve the original payment subtype only when it is explicitly non-standard.
            # Standard is the default and should remain unset so VERIFY mode does not fail
            # against XML inputs that never contained this internal metadata field.
            payment_type_original = None
            #if pay.paymentType is not None and pay.paymentType != PaymentTypeESTV.STANDARD:
            #    payment_type_original = PaymentTypeOriginal(pay.paymentType.value)

            payment_value: Decimal = pay.amount / quantity
            exchange_rate: Decimal = None
            chf_amount: Decimal = None
            if pay.amountCurrency and pay.amountCurrency != "CHF":
                if pay.exchangeRate is not None and self.use_broker_exch_rate:
                    exchange_rate = pay.exchangeRate
                    chf_amount = pay.amount * exchange_rate
                else:
                    chf_amount, exchange_rate = self._convert_to_chf(
                        pay.amount,
                        pay.amountCurrency,
                        f"{path_prefix}.exchangeRate",
                        pay.paymentDate
                    )
            else:
                chf_amount = pay.amount
                exchange_rate = Decimal("1")

            payment_value_chf = chf_amount / quantity

            if payment_value is None:
                raise ValueError(
                    f"Payment on {pay.paymentDate} for {security.isin or security.securityName} missing paymentValueCHF"
                )

            rate = exchange_rate
            if rate is None and payment_value_chf != 0:
                if pay.currency == "CHF":
                    rate = Decimal("1")
                else:
                    logger.error("Invalid Kursliste payment: %s", pay)
                    raise ValueError(
                        f"Kursliste payment on {pay.paymentDate} for {security.isin or security.securityName} missing exchangeRate"
                    )

            sec_payment = SecurityPayment(
                paymentDate=pay.paymentDate,
                exDate=pay.exDate if hasattr(pay, "exDate") else pay.paymentDate,
                name=payment_name,
                quotationType=security.quotationType,
                quantity=quantity,
                amountCurrency=pay.amountCurrency,
                amountPerUnit=payment_value,
                amount=pay.amount,
                exchangeRate=rate,
                payment_type_original=payment_type_original,
            )

            # Not all payment subtypes have these fields
            # TODO: Should the typing be smarter?
            effective_sign = pay.sign if hasattr(pay, "sign") and pay.sign is not None else None
            if self.flag_override_provider and security.isin:
                override_flag = self.flag_override_provider.get_flag(security.isin)
                if override_flag:
                    logger.debug("Found override flag '%s' for %s", override_flag, security.isin)
                    if not (override_flag.startswith("(") and override_flag.endswith(")")):
                        effective_sign = f"({override_flag})"
                    else:
                        effective_sign = override_flag

            sec_payment.sign = effective_sign

            wht_this_pay = (w for w in wht_payments if w.paymentDate == pay.paymentDate and w.amountCurrency == pay.amountCurrency and (pay.brokerActionId is None or (w.brokerActionId and w.brokerActionId == pay.brokerActionId)))
            for wht_pay in wht_this_pay:
                if wht_pay.withHoldingTaxClaim and wht_pay.withHoldingTaxClaim != Decimal("0"):
                    if sec_payment.withHoldingTaxClaim is None:
                        sec_payment.withHoldingTaxClaim = Decimal("0")
                    sec_payment.withHoldingTaxClaim += wht_pay.withHoldingTaxClaim * rate
                if wht_pay.nonRecoverableTaxAmountOriginal and wht_pay.nonRecoverableTaxAmountOriginal != Decimal("0"):
                    if sec_payment.nonRecoverableTaxAmount is None:
                        sec_payment.nonRecoverableTaxAmount = Decimal("0")
                    sec_payment.nonRecoverableTaxAmount += wht_pay.nonRecoverableTaxAmountOriginal * rate

            # Reality vs spec: Real-world files seem to have all three fields set when at least one is set,
            # possibly with zero values, even though our reading of the spec suggests they should be mutually exclusive
            if pay.withHoldingTaxClaim is not None:
                sec_payment.grossRevenueA = chf_amount
                sec_payment.grossRevenueB = None #Decimal("0")
                sec_payment.withHoldingTaxClaim = (chf_amount * WITHHOLDING_TAX_RATE).quantize(
                    Decimal("0.01")
                )
            else:
                sec_payment.grossRevenueA = None #Decimal("0")
                sec_payment.grossRevenueB = chf_amount
                sec_payment.withHoldingTaxClaim = None #Decimal("0")

            da1_security_group = security_group
            da1_security_type = security_type
            if effective_sign == "(Q)":
                da1_security_group = SecurityGroupESTV.SHARE
                da1_security_type = None

            da1_rate = accessor.get_da1_rate(
                security.country,
                da1_security_group,
                da1_security_type,
                reference_date=pay.paymentDate,
            )

            if da1_rate and effective_sign != "(Z)" and self.include_da1(security, pay.paymentDate, da1_rate):
                lump_sum_amount = chf_amount * da1_rate.value / Decimal(100)
                non_recoverable_amount = chf_amount * da1_rate.nonRecoverable / Decimal(100)
                if lump_sum_amount > 0 or non_recoverable_amount > 0:
                    sec_payment.lumpSumTaxCreditPercent = da1_rate.value
                    sec_payment.lumpSumTaxCreditAmount = lump_sum_amount
                    #sec_payment.nonRecoverableTaxPercent = da1_rate.nonRecoverable
                    #sec_payment.nonRecoverableTaxAmount = non_recoverable_amount

                    if security.country == "US":
                        if sec_payment.nonRecoverableTaxAmount > non_recoverable_amount * Decimal("1.1"):  # Allow for some small discrepancies due to rounding or data issues
                            sec_payment.additionalWithHoldingTaxUSA = sec_payment.nonRecoverableTaxAmount - non_recoverable_amount
                            sec_payment.nonRecoverableTaxAmount = non_recoverable_amount

                        if sec_payment.additionalWithHoldingTaxUSA is None:
                            sec_payment.additionalWithHoldingTaxUSA = Decimal("0")
                    sec_payment.lumpSumTaxCredit = True

            if chf_amount and sec_payment.nonRecoverableTaxAmount and chf_amount != Decimal("0") and sec_payment.nonRecoverableTaxAmount != Decimal("0"):
                sec_payment.nonRecoverableTaxPercent = (sec_payment.nonRecoverableTaxAmount / chf_amount * Decimal("100")).quantize(Decimal("0.2"))
            else:
                sec_payment.nonRecoverableTaxPercent = None

            if effective_sign == "(V)":
                raise NotImplementedError(
                    f"DA-1 for sign='(V)' not implemented for {security.isin or security.securityName} on {pay.paymentDate}"
                )

            result.append(sec_payment)

        self.setKurslistePayments(security, result, path_prefix)
