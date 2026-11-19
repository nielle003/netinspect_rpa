# New Vincent Code Start: November 14, 2026
import atexit

# New Vincent Code End: November 14, 2026
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

load_dotenv(override=True)  # .env wins over any old value set in the terminal/system

Part = namedtuple("Part", "number name po rev lot")


def env_bool(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def env_str(name, default=""):
    return os.getenv(name, default).strip()


def env_list(name):
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


DASHBOARD_URL = "https://www.net-inspect.com/dashboard"
CREATE_FAIR_URL = "https://www.net-inspect.com/firstarticles/create/new"
CDP_URL = env_str("CDP_URL")

USER_ID = os.getenv("NI_USER_ID")
PASSWORD = os.getenv("NI_PASSWORD")
COMPANY = env_str("NI_COMPANY")
COUNTRY = env_str("NI_COUNTRY", "PHILIPPINES")
CUSTOMER = env_str("NI_CUSTOMER", "COLLINS AEROSPACE - INTERIORS")
DIVISION = env_str("NI_DIVISION", "3700001-Philippines-OE-Galleys")
FAIR_PROGRAM = env_str("NI_FAIR_PROGRAM", "Low_Tech_Data")

DB_TABLE = env_str("DB_TABLE")
COL_PART_NUMBER = env_str("COL_PART_NUMBER", "part_number")
COL_PART_NAME = env_str("COL_PART_NAME", "part_name")
COL_STATUS = env_str("COL_STATUS", "status")
COL_PO_NUMBER = env_str("COL_PO_NUMBER", "poNumber")
STATUS_PENDING = env_str("STATUS_PENDING", "0")
DB_LIMIT = int(os.getenv("DB_LIMIT", "1"))
# New Vincent Code Start: October 21, 2026
# New Vincent Code Start: October 28, 2026
# Optional row filter. Empty or missing = no prefix filter (XA and XP both allowed).
PART_PREFIX = env_str("PART_PREFIX", "")
# New Vincent Code End: October 28, 2026
# New Vincent Code End: October 21, 2026
LOOKUP_PARTS = env_list("LOOKUP_PART_NUMBERS")
COL_ID = env_str("COL_ID", "netId")
LOOKUP_IDS = env_list("LOOKUP_IDS")
COL_DRAWING_REV = env_str("COL_DRAWING_REV")
COL_LOT_NUMBER = env_str("COL_LOT_NUMBER")
COL_PENDING = env_str("COL_PENDING", "isPending")
PENDING_VALUE = env_str("PENDING_VALUE", "1")

# Timestamps: startStamp = set right after the first query picks the part up (only if still empty),
# finishStamp = set when the script reaches the final OK button of the Submit popup.
# UPDATE_STAMPS=false in .env turns both off.
UPDATE_STAMPS = env_bool("UPDATE_STAMPS", True)
COL_START_STAMP = env_str("COL_START_STAMP", "startStamp")
COL_FINISH_STAMP = env_str("COL_FINISH_STAMP", "finishStamp")

# New Vincent Code Start: November 5, 2026
# --- isFinished cycle ---
# GATHER_FIRST: before the run, copy new records into DB_TABLE with GATHER_SQL (step 1 of the cycle).
# COL_FINISHED: rows with isFinish = 0 are the ones the RPA picks up.
# UPDATE_FINISHED: after a part ends with a status listed in FINISHED_STATUSES, set isFinish = 1.
# FINISHED_STATUSES: comma separated in .env. Default FAIR_SUBMITTED = SIGN ALL FORMS + SUBMIT both done.
GATHER_FIRST = env_bool("GATHER_FIRST", False)
COL_FINISHED = env_str("COL_FINISHED", "isFinished")
FINISHED_VALUE = env_str("FINISHED_VALUE", "1")
UPDATE_FINISHED = env_bool("UPDATE_FINISHED", False)
FINISHED_STATUSES = set(env_list("FINISHED_STATUSES")) or {"FAIR_SUBMITTED"}
# near FINISHED_STATUSES
REUSE_STATUSES = set(env_list("REUSE_STATUSES")) or {"FAIR_SUBMITTED"}

# New Vincent Code Start: November 13, 2026
# Retry settings.
# STEP_ATTEMPTS: how many times retry_step() runs one small step before giving up (only for steps safe to repeat).
# PART_MAX_ATTEMPTS: how many times the SAME part (same part number and lot) is run from the start (1 = no restart).
# RETRY_STATUSES: results that trigger a restart of the part (comma separated in .env).
# RETRY_AFTER_CREATE: false = never restart a part once CREATE FAIR was clicked (a restart could create a second FAIR).
STEP_ATTEMPTS = max(1, int(env_str("STEP_ATTEMPTS", "3")))
PART_MAX_ATTEMPTS = max(1, int(env_str("PART_MAX_ATTEMPTS", "2")))
RETRY_AFTER_CREATE = env_bool("RETRY_AFTER_CREATE", False)
RETRY_STATUSES = set(env_list("RETRY_STATUSES")) or {
    "ERROR",
    "FAIR_UNCONFIRMED",
    "FAIR_SAVE_FAILED",
    "FAIR_SAVE_SKIPPED",
    "FAIR_INDEX_FAILED",
    "FORM2_NOT_READY",
    "FORM2_SAVE_FAILED",
    "FORM2_UPLOAD_SAVE_FAILED",
    "FORM3_FAILED",
    "DOCUMENTS_FAILED",
    "DOCUMENTS_NOT_READY",
    "CHECKLIST_FAILED",
}
# New Vincent Code End: November 13, 2026
# finishRemarks is written in the same UPDATE as isFinish (same table).
# Set COL_FINISH_REMARKS= (empty) in .env to skip it.
COL_FINISH_REMARKS = env_str("COL_FINISH_REMARKS", "finishRemarks")
FINISH_REMARKS_VALUE = env_str("FINISH_REMARKS_VALUE", "RPA")

# The INSERT ... SELECT that gathers new records into DB_TABLE. Fill in your source table and condition.
# The new rows must have isFinish = 0 and isPending = 0, and must also fill isSub (the checklist needs it),
# resultId (Form 3), lotNumber, revision and poNumber.
# Leave it empty to skip the gather step. {table} is replaced with DB_TABLE.
GATHER_SQL = """
-- INSERT INTO {table}
--     (partNumber, partName, poNumber, revision, lotNumber, isSub, resultId, isPending, isFinish)
-- SELECT s.partNumber, s.partName, s.poNumber, s.revision, s.lotNumber, s.isSub, s.resultId, 0, 0
-- FROM <SOURCE_TABLE> s
-- WHERE <CONDITION: the record is ready to be sent to Net-Inspect>
--   AND NOT EXISTS (
--       SELECT 1 FROM {table} n
--       WHERE TRIM(n.lotNumber) = TRIM(s.lotNumber)
--         AND TRIM(n.partNumber) = TRIM(s.partNumber)
--   )
"""
# New Vincent Code End: November 5, 2026

SHOW_REQUEST_DIALOG = env_bool("SHOW_REQUEST_DIALOG", True)
AUTO_REQUEST_YES = env_bool("AUTO_REQUEST_YES", False)
SUBMIT_REQUEST_FORM = env_bool("SUBMIT_REQUEST_FORM", False)
UPDATE_STATUS = env_bool("UPDATE_STATUS", False)
INTERACTIVE = env_bool("INTERACTIVE", True)
CLICK_CREATE_FAIR = env_bool("CLICK_CREATE_FAIR", False)
NO_TIMEOUT = env_bool("NO_TIMEOUT", True)
# --- speed settings (all optional) ---
SETTLE_MS = int(
    env_str("SETTLE_MS", "1500")
)  # max wait for "network idle" after a page action
SESSION_FILE = env_str(
    "SESSION_FILE", "ni_session.json"
)  # saved login cookies; empty = always log in
PART_CHECK_SECONDS = float(
    env_str("PART_CHECK_SECONDS", "1.5")
)  # grace time to see the "not set up" dialog
TIMING = env_bool("TIMING", True)  # print how long each stage took

FILL_EXTRA_FIELDS = env_bool("FILL_EXTRA_FIELDS", False)
DEBUG_FIELDS = env_bool("DEBUG_FIELDS", False)
KEEP_OPEN = env_bool("KEEP_OPEN", True)
CLICK_SAVE_FAIR = env_bool("CLICK_SAVE_FAIR", False)
SAVE_SUCCESS_TEXT = env_str("SAVE_SUCCESS_TEXT")
SAVE_SCREENSHOTS = env_bool("SAVE_SCREENSHOTS", False)
SCREENSHOT_FULL_PAGE = env_bool("SCREENSHOT_FULL_PAGE", False)
FILL_PART_REVISION = env_bool("FILL_PART_REVISION", True)
FILL_CUSTOMER_PART = env_bool("FILL_CUSTOMER_PART", True)
FILL_PO_NUMBER = env_bool("FILL_PO_NUMBER", True)
FAIR_NONCONFORMANCE = env_str("FAIR_NONCONFORMANCE", "No")
FAIR_SERIAL_NUMBER = env_str("FAIR_SERIAL_NUMBER", "none")
FAIR_SUPPLIER_CODE = env_str("FAIR_SUPPLIER_CODE")
FAIR_DETAIL_ASSEMBLY = env_str("FAIR_DETAIL_ASSEMBLY", "Detail")
FAIR_FULL_PARTIAL = env_str("FAIR_FULL_PARTIAL", "Full FAI")
FAIR_FULL_PARTIAL_REASON = env_str("FAIR_FULL_PARTIAL_REASON", "New design or product")

QC_TABLE = env_str("QC_TABLE", "qc_fai2")
COL_QC_LOT = env_str("COL_QC_LOT", "lotNumber")
FILL_FORM2_MATERIALS = env_bool("FILL_FORM2_MATERIALS", False)
FILL_FORM2_PROCESSES = env_bool("FILL_FORM2_PROCESSES", False)
FORM2_OVERWRITE_FIRST_ROW = env_bool("FORM2_OVERWRITE_FIRST_ROW", False)
DEBUG_FORM2 = env_bool("DEBUG_FORM2", False)
FORM2_LOT_OVERRIDE = env_str("FORM2_LOT_OVERRIDE")
PROCESS_ROOT = env_str("PROCESS_ROOT", "ni-fair-processes")
WANT_FORM2 = FILL_FORM2_MATERIALS or FILL_FORM2_PROCESSES
SAVE_FORM2_AT_END = env_bool("SAVE_FORM2_AT_END", False)
UPLOAD_FORM2_FILES = env_bool("UPLOAD_FORM2_FILES", False)
CERT_FOLDER = env_str(
    "CERT_FOLDER", r"\\192.168.254.163\www\html\V4\cocprint\coc_documents"
)

SCREENSHOT_DIR = Path("screenshots")
ERROR_WORDS = ("error occurred", "something went wrong", "failed")
SAVE_ERROR_WORDS = ERROR_WORDS + ("unable to save", "could not save")

NO_SMOOTH_SCROLL_JS = """
(() => {
  const add = () => {
    const s = document.createElement('style');
    s.textContent = 'html, body, * { scroll-behavior: auto !important; }';
    (document.head || document.documentElement).appendChild(s);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add);
  else add();
})();
"""

RESULT_LABELS = {
    "EXISTS": "exists at customer -> form filled, CREATE FAIR not clicked (CLICK_CREATE_FAIR is off)",
    "FAIR_CREATED": "exists at customer -> CREATE FAIR clicked, FAIR created",
    "FAIR_FILLED": "exists at customer -> FAIR created and fixed fields filled (not saved)",
    "FAIR_FILLED_PARTIAL": "exists at customer -> FAIR created, SOME fixed fields failed (check page)",
    "FAIR_SAVED_FORM2": "exists at customer -> FAIR filled, SAVE FAIR clicked, Form 2 opened",
    "FAIR_SAVED_FORM2_UNCONFIRMED": "exists at customer -> FAIR saved, but Form 2 did not open (check page)",
    "FAIR_SAVED_UNVERIFIED": "exists at customer -> SAVE FAIR clicked, save not verified (check page)",
    "FAIR_SAVE_FAILED": "exists at customer -> SAVE FAIR failed or showed an error (check page)",
    "FAIR_SAVE_SKIPPED": "exists at customer -> some fields failed, SAVE FAIR NOT clicked (check page)",
    "FAIR_UNCONFIRMED": "exists at customer -> CREATE FAIR clicked but not confirmed (check Net-Inspect)",
    "FORM2_SAVED": "Form 2 filled (Materials + Processes), SAVE FAIR clicked once at the end",
    "FORM2_NOT_READY": "Form 2 still saving/uploading or unsaved, Form 3 tab NOT clicked (check page)",
    "FORM2_UPLOAD_SAVE_FAILED": "Form 2 uploads done, but the SAVE FAIR after them failed; Form 3 NOT opened (check page)",
    "FORM2_SAVE_FAILED": "Form 2 filled, but the final SAVE FAIR failed or showed an error (check page)",
    "FAIR_FORM2_OPENED": "FAIR created and filled, Form 2 opened (SAVE FAIR not clicked), nothing to fill there",
    "FAIR_FORM2_UNCONFIRMED": "FAIR created and filled, but Form 2 did not open (SAVE FAIR not clicked; check page)",
    "FORM2_MATERIALS_FILLED": "Form 2 opened, Materials row filled (nothing saved or locked by the script on Form 2)",
    "FORM2_MATERIALS_PARTIAL": "Form 2 Materials row: SOME fields failed (check page)",
    "FORM2_MATERIALS_SKIPPED": "Form 2 opened, Materials skipped (no lot / no qc_fai2 row)",
    "FORM2_PROCESSES_FILLED": "Form 2: Processes rows filled (nothing saved or locked by the script on Form 2)",
    "FORM2_PROCESSES_PARTIAL": "Form 2: Processes rows, SOME fields failed (check page)",
    "FORM2_PROCESSES_SKIPPED": "Form 2: Processes skipped (no lot / no qc_fai2 row / no process items)",
    "FORM3_FILLED": "Form 3: Add Characteristic form filled from the inspection result (not saved; check the page)",
    "FORM3_SKIPPED": "Form 3 skipped (no resultId / no inspection row found for this part)",
    "FORM3_FAILED": "Form 3 failed (see message above, check the page)",
    "NOT_AT_CUSTOMER": "NOT at customer -> no request started",
    "REQUEST_SUBMITTED": "NOT at customer -> part request SUBMITTED to the customer",
    "REQUEST_ALREADY_PENDING": "NOT at customer -> request was ALREADY made and is Pending (no new request sent)",
    "REQUEST_UNCONFIRMED": "NOT at customer -> Submit clicked but not confirmed (check Net-Inspect; database NOT updated)",
    "REQUEST_FORM_FILLED": "NOT at customer -> YES clicked, request form filled (NOT submitted)",
    # New Vincent Code Start: November 5, 2026
    "FAIR_SUBMITTED": "FAIR signed and SUBMITTED to the customer (isFinish = 1 and finishRemarks = RPA are set when UPDATE_FINISHED is on)",
    "CHECKLIST_COMPLETED": "Checklist signed, but SIGN ALL FORMS / SUBMIT not confirmed (check page; isFinish stays 0)",
    "FAIR_READY_TO_SUBMIT": "Final OK found, NOT clicked; submit manually in Net-Inspect (isFinished = 1 already set)",
    "CHECKLIST_FILLED": "Checklist answered, not signed (check page; isFinish stays 0)",
    "CHECKLIST_FAILED": "Checklist failed (check page; isFinish stays 0)",
    "DOCUMENTS_FAILED": "Documents upload failed (check page; isFinish stays 0)",
    "DOCUMENTS_NOT_READY": "Documents page still busy, Checklist NOT opened (check page; isFinish stays 0)",
    "FAIR_INDEX_FAILED": "Part Number Index failed (check page; isFinish stays 0)",
    # New Vincent Code End: November 5, 2026
    "ERROR": "ERROR (see message above)",
}


# New Vincent Code Start: November 14, 2026
# --- Run logs (.txt) ---
# Everything printed to the terminal is also written to a text file in LOG_DIR:
#   <lot>_<Y-m-d>_<HHMMSS>.txt      one file per part (a retry of the same part continues in the same file)
#   session_<Y-m-d>_<HHMMSS>.txt    everything printed outside a part (run plan, login, RESULT SUMMARY)
# Snackbar (toast) messages shown by Net-Inspect are caught in the browser and written as
# "[SNACKBAR ERROR] <message>" lines. LOG_ENABLED=false in .env turns all of this off.
LOG_ENABLED = env_bool("LOG_ENABLED", True)
LOG_DIR = Path(env_str("LOG_DIR", "logs"))
LOG_SKIP = (
    "nr-data.net",
)  # New Relic telemetry lines ([net] POST ...): noise, not written
SUCCESS_STATUSES = FINISHED_STATUSES | {"FAIR_READY_TO_SUBMIT"}
_LOG = {
    "session": None,  # session log file handle
    "fh": None,  # current part log file handle
    "path": None,
    "snackbars": 0,
    "blank": False,
    "last_toast": ("", 0.0),
}

SNACKBAR_WATCH_JS = r"""
(() => {
  if (window.__niToastWatch) return;
  window.__niToastWatch = true;
  const SEL = '.toast-message, .mat-mdc-snack-bar-label, .k-notification-content';
  const seen = new WeakSet();
  const kindOf = (el) => {
    for (let n = el, i = 0; n && i < 4; n = n.parentElement, i++) {
      const m = /(?:toast|k-notification)-(error|success|warning|info)/.exec(
        typeof n.className === 'string' ? n.className : '');
      if (m) return m[1];
    }
    return '';
  };
  const report = (el) => {
    if (seen.has(el)) return;
    const text = (el.innerText || el.textContent || '').trim();
    if (!text) return;
    seen.add(el);
    try { window.__niToast(text, kindOf(el), el.outerHTML.slice(0, 300), location.href.slice(0, 120)); } catch (e) {}
  };
  const scan = () => document.querySelectorAll(SEL).forEach(report);
  let t = null;
  const start = () => {
    new MutationObserver(() => { if (!t) t = setTimeout(() => { t = null; scan(); }, 50); })
      .observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    scan();
  };
  if (document.documentElement) start();
  else document.addEventListener('DOMContentLoaded', start);
})();
"""


def _log_target():
    return _LOG["fh"] or _LOG["session"]


def _log_line(line):
    if any(s in line for s in LOG_SKIP):
        return
    fh = _log_target()
    if fh is None:
        return
    if not line.strip():
        if not _LOG["blank"]:  # collapse repeated blank lines
            _LOG["blank"] = True
            fh.write("\n")
        return
    _LOG["blank"] = False
    fh.write(f"{datetime.now():%H:%M:%S}  {line}\n")


class _Tee:
    """Wraps sys.stdout / sys.stderr: the terminal output is unchanged, each full line is also logged."""

    def __init__(self, stream):
        self._s = stream
        self._buf = ""

    def write(self, text):
        n = self._s.write(text)
        try:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                _log_line(line.rstrip("\r"))
        except Exception:
            pass  # a logging problem must never stop the run
        return n

    def flush(self):
        self._s.flush()

    def __getattr__(self, name):
        return getattr(self._s, name)


def _open_log(name):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "unknown"
    path = LOG_DIR / f"{safe}_{datetime.now():%Y-%m-%d_%H%M%S}.txt"
    return open(path, "a", encoding="utf-8", buffering=1), path


def _close_logs():
    for key in ("fh", "session"):
        try:
            if _LOG[key]:
                _LOG[key].close()
        except Exception:
            pass
        _LOG[key] = None


def install_log_tee():
    if not LOG_ENABLED or isinstance(sys.stdout, _Tee):
        return
    try:
        _LOG["session"], path = _open_log("session")
    except OSError as e:
        print(f"(logging is off: cannot create the log file in {LOG_DIR}: {e})")
        return
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)
    atexit.register(_close_logs)
    print(f"Logging terminal output to {LOG_DIR.resolve()} (session log: {path.name})")


def log_begin_part(part, n, total):
    """Start the log file of one part, named <lot>_<Y-m-d>_<HHMMSS>.txt (part number if there is no lot)."""
    if _LOG["session"] is None:
        return
    log_end_part(part, None)
    try:
        _LOG["fh"], _LOG["path"] = _open_log(part.lot or part.number)
    except OSError as e:
        print(f"(part log not created: {e})")
        return
    _LOG["snackbars"] = 0
    _LOG["blank"] = False
    _LOG["fh"].write(
        f"Part {n} of {total} | {part.number} | {part.name} | PO {part.po or '-'} | "
        f"rev {part.rev or '-'} | lot {part.lot or '-'} | started {datetime.now():%Y-%m-%d %H:%M:%S}\n"
    )


def log_end_part(part, status):
    """Write the result footer and close the part log. status=None means the part never reported a result."""
    fh = _LOG["fh"]
    if fh is None:
        return
    if status is None:
        outcome, meaning = "INTERRUPTED", "no result was reported for this part"
    else:
        if status in SUCCESS_STATUSES:
            outcome = "SUCCESS"
        elif (
            status == "ERROR" or status in RETRY_STATUSES or status.endswith("_FAILED")
        ):
            outcome = "ERROR"
        else:
            outcome = "INCOMPLETE"
        meaning = RESULT_LABELS.get(status, status)
    try:
        fh.write("\n" + "=" * 60 + "\n")
        fh.write(f"PART            : {part.number} (lot {part.lot or '-'})\n")
        fh.write(f"RESULT          : {status}\n")
        fh.write(f"OUTCOME         : {outcome}\n")
        fh.write(f"MEANING         : {meaning}\n")
        fh.write(f"SNACKBAR ERRORS : {_LOG['snackbars']}\n")
        fh.write(f"FINISHED        : {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        fh.write("=" * 60 + "\n")
        fh.close()
    except Exception:
        pass
    path = _LOG["path"]
    _LOG["fh"] = None
    print(f"  Run log saved ({outcome}): {path}")


def _on_snackbar(text, kind="", html="", url=""):
    """Called from the browser (see SNACKBAR_WATCH_JS) each time a snackbar/toast appears."""
    try:
        text = " ".join(str(text).split())
        last_text, last_at = _LOG["last_toast"]
        if text == last_text and time.time() - last_at < 1.5:
            return  # same toast reported twice
        _LOG["last_toast"] = (text, time.time())
        kind = (kind or "").lower()
        is_error = kind in ("", "error")
        label = "SNACKBAR ERROR" if is_error else f"SNACKBAR {kind.upper()}"
        if is_error:
            _LOG["snackbars"] += 1
        print(f"  [{label}] {text}")
        fh = _log_target()
        if fh and html:  # the raw element goes only in the file
            fh.write(f"{'':10}{html}\n")
    except Exception:
        pass


# New Vincent Code End: November 14, 2026


def T(ms):
    return ms * 6 if NO_TIMEOUT else ms


def patient(seconds):
    return max(seconds, 300) if NO_TIMEOUT else seconds


@contextmanager
def timed(label):
    start = time.time()
    try:
        yield
    finally:
        if TIMING:
            print(f"  [time] {label}: {time.time() - start:.1f}s")


def pause(message):
    if INTERACTIVE:
        input(message)


def ask(message):
    return input(message).strip() if INTERACTIVE else ""


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
    except PWTimeout:
        pass


def poll(page, check, seconds, interval=250):
    deadline = time.time() + seconds
    while True:
        try:
            if check():
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        page.wait_for_timeout(interval)


def wait_enabled(page, target, seconds):
    return poll(page, lambda: target.is_visible() and target.is_enabled(), seconds)


# New Vincent Code Start: November 13, 2026
def retry_step(page, label, action, attempts=None, wait_ms=1500, recover=None):
    """Run action(); if it raises, wait, run recover() (optional) and try again.

    Only use it for steps that are safe to repeat (open a tab, find a button, tick a box, select a file).
    NEVER wrap CREATE FAIR, SAVE FAIR, UPLOAD or SUBMIT: a repeated click could do the action twice.
    Returns what action() returns; raises the last error when every attempt failed.
    """
    attempts = attempts or STEP_ATTEMPTS
    last = None
    for i in range(1, attempts + 1):
        try:
            return action()
        except Exception as e:
            last = e
            print(
                f"  Step '{label}' failed (attempt {i} of {attempts}): "
                f"{type(e).__name__}: {str(e).strip()[:200]}"
            )
            if i == attempts:
                break
            save_screenshot(page, f"retry_{safe_name(label)}_{i}")
            if recover is not None:
                try:
                    recover()
                except Exception as r:
                    print(f"  (recover for '{label}' failed: {type(r).__name__})")
            page.wait_for_timeout(wait_ms)
            print(f"  Retrying step '{label}'...")
    raise last


# New Vincent Code End: November 13, 2026


def save_screenshot(page, tag):
    if not SAVE_SCREENSHOTS:
        return
    try:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", tag)
        path = SCREENSHOT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.png"
        page.screenshot(path=str(path), full_page=SCREENSHOT_FULL_PAGE)
        print(f"Screenshot saved: {path}")
    except Exception as e:
        print(f"(could not save screenshot: {type(e).__name__})")


def save_html(name, html, limit=60000):
    try:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        path = SCREENSHOT_DIR / name
        path.write_text(html[:limit], encoding="utf-8")
        print(f"  (saved HTML to {path})")
    except Exception as e:
        print(f"  (could not save HTML: {type(e).__name__})")


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text)


def dump_open_popup(ctx, page, tag):
    try:
        html = ctx.evaluate("""() => [...document.querySelectorAll(
                'kendo-popup, .k-popup, .k-animation-container, [role=listbox], ul.k-list, .k-list-container')]
                .map(e => e.outerHTML).join('\\n\\n=====\\n\\n')""")
        save_html(f"popup_{safe_name(tag)}.html", html)
        save_screenshot(page, f"popup_{tag}")
    except Exception as e:
        print(f"  (could not dump popup: {type(e).__name__})")


def print_form_controls(ctx):
    try:
        rows = ctx.evaluate(
            """() => [...document.querySelectorAll('[formcontrolname]')]
                .map(e => e.tagName.toLowerCase() + ' -> ' + e.getAttribute('formcontrolname'))"""
        )
        print("formcontrolname list:")
        for r in rows:
            print("   ", r)
    except Exception as e:
        print(f"(could not list form controls: {type(e).__name__})")


def any_frame_shows(page, text):
    for fr in page.frames:
        try:
            loc = fr.get_by_text(text)
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            pass
    return False


def error_shown(page, words=ERROR_WORDS):
    return any(any_frame_shows(page, w) for w in words)


def find_frame_with_text(page, text, timeout=20):
    deadline = time.time() + timeout
    while True:
        for fr in page.frames:
            try:
                if fr.get_by_text(text).count() > 0:
                    return fr
            except Exception:
                pass
        if time.time() >= deadline:
            return None
        page.wait_for_timeout(250)


def ident(name, what):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        sys.exit(
            f"Invalid or missing {what}: {name!r} (letters, digits and _ only). Check .env."
        )
    return f"`{name}`"


def get_conn():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        connection_timeout=10,
    )


def run_sql(sql, params=None, fetch="all", write=False):
    conn = get_conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params or ())
        if write:
            conn.commit()
            result = cur.rowcount
        elif fetch == "one":
            result = cur.fetchone()
        else:
            result = cur.fetchall()
        cur.close()
        return result
    finally:
        conn.close()


def check_columns():
    table = ident(DB_TABLE, "DB_TABLE")
    have = {
        r["Field"].lower(): r["Field"] for r in run_sql(f"SHOW COLUMNS FROM {table}")
    }

    required = {"COL_PART_NUMBER": COL_PART_NUMBER, "COL_PART_NAME": COL_PART_NAME}
    if COL_STATUS and not LOOKUP_IDS:
        required["COL_STATUS"] = COL_STATUS
    if COL_PENDING:
        required["COL_PENDING"] = COL_PENDING
    if LOOKUP_IDS:
        required["COL_ID"] = COL_ID
    if COL_DRAWING_REV:
        required["COL_DRAWING_REV"] = COL_DRAWING_REV
    if COL_LOT_NUMBER:
        required["COL_LOT_NUMBER"] = COL_LOT_NUMBER
    # New Vincent Code Start: November 5, 2026
    if COL_FINISHED:
        required["COL_FINISHED"] = COL_FINISHED
    # finishRemarks is only needed when the isFinish update is on
    if COL_FINISH_REMARKS and UPDATE_FINISHED:
        required["COL_FINISH_REMARKS"] = COL_FINISH_REMARKS
    if UPDATE_STAMPS:
        if COL_START_STAMP:
            required["COL_START_STAMP"] = COL_START_STAMP
        if COL_FINISH_STAMP:
            required["COL_FINISH_STAMP"] = COL_FINISH_STAMP
    # New Vincent Code End: November 5, 2026
    missing = [
        f"{k}={v!r}" for k, v in required.items() if (v or "").lower() not in have
    ]
    if missing:
        sys.exit(
            f"These columns are not in {DB_TABLE}: {', '.join(missing)}\n"
            f"Columns found: {', '.join(have.values())}\nFix the names in .env."
        )

    if COL_PO_NUMBER and COL_PO_NUMBER.lower() not in have:
        print(
            f"NOTE: column {COL_PO_NUMBER!r} not found in {DB_TABLE}; P.O. Number will be skipped."
        )
        return ""
    return COL_PO_NUMBER


def get_pending_parts(po_col):
    table = ident(DB_TABLE, "DB_TABLE")
    c_num = ident(COL_PART_NUMBER, "COL_PART_NUMBER")
    c_name = ident(COL_PART_NAME, "COL_PART_NAME")
    optional = [
        ("po_number", po_col, "COL_PO_NUMBER"),
        ("drawing_rev", COL_DRAWING_REV, "COL_DRAWING_REV"),
        ("lot_number", COL_LOT_NUMBER, "COL_LOT_NUMBER"),
    ]
    extra = "".join(
        f", MIN({ident(col, what)}) AS {alias}" for alias, col, what in optional if col
    )

    sql = (
        f"SELECT {c_num} AS part_number, MIN({c_name}) AS part_name{extra} FROM {table}"
    )
    where, params = [], []
    if LOOKUP_PARTS:
        where.append(f"{c_num} IN ({', '.join(['%s'] * len(LOOKUP_PARTS))})")
        params.extend(LOOKUP_PARTS)
    if LOOKUP_IDS:
        where.append(
            f"{ident(COL_ID, 'COL_ID')} IN ({', '.join(['%s'] * len(LOOKUP_IDS))})"
        )
        params.extend(LOOKUP_IDS)
    else:
        if COL_STATUS:
            where.append(f"COALESCE({ident(COL_STATUS, 'COL_STATUS')}, 0) = %s")
            params.append(STATUS_PENDING)
        if COL_PENDING:
            where.append(f"COALESCE({ident(COL_PENDING, 'COL_PENDING')}, 0) = %s")
            params.append("0")
        # New Vincent Code Start: November 5, 2026
        # The cycle only picks up rows that are not finished yet.
        if COL_FINISHED:
            where.append(f"COALESCE({ident(COL_FINISHED, 'COL_FINISHED')}, 0) = %s")
            params.append("0")
        # New Vincent Code End: November 5, 2026
    # New Vincent Code Start: October 21, 2026
    # Skipped when LOOKUP_PART_NUMBERS / LOOKUP_IDS name the rows explicitly (they win over the prefix).
    if PART_PREFIX and not LOOKUP_PARTS and not LOOKUP_IDS:
        # Escape LIKE wildcards para literal ang prefix
        like = PART_PREFIX.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(f"{c_num} LIKE %s")
        params.append(like + "%")
    # New Vincent Code End: October 21, 2026
    if where:
        sql += " WHERE " + " AND ".join(where)
    # New Vincent Code Start: November 11, 2026
    # sql += f" GROUP BY {c_num} ORDER BY {c_num} ASC"  # Added "ASC" Vincent October 21, 2026
    sql += (
        f" GROUP BY {c_num} "
        f"ORDER BY CASE "
        f"WHEN UPPER(TRIM({c_num})) LIKE 'XP%' THEN 0 "
        f"WHEN UPPER(TRIM({c_num})) LIKE 'XA%' THEN 1 "
        f"ELSE 2 END, {c_num} DESC"
    )
    # New Vincent Code End: November 11, 2026
    if DB_LIMIT > 0:
        sql += " LIMIT %s"
        params.append(DB_LIMIT)

    return [
        Part(
            str(r["part_number"]).strip(),
            str(r["part_name"] or "").strip(),
            *(
                str(r.get(k) or "").strip()
                for k in ("po_number", "drawing_rev", "lot_number")
            ),
        )
        for r in run_sql(sql, params)
    ]


def mark_part_pending(part_number):
    table = ident(DB_TABLE, "DB_TABLE")
    c_num = ident(COL_PART_NUMBER, "COL_PART_NUMBER")
    c_pend = ident(COL_PENDING, "COL_PENDING")
    return run_sql(
        f"UPDATE {table} SET {c_pend} = %s WHERE TRIM({c_num}) = %s AND COALESCE({c_pend}, 0) <> %s",
        (PENDING_VALUE, part_number, PENDING_VALUE),
        write=True,
    )


# New Vincent Code Start: November 5, 2026
def mark_part_finished(part_number):
    """Set isFinish = 1 and finishRemarks = 'RPA' for the part (all its rows in DB_TABLE).
    Returns the number of rows changed."""
    table = ident(DB_TABLE, "DB_TABLE")
    c_num = ident(COL_PART_NUMBER, "COL_PART_NUMBER")
    c_fin = ident(COL_FINISHED, "COL_FINISHED")
    sets, params = [f"{c_fin} = %s"], [FINISHED_VALUE]
    if COL_FINISH_REMARKS:
        sets.append(f"{ident(COL_FINISH_REMARKS, 'COL_FINISH_REMARKS')} = %s")
        params.append(FINISH_REMARKS_VALUE)
    params += [part_number, FINISHED_VALUE]
    return run_sql(
        f"UPDATE {table} SET {', '.join(sets)} "
        f"WHERE TRIM({c_num}) = %s AND COALESCE({c_fin}, 0) <> %s",
        tuple(params),
        write=True,
    )


def mark_part_started(part_number):
    """startStamp = NOW() for one part (all its rows in DB_TABLE). Returns the rows changed."""
    table = ident(DB_TABLE, "DB_TABLE")
    c_num = ident(COL_PART_NUMBER, "COL_PART_NUMBER")
    c_start = ident(COL_START_STAMP, "COL_START_STAMP")
    return run_sql(
        f"UPDATE {table} SET {c_start} = NOW() WHERE TRIM({c_num}) = %s",
        (part_number,),
        write=True,
    )


def mark_finish_stamp(part_number):
    """At the final OK: finishStamp = NOW(), isFinished = 1, finishRemarks = 'RPA'.
    Returns the rows changed by the stamp update."""
    table = ident(DB_TABLE, "DB_TABLE")
    c_num = ident(COL_PART_NUMBER, "COL_PART_NUMBER")
    c_fin = ident(COL_FINISH_STAMP, "COL_FINISH_STAMP")
    changed = run_sql(
        f"UPDATE {table} SET {c_fin} = NOW() WHERE TRIM({c_num}) = %s",
        (part_number,),
        write=True,
    )
    # Separate UPDATE so a problem with these columns can't block the stamp.
    try:
        mark_part_finished(part_number)  # isFinished = 1 and finishRemarks = RPA
    except Exception as e:
        print(f"  isFinished update failed: {type(e).__name__}: {str(e).strip()[:200]}")
    return changed


def gather_new_rows():
    """Step 1 of the cycle: copy new records into DB_TABLE (isFinish = 0) with GATHER_SQL.
    Returns the number of rows added, or None when GATHER_SQL is not filled in (nothing is run).
    """
    lines = [l for l in GATHER_SQL.splitlines() if not l.strip().startswith("--")]
    sql = "\n".join(lines).strip()
    if not sql:
        print("GATHER: GATHER_SQL is empty (edit it in login.py); nothing gathered.")
        return None
    table = ident(DB_TABLE, "DB_TABLE")
    return run_sql(sql.replace("{table}", table), write=True)


# New Vincent Code End: November 5, 2026


def handle_location(page, quick=False):
    try:
        wait_ms = 3000 if quick else (20000 if NO_TIMEOUT else 8000)
        page.get_by_text("Please Choose Your Country").wait_for(timeout=wait_ms)
    except PWTimeout:
        return
    try:
        page.locator("select").first.select_option(label=COUNTRY, timeout=T(5000))
    except Exception:
        pass
    page.locator(
        "input[type=submit]:visible, button[type=submit]:visible"
    ).first.click()
    page.get_by_text("Please Choose Your Country").wait_for(
        state="detached", timeout=T(20000)
    )
    settle(page)
    print(f"Location set ({COUNTRY}). Current page: {page.url}")


def set_browser_zoom_75(browser, page=None):
    """75% zoom is applied at browser launch (--force-device-scale-factor=0.75).

    The old CDP 'Browser.executeBrowserCommand' zoomIn/zoomOut does not exist in
    Chrome, so it never worked. This function is kept only so existing calls
    still work. When connected over CDP_URL (launch flags do not apply), a CSS
    zoom fallback is used instead.
    """
    if CDP_URL and page is not None:
        try:
            page.evaluate("document.documentElement.style.zoom = '0.75'")
            print("Zoom fallback: CSS zoom 75% applied (CDP_URL mode).")
        except Exception as e:
            print(f"Could not apply CSS zoom: {type(e).__name__}: {str(e)[:200]}")


def login(page, browser):
    page.goto(DASHBOARD_URL)
    set_browser_zoom_75(browser, page)

    # Wait until we know where we landed: login form, or a real logged-in page.
    def landed():
        if "Authentication/Login" in page.url:
            return True
        if page.locator("input[type=password]").count() > 0:
            return True
        return (
            "dashboard" in page.url.lower()
            and page.get_by_text("FAI", exact=True).count() > 0
        )

    poll(page, landed, 15, 300)
    settle(page)

    on_login = (
        "Authentication/Login" in page.url
        or page.locator("input[type=password]").count() > 0
    )
    if not on_login:
        print(f"Already logged in (saved session). Current page: {page.url}")
        return False
    try:
        page.wait_for_selector(
            "input[type=password]", state="attached", timeout=T(15000)
        )
        page.locator("#UserId").fill(USER_ID, timeout=T(10000))
        page.locator("#Password").fill(PASSWORD, timeout=T(10000))
        if COMPANY:
            page.locator("#CompanyName").fill(COMPANY, timeout=T(10000))
        page.locator("#Password").press("Enter")
        page.wait_for_url(
            lambda url: "Authentication/Login" not in url,
            timeout=60000 if NO_TIMEOUT else 20000,
        )
    except PWTimeout:
        print("\n" + "=" * 60)
        print("LOGIN FAILED")
        print("User ID loaded  :", "yes" if USER_ID else "NO")
        print("Password loaded :", "yes" if PASSWORD else "NO")
        print(
            "Company loaded  :",
            "yes" if COMPANY else "NO (form requires one, e.g. ARKTECH)",
        )
        print("Current URL     :", page.url)
        try:
            msgs = page.locator(
                ".validation-summary-errors, .field-validation-error, .alert, .text-danger, .error"
            ).all_inner_texts()
            msgs = [m.strip() for m in msgs if m.strip()]
            print("Message on page :", msgs if msgs else "(none found)")
        except Exception:
            pass
        print("=" * 60)
        save_screenshot(page, "login_failed")
        if not CDP_URL:
            pause("Look at the browser window, then press Enter to close...")
            browser.close()
        sys.exit("Login did not complete.")
    print(f"Logged in. Current page: {page.url}")
    return True


def save_session(context):
    if not SESSION_FILE or CDP_URL:
        return
    try:
        context.storage_state(path=SESSION_FILE)
        print(
            f"Session saved to {SESSION_FILE} (next run can skip the login; keep this file private)."
        )
    except Exception as e:
        print(f"(could not save the session: {type(e).__name__})")


def finish_fair(page, part):
    import form1
    import form2

    with timed("Form 1 extra fields"):
        status = form1.fill_extra_fields(page, part)
    # New Vincent Code Start: October 28, 2026
    # Sub-part (isSub = 1): fill the Part Number Index of Form 1 before SAVE FAIR.
    if status == "FAIR_FILLED":
        import part_index

        with timed("Part Number Index (sub-parts)"):
            status = part_index.run_part_index(page, part, status)
    # New Vincent Code End: October 28, 2026
    form2_ready = False
    if CLICK_SAVE_FAIR:
        if status != "FAIR_FILLED":
            print(
                "SAVE FAIR NOT clicked: some fields failed, so the FAIR is left unsaved."
            )
            return "FAIR_SAVE_SKIPPED"
        with timed("SAVE FAIR + open Form 2"):
            status = form1.save_and_open_form_2(page, part.number)
        form2_ready = status == "FAIR_SAVED_FORM2"
    elif WANT_FORM2:
        print(
            "SAVE FAIR is NOT clicked (CLICK_SAVE_FAIR is off). Opening Form 2 directly."
        )
        form2_ready = form1.open_form_2(page, part.number)
        status = "FAIR_FORM2_OPENED" if form2_ready else "FAIR_FORM2_UNCONFIRMED"
    if form2_ready:
        with timed("Form 2 (fill + save + upload)"):
            status = form2.run_form2(page, part, status)
    return status


def run_part(page, part):
    import form1

    with timed("Create FAIR form (dropdowns, part check, CREATE FAIR)"):
        status = form1.fill_form(page, part)
    if status == "FAIR_CREATED" and FILL_EXTRA_FIELDS:
        status = finish_fair(page, part)
    if (
        status in ("REQUEST_SUBMITTED", "REQUEST_ALREADY_PENDING")
        and UPDATE_STATUS
        and COL_PENDING
    ):
        try:
            changed = mark_part_pending(part.number)
            print(
                f"DATABASE UPDATED: {COL_PENDING} = {PENDING_VALUE} for part {part.number} "
                f"({changed} row(s) changed)."
            )
        except Exception as e:
            print("DATABASE UPDATE FAILED:", type(e).__name__, str(e).strip()[:300])
    # New Vincent Code Start: November 5, 2026
    # Cycle step 3: the FAIR was signed and submitted -> isFinish = 1 (+ finishRemarks = RPA),
    # so the next run skips this part.
    if UPDATE_FINISHED and COL_FINISHED and status in FINISHED_STATUSES:
        try:
            changed = mark_part_finished(part.number)
            remarks = (
                f", {COL_FINISH_REMARKS} = {FINISH_REMARKS_VALUE}"
                if COL_FINISH_REMARKS
                else ""
            )
            print(
                f"DATABASE UPDATED: {COL_FINISHED} = {FINISHED_VALUE}{remarks} for part {part.number} "
                f"({changed} row(s) changed)."
            )
        except Exception as e:
            print("FINISHED UPDATE FAILED:", type(e).__name__, str(e).strip()[:300])
    # New Vincent Code End: November 5, 2026
    return status


def print_run_plan(parts):
    can_fill = CLICK_CREATE_FAIR and FILL_EXTRA_FIELDS
    print(f"\n{len(parts)} part(s) fetched.")
    print(
        "CLICK_CREATE_FAIR is ON: CREATE FAIR will be clicked for parts that exist at the customer."
        if CLICK_CREATE_FAIR
        else "CREATE FAIR is never clicked (CLICK_CREATE_FAIR is off)."
    )
    if FILL_EXTRA_FIELDS:
        print(
            "FILL_EXTRA_FIELDS is ON: the fixed FAIR fields are filled after CREATE FAIR (not saved)."
            if CLICK_CREATE_FAIR
            else "NOTE: FILL_EXTRA_FIELDS is on but CLICK_CREATE_FAIR is off, so the fixed fields will not be filled."
        )
    if CLICK_SAVE_FAIR:
        print(
            "CLICK_SAVE_FAIR is ON: SAVE FAIR is clicked after every field is filled, then Form 2 is opened."
            if can_fill
            else "NOTE: CLICK_SAVE_FAIR is on but needs CLICK_CREATE_FAIR=true and FILL_EXTRA_FIELDS=true; "
            "SAVE FAIR will not be clicked."
        )
    if WANT_FORM2 and not can_fill:
        print(
            "NOTE: FILL_FORM2_MATERIALS / FILL_FORM2_PROCESSES needs CLICK_CREATE_FAIR=true and "
            "FILL_EXTRA_FIELDS=true, so nothing will be filled on Form 2."
        )
    elif WANT_FORM2:
        if not CLICK_SAVE_FAIR:
            print(
                "CLICK_SAVE_FAIR is off: SAVE FAIR is NOT clicked; Form 2 is opened and filled directly."
            )
        if SAVE_FORM2_AT_END:
            print(
                "SAVE_FORM2_AT_END is ON: SAVE FAIR is clicked ONCE, after Form 2 is completely filled."
            )
        if FILL_FORM2_MATERIALS:
            print(
                f"FILL_FORM2_MATERIALS is ON: one Materials row is filled on Form 2 from {QC_TABLE} "
                "(Form 2 is not saved or locked)."
            )
        if FILL_FORM2_PROCESSES:
            print(
                f"FILL_FORM2_PROCESSES is ON: one Processes row per extra backtick item is filled from "
                f"{QC_TABLE} (Form 2 is not saved or locked)."
            )
    if LOOKUP_IDS:
        print(
            f"NOTE: LOOKUP_IDS is set ({', '.join(LOOKUP_IDS)}): the status/pending/finished filters are skipped, "
            "so rows already registered, pending or finished can be picked again."
        )
    if FORM2_LOT_OVERRIDE:
        print(
            f"NOTE: FORM2_LOT_OVERRIDE is set ({FORM2_LOT_OVERRIDE}): Form 2 uses this lot for EVERY part."
        )
    if SHOW_REQUEST_DIALOG and AUTO_REQUEST_YES and SUBMIT_REQUEST_FORM:
        print(
            "WARNING: for a part unknown to the customer, a REAL part request will be sent automatically."
        )
    else:
        print(
            "No part request will be sent automatically "
            f"(AUTO_REQUEST_YES={AUTO_REQUEST_YES}, SUBMIT_REQUEST_FORM={SUBMIT_REQUEST_FORM})."
        )
    print(
        f"Database update after a request: {'ON' if UPDATE_STATUS else 'OFF'} (UPDATE_STATUS)"
    )
    # New Vincent Code Start: November 5, 2026
    remarks_note = (
        f", {COL_FINISH_REMARKS} = {FINISH_REMARKS_VALUE}" if COL_FINISH_REMARKS else ""
    )
    print(
        f"Database update when the FAIR is submitted: {'ON' if UPDATE_FINISHED and COL_FINISHED else 'OFF'} "
        f"(UPDATE_FINISHED; {COL_FINISHED} = {FINISHED_VALUE}{remarks_note} for status "
        f"{', '.join(sorted(FINISHED_STATUSES))})"
    )
    # New Vincent Code End: November 5, 2026
    print(
        "NO_TIMEOUT is ON: waits 6x longer for slow pages (still not endless; Ctrl+C to stop)."
        if NO_TIMEOUT
        else "NO_TIMEOUT is off: normal timeouts."
    )
    for n, p in enumerate(parts, 1):
        print(
            f"  {n}. {p.number} | {p.name} | PO {p.po or '-'} | rev {p.rev or '-'} | lot {p.lot or '-'}"
        )


def open_browser(p):
    if CDP_URL:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
    else:
        launch_args = [
            "--start-maximized",
            "--force-device-scale-factor=0.75",
            "--high-dpi-support=1",
        ]
        # Docker has no window manager, so set the window size explicitly.
        if env_str("WINDOW_SIZE"):
            launch_args += [
                f"--window-size={env_str('WINDOW_SIZE')}",
                "--window-position=0,0",
            ]
        browser = p.chromium.launch(headless=False, args=launch_args)
    state = SESSION_FILE if SESSION_FILE and Path(SESSION_FILE).exists() else None
    context = browser.new_context(no_viewport=True, storage_state=state)
    if state:
        print(f"Using saved session from {state} (delete it if the login seems stuck).")
    page = context.new_page()
    # A browser "Leave site?" dialog would block page.goto() on the reused tab.
    page.on("dialog", lambda d: d.accept())
    context.add_init_script(NO_SMOOTH_SCROLL_JS)
    # New Vincent Code Start: November 14, 2026
    if LOG_ENABLED:
        context.expose_function("__niToast", _on_snackbar)
        context.add_init_script(SNACKBAR_WATCH_JS)
    # New Vincent Code End: November 14, 2026

    # Debug logging: shows new tabs/windows, failed requests and HTTP errors.
    def _log_new_page(pg):
        print(f"  [new tab/window] url={pg.url or '(blank)'}")
        pg.on("dialog", lambda d: d.accept())
        pg.on(
            "framenavigated",
            lambda fr: (
                print(f"  [navigated] {fr.url[:150]}") if fr == pg.main_frame else None
            ),
        )

    context.on("page", _log_new_page)
    context.on(
        "requestfailed",
        lambda r: print(f"  [request FAILED] {r.method} {r.url[:150]} -> {r.failure}"),
    )
    context.on(
        "response",
        lambda r: (
            print(f"  [HTTP {r.status}] {r.url[:150]}") if r.status >= 400 else None
        ),
    )
    if NO_TIMEOUT:
        context.set_default_timeout(120000)
        context.set_default_navigation_timeout(0)
    return browser, context, page


def load_parts():
    try:
        return get_pending_parts(check_columns())
    except Exception as e:
        print("\n" + "=" * 60)
        print("DATABASE ERROR")
        print("Type   :", type(e).__name__)
        print("Message:", str(e).strip()[:500])
        print(
            "DB_HOST:",
            os.getenv("DB_HOST", "localhost"),
            "| DB_NAME:",
            os.getenv("DB_NAME"),
            "| DB_TABLE:",
            DB_TABLE,
        )
        print("=" * 60)
        sys.exit("Could not read from the database.")


def open_create_fair_page(page):
    """Go straight to the Create FAIR page; fall back to the FAI menu if the form does not show."""
    import form1

    try:
        page.goto(CREATE_FAIR_URL)
        settle(page)
        if form1.find_create_form(page, 15) is not None:
            return
        print(
            "  (the direct Create FAIR URL did not show the form; using the FAI menu instead)"
        )
    except Exception as e:
        print(
            f"  (direct Create FAIR URL failed: {type(e).__name__}; using the FAI menu instead)"
        )
    form1.go_to_fai(page)
    form1.open_create_fair_tab(page)


REUSE_TAB = env_bool("REUSE_TAB", True)


def leave_current_fair(page):
    """Get the current tab ready to be reused for the next part."""
    # 1. Close any popup left open (e.g. 'Submit FAIR to Customer' when OK was not clicked).
    for fr in page.frames:
        try:
            dlg = fr.locator("[role=dialog], .k-window, .k-dialog, .modal").filter(
                has=fr.get_by_role("button", name=re.compile(r"^\s*cancel\s*$", re.I))
            )
            if dlg.count() and dlg.first.is_visible():
                dlg.first.get_by_role(
                    "button", name=re.compile(r"^\s*cancel\s*$", re.I)
                ).first.click(timeout=3000)
                print("  Closed a leftover popup with Cancel.")
                page.wait_for_timeout(500)
                break
        except Exception:
            pass

    # 2. Net-Inspect's "unsaved changes" box: keep the work (click SAVE).
    try:
        from checklist import handle_unsaved_modal

        handle_unsaved_modal(page, "before starting the next part")
    except Exception:
        pass

    # 3. Forget requests from the previous part so wait_ready_to_leave() starts clean.
    try:
        inflight = getattr(page, "_ni_inflight", None)
        if inflight is not None:
            inflight.clear()
    except Exception:
        pass


def main():
    import form1

    install_log_tee()  # New Vincent Code: November 14, 2026

    missing = [
        k for k, v in {"NI_USER_ID": USER_ID, "NI_PASSWORD": PASSWORD}.items() if not v
    ]
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}")

    # New Vincent Code Start: November 5, 2026
    # Cycle step 1: gather the new records into DB_TABLE first, then step 2 picks up the isFinish = 0 rows.
    if GATHER_FIRST:
        try:
            added = gather_new_rows()
            if added is not None:
                print(f"GATHER: {added} new row(s) added to {DB_TABLE}.")
        except Exception as e:
            print("GATHER FAILED:", type(e).__name__, str(e).strip()[:300])
            sys.exit(
                "Stopped: the gather step failed, so the data in the table is not up to date."
            )
    # New Vincent Code End: November 5, 2026

    parts = load_parts()
    if not parts:
        sys.exit(
            "No pending rows found (check DB_TABLE, COL_STATUS, STATUS_PENDING, COL_PENDING and COL_FINISHED in .env)."
        )
    print_run_plan(parts)

    with sync_playwright() as p:
        browser, context, page = open_browser(p)
        with timed("login + location"):
            fresh_login = login(page, browser)
            handle_location(page, quick=not fresh_login)
            save_session(context)

        results = []
        for n, part in enumerate(parts, 1):
            log_begin_part(part, n, len(parts))  # New Vincent Code: November 14, 2026
            print(f"\n--- Part {n} of {len(parts)}: {part.number} ---")
            form1.CREATE_STATE["clicked"] = False  # New Vincent Code: November 13, 2026
            if UPDATE_STAMPS and COL_START_STAMP:
                try:
                    changed = mark_part_started(part.number)
                    print(
                        f"DATABASE UPDATED: {COL_START_STAMP} = NOW() for part {part.number} "
                        f"({changed} row(s) changed)."
                    )
                except Exception as e:
                    print("START STAMP FAILED:", type(e).__name__, str(e).strip()[:300])
            try:
                if n == 1:
                    form1.go_to_fai(page)
                    form1.open_create_fair_tab(page)
                else:
                    prev_status = results[-1][1] if results else "ERROR"
                    if prev_status in REUSE_STATUSES:
                        leave_current_fair(page)  # clean finish: reuse this tab
                    else:
                        print(
                            f"  Previous part ended as {prev_status}: keeping its tab, opening a new one."
                        )
                        page = context.new_page()
                    page.goto(CREATE_FAIR_URL)
                    set_browser_zoom_75(browser, page)
                    settle(page)
                    page.wait_for_timeout(800)
                    set_browser_zoom_75(browser, page)
                    settle(page)
                    page.wait_for_timeout(800)
                results.append((part.number, run_part(page, part)))
            except Exception as e:
                results.append((part.number, "ERROR"))
                print("\n" + "=" * 60)
                print("ERROR TYPE   :", type(e).__name__)
                print("ERROR MESSAGE:", str(e).strip()[:900])
                print("=" * 60)
                save_screenshot(page, f"error_{part.number}")
            # New Vincent Code Start: November 13, 2026
            # Part-level retry: run the SAME part (same part number and lot) again from the very start in a
            # new tab. The failed tab stays open so you can look at it.
            attempt = 1
            while results[-1][1] in RETRY_STATUSES and attempt < PART_MAX_ATTEMPTS:
                if form1.CREATE_STATE["clicked"] and not RETRY_AFTER_CREATE:
                    print(
                        f"  Retry skipped for {part.number}: CREATE FAIR was already clicked, so a restart "
                        "could create a second FAIR. Check Net-Inspect (or set RETRY_AFTER_CREATE=true)."
                    )
                    break
                attempt += 1
                print(
                    f"\n  RETRY: {part.number} (lot {part.lot or '-'}) from the start, "
                    f"attempt {attempt} of {PART_MAX_ATTEMPTS} (last result: {results[-1][1]})."
                )
                results.pop()
                try:
                    page = context.new_page()
                    page.goto(CREATE_FAIR_URL)
                    set_browser_zoom_75(browser, page)
                    settle(page)
                    page.wait_for_timeout(800)
                    set_browser_zoom_75(browser, page)
                    settle(page)
                    page.wait_for_timeout(800)
                    results.append((part.number, run_part(page, part)))
                except Exception as e:
                    results.append((part.number, "ERROR"))
                    print("\n" + "=" * 60)
                    print("ERROR TYPE   :", type(e).__name__)
                    print("ERROR MESSAGE:", str(e).strip()[:900])
                    print("=" * 60)
                    save_screenshot(page, f"error_{part.number}_attempt{attempt}")
            # New Vincent Code End: November 13, 2026
            log_end_part(part, results[-1][1])  # New Vincent Code: November 14, 2026
            if n < len(parts):
                pause("Press Enter for the next part...")

        print("\n" + "=" * 60)
        print("RESULT SUMMARY")
        for pn, status in results:
            print(f"  {pn}: {RESULT_LABELS.get(status, status)}")
        print("=" * 60)
        if KEEP_OPEN:
            print(
                "KEEP_OPEN is ON: the FAIR page(s) stay open. "
                + (
                    "SAVE FAIR was clicked where every field was filled."
                    if CLICK_SAVE_FAIR
                    else "Nothing was saved or submitted."
                )
            )
            print(
                "Closing this script also closes the browser it launched "
                "(set CDP_URL to use your own Chrome if you want it to outlive the script)."
            )
            try:
                if INTERACTIVE:
                    input(
                        "Review the page(s), then press Enter to close the browser..."
                    )
                else:
                    while True:
                        time.sleep(3600)
            except KeyboardInterrupt:
                pass
        else:
            pause("\nDone. Press Enter to close...")
        if not CDP_URL:
            browser.close()


if __name__ == "__main__":
    main()
