"""Fit the viewer cost model (SPEC 8) from human reading logs (M7).

    python scripts/calibrate_costs.py logs/sessions --fitted fitted.yaml

Prints a table (n, fitted value, 95% interval, current value) and writes the fitted `cost_model`
block. Method: slidetwin/eval/calibrate.py. Same as `python -m slidetwin.eval calibrate`.
"""

from __future__ import annotations

import sys

from slidetwin.eval.__main__ import main

if __name__ == "__main__":
    main(["calibrate", *sys.argv[1:]])
