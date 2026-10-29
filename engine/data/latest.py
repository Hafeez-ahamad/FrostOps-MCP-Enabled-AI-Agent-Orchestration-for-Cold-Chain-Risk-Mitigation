"""Python reference for rpt.VW_LATEST_VALID_READING.

"Active" means the latest *valid* reading per shipment (by device time, then sequence).
The SQL view implements the same rule; an integration test checks they agree.
"""

import pandas as pd


def latest_valid_readings(readings: pd.DataFrame, statuses: list[str]) -> pd.DataFrame:
    """`readings` in legacy columns; `statuses` aligned with its rows."""
    if len(statuses) != len(readings):
        raise ValueError("one status per reading is required")
    valid = readings.loc[[s == "valid" for s in statuses]]
    ordered = valid.sort_values(["CNSG_NO", "EVT_DTTM", "PKT_SEQ"], kind="stable")
    return ordered.groupby("CNSG_NO", sort=True).tail(1).reset_index(drop=True)
