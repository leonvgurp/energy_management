"""Compatibility entry point; model lives in components.phev."""
import sys
from .components import phev as _model

if __name__ == '__main__':
    _model.main()
else:
    sys.modules[__name__] = _model
