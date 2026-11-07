"""Compatibility entry point; model lives in components.boiler."""
import sys
from .components import boiler as _model

if __name__ == '__main__':
    _model.main()
else:
    sys.modules[__name__] = _model
