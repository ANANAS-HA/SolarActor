"""Download one year of hourly NASA POWER weather for the site in .env (run once).

    uv run python scripts/download_weather.py [--force]

Writes data/weather/nasa_power_<lat>_<lon>_<year>.csv with columns
ts_utc, ghi_wm2, dni_wm2, dhi_wm2, ambient_c, wind_ms (hourly, UTC).
The untouched API response is kept next to it as *_raw.csv.
Resampling to 5-minute steps happens in raqeeb/twin/weather.py.
"""

from __future__ import annotations

import argparse
import io
import sys

import numpy as np
import pandas as pd
import requests

from raqeeb.config import load_settings

API_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
PARAMETERS = {
    "ALLSKY_SFC_SW_DWN": "ghi_wm2",
    "ALLSKY_SFC_SW_DNI": "dni_wm2",
    "ALLSKY_SFC_SW_DIFF": "dhi_wm2",
    "T2M": "ambient_c",
    "WS10M": "wind_ms",
}
FILL_VALUE = -999


def fetch_raw(lat: float, lon: float, year: int) -> str:
    params = {
        "parameters": ",".join(PARAMETERS),
        "community": "RE",
        "latitude": lat,
        "longitude": lon,
        "start": f"{year}0101",
        "end": f"{year}1231",
        "format": "CSV",
        "time-standard": "UTC",
    }
    response = requests.get(API_URL, params=params, timeout=180)
    response.raise_for_status()
    return response.text


def parse_raw(text: str) -> pd.DataFrame:
    lines = text.splitlines()
    end = next(i for i, line in enumerate(lines) if line.strip() == "-END HEADER-")
    raw = pd.read_csv(io.StringIO("\n".join(lines[end + 1 :])))
    ts = pd.to_datetime(
        {"year": raw["YEAR"], "month": raw["MO"], "day": raw["DY"], "hour": raw["HR"]}, utc=True
    )
    df = raw[list(PARAMETERS)].rename(columns=PARAMETERS).replace(FILL_VALUE, np.nan)
    df.insert(0, "ts_utc", ts.dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return df


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="download again even if the file exists")
    args = parser.parse_args()

    settings = load_settings()
    out = settings.weather_csv
    raw_out = out.with_name(out.stem + "_raw.csv")
    if out.exists() and not args.force:
        print(f"{out} already exists; use --force to download again.")
        return 0

    print(f"Downloading NASA POWER hourly {settings.weather_year} for {settings.site_name} "
          f"({settings.site_lat}, {settings.site_lon}) ...")
    text = fetch_raw(settings.site_lat, settings.site_lon, settings.weather_year)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw_out.write_text(text, encoding="utf-8")

    df = parse_raw(text)
    df.to_csv(out, index=False, lineterminator="\n")
    missing = df.drop(columns="ts_utc").isna().sum()
    print(f"Wrote {len(df)} rows to {out}")
    print(f"Range {df['ts_utc'].iloc[0]} .. {df['ts_utc'].iloc[-1]}")
    print("Missing values per column:", missing.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
