# tfl-to-sql

Turns a Tableau Prep flow into ready-to-use SQL — so logic built in Tableau's
drag-and-drop flow editor doesn't have to be rebuilt by hand in a data
warehouse.

## 🚀 Try it now

**[tfl-to-sql.streamlit.app](https://tfl-to-sql.streamlit.app/)**

No install, no account, no setup — upload a `.tfl`/`.tflx` flow file and get
SQL back.

<img width="1435" height="807" alt="Screenshot 2026-07-04 at 22 48 40" src="https://github.com/user-attachments/assets/69be6d18-63ad-42a6-b75d-ccdc4f5b9e85" />


---

## How to use the web app

1. **Upload your flow.** Drag in a `.tfl` or `.tflx` file.
2. **(Optional) Add a schema.** Expand *"Build a schema from a column list"*
   in the sidebar, pick a source table, and paste or upload its column
   names. This isn't required, but it unlocks a couple of things the tool
   otherwise can't do safely — the ⓘ next to **Schema** explains exactly
   what and why.
3. **(Optional) Add overrides.** For anything flagged that you want fixed
   permanently (a Tableau parameter's real value, for example), upload an
   `overrides.json` — see the ⓘ next to **Overrides** for the format.
4. **Click Convert.** You'll get:
   - A summary of how many steps and formulas were found, and what
     percentage translated cleanly (hover the ⓘ on any number for what it
     means)
   - The generated SQL, with anything needing a manual check highlighted
     right in the code — not just buried in a separate list
   - Download buttons for each file, or everything as one zip
5. **Check anything highlighted**, then drop the SQL into your warehouse or
   Dataform project.

That's the whole workflow — no command line required.

---

## What it actually handles

Most of what Tableau Prep can do translates automatically and correctly:
calculated fields (however deeply nested), `IF/THEN/ELSE` logic, date math,
joins, unions, groupings, pivots, and column rename rules. The few things
that genuinely *can't* be figured out automatically — a value that only
lives inside Tableau, or a calculation the tool doesn't recognise — are
never silently guessed. They're marked with a `TODO` right in the SQL and
listed in the summary, so nothing wrong-but-plausible slips through.

Verified against real production Tableau Prep flows: zero crashes, zero
invalid SQL, 100% of calculated fields translating cleanly.

---

Designed by Yash Sakhuja | Data & AI Scientist
