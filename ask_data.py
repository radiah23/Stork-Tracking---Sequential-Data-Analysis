"""
ask_data.py - "Ask the data": plain-English questions -> SQL -> answer.

This is TEXT-TO-SQL with an LLM:
  1. Send the question + the table schema to Claude; it writes a SQL query.
  2. Safety-check the SQL and run it on a READ-ONLY copy of tracking.db.
  3. If the SQL fails, send the error back once so Claude can fix it.
  4. Send the result table to Claude again; it explains it in plain English.

Usage:
  python ask_data.py "Which stork flew the farthest?"
  python ask_data.py --test          # run the 5 test questions
  python ask_data.py --safety-test   # show the safety layer (no API key needed)

Questions need ANTHROPIC_API_KEY in a .env file (see README). Never commit .env.
"""

import re
import sqlite3
import sys

import anthropic
import pandas as pd
from dotenv import load_dotenv

load_dotenv()  # reads .env and puts ANTHROPIC_API_KEY into the environment

MODEL = "claude-haiku-5-5"  # cheap and fast; plenty for short SQL queries
DB_URI = "file:tracking.db?mode=ro"  # mode=ro = read-only: writes are impossible
MAX_ROWS = 200

# ---------------------------------------------------------------------------
# 1. PROMPT: schema + rules + few-shot examples
# ---------------------------------------------------------------------------
# The LLM has never seen our database. Everything it knows about it comes
# from this text: column names, types, what each column MEANS, and the
# data-quality rules we discovered in Phases B-C.
SCHEMA = """
SQLite table: tracking   (one row = one GPS fix of one white stork)
Data: 60 white storks tagged in southern Germany, fall migration
1 Aug - 1 Oct 2014, one GPS fix about every 5 minutes, ordered per stork by time.

Columns:
  animal_id        TEXT    stork name + tag, e.g. 'Ekky / DER AU007 (eobs 3929)'.
                           Match a stork by name with: animal_id LIKE 'Ekky%'
  timestamp        TEXT    'YYYY-MM-DD HH:MM:SS' (UTC). Use DATE(timestamp) for the day.
  latitude         REAL    degrees north. Lower = further south. ~47.8 = start in Germany,
                           ~36 = Strait of Gibraltar, below ~35.9 = Africa.
  longitude        REAL    degrees east (negative = west of Greenwich).
  ground_speed_ms  REAL    speed measured by the tag, metres per second.
  step_km          REAL    distance in km from this stork's previous fix (NULL for its first fix).
  gap_hours        REAL    hours since this stork's previous fix (NULL for its first fix).
  speed_kmh        REAL    step_km / gap_hours (NULL for first fix).
  lat_change       REAL    latitude minus previous latitude (negative = moved south).
  point_no         INTEGER 1, 2, 3, ... per stork in time order.
  is_stop          INTEGER 1 if step_km < 1 (stork barely moved), else 0.

Rules for correct answers:
  - Speeds above 120 km/h are GPS errors: add WHERE speed_kmh <= 120 to speed AND distance
    questions (use "speed_kmh <= 120 OR speed_kmh IS NULL" when first fixes must stay).
  - For per-DAY distances also require gap_hours <= 1 (steps after long tag silences
    would otherwise put several days of travel on one day).
  - "Flying" means speed_kmh > 12. "Resting" means is_stop = 1.
  - Some tags stopped early, so compare storks fairly with km per day when relevant.
  - Show short names with: substr(animal_id, 1, instr(animal_id, ' /') - 1) AS stork
"""

FEW_SHOT = """
Example questions and correct SQL:

Q: How many GPS points does each stork have?
SQL: SELECT substr(animal_id, 1, instr(animal_id, ' /') - 1) AS stork, COUNT(*) AS n_points
     FROM tracking GROUP BY animal_id ORDER BY n_points DESC;

Q: What was Ekky's top speed?
SQL: SELECT MAX(speed_kmh) AS top_speed_kmh FROM tracking
     WHERE animal_id LIKE 'Ekky%' AND speed_kmh <= 120;

Q: Which storks reached Africa?
SQL: SELECT substr(animal_id, 1, instr(animal_id, ' /') - 1) AS stork,
            ROUND(MIN(latitude), 2) AS southernmost_lat
     FROM tracking GROUP BY animal_id HAVING MIN(latitude) < 35.9
     ORDER BY southernmost_lat;
"""

SQL_SYSTEM_PROMPT = f"""You translate questions about stork GPS data into ONE SQLite query.
{SCHEMA}
{FEW_SHOT}
Reply with ONLY the SQL query: no explanation, no markdown fences.
The query must be a single SELECT (or WITH ... SELECT) statement that only reads data."""

ANSWER_SYSTEM_PROMPT = """You explain database results to a non-technical user.
Answer the user's question in one or two plain-English sentences, using the numbers
from the result table. Do not mention SQL. If the table is empty, say no data matched."""

def call_claude(system, user_text):
    """Send one message to Claude and return its text reply."""
    # Created here (not at the top) so --safety-test works without any API key.
    client = anthropic.Anthropic()  # finds ANTHROPIC_API_KEY automatically
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "low"},  # short, simple task -> little thinking needed
        system=system,
        messages=[{"role": "user", "content": user_text}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined this request.")
    # The reply is a list of blocks (thinking, text, ...): keep only the text
    return "".join(b.text for b in response.content if b.type == "text").strip()


# ---------------------------------------------------------------------------
# 2. SAFETY: never run LLM-written SQL that could change data
# ---------------------------------------------------------------------------
FORBIDDEN = r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b"


def clean_sql(text):
    """Remove markdown fences like ```sql ... ``` if the model added them anyway."""
    text = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    return text.strip().rstrip(";").strip()


def check_safe(sql):
    """Raise an error if the SQL is not a single read-only query.

    These checks are the first line of defence. The second (and strongest)
    is that the database is opened read-only, so even a query that slipped
    through could not change anything.
    """
    code = re.sub(r"--[^\n]*", "", sql)          # ignore -- comments
    lowered = code.strip().lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("Only SELECT / WITH queries are allowed.")
    if ";" in code:
        raise ValueError("Only one SQL statement is allowed.")
    if re.search(FORBIDDEN, lowered):
        raise ValueError("Query contains a forbidden (data-changing) keyword.")


def add_limit(sql):
    """Add LIMIT 200 if the query has none, so a huge result can't flood the app."""
    if re.search(r"\blimit\s+\d+", sql, flags=re.IGNORECASE):
        return sql
    return f"{sql}\nLIMIT {MAX_ROWS}"


def run_sql(sql):
    """Run the query on a READ-ONLY connection and return a DataFrame."""
    conn = sqlite3.connect(DB_URI, uri=True)
    try:
        return pd.read_sql(sql, conn)
    finally:
        conn.close()


def safe_run(sql):
    sql = add_limit(clean_sql(sql))
    check_safe(sql)
    return sql, run_sql(sql)


# ---------------------------------------------------------------------------
# 3. THE PIPELINE: question -> SQL -> (fix once) -> result -> plain answer
# ---------------------------------------------------------------------------
def ask(question):
    """Answer a question. Returns a dict with the sql, the table and the answer."""
    sql = call_claude(SQL_SYSTEM_PROMPT, question)
    try:
        sql, table = safe_run(sql)
    except Exception as error:
        # Self-correction: show Claude its own SQL and the error, ask for a fix.
        # Only ONE retry, so a confused model can't loop forever (or cost much).
        fix_request = (f"Question: {question}\n\nYour SQL:\n{sql}\n\n"
                       f"It failed with this error:\n{error}\n\nReturn a corrected query.")
        sql = call_claude(SQL_SYSTEM_PROMPT, fix_request)
        sql, table = safe_run(sql)   # if this fails too, the error goes to the caller

    answer = call_claude(
        ANSWER_SYSTEM_PROMPT,
        f"Question: {question}\n\nResult table:\n{table.head(30).to_string(index=False)}",
    )
    return {"question": question, "sql": sql, "table": table, "answer": answer}


TEST_QUESTIONS = [
    "Which stork flew the farthest?",
    "Where did the storks stop to rest the longest?",
    "What was the fastest day of migration?",
    "How many km did each stork fly per day on average?",
    "What share of time was each stork flying?",
]


def print_result(result):
    print("=" * 80)
    print("Q:", result["question"])
    print("-" * 80)
    print("SQL:\n" + result["sql"])           # always show the SQL (transparency)
    print("-" * 80)
    print(result["table"].head(10).to_string(index=False))
    print("-" * 80)
    print("A:", result["answer"], "\n")


def safety_test():
    """Show the safety layer working - no LLM or API key needed.

    These are the kinds of SQL a confused (or manipulated) LLM might write.
    """
    attempts = {
        "normal question":          "SELECT COUNT(*) AS n FROM tracking",
        "markdown fences":          "```sql\nSELECT MAX(step_km) FROM tracking;\n```",
        "drop the table":           "DROP TABLE tracking",
        "two statements":           "SELECT 1; DELETE FROM tracking",
        "DELETE hidden in WITH":    "WITH x AS (SELECT 1) DELETE FROM tracking",
        "attach another database":  "ATTACH 'evil.db' AS e",
    }
    print("Layer 1: SQL checks (SELECT/WITH only, one statement, no write keywords, LIMIT)")
    for name, sql in attempts.items():
        try:
            final_sql, table = safe_run(sql)
            print(f"  ALLOWED  {name:25} -> result {table.iloc[0, 0]}")
        except Exception as error:
            print(f"  BLOCKED  {name:25} -> {error}")

    print("\nLayer 2: read-only connection (even if a write got past layer 1)")
    conn = sqlite3.connect(DB_URI, uri=True)
    try:
        conn.execute("DELETE FROM tracking")
    except sqlite3.OperationalError as error:
        print(f"  BLOCKED  direct DELETE              -> {error}")
    rows = conn.execute("SELECT COUNT(*) FROM tracking").fetchone()[0]
    conn.close()
    print(f"  Rows still in table: {rows:,}")


def main():
    if sys.argv[1:] == ["--safety-test"]:
        safety_test()
        return
    questions = TEST_QUESTIONS if sys.argv[1:] == ["--test"] else [" ".join(sys.argv[1:])]
    if questions == [""]:
        print(__doc__)
        return
    for q in questions:
        try:
            print_result(ask(q))
        except anthropic.AuthenticationError:
            print("No valid API key. Put ANTHROPIC_API_KEY=... in a .env file.")
            return
        except Exception as error:
            print("=" * 80)
            print("Q:", q)
            print("FAILED:", error, "\n")


if __name__ == "__main__":
    main()
