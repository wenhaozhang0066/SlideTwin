"""Convert CAMELYON ASAP annotations to micrometre GeoJSON for evaluation (M6).

    python scripts/convert_annotations.py --xml-dir /path/to/CAMELYON16/annotations

Writes data/annotations/<slide_id>.geojson (git-ignored) for every manifest slide whose
`eval_only.annotation` names a file. The same as `python -m slidetwin.eval convert`.
"""

from __future__ import annotations

import sys

from slidetwin.eval.__main__ import main

if __name__ == "__main__":
    main(["convert", *sys.argv[1:]])
