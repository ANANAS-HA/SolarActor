import pandas as pd

from raqeeb.config import load_settings


def test_weather_csv_downloaded():
    path = load_settings().weather_csv
    assert path.exists(), f"missing {path}; run: uv run python scripts/download_weather.py"
    df = pd.read_csv(path)
    assert list(df.columns) == ["ts_utc", "ghi_wm2", "dni_wm2", "dhi_wm2", "ambient_c", "wind_ms"]
    ts = pd.to_datetime(df["ts_utc"], utc=True)
    assert len(df) in (8760, 8784)
    assert ts.is_monotonic_increasing and ts.is_unique
    assert df.drop(columns="ts_utc").notna().all().all()
    assert df["ghi_wm2"].between(0, 1400).all()
    assert df["ghi_wm2"].max() > 800  # sunny desert site
