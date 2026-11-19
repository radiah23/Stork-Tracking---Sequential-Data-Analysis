"""
extract.py - ETL pipeline for white stork GPS tracking data.

Extract  : read the raw Movebank CSV
Transform: clean, order as sequences, add sequential features
Load     : save the result into SQLite (tracking.db)

Run with:  python extract.py
"""

import pandas as pd

CSV_PATH = "data/storks.csv"

# ---------------------------------------------------------------------------
# B1. EXTRACT: load the raw CSV
# ---------------------------------------------------------------------------
# The raw file is 1.8 GB with 23 columns, but we only need 6 of them.
# usecols tells pandas to skip the other 17 columns while reading, which
# saves a lot of memory and time.
# low_memory=False makes pandas read the whole file before deciding each
# column's data type, instead of guessing chunk by chunk (which can give
# "mixed types" warnings).
RAW_COLUMNS = [
    "individual-local-identifier",  # which stork
    "timestamp",                    # when the GPS fix was taken
    "location-lat",                 # latitude  (north/south position)
    "location-long",                # longitude (east/west position)
    "visible",                      # False = flagged as an outlier
    "ground-speed",                 # speed measured by the tag (m/s)
]

print("B1. Loading raw CSV...")
df = pd.read_csv(CSV_PATH, usecols=RAW_COLUMNS, low_memory=False)
print(f"    Raw rows loaded: {len(df):,}")

# ---------------------------------------------------------------------------
# B2. Data quality filtering + rename columns
# ---------------------------------------------------------------------------
# Movebank sets visible = False on points it has flagged as outliers
# (e.g. a GPS fix that jumps 300 km and back). We keep only trusted points.
# pandas already read this column as True/False (booleans), so
# df["visible"] is a column of True/False that we can use as a filter:
# df[mask] keeps only the rows where the mask is True.
rows_before = len(df)
df = df[df["visible"] == True]
print(f"B2. Dropped {rows_before - len(df):,} outlier rows (visible = False)")

# Rename the long Movebank names to short, SQL-friendly names
# (no dashes, so we don't need quotes around them in SQL later).
df = df.rename(columns={
    "individual-local-identifier": "animal_id",
    "location-lat": "latitude",
    "location-long": "longitude",
    "ground-speed": "ground_speed_ms",  # tag's own speed, metres/second
})

# Keep only the columns we need, in a sensible order.
# (visible is now always True, so it carries no information -> drop it.)
df = df[["animal_id", "timestamp", "latitude", "longitude", "ground_speed_ms"]]
print(f"    Rows after filtering: {len(df):,}")
print(f"    Columns: {list(df.columns)}")

# ---------------------------------------------------------------------------
# B3. Clean: missing values + real dates
# ---------------------------------------------------------------------------
# A GPS point is useless without a position AND a time, so drop any row
# where latitude, longitude or timestamp is missing (NaN).
rows_before_clean = len(df)
df = df.dropna(subset=["latitude", "longitude", "timestamp"])
rows_after_dropna = len(df)
print(f"B3. Dropped {rows_before_clean - rows_after_dropna:,} rows with missing lat/lon/timestamp")

# Right now timestamp is just TEXT like "2014-08-01 02:01:23.000".
# pd.to_datetime turns it into a real date-time value, so we can sort it
# correctly and subtract two times to get a duration.
# - format=... tells pandas the exact layout, which is much faster on
#   7 million rows than letting it guess each one.
# - errors="coerce" means: if a value can't be read, turn it into NaT
#   ("Not a Time", the date version of NaN) instead of crashing.
df["timestamp"] = pd.to_datetime(df["timestamp"],
                                 format="%Y-%m-%d %H:%M:%S.%f",
                                 errors="coerce")

# Drop the rows whose timestamp could not be read (now NaT).
df = df.dropna(subset=["timestamp"])
print(f"    Dropped {rows_after_dropna - len(df):,} rows with unreadable timestamps")
print(f"    Rows before cleaning: {rows_before_clean:,}  ->  after: {len(df):,}")
print(f"    timestamp type is now: {df['timestamp'].dtype}")

# ---------------------------------------------------------------------------
# B4. ORDER THE SEQUENCE: sort by animal, then time; remove duplicates
# ---------------------------------------------------------------------------
# Sequential data only makes sense in order. We sort by animal_id FIRST so
# each stork's points sit together in one block, and by timestamp SECOND so
# that inside each block the points run from oldest to newest.
# Result: for every row, the row directly above it is the same stork's
# previous GPS fix (except the very first row of each stork).
df = df.sort_values(["animal_id", "timestamp"])

# Two rows with the same stork AND the same timestamp are duplicates
# (the tag can't be in two places at the same moment). Keep the first one.
rows_before_dedup = len(df)
df = df.drop_duplicates(subset=["animal_id", "timestamp"], keep="first")
print(f"B4. Sorted by animal_id, timestamp. "
      f"Dropped {rows_before_dedup - len(df):,} duplicate (animal_id, timestamp) rows")

# REGULARISE THE SAMPLING: one point per 5 minutes per stork.
# The tags record two kinds of points: a regular fix every 5-15 minutes,
# plus high-resolution BURSTS of one fix per second (93.6% of all rows!).
# Mixing 1-second and 5-minute steps makes the features meaningless:
# a 1-second step is always < 1 km (so almost everything looks like a
# "stop"), and a few metres of GPS wobble over 1 second looks like a
# huge speed. So we keep only the FIRST point in each 5-minute window:
#   dt.floor("5min") rounds 04:03:27 down to 04:00:00, 04:07:10 to 04:05:00
#   drop_duplicates on (animal_id, window) keeps the first point per window
# Because the data is already sorted, "first" = earliest point in the window.
# Note: 46 storks have 5-minute tags; 14 storks have 10-15 minute tags,
# so their steps stay 10-15 minutes long (they have no points in between).
SAMPLE_INTERVAL = "5min"
rows_before_thin = len(df)
df["window"] = df["timestamp"].dt.floor(SAMPLE_INTERVAL)
df = df.drop_duplicates(subset=["animal_id", "window"], keep="first")
df = df.drop(columns="window")  # helper column, no longer needed
print(f"    Thinned to one point per {SAMPLE_INTERVAL} per stork: "
      f"{rows_before_thin:,} -> {len(df):,} rows")

# After filtering and sorting, the old row numbers (index) are scrambled.
# reset_index gives clean numbers 0, 1, 2, ... again.
df = df.reset_index(drop=True)
print(f"    Rows: {len(df):,}")

# Quick look: the first 3 points of the first 2 storks, in order.
print(df.groupby("animal_id").head(3).head(6).to_string())

# ---------------------------------------------------------------------------
# B5. PREVIOUS POINT: groupby + shift(1)
# ---------------------------------------------------------------------------
# shift(1) moves a column down by one row, so each row gets the value from
# the row above it = the previous GPS fix.
# groupby("animal_id") makes the shift happen SEPARATELY for each stork.
# Without it, the first point of stork B would get the LAST point of
# stork A as its "previous point" - a fake jump between two birds.
# With it, the first point of each stork gets NaN (it has no previous point).
#
# This is the pandas version of SQL:
#   LAG(latitude) OVER (PARTITION BY animal_id ORDER BY timestamp)
# groupby = PARTITION BY, the sort from B4 = ORDER BY, shift(1) = LAG.
grouped = df.groupby("animal_id")
df["prev_lat"] = grouped["latitude"].shift(1)
df["prev_lon"] = grouped["longitude"].shift(1)
df["prev_time"] = grouped["timestamp"].shift(1)

# Show the border between the first two storks: the last 2 rows of Balou
# and the first 2 rows of Bello. Bello's first row must have NaN/NaT.
first_bello = df.index[df["animal_id"] != df["animal_id"].iloc[0]][0]
cols = ["animal_id", "timestamp", "latitude", "prev_lat", "prev_time"]
print("B5. Added prev_lat, prev_lon, prev_time. Border between two storks:")
print(df.loc[first_bello - 2 : first_bello + 1, cols].to_string())

# ---------------------------------------------------------------------------
# B6. EXTRACT SEQUENTIAL FEATURES
# ---------------------------------------------------------------------------
# Every feature below compares a point with its PREVIOUS point, so they only
# exist because the data is ordered. The first point of each stork has no
# previous point, so its features are NaN.
import numpy as np

EARTH_RADIUS_KM = 6371


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two GPS points.

    The Earth is a sphere, so we can't use a flat ruler (Pythagoras).
    Haversine measures the shortest path along the surface of the sphere.
    Works on whole pandas columns at once (numpy does the maths per row).
    """
    # 1. Trig functions need radians, not degrees
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    # 2. 'a' combines the north-south and east-west differences,
    #    with cos(lat) shrinking east-west distances away from the equator
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    # 3. c = the angle between the two points, seen from Earth's centre
    c = 2 * np.arcsin(np.sqrt(a))
    # 4. arc length = angle (radians) x radius
    return EARTH_RADIUS_KM * c


# step_km: distance flown since the previous point
df["step_km"] = haversine_km(df["prev_lat"], df["prev_lon"],
                             df["latitude"], df["longitude"])

# gap_hours: time since the previous point.
# Subtracting two datetimes gives a duration; .dt.total_seconds() turns it
# into a number of seconds, / 3600 into hours.
df["gap_hours"] = (df["timestamp"] - df["prev_time"]).dt.total_seconds() / 3600

# speed_kmh: distance / time. (gap is never 0 because we removed duplicate
# timestamps in B4, but we replace any infinity with NaN just to be safe.)
df["speed_kmh"] = (df["step_km"] / df["gap_hours"]).replace([np.inf, -np.inf], np.nan)

# lat_change: negative = moved south, positive = moved north
df["lat_change"] = df["latitude"] - df["prev_lat"]

# point_no: 1, 2, 3, ... per stork. cumcount() counts rows inside each
# group starting at 0, so we add 1.
df["point_no"] = df.groupby("animal_id").cumcount() + 1

# is_stop: moved less than 1 km since the previous point = possibly resting.
# (NaN < 1 is False, so the first point of each stork counts as not a stop.)
df["is_stop"] = df["step_km"] < 1

# --- Checks ---------------------------------------------------------------
features = ["step_km", "gap_hours", "speed_kmh", "lat_change", "point_no", "is_stop"]
print("B6. Added features:", features)
print(f"    Rows with NaN step_km (should be 60 = one per stork): "
      f"{df['step_km'].isna().sum()}")
print(df[["step_km", "gap_hours", "speed_kmh", "lat_change"]]
      .describe(percentiles=[0.5, 0.99]).round(3).to_string())
print(f"    Share of points flagged is_stop: {df['is_stop'].mean():.1%}")

# Sanity check: our speed vs the tag's own GPS speed (m/s x 3.6 = km/h)
print(f"    Median speed_kmh (ours): {df['speed_kmh'].median():.2f}   "
      f"median tag speed in km/h: {(df['ground_speed_ms'] * 3.6).median():.2f}")

# ---------------------------------------------------------------------------
# B7. LOAD: save the result into SQLite
# ---------------------------------------------------------------------------
import sqlite3

DB_PATH = "tracking.db"

# The prev_* columns were only helpers to calculate the features.
# The database doesn't need them (SQL can recompute them with LAG()).
df = df.drop(columns=["prev_lat", "prev_lon", "prev_time"])

# SQLite has no real date-time type; dates are stored as TEXT.
# "%Y-%m-%d %H:%M:%S" (e.g. 2014-08-01 02:01:23) is the format SQLite's
# own date functions understand, and it sorts correctly as text.
df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

# Write the DataFrame to a table called "tracking".
# if_exists="replace": re-running the script rebuilds the table from scratch
#                      instead of adding the rows a second time.
# index=False: don't save pandas' row numbers as an extra column.
# (SQLite has no True/False type either, so is_stop is stored as 1/0.)
conn = sqlite3.connect(DB_PATH)
df.to_sql("tracking", conn, if_exists="replace", index=False)

# An index works like the index at the back of a book: SQLite can jump
# straight to one stork's points in time order instead of scanning all
# 420k rows. This speeds up the PARTITION BY animal_id ORDER BY timestamp
# window-function queries in Phase C.
conn.execute("CREATE INDEX IF NOT EXISTS idx_animal_time ON tracking (animal_id, timestamp)")
conn.commit()

# Check: count the rows that are actually in the database.
n_db = conn.execute("SELECT COUNT(*) FROM tracking").fetchone()[0]
conn.close()

print(f"B7. Saved to {DB_PATH}, table 'tracking': {n_db:,} rows")
print(f"    Columns: {list(df.columns)}")
print(df.head(10).to_string())
