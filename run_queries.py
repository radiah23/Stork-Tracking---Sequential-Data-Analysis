"""
run_queries.py - run the SQL queries in queries.sql and print the results.

Usage:
    python run_queries.py          # run all queries
    python run_queries.py C1 C3    # run only the queries named C1 and C3
"""

import re
import sqlite3
import sys

import pandas as pd

DB_PATH = "tracking.db"
SQL_PATH = "queries.sql"

# Show wide tables in full instead of cutting columns off with "..."
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)
pd.set_option("display.max_rows", 70)


def load_queries(path):
    """Split queries.sql into a list of (name, title, sql) tuples.

    We read the file line by line:
    - A line starting with '-- C<number>:' begins a new query and is its title.
    - A query ends on the line whose CODE part ends with ';'.
      (We first cut off any '-- comment' on that line, so a ';' written
      inside a comment does NOT end the query.)
    """
    queries = []
    title, lines = None, []

    with open(path) as f:
        for line in f:
            stripped = line.strip()

            # A title line like "-- C3: What is ..." starts a new query
            # (C must be followed by a digit, so "-- Classic ...:" doesn't count)
            if re.match(r"-- C\d+\w*:", stripped):
                title, lines = stripped[3:], []   # e.g. "C3: What is ..."

            if title is None:                     # file header, not a query yet
                continue
            lines.append(line)

            code = stripped.split("--")[0].strip()  # the part before any comment
            if code.endswith(";"):                  # end of this query
                name = title.split(":")[0]          # e.g. "C3"
                queries.append((name, title, "".join(lines)))
                title, lines = None, []

    return queries


def main():
    wanted = [arg.upper() for arg in sys.argv[1:]]  # e.g. ["C1", "C3"]
    conn = sqlite3.connect(DB_PATH)

    for name, title, sql in load_queries(SQL_PATH):
        if wanted and name.upper() not in wanted:
            continue
        print("=" * 80)
        print(title)
        print("=" * 80)
        result = pd.read_sql(sql, conn)  # runs the SQL, returns a DataFrame
        print(result.to_string(index=False))
        print(f"({len(result)} rows)\n")

    conn.close()


if __name__ == "__main__":
    main()
