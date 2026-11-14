import numpy as np
from scipy.stats import median_abs_deviation


def numeric_profiles(catalog):
    output = {}
    for name, schema in catalog.manifest.items():
        fields = {}
        for field, kind in schema["columns"].items():
            if kind.rstrip("?") not in ("int", "number", "money"):
                continue
            values = [float(record[field]) for record in catalog.rows(name) if record[field] is not None]
            array = np.asarray(values, dtype=float)
            fields[field] = {"observed": len(values), "missing": len(catalog.tables[name]) - len(values),
                             "median": float(np.median(array)) if values else None,
                             "mad": float(median_abs_deviation(array)) if values else None}
        output[name] = fields
    return output
