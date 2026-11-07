"""Compatibility entry point; model lives in components.general."""
import sys
from .components import general as _model

if __name__ == '__main__':
    _model.main()
else:
    sys.modules[__name__] = _model
