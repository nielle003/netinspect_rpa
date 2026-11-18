import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from checklist import run_checklist
from form1 import wait_ready_to_leave
from login import (
    COL_ID,
    COL_LOT_NUMBER,
    COL_PART_NUMBER,
    DB_TABLE,
    T,
    env_bool,
    env_str,
    find_frame_with_text,
    ident,
    patient,
    poll,
    run_sql,
    save_screenshot,
    settle,
    wait_enabled,
)

FILL_DOCUMENTS = env_bool("FILL_DOCUMENTS", False)
DOCUMENT_ROOT = env_str(
    "DOCUMENT_ROOT",
    r"\\192.168.254.163\www\html\Document Management System\Net Inspect Drawing Folder\For Upload",
)


def _int_env(name, default):
    try:
        return max(1, int(env_str(name, str(default))))
    except (TypeError, ValueError):
        return default


DOCUMENT_UPLOAD_WAIT = _int_env("DOCUMENT_UPLOAD_WAIT", 120)

SELECT_FILES_RE = re.compile(r"select\s+files", re.I)
UPLOAD_BUTTON_RE = re.compile(r"^\s*UPLOAD\s*$")
SKIP_FILES = ("thumbs.db", "desktop.ini")
WRITE_METHODS = ("POST", "PUT", "PATCH")

_TRACK = {"started": 0, "finished": 0, "responses": []}


def _is_upload_request(req):
    try:
        if req.method not in WRITE_METHODS or "net-inspect.com" not in req.url:
            return False
        ctype = (req.headers.get("content-type") or "").lower()
        return "multipart" in ctype or "/documents" in req.url
    except Exception:
        return False


def _on_request(req):
    if _is_upload_request(req):
        _TRACK["started"] += 1


def _on_finished(req):
    if _is_upload_request(req):
        _TRACK["finished"] += 1


def _on_response(resp):
    try:
        if _is_upload_request(resp.request):
            _TRACK["responses"].append((resp.status, resp.url))
    except Exception:
        pass


def _uploads_idle():
    return _TRACK["finished"] >= _TRACK["started"]


def lot_for(part):
    lot = str(part.lot or "").strip()
    if lot:
        return lot
    if not COL_LOT_NUMBER:
        return ""
    table = ident(DB_TABLE, "DB_TABLE")
    c_part = ident(COL_PART_NUMBER, "COL_PART_NUMBER")
    c_id = ident(COL_ID, "COL_ID")
    c_lot = ident(COL_LOT_NUMBER, "COL_LOT_NUMBER")
    row = run_sql(
        f"SELECT {c_lot} AS lot FROM {table} WHERE TRIM({c_part}) = %s ORDER BY {c_id} ASC LIMIT 1",
        (part.number,),
        fetch="one",
    )
    return str(row.get("lot") or "").strip() if row else ""


def find_lot_folder(lot):
    root = Path(DOCUMENT_ROOT)
    try:
        direct = root / lot
        if direct.is_dir():
            return direct
        for name in os.listdir(root):
            if name.strip().lower() == lot.strip().lower() and (root / name).is_dir():
                return root / name
    except OSError as e:
        print(f"  Documents: cannot read {DOCUMENT_ROOT}: {e}")
    return None


def list_files(folder):
    out = []
    for name in sorted(os.listdir(folder), key=str.lower):
        path = folder / name
        if not path.is_file():
            continue
        if name.startswith("~$") or name.lower() in SKIP_FILES:
            continue
        out.append(path)
    return out


def stage_files(files, stage_dir):
    staged = []
    for pdf in files:
        local = Path(stage_dir) / pdf.name
        shutil.copyfile(pdf, local)
        src, dst = os.path.getsize(pdf), os.path.getsize(local)
        if dst == 0 or src != dst:
            raise RuntimeError(
                f"{pdf.name}: copy is {dst} bytes but the original is {src} bytes"
            )
        print(f"  Copied {pdf.name} locally: {dst} bytes.")
        staged.append(local)
    return staged


def doc_count(page):
    for fr in page.frames:
        try:
            loc = fr.locator("span.doc-count")
            if loc.count():
                m = re.search(r"\d+", loc.first.inner_text())
                if m:
                    return int(m.group(0))
        except Exception:
            pass
    return None


def file_shown(page, name):
    for fr in page.frames:
        try:
            if fr.get_by_text(name, exact=False).count() > 0:
                return True
        except Exception:
            pass
    return False


def progress_visible(page):
    for fr in page.frames:
        try:
            if fr.locator("div.progress-bar:visible").count() > 0:
                return True
        except Exception:
            pass
    return False


def open_documents_tab(page, part_number):
    if not wait_ready_to_leave(page, "Form 3 -> Documents"):
        print("  The Documents tab was NOT clicked (Form 3 is not finished).")
        return None
    ctx = find_frame_with_text(page, "Documents", timeout=patient(15))
    if ctx is None:
        print("The Documents tab was not found.")
        save_screenshot(page, f"documents_tab_missing_{part_number}")
        return None
    link = ctx.locator('a[routerlink="documents"], a[href$="/documents"]').filter(
        has_text=re.compile(r"^\s*Documents")
    )
    if link.count():
        link.first.click(timeout=T(5000))
    else:
        ctx.get_by_role("link", name=re.compile(r"^\s*Documents")).first.click(
            timeout=T(5000)
        )
    print("Clicked the Documents tab.")
    poll(page, lambda: page.url.rstrip("/").endswith("/documents"), 15)
    print(f"  Page URL now: {page.url}")
    settle(page)
    ctx = find_frame_with_text(page, "Select files", timeout=patient(15))
    if ctx is None:
        print("Clicked Documents, but the 'Select files...' button did not appear.")
        save_screenshot(page, f"documents_unconfirmed_{part_number}")
        return None
    print("Documents page is open.")
    return ctx


def select_files_button(ctx):
    btn = ctx.locator("button.k-upload-button").filter(has_text=SELECT_FILES_RE)
    if btn.count() == 0:
        btn = ctx.get_by_role("button", name=SELECT_FILES_RE)
    return btn.first if btn.count() else None


def choose_files(page, ctx, paths):
    btn = select_files_button(ctx)
    if btn is None:
        raise RuntimeError("the 'Select files...' button was not found")
    btn.scroll_into_view_if_needed(timeout=T(5000))
    with page.expect_file_chooser(timeout=T(10000)) as fc:
        btn.click(timeout=T(5000))
    fc.value.set_files([str(p) for p in paths])
    page.wait_for_timeout(300)


def select_all_files(page, ctx, staged):
    choose_files(page, ctx, staged)
    print(f"  Selected {len(staged)} file(s) in one go.")
    names = [p.name for p in staged]
    poll(page, lambda: all(file_shown(page, n) for n in names), 15, 300)
    missing = [p for p in staged if not file_shown(page, p.name)]
    for p in missing:
        print(f"  {p.name} is not in the list yet; selecting it again on its own.")
        choose_files(page, ctx, [p])
        poll(page, lambda: file_shown(page, p.name), 10, 300)
    still = [p.name for p in staged if not file_shown(page, p.name)]
    if still:
        raise RuntimeError(f"these files did not show up in the list: {still}")
    print(f"  All {len(staged)} file(s) are in the upload list: {names}")


def tick_agree(page, ctx):
    boxes = ctx.locator("input#acknowledgementCbx")
    if not poll(page, lambda: boxes.count() > 0, patient(15), 250):
        raise RuntimeError("the 'I Agree' checkbox did not appear")
    ticked = 0
    for i in range(boxes.count()):
        box = boxes.nth(i)
        try:
            if box.is_checked():
                ticked += 1
                continue
        except Exception:
            pass
        for how in ("check", "label", "js"):
            try:
                if how == "check":
                    box.check(force=True, timeout=3000)
                elif how == "label":
                    ctx.locator('label[for="acknowledgementCbx"]').nth(i).click(
                        timeout=3000
                    )
                else:
                    box.evaluate("e => e.click()")
                if box.is_checked():
                    ticked += 1
                    break
            except Exception:
                pass
    if ticked == 0:
        raise RuntimeError("the 'I Agree' checkbox could not be ticked")
    print(f"  'I Agree' ticked ({ticked}).")


def click_upload(page, ctx):
    btn = ctx.locator("button.btn-success").filter(has_text=UPLOAD_BUTTON_RE)
    if btn.count() == 0:
        btn = ctx.get_by_role("button", name=UPLOAD_BUTTON_RE)
    if btn.count() == 0:
        raise RuntimeError("the UPLOAD button was not found")
    target = btn.last
    if not wait_enabled(page, target, patient(15)):
        raise RuntimeError("the UPLOAD button stayed disabled")
    started_before = _TRACK["started"]
    target.click(timeout=T(5000))
    print("  Clicked UPLOAD.")
    return started_before


def wait_upload_finished(page, started_before, expected_total):
    if not poll(page, lambda: _TRACK["started"] > started_before, 20, 200):
        print("  No upload request was seen after clicking UPLOAD.")
    print(
        f"  Upload in progress: {_TRACK['started'] - started_before} request(s) started."
    )

    def finished():
        if not _uploads_idle():
            return False
        if not progress_visible(page):
            return True
        count = doc_count(page)
        return count is not None and count >= expected_total

    if not poll(page, finished, DOCUMENT_UPLOAD_WAIT, 300):
        raise RuntimeError(
            f"the upload is still running after {DOCUMENT_UPLOAD_WAIT} seconds"
        )
    page.wait_for_timeout(1500)
    if not poll(page, _uploads_idle, DOCUMENT_UPLOAD_WAIT, 300):
        raise RuntimeError("an upload request is still running")
    settle(page)


def verify_uploaded(page, staged, expected_total):
    bad = [(s, u) for s, u in _TRACK["responses"] if not 200 <= s < 300]
    for status, url in _TRACK["responses"]:
        print(f"  Upload request: HTTP {status}  {url.split('/api/v1/')[-1][:120]}")
    if bad:
        raise RuntimeError(f"{len(bad)} upload request(s) were rejected: {bad[:3]}")
    names = [p.name for p in staged]
    count = None

    def confirmed():
        nonlocal count
        count = doc_count(page)
        if count is not None and count >= expected_total:
            return True
        return all(file_shown(page, n) for n in names) and not progress_visible(page)

    if not poll(page, confirmed, 20, 500):
        raise RuntimeError(
            f"the upload is not confirmed: document counter shows {count}, expected {expected_total}"
        )
    print(f"  Upload confirmed: document counter shows {doc_count(page)}.")


def upload_documents(page, part):
    lot = lot_for(part)
    if not lot:
        print(f"  Documents: no lot number for part {part.number}.")
        return False
    folder = find_lot_folder(lot)
    if folder is None:
        print(f"  Documents: no folder named {lot!r} in {DOCUMENT_ROOT}.")
        return False
    files = list_files(folder)
    if not files:
        print(f"  Documents: the folder {folder} has no files.")
        return False
    print(f"  Documents: lot {lot}, folder {folder}, {len(files)} file(s):")
    for f in files:
        print(f"    - {f.name}")

    ctx = open_documents_tab(page, part.number)
    if ctx is None:
        return False

    before = doc_count(page) or 0
    expected_total = before + len(files)
    stage_dir = tempfile.mkdtemp(prefix="fair_docs_")
    _TRACK["started"] = 0
    _TRACK["finished"] = 0
    _TRACK["responses"] = []
    page.on("request", _on_request)
    page.on("requestfinished", _on_finished)
    page.on("requestfailed", _on_finished)
    page.on("response", _on_response)
    try:
        staged = stage_files(files, stage_dir)
        select_all_files(page, ctx, staged)
        save_screenshot(page, f"documents_selected_{part.number}")
        tick_agree(page, ctx)
        started_before = click_upload(page, ctx)
        wait_upload_finished(page, started_before, expected_total)
        verify_uploaded(page, staged, expected_total)
        save_screenshot(page, f"documents_uploaded_{part.number}")
        return True
    except Exception as e:
        print(f"  Documents failed: {type(e).__name__}: {str(e).strip()[:400]}")
        save_screenshot(page, f"documents_error_{part.number}")
        return False
    finally:
        for event, handler in (
            ("request", _on_request),
            ("requestfinished", _on_finished),
            ("requestfailed", _on_finished),
            ("response", _on_response),
        ):
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass
        shutil.rmtree(stage_dir, ignore_errors=True)


def run_documents(page, part, status):
    if not FILL_DOCUMENTS:
        print("  Documents NOT run: FILL_DOCUMENTS is off. Add FILL_DOCUMENTS=true to .env.")
        return run_checklist(page, part, status)
    if not upload_documents(page, part):
        print("  Checklist NOT opened: the document upload did not finish.")
        return "DOCUMENTS_FAILED"
    if not wait_ready_to_leave(page, "Documents -> Checklist"):
        print("  Checklist NOT opened: the Documents page is still busy.")
        return "DOCUMENTS_NOT_READY"
    print("  Documents uploaded. Going on to the Checklist.")
    return run_checklist(page, part, status)