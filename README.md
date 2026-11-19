# Stork Migration Tracking: Sequential GPS Data + "Ask the Data" with an LLM

A small data pipeline that turns 7.2 million raw GPS fixes from 60 white storks into ordered
movement sequences, analyses them with SQL window functions, maps the migration, and lets
anyone ask questions in plain English (an LLM writes the SQL, safely).


## Data

- **Source:** Movebank Data Repository, *Fall migration of white storks in 2014*
  (Flack, Fiedler, Wikelski), [doi:10.5441/001/1.bj96m274](https://doi.org/10.5441/001/1.bj96m274).
  See the DOI page for the full citation and licence.
- 60 storks tagged in southern Germany, tracked 1 Aug – 1 Oct 2014.

| Cleaning step | Rows |
|---|---|
| Raw file (1.8 GB, 23 columns; 6 loaded) | 7,169,458 |
| Drop Movebank-flagged outliers (`visible = false`) | 7,153,159 |
| Drop missing lat/lon/timestamp | 7,149,404 |
| Drop duplicate (stork, timestamp) | 7,149,404 |
| **Regularise to one fix per 5 minutes per stork** | **421,898** |

**Why regularise?** The tags record a fix every 5–15 minutes *plus* 1-second bursts, and
those bursts are 93.6% of all rows. With mixed 1-second and 5-minute steps, 99.4% of
points looked like "stops" and the top speed came out at 64,768 km/h. After keeping the
first fix in each 5-minute window, the 99th-percentile speed is 47 km/h, which is realistic
for a stork. (46 storks have 5-minute tags; 14 have 10–15-minute tags, so their steps
stay longer.)

## Pipeline

```
storks.csv 
  -> EXTRACT   pandas, only the 6 needed columns
  -> CLEAN     outliers, missing values, real datetimes
  -> ORDER     sort by stork, then time; one fix per 5 min
  -> FEATURES  groupby + shift(1) -> step_km, speed_kmh, lat_change, ...
  -> LOAD      SQLite tracking.db (+ index on animal_id, timestamp)
  -> QUERY     SQL incl. window functions (queries.sql)
  -> VISUALIZE matplotlib charts + folium map
  -> ASK       LLM text-to-SQL, read-only (ask_data.py)
```

## Sequential features extracted

All features come from `df.groupby("animal_id").shift(1)`, the pandas version of SQL's
`LAG(...) OVER (PARTITION BY animal_id ORDER BY timestamp)`. Each stork's first fix has no
previous point, so its features are NULL.

| Column | Meaning |
|---|---|
| `step_km` | Haversine (great-circle) distance from the previous fix |
| `gap_hours` | Hours since the previous fix |
| `speed_kmh` | `step_km / gap_hours` |
| `lat_change` | Latitude minus previous latitude (negative = moving south) |
| `point_no` | 1, 2, 3, … per stork (`groupby().cumcount() + 1`) |
| `is_stop` | `step_km < 1` (stork barely moved) |

## Example: the LAG window function

The SQL version gives exactly the same result as the pandas version: **0 mismatches across
all 421,898 rows** (query C6b).

```sql
WITH lagged AS (
    SELECT animal_id, timestamp, latitude,
           LAG(latitude) OVER (PARTITION BY animal_id ORDER BY timestamp) AS prev_lat_sql,
           lat_change AS lat_change_pandas
    FROM tracking
)
SELECT animal_id, timestamp, latitude, prev_lat_sql,
       latitude - prev_lat_sql AS lat_change_sql, lat_change_pandas
FROM lagged
WHERE animal_id LIKE 'Ekky%' AND timestamp >= '2014-09-01 11:00'
LIMIT 3;
```

| timestamp | latitude | prev_lat_sql | lat_change_sql | lat_change_pandas |
|---|---|---|---|---|
| 2014-09-01 11:00:06 | 33.357155 | 33.361843 | -0.004688 | -0.004688 |
| 2014-09-01 11:05:07 | 33.338836 | 33.357155 | -0.018319 | -0.018319 |
| 2014-09-01 11:10:06 | 33.337786 | 33.338836 | -0.001050 | -0.001050 |

**Lesson learned:** `WHERE` runs *before* window functions. My first version filtered
first, so the first row had no "previous" row and a running total only summed the filtered
days. The fix is to compute the window function in a CTE first and filter afterwards.

Other queries in [queries.sql](queries.sql) include a running total (`SUM() OVER`),
`RANK()`, and stopover detection with the **gaps-and-islands** trick (consecutive rest
days share the same `julianday(day) - ROW_NUMBER()` value).

## Findings

- **Ekky flew the farthest: 7,956 km in 62 days** (about 129 km per day), reaching the
  Sahel near the Mali/Niger border.
  The top 5 by distance are exactly the 5 storks that crossed the Sahara to the Sahel
  (lat ~15–17° N).
- **Everyone took the western route:** Germany → France → Spain → Strait of Gibraltar.
  Furthest point reached (by 1 Oct, or when the track ended): Sahel 5, Morocco 9, near
  Gibraltar 7, Spain or southern France 32 (where many white storks now spend the winter),
  and 7 barely left Germany.
- **Biggest single day:** Schwitza flew **558 km on 14 Sep 2014** (counting only steps
  after gaps of 1 hour or less).
- **Stopovers:** Blacky and Chompy each waited about 8 days near the Strait of Gibraltar.
  Some long "rests" end on the day the stork's track ends, which more likely means the bird
  died or lost its tag than that it was resting.
- **Data gaps matter:** 28 of 60 tracks end early, 5 within the first 11 days. One tag was
  silent for 5 days and then reported a single 1,097 km step.


## Ask the data (LLM text-to-SQL)

`ask_data.py` lets a non-technical user type a question such as *"Which stork flew the
farthest?"*:

1. **Prompt:** Claude (`claude-haiku-5-5`) gets the table schema, what each column *means*
   (e.g. negative `lat_change` = south), the data-quality rules found above (speeds over
   120 km/h are GPS errors), and 3 example question → SQL pairs (**few-shot prompting**).
2. **Safety:** the SQL runs only if it is a single `SELECT`/`WITH` statement with no
   data-changing keywords, gets `LIMIT 200` added, and runs on a **read-only** connection
   (`file:tracking.db?mode=ro`). Tested: `DROP`, `DELETE` hidden inside `WITH`, stacked
   statements and `ATTACH` are all blocked, and a direct write is refused by SQLite itself.
3. **Self-correction:** if the SQL errors, the error is sent back once for a fix.
4. **Answer:** a second call explains the result table in one or two sentences.
5. **Transparency:** the generated SQL is always shown, so the user can check it.

**Limits:** an LLM can write SQL that runs but answers a slightly different question.
That is why the SQL is shown and why the schema prompt spells out the data rules.

**Status:** the LLM calls have not been run yet (they need an Anthropic API key). The
safety layer is tested and runs without any key:

```
$ python ask_data.py --safety-test
Layer 1: SQL checks (SELECT/WITH only, one statement, no write keywords, LIMIT)
  ALLOWED  normal question           -> result 421898
  BLOCKED  drop the table            -> Only SELECT / WITH queries are allowed.
  BLOCKED  two statements            -> Only one SQL statement is allowed.
  BLOCKED  DELETE hidden in WITH     -> Query contains a forbidden (data-changing) keyword.
  BLOCKED  attach another database   -> Only SELECT / WITH queries are allowed.

Layer 2: read-only connection (even if a write got past layer 1)
  BLOCKED  direct DELETE              -> attempt to write a readonly database
```

## How to run

```bash
pip install -r requirements.txt
# download the CSV from the DOI above into data/storks.csv
python extract.py        # build tracking.db (~30 s)
python run_queries.py    # all SQL queries (or: python run_queries.py C6 C9)
python visualize.py      # charts + map.html

python ask_data.py --safety-test   # safety layer demo, no key needed

# LLM part: put your key in .env (this file is git-ignored)
echo "ANTHROPIC_API_KEY=your-key-here" > .env
python ask_data.py "Which stork flew the farthest?"
python ask_data.py --test
```

