"""
Central settings module that loads and exports all configuration.
"""
# First, import all from generic config
from generic_config import *

# Then try to override with actual config
try:
    import config
    # Update all variables from config.py
    for var in dir(config):
        if not var.startswith('_'):  # Skip private variables
            globals()[var] = getattr(config, var)
    print("Loaded private config.py")
except ImportError:
    print("Warning: Using generic_config.py placeholders. Create config.py for real run.")