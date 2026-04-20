"""
Symbol names and metadata for Deriv trading instruments.
"""

# Symbol code to friendly name mapping
SYMBOL_NAMES = {
    # Volatility Indices
    'R_10': 'Volatility 10 Index',
    'R_25': 'Volatility 25 Index',
    'R_50': 'Volatility 50 Index',
    'R_75': 'Volatility 75 Index',
    'R_100': 'Volatility 100 Index',

    # Volatility Indices (1s)
    '1HZ10V': 'Volatility 10 (1s) Index',
    '1HZ25V': 'Volatility 25 (1s) Index',
    '1HZ50V': 'Volatility 50 (1s) Index',
    '1HZ75V': 'Volatility 75 (1s) Index',
    '1HZ100V': 'Volatility 100 (1s) Index',

    # Jump Indices
    'JD10': 'Jump 10 Index',
    'JD25': 'Jump 25 Index',
    'JD50': 'Jump 50 Index',
    'JD75': 'Jump 75 Index',
    'JD100': 'Jump 100 Index',

    # Crash/Boom Indices
    'CRASH300N': 'Crash 300 Index',
    'CRASH500': 'Crash 500 Index',
    'CRASH500N': 'Crash 500 Index',
    'CRASH1000': 'Crash 1000 Index',
    'CRASH1000N': 'Crash 1000 Index',
    'BOOM300N': 'Boom 300 Index',
    'BOOM500': 'Boom 500 Index',
    'BOOM500N': 'Boom 500 Index',
    'BOOM1000': 'Boom 1000 Index',
    'BOOM1000N': 'Boom 1000 Index',

    # Step Indices
    'stpRNG': 'Step Index',

    # Range Break Indices
    'RNGBRKH100': 'Range Break 100 Index',
    'RNGBRKL100': 'Range Break 100 Index',
}

def get_symbol_display_name(symbol_code: str) -> str:
    """
    Get the friendly display name for a symbol code.

    Args:
        symbol_code: The symbol code (e.g., 'R_50')

    Returns:
        Friendly name if found, otherwise returns the code itself
    """
    return SYMBOL_NAMES.get(symbol_code, symbol_code)

def format_symbol_display(symbol_code: str, max_width: int = 30) -> str:
    """
    Format symbol for display with both code and name.

    Args:
        symbol_code: The symbol code
        max_width: Maximum width for the display string

    Returns:
        Formatted string like "R_50 (Volatility 50)"
    """
    name = SYMBOL_NAMES.get(symbol_code)
    if name:
        # Shorten name if needed
        if len(f"{symbol_code} ({name})") > max_width:
            return f"{symbol_code} ({name[:max_width-len(symbol_code)-4]}..)"
        return f"{symbol_code} ({name})"
    return symbol_code

def get_short_name(symbol_code: str) -> str:
    """
    Get a shorter version of the symbol name for compact displays.

    Returns something like "Vol 50" instead of "Volatility 50 Index"
    """
    name = SYMBOL_NAMES.get(symbol_code, symbol_code)
    # Simplify common patterns
    name = name.replace(' Index', '').replace('Volatility', 'Vol')
    return name
