from decimal import Decimal
from typing import Set

# For marking a quantity that is mandatory in the model, but we cannot compute it yet.
# TODO(consider making it optional in our internal copy of the model)
UNINITIALIZED_QUANTITY = Decimal('-1')
# Standard Swiss withholding tax rate used for revenue subject to withholding.
WITHHOLDING_TAX_RATE = Decimal("0.35")

# Signs that indicate non-taxable payments that should be skipped entirely
NON_TAXABLE_SIGNS: Set[str] = {
    "KEP",  # Return of capital contributions
    "(KG)",  # Capital gain
    "(KR)",  # Return of Capital
}