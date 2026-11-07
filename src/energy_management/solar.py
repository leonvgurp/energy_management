"""Compatibility entry point; model lives in components.solar."""
import sys
from .components import solar as _model

if __name__ == '__main__':
    _model.main()
else:
    sys.modules[__name__] = _model
