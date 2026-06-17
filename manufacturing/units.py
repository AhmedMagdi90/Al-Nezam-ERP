from decimal import Decimal

class UnitService:
    """
    Handles Unit Conversions.
    Base Units: 'kg' (Weight), 'm' (Length), 'l' (Volume), 'pcs' (Count).
    """
    
    CONVERSION_RATES = {
        # Weight (Base: kg)
        'kg': Decimal('1.0'),
        'gm': Decimal('0.001'),
        'mg': Decimal('0.000001'),
        'tonne': Decimal('1000.0'),
        
        # Length (Base: m)
        'm': Decimal('1.0'),
        'cm': Decimal('0.01'),
        'mm': Decimal('0.001'),
        'km': Decimal('1000.0'),
        
        # Volume (Base: l)
        'l': Decimal('1.0'),
        'ml': Decimal('0.001'),
        
        # Count (Base: pcs)
        'pcs': Decimal('1.0'),
        'dozen': Decimal('12.0'),
    }

    TYPE_MAP = {
        'kg': 'weight', 'gm': 'weight', 'mg': 'weight', 'tonne': 'weight',
        'm': 'length', 'cm': 'length', 'mm': 'length', 'km': 'length',
        'l': 'volume', 'ml': 'volume',
        'pcs': 'count', 'dozen': 'count'
    }

    @staticmethod
    def get_type(unit):
        return UnitService.TYPE_MAP.get(unit)

    @staticmethod
    def convert(quantity, from_unit, to_unit):
        """
        Convert quantity from one unit to another.
        """
        if from_unit == to_unit:
            return Decimal(quantity)

        type_from = UnitService.get_type(from_unit)
        type_to = UnitService.get_type(to_unit)
        
        if not type_from or not type_to:
            raise ValueError(f"Unknown unit: {from_unit} or {to_unit}")
            
        if type_from != type_to:
            raise ValueError(f"Incompatible unit conversion: {from_unit} ({type_from}) -> {to_unit} ({type_to})")
            
        # Convert to Base Unit first, then to Target Unit
        # Qty (Base) = Qty * Rate
        # Qty (Target) = Qty (Base) / Target_Rate
        
        qty_base = Decimal(quantity) * UnitService.CONVERSION_RATES[from_unit]
        qty_target = qty_base / UnitService.CONVERSION_RATES[to_unit]
        
        return qty_target
