GEO_PROJECT/
│
├── data/
│   ├── properties_geocoded.csv     # Master dataset (source of truth)
│   └── properties.db               # SQLite database used by app
│
├── app.py                          # Main application
├── reload_db.py                    # Reload DB from CSV
├── requirements.txt
└── README.md

🔁 Data Update Workflow

When the geocoded CSV is updated, you must reload the database before running the app.

Step 1 — Replace the CSV

Place the updated file at:

data/properties_geocoded.csv

This file is the master dataset.

Step 2 — Reload the Database

From project root:

py reload_db.py

This will:

Overwrite the properties table

Sync the database with the CSV

Ensure the app uses updated coordinates

Expected output:

Database successfully reloaded.
Step 3 — Run the Application
py app.py

(or however the app is normally launched)

⚠ Important Rules

The CSV is the source of truth.

The SQLite DB is derived from the CSV.

Never manually edit the SQLite database.

Always reload using reload_db.py.

Keep only one database file: data/properties.db.

🧠 Why This Architecture

CSV = portable, human-readable, version-controlled

SQLite = fast query layer for runtime

reload_db.py = deterministic, repeatable synchronization

This keeps the system clean, modular, and reproducible.


🔎 Quick Database Check

To verify DB integrity:

py -c "import sqlite3; c=sqlite3.connect('data/properties.db'); print(c.execute('select count(*) from properties').fetchone()); c.close()"
🔮 Future Improvements (Optional)

Add timestamp validation (warn if CSV newer than DB)

Automate reload on app startup

Migrate to Postgres for production scale

Add route optimization module
