# Scheduling tendium_scraper.py to run daily at 08:00

Before scheduling, make sure:
1. `"headless": true` is set in [config.json](config.json) — either edit the file directly, or check the "Headless mode" box in the [manage.py](manage.py) dashboard and click "Save settings". The first, manual login run must already be complete so `tendium_session/` holds a valid session (cron can't complete an interactive login).
2. You've confirmed the extraction selectors actually match Tendium's real markup (run the script manually once with headless off and check `tendium_tenders.csv`).

## macOS / Linux (cron)

1. Find the absolute paths you'll need:
   ```bash
   cd /Users/valtergeorge/Claude-apps/Tendium-agent
   pwd                        # project directory
   which python3               # only needed if not using the venv python directly
   ```
   The venv's interpreter is at:
   ```
   /Users/valtergeorge/Claude-apps/Tendium-agent/.venv/bin/python
   ```

2. Open your crontab for editing:
   ```bash
   crontab -e
   ```

3. Add a line that runs the venv's Python against the script every day at 08:00, logging output to a file:
   ```cron
   0 8 * * * cd /Users/valtergeorge/Claude-apps/Tendium-agent && /Users/valtergeorge/Claude-apps/Tendium-agent/.venv/bin/python tendium_scraper.py >> /Users/valtergeorge/Claude-apps/Tendium-agent/cron.log 2>&1
   ```
   - `cd` into the project directory first so relative paths (session folder, CSV) resolve correctly.
   - `>> cron.log 2>&1` captures stdout/stderr so you can debug failed runs.

4. Save and exit. Verify it's registered:
   ```bash
   crontab -l
   ```

5. On macOS, if the job silently doesn't run, grant "Full Disk Access" (or at least folder access) to `/usr/sbin/cron` (or your terminal app, depending on setup) in System Settings > Privacy & Security, since cron-launched processes can be sandboxed.

## Windows (Task Scheduler)

1. Locate your venv's Python executable, e.g.:
   ```
   C:\path\to\Tendium-agent\.venv\Scripts\python.exe
   ```

2. Open **Task Scheduler** > **Create Task...** (not "Basic Task", so you get full control).

3. **General tab**:
   - Name: `Tendium Scraper Daily`
   - Select "Run whether user is logged on or not" if you want it to run unattended (requires storing your password), otherwise leave "Run only when user is logged on".

4. **Triggers tab** > New:
   - Begin the task: "On a schedule"
   - Daily, start time `08:00:00`, recur every 1 day.

5. **Actions tab** > New:
   - Action: "Start a program"
   - Program/script: `C:\path\to\Tendium-agent\.venv\Scripts\python.exe`
   - Add arguments: `tendium_scraper.py`
   - Start in: `C:\path\to\Tendium-agent`  (important — sets the working directory so the session folder and CSV resolve correctly)

6. **Conditions/Settings tabs**: uncheck "Start the task only if the computer is on AC power" if this is a laptop, so it still runs on battery.

7. Save the task (you may be prompted for your Windows credentials if you chose "run whether logged on or not").

8. Test it immediately: right-click the task > **Run**, then check `tendium_tenders.csv` and `cron.log`/Task Scheduler's history tab for errors.

## Notes for both platforms

- Cron/Task Scheduler runs headlessly and unattended — if the saved session in `tendium_session/` ever expires (Tendium logs you out), the script will hang waiting for `input()` during the manual-login step, since there's no interactive terminal. Consider adding an alert/timeout, or periodically re-running the script manually with `HEADLESS = False` to refresh the session if you notice the CSV stops updating.
- The scraper appends to `tendium_tenders.csv` by default (`CSV_WRITE_MODE = "append"` in the script) and de-duplicates on (title, deadline, buyer, url), so running it daily accumulates new tenders without duplicating old ones.
