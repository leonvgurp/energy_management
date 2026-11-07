"""Compatibility entry point; model lives in components.heat_pump."""
import sys
from .components import heat_pump as _model

if __name__ == '__main__':
    _model.main()
else:
    sys.modules[__name__] = _model
