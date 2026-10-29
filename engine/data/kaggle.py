"""Optional stress set: load the Kaggle CSV into dbo.RFR_TELEM_KGL.

python -m engine.data.kaggle [--csv data/raw/dynamic_supply_chain_logistics_dataset.csv]

The Kaggle rows are not a coherent fleet (US-wide coordinates, hourly rows from 2021-2024,
no shipment ids), so they go to their own table and never mix with the simulator's data.
Each row becomes its own pseudo-shipment `KGL-nnnnn` so the same append-and-dedup ingestion
applies and re-running the load inserts nothing new. The CSV is not committed; download it
from Kaggle (link in docs/data-card.md) and check its licence before redistributing it.
"""

import argparse
from pathlib import Path

import pandas as pd

from engine.data.ingest import IngestResult, ingest_readings
from engine.data.schema import KAGGLE_TABLE, RAW_COLUMNS
from engine.db import build_engine
from engine.settings import PROJECT_ROOT, load_settings

DEFAULT_CSV = PROJECT_ROOT / "data" / "raw" / "dynamic_supply_chain_logistics_dataset.csv"

# Kaggle column -> raw column (the dataset has no shipment, vehicle or depot ids)
KAGGLE_MAPPING = {
    "timestamp": "EVT_DTTM",
    "vehicle_gps_latitude": "GPS_Y",
    "vehicle_gps_longitude": "GPS_X",
    "iot_temperature": "RFR_TMP_C",
    "cargo_condition_status": "LOAD_QLTY_IX",
    "risk_classification": "HZD_TIER",
    "delay_probability": "LATE_PCT",
    "port_congestion_level": "PORT_Q_IX",
    "route_risk_level": "LANE_HZD_IX",
}


def kaggle_to_raw(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in [*KAGGLE_MAPPING, "eta_variation_hours"] if c not in frame.columns]
    if missing:
        raise ValueError(f"not the expected Kaggle file; missing columns {missing}")
    legacy = frame[list(KAGGLE_MAPPING)].rename(columns=KAGGLE_MAPPING)
    legacy["RCPT_DTTM"] = legacy["EVT_DTTM"]
    legacy["CNSG_NO"] = [f"KGL-{i:05d}" for i in range(len(frame))]
    legacy["TRLR_NO"] = None
    legacy["HUB_CD"] = "KAGGLE"
    legacy["PROD_CLS"] = None
    legacy["PKT_SEQ"] = 1
    legacy["ARR_SLIP_MIN"] = frame["eta_variation_hours"] * 60.0
    legacy["LEG_STAT"] = None
    legacy["LOAD_FLG"] = "Y"
    return legacy[RAW_COLUMNS]


def load_kaggle(csv_path: Path = DEFAULT_CSV) -> IngestResult:
    engine = build_engine(load_settings(), "ingest", fast_executemany=True)
    return ingest_readings(engine, kaggle_to_raw(pd.read_csv(csv_path)), table=KAGGLE_TABLE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the Kaggle stress set")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()
    print(load_kaggle(args.csv))


if __name__ == "__main__":
    main()
