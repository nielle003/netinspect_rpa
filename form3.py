import re
import time
from decimal import Decimal, InvalidOperation

from playwright.sync_api import expect  # New Vincent Code: November 14, 2026

from document import run_documents
from form1 import (
    click_nav,
    find_clickable,
    invalid_controls,  # New Vincent Code: November 14, 2026
    save_fair,
    select_kendo_option,
)
from login import (
    COL_ID,
    COL_PART_NUMBER,
    DB_TABLE,
    T,
    env_bool,
    env_str,
    find_frame_with_text,
    ident,
    patient,
    poll,
    print_form_controls,
    run_sql,
    save_html,
    save_screenshot,
    settle,
    wait_enabled,
)

# --- Form 3 settings (all optional, read from .env) ---
FILL_FORM3 = env_bool("FILL_FORM3", False)
INSPECT_TABLE = env_str(
    "INSPECT_TABLE", "qc_inspectionresult"
)  # table that holds the characteristics
COL_NET_RESULT_ID = env_str(
    "COL_NET_RESULT_ID", "resultId"
)  # resultId column in DB_TABLE
COL_INSPECT_RESULT_ID = env_str("COL_INSPECT_RESULT_ID", "resultId")
COL_INSPECT_ORDER = env_str(
    "COL_INSPECT_ORDER", "listId"
)  # no longer used for the order (rows are ordered by charNo now); kept so old .env files still load
FORM3_PART_NUMBER = env_str("FORM3_PART_NUMBER")  # empty = the part being processed

# New Allen Code Start: October 27, 2026
# One row per characteristic number: the QC table can hold the same charNo more than once (different listId).
COL_INSPECT_CHARNO = env_str(
    "COL_INSPECT_CHARNO", "charNo"
)  # the characteristic number; only one row per value is used
COL_INSPECT_LIST = env_str(
    "COL_INSPECT_LIST", "listId"
)  # unique id of each row, used to pick which copy to keep
# Which copy to keep when the same charNo appears more than once: "first" = lowest listId, "last" = highest.
DUP_KEEP = (
    "MAX"
    if str(env_str("FORM3_DUP_KEEP", "first")).strip().lower() == "last"
    else "MIN"
)
# New Allen Code End: October 27, 2026

ADD_CHARACTERISTIC_RE = r"add\s+characteristic"
TYPE_OPTION_RE = re.compile(r"Symmetrical|Bilateral|Range\s+Inclusive", re.I)

NOMINAL = 'input[name="nominal"]'
HIGH_TOL = 'input[name="High Tolerance"]'
LOW_TOL = 'input[name="Low Tolerance"]'
LOW_TOL_XPATH = "xpath=following::input[@name='Low Tolerance'][1]"
# New Vincent Code Start: October 23, 2026
COL_INSPECT_VALUE = env_str(
    "COL_INSPECT_VALUE", "inspectionResult"
)  # value typed in the Results box
GRID_PAGE_SIZE = 10  # the Net-Inspect grid shows 10 rows per page


def _int_env(name, default):
    try:
        return max(1, int(env_str(name, str(default))))
    except (TypeError, ValueError):
        return default


# How many seconds to keep trying to open a cell (Reference Location, Description, Tooling, Comments)
# before giving up with an error. Each try clicks the cell and waits 2 seconds for its box.
FORM3_CELL_WAIT = _int_env("FORM3_CELL_WAIT", 5)
# Most seconds to wait for the Save FAIR spinner to go away after the second Save FAIR (maxTyp rows).
FORM3_SAVE_WAIT = _int_env("FORM3_SAVE_WAIT", 10)


# New Allen Code Start: October 26, 2026
def _int_env0(name, default):
    # Like _int_env but 0 is allowed.
    try:
        return max(0, int(env_str(name, str(default))))
    except (TypeError, ValueError):
        return default


# Speed settings (.env):
#   FORM3_TYPE_DELAY    = milliseconds per typed character. 0 = paste the whole value at once (fastest).
#   FORM3_PAUSE_PERCENT = size of the small fixed pauses between steps. 100 = the old speed, 30 = default,
#                         0 = no pauses. If clicks start missing, raise it (e.g. 100).
TYPE_DELAY = _int_env0("FORM3_TYPE_DELAY", 0)
PAUSE_SCALE = _int_env0("FORM3_PAUSE_PERCENT", 30) / 100.0


def _bbox(loc, ms=2000):
    # bounding_box() that gives up after `ms` instead of waiting (a row the grid just re-drew can vanish).
    try:
        return loc.bounding_box(timeout=ms)
    except Exception:
        return None


def nap(page, ms):
    # Short fixed pause, scaled by FORM3_PAUSE_PERCENT.
    ms = int(ms * PAUSE_SCALE)
    if ms > 0:
        page.wait_for_timeout(ms)


# New Allen Code End: October 26, 2026

# The Results box (Variable type) has no name attribute; this is the first one after the row's Results dropdown.
RESULT_INPUT_XPATH = "xpath=following::input[contains(@class,'wh-100') and @maxlength='15' and not(@name)][1]"
# The green check (Attribute type) that appears after the row's Results dropdown.
CHECK_XPATH = (
    "xpath=following::span[contains(concat(' ', normalize-space(@class), ' '), ' text-success ')]"
    "[contains(@class,'material-icons')][1]"
)
RESULT_TYPE_RE = re.compile(r"Variable|Attribute", re.I)
# The Description box of a characteristic (one per row).
DESCRIPTION_EDITOR = 'div.max-character-container div#requirement-editor-container[contenteditable="true"]'
DESCRIPTION_MAX = 2500
# 6. Reference Location box (textarea, one per row) and the column that feeds it.
COL_INSPECT_LOCATION = env_str("COL_INSPECT_LOCATION", "location")
REFERENCE_LOCATION = 'textarea[placeholder="Enter 6. Reference Location"]'
# New Vincent Code End: October 23, 2026

# New Vincent Code Start: October 24, 2026
# "maxTyp" rows: columns that drive the extra steps.
COL_INSPECT_MAXTYP = env_str("COL_INSPECT_MAXTYP", "maxTyp")
COL_INSPECT_TOOL_ID = env_str("COL_INSPECT_TOOL_ID", "toolId")
COL_INSPECT_JAMCO = env_str("COL_INSPECT_JAMCO", "jamcoNA")
TOOL_ID_RADIUS_GAUGE = "3"  # toolId that means Radius Gauge
RADIUS_GAUGE_NAME = "Radius Gauge"
ATTRIBUTE_COMMENT = env_str(
    "ATTRIBUTE_COMMENT", "Visual Inspection"
)  # 12. Additional Data / Comments for Attribute rows (minTol and maxTol blank)
RANGE_INCLUSIVE_NAME = (
    "Range Inclusive"  # Tolerance type set as the last step of a maxTyp row
)
TOOLING_INPUT = 'input[placeholder="Tooling"]'  # 10. Designed / Qualified Tooling
COMMENTS_BOX = 'textarea[placeholder="Enter 12. Additional Data / Comments"]'
# New Vincent Code End: October 24, 2026


# New Vincent Code Start: October 23, 2026
def _result_id_subquery():
    # partNumber -> resultId (subquery, no JOIN).
    net = ident(DB_TABLE, "DB_TABLE")
    c_part = ident(COL_PART_NUMBER, "COL_PART_NUMBER")
    c_net_res = ident(COL_NET_RESULT_ID, "COL_NET_RESULT_ID")
    c_net_id = ident(COL_ID, "COL_ID")
    return (
        f"(SELECT {c_net_res} FROM {net} WHERE TRIM({c_part}) = %s "
        f"ORDER BY {c_net_id} ASC LIMIT 1)"
    )


# New Allen Code Start: October 27, 2026
def count_characteristics(part_number):
    # Number of DIFFERENT characteristics (distinct charNo) for the part's resultId.
    insp = ident(INSPECT_TABLE, "INSPECT_TABLE")
    c_insp_res = ident(COL_INSPECT_RESULT_ID, "COL_INSPECT_RESULT_ID")
    c_char = ident(COL_INSPECT_CHARNO, "COL_INSPECT_CHARNO")
    sql = (
        f"SELECT COUNT(DISTINCT TRIM({c_char})) AS total FROM {insp} "
        f"WHERE {c_insp_res} = {_result_id_subquery()}"
    )
    row = run_sql(sql, (part_number,), fetch="one")
    return int(row["total"]) if row and row.get("total") is not None else 0


def fetch_characteristics(part_number, how_many):
    # One row per charNo (the copy with the lowest or highest listId, see DUP_KEEP), ordered by charNo,
    # so the grid row number matches the characteristic number.
    insp = ident(INSPECT_TABLE, "INSPECT_TABLE")
    c_insp_res = ident(COL_INSPECT_RESULT_ID, "COL_INSPECT_RESULT_ID")
    c_char = ident(COL_INSPECT_CHARNO, "COL_INSPECT_CHARNO")
    c_list = ident(COL_INSPECT_LIST, "COL_INSPECT_LIST")
    sql = (
        f"SELECT r.* FROM {insp} r "
        f"JOIN (SELECT {DUP_KEEP}({c_list}) AS keep_id FROM {insp} "
        f"      WHERE {c_insp_res} = {_result_id_subquery()} "
        f"      GROUP BY TRIM({c_char})) k ON k.keep_id = r.{c_list} "
        f"ORDER BY CAST(r.{c_char} AS UNSIGNED) ASC, r.{c_list} ASC "
        f"LIMIT {int(how_many)}"
    )
    return run_sql(sql, (part_number,))


# New Allen Code End: October 27, 2026
# New Vincent Code End: October 23, 2026


def clean(value):
    return "" if value is None else " ".join(str(value).split())


def tolerance_type(min_tol, max_tol):
    # minTol == maxTol -> Symmetrical, otherwise Bilateral (numeric compare when both are numbers).
    try:
        same = Decimal(min_tol) == Decimal(max_tol)
    except InvalidOperation:
        same = min_tol == max_tol
    return "Symmetrical" if same else "Bilateral"


def frame_with_selector(page, selector, timeout):
    def found():
        for fr in page.frames:
            try:
                loc = fr.locator(selector)
                if loc.count() > 0 and loc.first.is_visible():
                    return fr
            except Exception:
                pass
        return None

    result = []
    poll(page, lambda: result.append(found()) or result[-1] is not None, timeout)
    return result[-1] if result and result[-1] is not None else None


SAVE_LOADING = 'button[title="Save Fair"] i.loading'


def save_loading_visible(page):
    # Really showing = has a size, is not display:none / visibility:hidden and is not fully transparent.
    shown_js = (
        "e => { const s = getComputedStyle(e); const r = e.getBoundingClientRect();"
        " return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'"
        " && parseFloat(s.opacity) > 0; }"
    )
    for fr in page.frames:
        try:
            loc = fr.locator(SAVE_LOADING)
            for i in range(loc.count()):
                if loc.nth(i).evaluate(shown_js):
                    return True
        except Exception:
            pass
    return False


def save_fair_and_wait(page, part_number):
    # 1. click SAVE FAIR  2. wait until the save is finished
    status = save_fair(page, part_number)
    if status == "FAIR_SAVE_FAILED":
        print(
            "  SAVE FAIR failed, so Form 3 is NOT opened (nothing is lost; check the page)."
        )
        return False
    if not poll(page, lambda: not save_loading_visible(page), FORM3_SAVE_WAIT):
        print(
            f"  The Save FAIR spinner is still showing after {FORM3_SAVE_WAIT} seconds; going on anyway."
        )
    settle(page)
    nap(page, 500)
    return True


def open_form_3(page, part_number):
    ctx = find_frame_with_text(page, "Form 3", timeout=patient(15))
    if ctx is None:
        print("The Form 3 tab was not found.")
        save_screenshot(page, f"form3_tab_missing_{part_number}")
        return None
    link = ctx.locator('a[routerlink="3"], a[href$="/3"]').filter(
        has_text=re.compile(r"^\s*Form 3\s*$")
    )
    if link.count():
        link.first.click(timeout=T(5000))
    else:
        click_nav(ctx, "Form 3", roles=("tab", "link"), timeout=5000)
    print("Clicked the Form 3 tab.")
    poll(page, lambda: page.url.rstrip("/").endswith("/3"), 15)
    print(f"  Page URL now: {page.url}")
    settle(page)
    ctx = find_frame_with_text(page, "Add Characteristic", timeout=patient(15))
    if ctx is None:
        print(
            "Clicked Form 3, but the 'Add Characteristic' button did not appear. Check the page."
        )
        save_screenshot(page, f"form3_unconfirmed_{part_number}")
        return None
    print("Form 3 is open.")
    save_screenshot(page, f"form3_{part_number}")
    return ctx


# New Vincent Code Start: October 23, 2026
_NEW_ROW_JS = r"""() => {
  const dds = [...document.querySelectorAll('kendo-dropdownlist')]
    .filter(e => e.getClientRects().length && /Variable|Attribute/i.test(e.innerText));
  if (!dds.length) return {state: 'NONE'};
  const last = dds[dds.length - 1];
  const top = last.getBoundingClientRect().top;
  let nom = null;
  for (const i of document.querySelectorAll('input[name="nominal"]')) {
    const r = i.getBoundingClientRect();
    if (i.getClientRects().length && r.width > 0 && r.height > 0 && r.top >= top - 10 && r.top <= top + 130) {
      nom = i.value; break;
    }
  }
  return {state: 'OK', text: (last.innerText || '').trim(), nom: nom, n: dds.length};
}"""


# New Fix Code Start: November 13, 2026
# How many times + Add Characteristic is clicked (only while NO new row shows up) before giving up.
ADD_ROW_ATTEMPTS = _int_env("FORM3_ADD_ROW_ATTEMPTS", 4)
# New Vincent Code Start: November 14, 2026
# Seconds to wait (with expect) for the new row to be drawn after + Add Characteristic. If it is not drawn in that
# time, the app ignored the click and the button is clicked again.
ROW_APPEAR_WAIT = _int_env("FORM3_ROW_APPEAR_WAIT", 8)
# New Vincent Code End: November 14, 2026


def _nominal_count(page):
    # Number of visible Nominal boxes in all frames (a second way to see that a row was added).
    total = 0
    for fr in page.frames:
        try:
            total += fr.locator(f"{NOMINAL}:visible").count()
        except Exception:
            pass
    return total


def _new_row_ready(popup_ctx, idx):
    # The new row is the last row of the grid: a "Variable" row with an empty Nominal box.
    # Measured with one JavaScript call, so nothing can sit waiting for a row the grid just re-drew.
    try:
        scroll_to_last_row(popup_ctx)
        res = popup_ctx.evaluate(_NEW_ROW_JS)
        if not res or res.get("state") != "OK":
            print("  waiting for the new row: no Results dropdown is drawn yet")
            return False
        text = (res.get("text") or "").strip()
        nom = res.get("nom")
        if "variable" not in text.lower():
            print(
                f"  waiting for the new row: last row shows {text!r}, rows drawn={res.get('n')}"
            )
            return False
        if nom is None:
            print(
                f"  waiting for the new row: no Nominal box next to the last row, rows drawn={res.get('n')}"
            )
            return False
        if nom.strip():
            print(
                f"  waiting for the new row: last row's Nominal is {nom.strip()!r} (not empty), rows drawn={res.get('n')}"
            )
            return False
        return True
    except Exception as e:
        print(f"  waiting for the new row: {type(e).__name__}: {str(e).strip()[:150]}")
        return False


def _scroll_grid_left(ctx, page):
    try:
        ctx.evaluate(
            "() => document.querySelectorAll("
            "'.ag-body-horizontal-scroll-viewport, .ag-center-cols-viewport, .k-grid-content')"
            ".forEach(e => { e.scrollLeft = 0; })"
        )
    except Exception:
        pass
    nap(page, 500)


def _dump_add_row_state(page):
    save_screenshot(page, "form3_new_row_missing")
    try:
        for fr in page.frames:
            if (
                fr.locator(NOMINAL).count()
                or fr.get_by_text(re.compile(ADD_CHARACTERISTIC_RE, re.I)).count()
            ):
                save_html("form3_add_row.html", fr.content())
                break
    except Exception:
        pass


def add_characteristic_row(page, ctx, expected=None):
    # Click + Add Characteristic and wait for the new (empty, Variable) row at the bottom of the grid.
    # If no new row shows up, the button is clicked again (up to ADD_ROW_ATTEMPTS times). It is NOT clicked
    # again when a row WAS added, because that would leave an extra blank row in the grid.
    # `expected` = number of rows there should be before the click (our own count). The grid may draw
    # only the rows near the screen, so the number of rows in the page can be lower than the real count.
    in_page = results_dropdown_count(ctx)
    before = in_page if expected is None else expected
    nominal_before = _nominal_count(page)
    idx = before
    _hook_network(page)  # New Vincent Code: November 14, 2026
    for attempt in range(1, ADD_ROW_ATTEMPTS + 1):
        _prepare_add_click(page)  # New Vincent Code: November 14, 2026
        btn = find_clickable(ctx, ADD_CHARACTERISTIC_RE)
        if btn.count() == 0:
            raise RuntimeError("'Add Characteristic' button not found")
        target = btn.first
        # New Vincent Code Start: November 14, 2026
        if not wait_enabled(page, target, 20):
            print("  + Add Characteristic stayed disabled for 20 s")
        # New Vincent Code End: November 14, 2026
        target.click(timeout=T(5000))
        print(
            "Clicked + Add Characteristic."
            if attempt == 1
            else f"Clicked + Add Characteristic (attempt {attempt} of {ADD_ROW_ATTEMPTS})."
        )
        # New Vincent Code Start: November 14, 2026
        # expect(): fail fast when the click was lost. If no new row is drawn within ROW_APPEAR_WAIT seconds the app
        # ignored the click, so skip the slow checks below and click again.
        if not _row_appeared(page, ctx, in_page, nominal_before, ROW_APPEAR_WAIT):
            _log_add_row_clues(
                page, f"attempt {attempt}: no row appeared within {ROW_APPEAR_WAIT} s"
            )
            if attempt < ADD_ROW_ATTEMPTS:
                print("  no new row appeared, clicking + Add Characteristic again...")
            continue
        # New Vincent Code End: November 14, 2026
        popup_ctx = frame_with_selector(page, f"{NOMINAL}:visible", patient(6))
        if popup_ctx is None:
            _scroll_grid_left(ctx, page)  # Nominal column may just be off-screen
            popup_ctx = frame_with_selector(page, f"{NOMINAL}:visible", patient(2))
        if popup_ctx is not None:
            _NEWEST["idx"] = idx
            print(
                f"  Checking that row {idx + 1} is drawn at the bottom of the grid..."
            )
            if poll(page, lambda: _new_row_ready(popup_ctx, idx), 12):
                return popup_ctx, idx
        # Not ready. Was a row added at all?
        row_added = (
            results_dropdown_count(ctx) > in_page
            or _nominal_count(page) > nominal_before
        )
        print(
            f"  attempt {attempt}: new row not ready; Results dropdowns drawn: "
            f"{results_dropdown_count(ctx)} (before: {in_page}), Nominal boxes: "
            f"{_nominal_count(page)} (before: {nominal_before})"
        )
        if row_added:
            # New Vincent Code Start: November 14, 2026
            # A row exists but is not "ready" yet (still drawing / not scrolled to). Do NOT click again (that adds a
            # blank extra row) and do not give up yet: wait for it a while longer.
            pc = popup_ctx or ctx
            print(
                "  a row WAS added but is not ready yet; waiting for it (not clicking again)..."
            )
            if poll(page, lambda: _new_row_ready(pc, idx), 15):
                _NEWEST["idx"] = idx
                return pc, idx
            # New Vincent Code End: November 14, 2026
            break  # a row exists; clicking again would only add a blank extra row
        _log_add_row_clues(
            page, f"attempt {attempt}: no row was added"
        )  # New Vincent Code: November 14, 2026
        print("  no new row was added, clicking + Add Characteristic again...")
    _log_add_row_clues(page, "giving up")  # New Vincent Code: November 14, 2026
    _dump_add_row_state(page)
    raise RuntimeError(
        f"clicked Add Characteristic but the new row {idx + 1} did not appear as the last row "
        f"(rows drawn in page={results_dropdown_count(ctx)}); see form3_new_row_missing.png and form3_add_row.html"
    )


# New Vincent Code Start: November 14, 2026
# Silent-bug fixes for "+ Add Characteristic clicked, but no row appears":
#  1. before every click the focus is taken off the last typed box and the page must be idle (a click that lands
#     while Angular re-draws after a blur / save is lost, although Playwright reports it as clicked);
#  2. when no row appears, print the clues (invalid fields, focused box, failed requests) so the cause is visible.
_NET = {
    "failed": [],
    "bad": [],
}  # the last few failed requests / HTTP errors seen on the page
_PREP = {"focused": "-"}  # what had the focus just before the last click

_BLUR_JS = """() => {
  const a = document.activeElement;
  if (!a || a === document.body) return '';
  const d = (a.tagName || '') + (a.name ? '[name=' + a.name + ']' : '') + (a.id ? '#' + a.id : '');
  if (a.blur) a.blur();
  return d;
}"""


def _hook_network(page):
    if getattr(page, "_f3_net_hook", False):
        return

    def failed(r):
        _NET["failed"].append(f"{r.method} {r.url[:110]} -> {r.failure}")
        del _NET["failed"][:-8]

    def response(r):
        try:
            if r.status >= 400 and "net-inspect.com" in r.url:
                _NET["bad"].append(f"HTTP {r.status} {r.request.method} {r.url[:110]}")
                del _NET["bad"][:-8]
        except Exception:
            pass

    page.on("requestfailed", failed)
    page.on("response", response)
    page._f3_net_hook = True


def _pending_requests(page):
    return len(getattr(page, "_ni_inflight", None) or ())


def _prepare_add_click(page):
    """Take the focus off the last typed box, then wait until Save Fair is not loading and no request is running."""
    focused = []
    for fr in page.frames:
        try:
            d = fr.evaluate(_BLUR_JS)
            if d:
                focused.append(d)
        except Exception:
            pass
    _PREP["focused"] = ", ".join(focused) or "nothing"
    if not poll(
        page,
        lambda: not save_loading_visible(page) and _pending_requests(page) == 0,
        10,
    ):
        print(
            f"  (page still busy before the click: save loading={save_loading_visible(page)}, "
            f"requests running={_pending_requests(page)}; clicking anyway)"
        )
    nap(page, 300)


def _log_add_row_clues(page, why):
    print(f"  CLUES ({why}):")
    try:
        bad = invalid_controls(page)
        print(f"    fields Angular marks invalid: {bad[:8] if bad else 'none'}")
    except Exception as e:
        print(f"    invalid-field check failed: {type(e).__name__}")
    print(f"    had the focus before the click: {_PREP['focused']}")
    print(
        f"    save loading visible: {save_loading_visible(page)}; requests running: {_pending_requests(page)}"
    )
    print(f"    failed requests: {_NET['failed'][-4:] or 'none'}")
    print(f"    HTTP errors: {_NET['bad'][-4:] or 'none'}")


def _row_appeared(page, ctx, in_page, nominal_before, seconds):
    """expect(): wait (auto-retrying) until one more Results dropdown than before is drawn in the grid.
    nth(in_page) exists only when at least in_page + 1 are drawn, so this passes for "one more row" (or more) and
    does not break if the grid draws a few extra rows. A new Nominal box is accepted as a second sign.
    """
    dropdowns = ctx.locator("kendo-dropdownlist:visible").filter(
        has_text=RESULT_TYPE_RE
    )
    try:
        expect(dropdowns.nth(in_page)).to_be_visible(timeout=int(seconds * 1000))
        return True
    except AssertionError:
        return _nominal_count(page) > nominal_before


# New Vincent Code End: November 14, 2026


# New Fix Code End: November 13, 2026


# New Vincent Code End: October 23, 2026


# New Allen Code Start: October 26, 2026
ALL_RE = re.compile(r"^\s*all\s*$", re.I)
PAGE_SIZE_DROPDOWN = (
    "kendo-pager kendo-dropdownlist, kendo-pager-page-sizes kendo-dropdownlist"
)


def set_page_size(ctx, needed):
    # The grid shows 10 rows per page, and a new row after the 10th lands on page 2. So raise the page
    # size in the pager first: the smallest option that holds `needed` rows, else the biggest option.
    # Returns how many rows fit on one page now (GRID_PAGE_SIZE if the pager could not be changed).
    page = ctx.page
    item_sel = "li.k-item:visible, li.k-list-item:visible, [role=option]:visible"
    try:
        print(f"  Looking for the grid page-size dropdown (need {needed} rows)...")
        dd = ctx.locator(PAGE_SIZE_DROPDOWN).first
        if dd.count() == 0:
            # Fallback: any dropdown on the page whose whole text is just a number (10, 20, ...).
            dd = (
                ctx.locator("kendo-dropdownlist")
                .filter(has_text=re.compile(r"^\s*(\d+|all)\s*$", re.I))
                .last
            )
        if dd.count() == 0:
            # Last fallback: a plain <select> with numeric options.
            sel = ctx.locator("kendo-pager select, select.k-dropdown, select").last
            if sel.count() == 0:
                for fr in page.frames:
                    if fr.locator("kendo-pager").count():
                        save_html(
                            "form3_pager.html",
                            fr.locator("kendo-pager").first.evaluate(
                                "e => e.outerHTML"
                            ),
                        )
                        break
                raise RuntimeError("the page-size dropdown of the grid was not found")
            sel.scroll_into_view_if_needed(timeout=T(5000))
            opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
            all_opt = next((o for o in opts if ALL_RE.match(o)), None)
            if all_opt:
                sel.select_option(label=all_opt)
                settle(page)
                nap(page, 800)
                print(f"  Grid page size is now All (offered: {opts}).")
                return needed
            sizes = sorted({int(o) for o in opts if o.isdigit()})
            if not sizes:
                raise RuntimeError("the page-size <select> has no numeric options")
            fits = [s for s in sizes if s >= needed]
            target = fits[0] if fits else sizes[-1]
            sel.select_option(label=str(target))
            settle(page)
            nap(page, 800)
            print(f"  Grid page size is now {target} (offered sizes: {sizes}).")
            return target
        dd.scroll_into_view_if_needed(timeout=T(5000))
        current_txt = _dd_text(dd)
        print(f"  Page-size dropdown found, it shows {current_txt!r}.")
        if ALL_RE.match(current_txt):
            print("  Grid page size is already All.")
            return needed
        current = int(current_txt) if current_txt.isdigit() else GRID_PAGE_SIZE
        # Open the dropdown and read which sizes it offers.
        dd.click(timeout=T(5000))
        items = ctx.locator(item_sel)
        items.first.wait_for(timeout=T(4000))
        texts = [t.strip() for t in items.all_inner_texts()]
        sizes = sorted({int(t) for t in texts if t.isdigit()})
        page.keyboard.press("Escape")
        nap(page, 300)
        # Prefer the "All" option: every row on one page, whatever the row count.
        if any(ALL_RE.match(t) for t in texts):
            pick_kendo_option(ctx, dd, "Grid page size", "All")
            settle(page)
            nap(page, 800)
            print(f"  Grid page size is now All (offered: {texts}).")
            return needed
        if current >= needed:
            print(f"  Grid page size is {current}, enough for {needed} rows.")
            return current
        if not sizes:
            raise RuntimeError("the page-size dropdown has no numeric options")
        fits = [s for s in sizes if s >= needed]
        target = fits[0] if fits else sizes[-1]
        if target <= current:
            print(f"  Grid page size stays {current} (offered sizes: {sizes}).")
            return current
        pick_kendo_option(ctx, dd, "Grid page size", str(target))
        settle(page)
        nap(page, 800)
        print(f"  Grid page size is now {target} (offered sizes: {sizes}).")
        return target
    except Exception as e:
        print(
            f"  Could not change the grid page size ({type(e).__name__}: {str(e).strip()[:200]}); "
            f"keeping {GRID_PAGE_SIZE} rows."
        )
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return GRID_PAGE_SIZE


# New Allen Code End: October 26, 2026


def find_type_dropdown(ctx, idx=0):
    # The dropdown normally already shows 'Symmetrical' or 'Bilateral'.
    # One such dropdown per row, so the one for our row is the nth match in the whole frame.
    shown = ctx.locator("kendo-dropdownlist:visible").filter(has_text=TYPE_OPTION_RE)
    if shown.count() > idx:
        return shown.nth(idx)
    # Otherwise open each visible dropdown and look for those two options.
    boxes = ctx.locator("kendo-dropdownlist:visible")
    for i in range(boxes.count()):
        cand = boxes.nth(i)
        try:
            cand.click(timeout=T(3000))
            items = ctx.locator("li.k-item:visible, [role=option]:visible")
            items.first.wait_for(timeout=T(3000))
            texts = items.all_inner_texts()
            cand.press("Escape")
            if any(TYPE_OPTION_RE.search(t) for t in texts):
                return cand
        except Exception:
            try:
                cand.press("Escape")
            except Exception:
                pass
    return None


def type_into_box(box, label, value, commit=True, allow_minus=False):
    # commit=True takes the cursor out of the box at the end (a blur, NOT the Tab key, so no other cell
    # gets focused or opened); commit=False leaves the cursor in the box until the next click.
    # allow_minus=True: the page may put a "-" in front of the value by itself (Low Tolerance), so
    # "0.11" and "-0.11" both count as typed correctly.
    if not value:
        raise RuntimeError(f"{label}: there is no value to type")
    box.wait_for(state="visible", timeout=T(8000))
    limit = box.get_attribute("maxlength")
    if limit and limit.isdigit() and len(value) > int(limit):
        raise RuntimeError(
            f"{label}: {value!r} is {len(value)} characters but the box allows {limit}"
        )

    def same():
        shown_now = box.input_value().strip()
        if shown_now == value:
            return True
        return allow_minus and shown_now.lstrip("-") == value.lstrip("-")

    box.click(timeout=T(5000))
    if TYPE_DELAY == 0:
        box.fill(value)  # whole value at once (fires the input event the page needs)
        if not same():
            # the box rejected the paste: type it key by key instead
            box.press("Control+A")
            box.press("Delete")
            box.press_sequentially(value, delay=40)
    else:
        box.press("Control+A")
        box.press("Delete")
        box.press_sequentially(value, delay=TYPE_DELAY)
        if not same():
            box.fill(value)
    shown = box.input_value().strip()
    if not same():
        raise RuntimeError(f"{label}: typed {value!r} but the box shows {shown!r}")
    if commit:
        box.evaluate("e => e.blur()")
    print(f"Typed {label}: {shown}")


def type_value(ctx, selector, label, value, index=0):
    type_into_box(ctx.locator(f"{selector}:visible").nth(index), label, value)


# New Vincent Code Start: October 23, 2026
_NEWEST = {
    "idx": None
}  # row number (0-based) of the row being filled = the last row of the grid


def results_dropdown(ctx, idx):
    # The Variable / Attribute dropdown of row idx (one per row).
    boxes = ctx.locator("kendo-dropdownlist:visible").filter(has_text=RESULT_TYPE_RE)
    n = boxes.count()
    # The grid draws only the rows near the screen, so "row number = position in the page" breaks once
    # there are many rows. The row being filled is always the newest one = the LAST row drawn.
    if n and _NEWEST["idx"] == idx:
        return boxes.nth(n - 1)
    if n <= idx:
        raise RuntimeError(
            f"the Results (Variable/Attribute) dropdown of row {idx + 1} was not found"
        )
    return boxes.nth(idx)


def results_dropdown_count(ctx):
    return (
        ctx.locator("kendo-dropdownlist:visible")
        .filter(has_text=RESULT_TYPE_RE)
        .count()
    )


def row_anchor_box(ctx, idx):
    # Position of row idx on the page = position of its Results dropdown (the first thing at the top of the row).
    dd = results_dropdown(ctx, idx)
    # Fast path: the row is already well inside the screen, so measure it and go (no scrolling, no pause).
    try:
        screen_h = ctx.page.evaluate("window.innerHeight")
    except Exception:
        screen_h = 0
    quick = _bbox(dd, 1000)
    if quick and screen_h and 150 <= quick["y"] <= screen_h - 300:
        return quick
    dd.scroll_into_view_if_needed(timeout=T(5000))
    # scroll_into_view_if_needed leaves the row at the very edge of the screen. Centre it, so that
    # the clicks made BELOW the row (mouse clicks by x/y) still land inside the screen.
    try:
        dd.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest'})")
        nap(ctx.page, 200)
    except Exception:
        pass
    return _bbox(dd)


def _dd_text(dd):
    try:
        return " ".join(dd.inner_text(timeout=2000).split())
    except Exception:
        return ""


def _dd_shows(ctx, dd, option_text, wait_ms=2000):
    # Short wait (2s, was 5s) for the dropdown to show the chosen option.
    for _ in range(max(1, wait_ms // 150)):
        if option_text.lower() in _dd_text(dd).lower():
            return True
        nap(ctx.page, 150)
    return False


def pick_kendo_option(ctx, dd, label, option_text, attempts=4):
    # Sturdier version of select_kendo_option: scrolls the dropdown on screen first, waits for the
    # row to finish re-rendering, and changes the way it picks on every attempt:
    #   1 = normal click   2 = forced click   3 and 4 = click to open, then type the option + Enter.
    page = ctx.page
    pat = re.compile(rf"^\s*{re.escape(option_text)}\s*$", re.I)
    item_sel = "li.k-item:visible, li.k-list-item:visible, [role=option]:visible"
    if _dd_shows(ctx, dd, option_text, 300):
        print(f"Selected {label}: {_dd_text(dd)}")
        return
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                # retries: let the row finish re-rendering, then bring the dropdown to the middle
                nap(page, 300)
                dd.scroll_into_view_if_needed(timeout=T(4000))
                dd.evaluate(
                    "e => e.scrollIntoView({block: 'center', inline: 'nearest'})"
                )
                nap(page, 250)
            else:
                dd.scroll_into_view_if_needed(timeout=T(4000))
            if attempt <= 2:
                dd.click(timeout=T(5000), force=(attempt == 2))
                items = ctx.locator(item_sel)
                items.first.wait_for(timeout=T(4000))
                filt = ctx.locator(".k-list-filter input:visible")
                if filt.count():
                    filt.first.fill(option_text)
                    nap(page, 400)
                exact = items.filter(has_text=pat)
                target = (
                    exact if exact.count() else items.filter(has_text=option_text)
                ).first
                target.scroll_into_view_if_needed(timeout=T(3000))
                target.click(timeout=T(5000))
            else:
                dd.click(timeout=T(5000), force=True)
                nap(page, 400)
                page.keyboard.type(option_text, delay=70)
                nap(page, 300)
                page.keyboard.press("Enter")
        except Exception as e:
            last = f"(could not pick: {type(e).__name__})"
        if _dd_shows(ctx, dd, option_text, 2000):
            print(f"Selected {label}: {_dd_text(dd)} (attempt {attempt})")
            return
        last = _dd_text(dd) or last
        print(f"  {label}: attempt {attempt} shows {last!r}, retrying...")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        nap(page, 500)
    raise RuntimeError(f"{label}: expected {option_text!r} but the box shows {last!r}")


_LAST_ROW_JS = """() => {
  const els = [...document.querySelectorAll('kendo-dropdownlist')]
    .filter(e => e.getClientRects().length && /Variable|Attribute/i.test(e.innerText));
  if (!els.length) return null;
  const e = els[els.length - 1];
  return {n: els.length, y: Math.round(e.getBoundingClientRect().top)};
}"""
_CENTER_LAST_ROW_JS = """() => {
  const els = [...document.querySelectorAll('kendo-dropdownlist')]
    .filter(e => e.getClientRects().length && /Variable|Attribute/i.test(e.innerText));
  if (!els.length) return false;
  els[els.length - 1].scrollIntoView({block: 'center', inline: 'nearest'});
  return true;
}"""


def scroll_to_last_row(ctx, tries=20):
    # Scroll the mouse wheel down until the last drawn row stops moving (= the bottom of the grid), then
    # centre that row. Used for the newest row, whatever the grid has drawn or dropped meanwhile.
    # Everything is measured with one JavaScript call each, so nothing can sit waiting for an element.
    page = ctx.page
    try:
        page.mouse.move(400, 400)
    except Exception:
        pass
    for _ in range(tries):
        a = ctx.evaluate(_LAST_ROW_JS)
        page.mouse.wheel(0, 700)
        nap(page, 300)
        b = ctx.evaluate(_LAST_ROW_JS)
        if a and b and a["n"] == b["n"] and a["y"] == b["y"]:
            break
    if not ctx.evaluate(_CENTER_LAST_ROW_JS):
        raise RuntimeError(
            "no Results dropdown was found while scrolling to the last row"
        )
    nap(page, 500)
    boxes = ctx.locator("kendo-dropdownlist:visible").filter(has_text=RESULT_TYPE_RE)
    return boxes.nth(boxes.count() - 1)


def scroll_to_row(ctx, idx, tries=15):
    # Bring row idx on screen. After SAVE FAIR the grid can go back to the top, and the grid only
    # draws the rows near the screen, so a row far below may not exist in the page yet. Scroll
    # down with the mouse wheel until the row's Results dropdown exists, then centre it.
    page = ctx.page
    try:
        page.mouse.move(400, 400)
    except Exception:
        pass
    if _NEWEST["idx"] == idx:
        return scroll_to_last_row(ctx)
    for _ in range(tries):
        if results_dropdown_count(ctx) > idx:
            break
        page.mouse.wheel(0, 700)
        nap(page, 400)
    else:
        raise RuntimeError(f"row {idx + 1} did not show up after scrolling down")
    dd = results_dropdown(ctx, idx)
    dd.scroll_into_view_if_needed(timeout=T(5000))
    dd.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest'})")
    nap(page, 500)
    return dd


# New Allen Code Start: October 27, 2026
# One JavaScript call finds the box of a row (instead of measuring every box of the page one by one, which
# got slower and slower as the grid filled up).
_RANGE_JS = r"""(a) => {
  const re = a.re ? new RegExp(a.re, 'i') : null;
  for (const e of document.querySelectorAll(a.sel)) {
    if (!e.getClientRects().length) continue;
    const r = e.getBoundingClientRect();
    if (a.vis && !(r.width > 0 && r.height > 0)) continue;
    if (re && !re.test(e.innerText || '')) continue;
    if (r.top >= a.y0 && r.top <= a.y1) {
      e.setAttribute('data-form3-field', String(a.tag));
      return true;
    }
  }
  return false;
}"""
_FIELD_TAG = [0]


def find_in_range(ctx, selector, y0, y1, text_re=None):
    # The first box matching `selector` (a trailing ":visible" means it must have a size) whose top edge is
    # between y0 and y1 on the screen. Returns a locator, or None.
    vis = selector.endswith(":visible")
    sel = selector[: -len(":visible")] if vis else selector
    _FIELD_TAG[0] += 1
    tag = _FIELD_TAG[0]
    try:
        found = ctx.evaluate(
            _RANGE_JS,
            {"sel": sel, "vis": vis, "y0": y0, "y1": y1, "re": text_re, "tag": tag},
        )
    except Exception:
        return None
    return ctx.locator(f'[data-form3-field="{tag}"]').first if found else None


def type_dropdown_of_row(ctx, idx, above=10, below=130):
    # The Tolerance type dropdown (Symmetrical / Bilateral / Range Inclusive) that sits in row idx,
    # found by its position next to the row's Results dropdown, after scrolling that row on screen.
    anchor = scroll_to_row(ctx, idx)
    ab = _bbox(anchor)
    if ab:
        dd = find_in_range(
            ctx,
            "kendo-dropdownlist:visible",
            ab["y"] - above,
            ab["y"] + below,
            text_re=TYPE_OPTION_RE.pattern,
        )
        if dd is not None:
            return dd
    return find_type_dropdown(ctx, idx)


def field_of_row(ctx, selector, idx, above=10, below=130):
    # The box matching `selector` that sits in row idx. Rows are about 135px tall, so a box that starts
    # between `above` px above and `below` px under the row's Results dropdown belongs to this row.
    try:
        ab = row_anchor_box(ctx, idx)
    except RuntimeError:
        return None
    if not ab:
        return None
    return find_in_range(ctx, selector, ab["y"] - above, ab["y"] + below)


# New Allen Code End: October 27, 2026


def type_in_row(ctx, selector, label, value, idx):
    # Like type_value, but the box is found by its position in row idx, not by its number in the page.
    poll(ctx.page, lambda: field_of_row(ctx, f"{selector}:visible", idx) is not None, 5)
    box = field_of_row(ctx, f"{selector}:visible", idx)
    if box is None:
        raise RuntimeError(f"the {label} box of row {idx + 1} was not found")
    type_into_box(box, label, value)


def editor_text(box):
    return " ".join((box.evaluate("e => e.innerText") or "").split())


def type_into_editor(box, label, value):
    # For the contenteditable Description box. It can be empty with zero height, so it is focused
    # directly (a normal click would wait for it to be "visible") and typed into with the keyboard.
    if not value:
        raise RuntimeError(f"{label}: there is no value to type")
    if len(value) > DESCRIPTION_MAX:
        raise RuntimeError(
            f"{label}: {len(value)} characters but the box allows {DESCRIPTION_MAX}"
        )
    box.wait_for(state="attached", timeout=T(8000))
    box.evaluate("e => e.focus()")
    page = box.page
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    if TYPE_DELAY == 0:
        page.keyboard.insert_text(value)
    else:
        page.keyboard.type(value, delay=TYPE_DELAY)
    if editor_text(box) != value:
        # Fallback: set the text and tell Angular the box changed.
        box.evaluate(
            "(e, v) => { e.innerText = v; e.dispatchEvent(new Event('input', {bubbles: true})); }",
            value,
        )
    shown = editor_text(box)
    if shown != value:
        raise RuntimeError(f"{label}: typed {value!r} but the box shows {shown!r}")
    # No Tab here: it would move into the next cell and open it. The next click leaves this box.
    print(f"Typed {label}: {shown}")


def header_center_x(ctx, text, exact=False):
    # x (page coordinates) of the middle of a visible column header with this text.
    heads = ctx.get_by_text(text, exact=exact)
    for i in range(heads.count()):
        try:
            if heads.nth(i).is_visible():
                hb = _bbox(heads.nth(i))
                if hb:
                    return hb["x"] + hb["width"] / 2
        except Exception:
            pass
    return None


def click_cell(ctx, idx, header_text, exact=False, dy=0):
    # Click inside the column with this header, on the same line as row idx
    # (x from the column header, y from that row's Results dropdown).
    ab = row_anchor_box(ctx, idx)
    x = header_center_x(ctx, header_text, exact)
    if x is None or not ab:
        raise RuntimeError(
            f"could not find where the {header_text} cell is on the page"
        )
    # A mouse click by x/y only works on a spot that is really on the screen. If the spot is
    # outside the window (or under its edge), scroll with the wheel and measure again.
    page = ctx.page
    for _ in range(4):
        y = ab["y"] + 75 + dy
        try:
            screen_h = page.evaluate("window.innerHeight")
        except Exception:
            screen_h = 0
        if not screen_h or 8 <= y <= screen_h - 8:
            break
        page.mouse.move(max(x, 5), min(max(y, 5), max(screen_h - 5, 5)))
        page.mouse.wheel(0, y - screen_h / 2)
        nap(page, 350)
        ab = row_anchor_box(ctx, idx)
        if not ab:
            raise RuntimeError(f"row {idx + 1} could not be found after scrolling")
    page.mouse.click(x, ab["y"] + 75 + dy)


def open_cell_box(ctx, idx, ready, header_text, label, exact=False, click_first=False):
    # Click the cell, wait 2 seconds for its box; if it did not open, click again (a little higher or
    # lower in the cell) until it shows up or FORM3_CELL_WAIT seconds (default 5) are over.
    offsets = (0, -25, 20, -45, 35)
    deadline = time.time() + FORM3_CELL_WAIT
    tries = 0
    while time.time() < deadline:
        if ready() and (tries > 0 or not click_first):
            return True
        click_cell(ctx, idx, header_text, exact, dy=offsets[tries % len(offsets)])
        tries += 1
        if poll(ctx.page, ready, 2):
            return True
        print(
            f"  {label} box not showing after click {tries}; clicking the cell again..."
        )
    return ready()


def click_reference_location_cell(ctx, idx):
    click_cell(ctx, idx, "6. Reference Location")


def description_box_of_row(ctx, idx):
    # The Description editor that sits in row idx (found by its position next to the row's Results dropdown).
    # No ":visible": an empty editor can have zero height.
    return field_of_row(ctx, DESCRIPTION_EDITOR, idx, above=50, below=100)


def wait_description_box(ctx, idx):
    # The Description editor only opens when its cell is clicked: click it right away (skipped if it is
    # already showing), then wait until it is visible (same patient() wait as the other elements).
    open_cell_box(
        ctx,
        idx,
        lambda: description_box_of_row(ctx, idx) is not None,
        "Description",
        "Description",
        exact=True,
    )
    box = description_box_of_row(ctx, idx)
    if box is None:
        raise RuntimeError(
            f"the Description box of row {idx + 1} was not found "
            f"(in page={ctx.locator(DESCRIPTION_EDITOR).count()}, "
            f"visible={ctx.locator(DESCRIPTION_EDITOR + ':visible').count()}, "
            f"any with this id={ctx.locator('#requirement-editor-container').count()})"
        )
    return box


def fill_reference_location(ctx, row, idx):
    # Step before 8. Requirement: type the "location" column into 6. Reference Location.
    location = clean(row.get(COL_INSPECT_LOCATION))
    if not location:
        print(
            f"  {COL_INSPECT_LOCATION} is blank for this row, so 6. Reference Location was left empty."
        )
        return
    page = ctx.page

    def box_ready():
        return box_of_row(ctx, REFERENCE_LOCATION, idx) is not None

    # The textarea only opens when the cell is clicked, so click it right away (skipped if it is already
    # showing), then wait until it is visible (same patient() wait as the other elements).
    if not box_ready():
        open_cell_box(
            ctx, idx, box_ready, "6. Reference Location", "6. Reference Location"
        )
    box = box_of_row(ctx, REFERENCE_LOCATION, idx)
    if box is None:
        raise RuntimeError(
            f"the 6. Reference Location box of row {idx + 1} was not found "
            f"(in page={ctx.locator(REFERENCE_LOCATION).count()})"
        )
    type_into_box(box, "6. Reference Location", location, commit=False)


# New Vincent Code Start: October 24, 2026
def box_of_row(ctx, selector, idx):
    # The box matching `selector` that sits in row idx (found by its position next to the row's Results dropdown).
    return field_of_row(ctx, selector, idx, above=50, below=100)


def wait_cell_box(ctx, idx, selector, header_text, label):
    # Click the cell of this column on row idx first (so it opens for sure), then wait until its box is
    # showing (same patient() wait as the other elements).
    open_cell_box(
        ctx,
        idx,
        lambda: box_of_row(ctx, selector, idx) is not None,
        header_text,
        label,
        click_first=True,
    )
    box = box_of_row(ctx, selector, idx)
    if box is None:
        raise RuntimeError(
            f"the {label} box of row {idx + 1} was not found "
            f"(in page={ctx.locator(selector).count()}, visible={ctx.locator(selector + ':visible').count()})"
        )
    return box


def set_attribute_and_check(ctx, idx):
    # Results type Variable -> Attribute, then click the green check.
    rd = results_dropdown(ctx, idx)
    pick_kendo_option(ctx, rd, "Results type", "Attribute")
    check = rd.locator(CHECK_XPATH).first
    check.wait_for(state="visible", timeout=T(8000))
    check.click(timeout=T(5000))
    print("Clicked the check (Attribute).")


# New Vincent Code End: October 24, 2026


# New Allen Code Start: October 26, 2026
# 12a. Inspection Method, chosen from the requiredDimension value (Variable and maxTyp rows only, never when the tooling is Radius Gauge):
#   1 up to 300 -> Digital Caliper | above 300 up to 999 -> Analog Caliper | 1000 and more -> Meter Tape
INSPECTION_METHOD_HEADER = "12a. Inspection"
INSPECTION_METHOD_LABEL = "12a. Inspection Method"
_COLUMN_BOX_JS = """(a) => {
  for (const e of document.querySelectorAll('[data-form3-target]')) e.removeAttribute('data-form3-target');
  for (const e of document.querySelectorAll('input, textarea')) {
    if (!e.getClientRects().length) continue;
    const t = (e.type || '').toLowerCase();
    if (t === 'checkbox' || t === 'radio' || t === 'hidden') continue;
    const r = e.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (Math.abs(cx - a.cx) <= a.dx && cy >= a.y0 && cy <= a.y1) {
      e.setAttribute('data-form3-target', '1');
      return true;
    }
  }
  return false;
}"""


def inspection_method_for(required_dimension):
    # requiredDimension -> tool name, or "" when it is not a number or is below 1 (nothing is typed then).
    try:
        v = Decimal(clean(required_dimension).replace(",", ""))
        if v < 1:
            return ""
        if v <= 300:
            return "Digital Caliper"
        if v < 1000:
            return "Analog Caliper"
        return "Meter Tape"
    except (InvalidOperation, ValueError):
        return ""


def find_column_box(ctx, idx, header_text):
    # The text box that sits under the column header `header_text`, on the line of row idx.
    ab = row_anchor_box(ctx, idx)
    cx = header_center_x(ctx, header_text)
    if cx is None or not ab:
        return None
    found = ctx.evaluate(
        _COLUMN_BOX_JS, {"cx": cx, "dx": 45, "y0": ab["y"] - 10, "y1": ab["y"] + 130}
    )
    return ctx.locator('[data-form3-target="1"]').first if found else None


def fill_inspection_method(ctx, row, idx):
    # Extra step only: it never raises, so the other conditions of the row are not affected.
    # Designed / Qualified Tooling = Radius Gauge (toolId 3): 12a stays blank.
    if clean(row.get(COL_INSPECT_TOOL_ID)) == TOOL_ID_RADIUS_GAUGE:
        print(
            f"  {COL_INSPECT_TOOL_ID}={TOOL_ID_RADIUS_GAUGE} (Radius Gauge): {INSPECTION_METHOD_LABEL} left blank."
        )
        return
    method = inspection_method_for(row.get("requiredDimension"))
    if not method:
        print(
            f"  requiredDimension={clean(row.get('requiredDimension'))!r} is not a number of 1 or more: "
            f"{INSPECTION_METHOD_LABEL} left empty."
        )
        return
    try:
        open_cell_box(
            ctx,
            idx,
            lambda: find_column_box(ctx, idx, INSPECTION_METHOD_HEADER) is not None,
            INSPECTION_METHOD_HEADER,
            INSPECTION_METHOD_LABEL,
            click_first=True,
        )
        box = find_column_box(ctx, idx, INSPECTION_METHOD_HEADER)
        if box is None:
            raise RuntimeError(
                f"the {INSPECTION_METHOD_LABEL} box of row {idx + 1} was not found"
            )
        type_into_box(box, INSPECTION_METHOD_LABEL, method)
    except Exception as e:
        print(
            f"  WARNING: could not set {INSPECTION_METHOD_LABEL} to {method!r} on row {idx + 1}: "
            f"{type(e).__name__}: {str(e).strip()[:200]}"
        )
        try:
            save_screenshot(ctx.page, f"form3_inspection_method_row{idx + 1}")
        except Exception:
            pass
        try:
            ctx.page.keyboard.press("Escape")
        except Exception:
            pass


# New Allen Code End: October 26, 2026


def fill_variable_row(ctx, row, idx, nominal, inspection):
    # minTol/maxTol present: Tolerance type, Nominal, High/Low Tol, then the Results box (if inspectionResult has a value).
    min_tol = clean(row.get("minTol"))
    max_tol = clean(row.get("maxTol"))
    kind = tolerance_type(min_tol, max_tol)
    print(f"  Variable row -> {kind.upper()} (minTol={min_tol!r} maxTol={max_tol!r})")

    dd = type_dropdown_of_row(ctx, idx)
    if dd is None:
        raise RuntimeError("the Symmetrical/Bilateral dropdown was not found")
    pick_kendo_option(ctx, dd, "Tolerance type", kind)

    type_in_row(ctx, NOMINAL, "Nominal", nominal, idx)
    type_in_row(ctx, HIGH_TOL, "High Tolerance", max_tol, idx)
    if kind == "Bilateral":
        # Only Bilateral rows have a Low Tolerance box, so it cannot be picked by row number (idx).
        # Take the first Low Tolerance box after this row's High Tolerance box instead.
        high_box = field_of_row(ctx, f"{HIGH_TOL}:visible", idx)
        if high_box is None:
            raise RuntimeError(f"the High Tolerance box of row {idx + 1} was not found")
        low_box = high_box.locator(LOW_TOL_XPATH).first
        type_into_box(low_box, "Low Tolerance", min_tol, allow_minus=True)

    is_radius = clean(row.get(COL_INSPECT_TOOL_ID)) == TOOL_ID_RADIUS_GAUGE
    if is_radius:
        print(
            "  Radius Gauge row: the Results box is skipped (the row becomes Attribute)."
        )
    elif inspection:
        box = results_dropdown(ctx, idx).locator(RESULT_INPUT_XPATH).first
        type_into_box(box, "Inspection Result", inspection)
    else:
        print(
            f"  {COL_INSPECT_VALUE} is blank for this row, so the Results box was left blank."
        )

    # New rule: toolId 3 (Radius Gauge) with a blank maxTyp stays Symmetrical/Bilateral (no Range Inclusive),
    # but still gets Tooling = Radius Gauge and the 12. Additional Data / Comments text.
    if clean(row.get(COL_INSPECT_TOOL_ID)) == TOOL_ID_RADIUS_GAUGE:
        print(
            f"  {COL_INSPECT_TOOL_ID}={TOOL_ID_RADIUS_GAUGE} and {COL_INSPECT_MAXTYP} is blank -> "
            f"{kind} row with Tooling + Comments"
        )
        fill_tooling_and_comments(ctx, row, idx)
        # Radius Gauge rows end as Attribute: Results type Variable -> Attribute, then the green check.
        set_attribute_and_check(ctx, idx)

    # New condition: 12a. Inspection Method from the requiredDimension range.
    fill_inspection_method(ctx, row, idx)


# New Vincent Code Start: October 29, 2026
def is_number(text):
    # True when `text` can be typed into the numeric Nominal box (e.g. '1.5', '828.30'; not 'MS2003').
    try:
        return Decimal(clean(text).replace(",", "")).is_finite()
    except (InvalidOperation, ValueError):
        return False


# New Vincent Code End: October 29, 2026


def fill_attribute_row(ctx, row, idx, nominal):
    # minTol and maxTol are both blank: requiredDimension goes in the Description box,
    # the Results type is changed to Attribute, and the green check is clicked.
    print(
        "  Attribute row (Description = requiredDimension): minTol and maxTol are blank, "
        "or requiredDimension is not a number"
    )
    if not nominal:
        raise RuntimeError(
            f"minTol, maxTol and requiredDimension are all blank for listId={row.get('listId')}"
        )
    type_into_editor(wait_description_box(ctx, idx), "Description", nominal)
    # 7. Specification is skipped on purpose (left untouched).
    set_attribute_and_check(ctx, idx)

    # 12. Additional Data / Comments: always "Visual Inspection" on an Attribute row.
    box = wait_cell_box(
        ctx,
        idx,
        COMMENTS_BOX,
        "12. Additional Data",
        "12. Additional Data / Comments",
    )
    type_into_box(box, "12. Additional Data / Comments", ATTRIBUTE_COMMENT)


# New Vincent Code Start: October 24, 2026
def fill_tooling_and_comments(ctx, row, idx):
    # Tooling (only toolId 3 = Radius Gauge) and 12. Additional Data / Comments (jamcoNA, when it has a value).
    tool_id = clean(row.get(COL_INSPECT_TOOL_ID))
    if tool_id == TOOL_ID_RADIUS_GAUGE:
        box = wait_cell_box(ctx, idx, TOOLING_INPUT, "10. Designed", "Tooling")
        type_into_box(box, "Tooling", RADIUS_GAUGE_NAME)
    else:
        print(
            f"  {COL_INSPECT_TOOL_ID}={tool_id!r}, not {TOOL_ID_RADIUS_GAUGE}: Tooling left empty."
        )

    jamco = clean(row.get(COL_INSPECT_JAMCO))
    if jamco:
        box = wait_cell_box(
            ctx,
            idx,
            COMMENTS_BOX,
            "12. Additional Data",
            "12. Additional Data / Comments",
        )
        type_into_box(box, "12. Additional Data / Comments", jamco)
    else:
        print(
            f"  {COL_INSPECT_JAMCO} is blank, so 12. Additional Data / Comments was left empty."
        )


# New Allen Code Start: October 27, 2026
# Faster maxTyp rows: instead of SAVE FAIR + Range Inclusive after EVERY maxTyp row, the rows are filled
# first and ONE SAVE FAIR is done at the end, then Range Inclusive is set on all of them.
# FORM3_DEFER_RANGE=0 in .env goes back to the old way (save and change after each maxTyp row).
FORM3_DEFER_RANGE = str(env_str("FORM3_DEFER_RANGE", "1")).strip() != "0"
# Final SAVE FAIR at the end of Form 3 (default on). FORM3_FINAL_SAVE=0 in .env turns it off.
FORM3_FINAL_SAVE = str(env_str("FORM3_FINAL_SAVE", "1")).strip() != "0"
_PENDING_RANGE = (
    []
)  # (row number 0-based, DB row) waiting for Range Inclusive + the two Results values

# The grid row whose "Char #" box shows `n`. Tags its Tolerance type dropdown with data-form3-target="1"
# and its Results (Variable / Attribute) dropdown with data-form3-rd="1".
_CHAR_TYPE_JS = r"""(n) => {
  for (const e of document.querySelectorAll('[data-form3-target]')) e.removeAttribute('data-form3-target');
  for (const e of document.querySelectorAll('[data-form3-rd]')) e.removeAttribute('data-form3-rd');
  const inputs = [...document.querySelectorAll('input')];
  const labels = [...document.querySelectorAll('*')].filter(
    e => e.children.length === 0 && e.textContent.trim() === 'Char #' && e.getClientRects().length);
  for (const l of labels) {
    const inp = inputs.find(i => (l.compareDocumentPosition(i) & Node.DOCUMENT_POSITION_FOLLOWING) && i.getClientRects().length);
    if (!inp || inp.value.trim() !== String(n)) continue;
    const top = inp.getBoundingClientRect().top;
    const dds = [...document.querySelectorAll('kendo-dropdownlist')].filter(
      d => d.getClientRects().length && /Symmetrical|Bilateral|Range\s+Inclusive/i.test(d.innerText)
           && Math.abs(d.getBoundingClientRect().top - top) <= 70);
    if (!dds.length) return 'NO_DROPDOWN';
    dds[0].setAttribute('data-form3-target', '1');
    dds[0].scrollIntoView({block: 'center', inline: 'nearest'});
    const top2 = inp.getBoundingClientRect().top;
    const rds = [...document.querySelectorAll('kendo-dropdownlist')].filter(
      d => d.getClientRects().length && /Variable|Attribute/i.test(d.innerText)
           && Math.abs(d.getBoundingClientRect().top - top2) <= 70);
    if (rds.length) rds[0].setAttribute('data-form3-rd', '1');
    return 'FOUND';
  }
  return 'NOT_DRAWN';
}"""


def _find_row_by_char(ctx, number, tries=40):
    # The grid draws only the rows near the screen, so scroll from the top with the mouse wheel until the
    # row whose "Char #" is `number` is drawn. Tags its dropdowns (see _CHAR_TYPE_JS).
    page = ctx.page
    try:
        page.mouse.move(400, 400)
        for _ in range(6):
            page.mouse.wheel(0, -4000)
            nap(page, 100)
    except Exception:
        pass
    for _ in range(tries):
        res = ctx.evaluate(_CHAR_TYPE_JS, number)
        if res == "FOUND":
            nap(page, 400)
            return
        if res == "NO_DROPDOWN":
            raise RuntimeError(
                f"row with Char # {number} has no Tolerance type dropdown"
            )
        page.mouse.wheel(0, 600)
        nap(page, 250)
    raise RuntimeError(f"the row with Char # {number} was not found while scrolling")


def type_dropdown_by_char(ctx, number, tries=40):
    # The Tolerance type dropdown of the row whose "Char #" is `number`.
    _find_row_by_char(ctx, number, tries)
    return ctx.locator('[data-form3-target="1"]').first


def results_dropdown_by_char(ctx, number):
    # The Results (Variable / Attribute) dropdown of the row whose "Char #" is `number`.
    # Tries the row where it is first (it is normally still on screen); scrolls to find it if not.
    try:
        found = ctx.evaluate(_CHAR_TYPE_JS, number) == "FOUND"
    except Exception:
        found = False
    if not found:
        _find_row_by_char(ctx, number)
    rd = ctx.locator('[data-form3-rd="1"]')
    if rd.count() == 0:
        raise RuntimeError(
            f"the Results dropdown of the row with Char # {number} was not found"
        )
    nap(ctx.page, 200)
    return rd.first


# Inputs drawn under a Results dropdown (box 1, box 2, ...), top to bottom, stopping at the next row's
# Results dropdown. Each one is tagged data-form3-res="0", "1", ...; returns how many there are.
_RES_BOXES_JS = r"""(dd) => {
  for (const e of document.querySelectorAll('[data-form3-res]')) e.removeAttribute('data-form3-res');
  const r = dd.getBoundingClientRect();
  let limit = r.top + 260;
  for (const d of document.querySelectorAll('kendo-dropdownlist')) {
    if (!d.getClientRects().length || !/Variable|Attribute/i.test(d.innerText)) continue;
    const t = d.getBoundingClientRect().top;
    if (t > r.top + 5 && t < limit) limit = t;
  }
  const boxes = [...document.querySelectorAll('input, textarea')].filter(e => {
    if (!e.getClientRects().length) return false;
    const t = (e.type || '').toLowerCase();
    if (t === 'checkbox' || t === 'radio' || t === 'hidden') return false;
    const b = e.getBoundingClientRect();
    return b.width > 0 && b.height > 0 && b.left >= r.left - 5 && b.right <= r.right + 40
      && b.top >= r.bottom - 5 && b.top < limit;
  }).sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  boxes.forEach((e, i) => e.setAttribute('data-form3-res', String(i)));
  return boxes.length;
}"""
# The "+" button of a Results cell (a small element whose text is just + or add), tagged data-form3-plus="1".
_PLUS_JS = r"""(dd) => {
  for (const e of document.querySelectorAll('[data-form3-plus]')) e.removeAttribute('data-form3-plus');
  const r = dd.getBoundingClientRect();
  let limit = r.top + 260;
  for (const d of document.querySelectorAll('kendo-dropdownlist')) {
    if (!d.getClientRects().length || !/Variable|Attribute/i.test(d.innerText)) continue;
    const t = d.getBoundingClientRect().top;
    if (t > r.top + 5 && t < limit) limit = t;
  }
  const cands = [...document.querySelectorAll('button, span, a, i')].filter(e => {
    if (!e.getClientRects().length) return false;
    const txt = (e.textContent || '').trim();
    const label = ((e.getAttribute('title') || '') + ' ' + (e.getAttribute('aria-label') || '')).trim();
    if (!(/^(\+|add)$/i.test(txt) && e.children.length === 0) && !/^add\b/i.test(label)) return false;
    const b = e.getBoundingClientRect();
    return b.width > 0 && b.height > 0 && b.left >= r.left - 5 && b.right <= r.right + 60
      && b.top >= r.bottom - 5 && b.top < limit;
  }).sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  if (!cands.length) return false;
  cands[0].setAttribute('data-form3-plus', '1');
  return true;
}"""


def _result_boxes(ctx, rd):
    try:
        return int(rd.evaluate(_RES_BOXES_JS))
    except Exception:
        return 0


def fill_range_results(ctx, row, idx):
    # maxTyp row, AFTER Tolerance type became Range Inclusive:
    # Results Attribute -> Variable, press +, then box 1 = inspectionResult and box 2 = maxTyp.
    page = ctx.page
    number = idx + 1
    first = clean(row.get(COL_INSPECT_VALUE))
    second = clean(row.get(COL_INSPECT_MAXTYP))

    rd = results_dropdown_by_char(ctx, number)
    pick_kendo_option(ctx, rd, f"Results type (row {number})", "Variable")
    nap(page, 500)

    # Wait for the first Results box to show up under the Variable dropdown.
    rd = results_dropdown_by_char(ctx, number)
    poll(page, lambda: _result_boxes(ctx, rd) >= 1, 4)

    # Press + once to add the second box (skipped when two boxes are already there).
    if _result_boxes(ctx, rd) < 2:
        if not rd.evaluate(_PLUS_JS):
            save_screenshot(page, f"form3_plus_missing_row{number}")
            raise RuntimeError(
                f"row {number}: the + button of the Results cell was not found"
            )
        ctx.locator('[data-form3-plus="1"]').first.click(timeout=T(5000))
        print("Clicked + (second Results box).")
        nap(page, 400)
        rd = results_dropdown_by_char(ctx, number)
        poll(page, lambda: _result_boxes(ctx, rd) >= 2, 4)

    count = _result_boxes(ctx, rd)
    if count < 2:
        save_screenshot(page, f"form3_results_boxes_row{number}")
        raise RuntimeError(
            f"row {number}: expected 2 Results boxes after pressing +, found {count}"
        )

    if first:
        type_into_box(
            ctx.locator('[data-form3-res="0"]').first,
            "Result 1 (inspectionResult)",
            first,
        )
    else:
        print(f"  {COL_INSPECT_VALUE} is blank: Results box 1 left empty.")
    if second:
        _result_boxes(ctx, rd)  # tag the boxes again, in case the page redrew them
        type_into_box(
            ctx.locator('[data-form3-res="1"]').first, "Result 2 (maxTyp)", second
        )
    else:
        print(f"  {COL_INSPECT_MAXTYP} is blank: Results box 2 left empty.")


def apply_deferred_range(page, ctx, part_number):
    # One SAVE FAIR for all maxTyp rows, then on each of them:
    # Tolerance type -> Range Inclusive, Results -> Variable, press +, type the two values.
    rows_waiting = list(_PENDING_RANGE)
    _PENDING_RANGE.clear()
    print(
        f"  Setting Range Inclusive on {len(rows_waiting)} maxTyp row(s) after one SAVE FAIR..."
    )
    if not save_fair_and_wait(page, f"{part_number}_range"):
        print(
            "  SAVE FAIR failed, so Range Inclusive was NOT set on any row. Check the page."
        )
        return False
    failed = []
    for idx, row in rows_waiting:
        try:
            dd = type_dropdown_by_char(ctx, idx + 1)
            pick_kendo_option(
                ctx, dd, f"Tolerance type (row {idx + 1})", RANGE_INCLUSIVE_NAME
            )
            fill_range_results(ctx, row, idx)
        except Exception as e:
            failed.append(idx + 1)
            print(
                f"  WARNING: row {idx + 1}: Range Inclusive / Results not finished: {type(e).__name__}: {str(e).strip()[:200]}"
            )
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    if failed:
        print(
            f"  Range Inclusive / Results is MISSING on row(s) {failed}; set them by hand or run again."
        )
        return False
    return True


# New Allen Code End: October 27, 2026


def fill_maxtyp_row(ctx, row, idx, nominal):
    # maxTyp has data: Nominal + maxTol, Results type -> Attribute + check,
    # then Tooling (only toolId 3) and 12. Additional Data / Comments (jamcoNA). The location is already typed.
    # Later (after SAVE FAIR): Tolerance type -> Range Inclusive, Results -> Variable, press +,
    # box 1 = inspectionResult, box 2 = maxTyp (see apply_deferred_range / fill_range_results).
    max_tol = clean(row.get("maxTol"))
    print(
        f"  {COL_INSPECT_MAXTYP} has data ({clean(row.get(COL_INSPECT_MAXTYP))!r}) -> maxTyp row"
    )
    type_in_row(ctx, NOMINAL, "Nominal", nominal, idx)
    type_in_row(ctx, HIGH_TOL, "High Tolerance", max_tol, idx)
    set_attribute_and_check(ctx, idx)

    fill_tooling_and_comments(ctx, row, idx)
    # New condition: 12a. Inspection Method from the requiredDimension range.
    fill_inspection_method(ctx, row, idx)
    if FORM3_DEFER_RANGE:
        _PENDING_RANGE.append((idx, row))
        print(
            "  Range Inclusive + the two Results values will be set at the end, after one SAVE FAIR for all rows."
        )
        return
    # Last part: click SAVE FAIR again, then change the Tolerance type Symmetrical -> Range Inclusive.
    if not save_fair_and_wait(ctx.page, f"{row.get('resultId')}_row{idx + 1}"):
        raise RuntimeError(
            "SAVE FAIR failed, so the Tolerance type was not changed to Range Inclusive"
        )
    # The page may jump back to the top after the save, so scroll down to this row first.
    nap(ctx.page, 800)
    dd = type_dropdown_of_row(ctx, idx)
    if dd is None:
        raise RuntimeError("the Tolerance type dropdown was not found")
    dd.scroll_into_view_if_needed(timeout=T(5000))
    pick_kendo_option(ctx, dd, "Tolerance type", RANGE_INCLUSIVE_NAME)
    fill_range_results(ctx, row, idx)


# New Vincent Code End: October 24, 2026
# New Vincent Code End: October 23, 2026


def fill_characteristic(ctx, row, idx):
    nominal = clean(row.get("requiredDimension"))
    inspection = clean(row.get(COL_INSPECT_VALUE))
    print(
        f"  listId={row.get('listId')} resultId={row.get('resultId')} requiredDimension={nominal!r} "
        f"minTol={clean(row.get('minTol'))!r} maxTol={clean(row.get('maxTol'))!r} "
        f"{COL_INSPECT_VALUE}={inspection!r} {COL_INSPECT_LOCATION}={clean(row.get(COL_INSPECT_LOCATION))!r} "
        f"{COL_INSPECT_MAXTYP}={clean(row.get(COL_INSPECT_MAXTYP))!r} {COL_INSPECT_TOOL_ID}={clean(row.get(COL_INSPECT_TOOL_ID))!r} "
        f"(grid row {idx + 1})"
    )
    # New Vincent Code Start: October 23, 2026
    fill_reference_location(
        ctx, row, idx
    )  # 6. Reference Location first, then 8. Requirement
    # maxTyp has data -> maxTyp row; both minTol and maxTol blank -> Attribute row; otherwise -> Variable row.
    if clean(row.get(COL_INSPECT_MAXTYP)):
        fill_maxtyp_row(ctx, row, idx, nominal)
    elif (not clean(row.get("minTol")) and not clean(row.get("maxTol"))) or (
        # New Vincent Code Start: October 29, 2026
        # requiredDimension such as 'MS2003' cannot go in the numeric Nominal box (it keeps only '2003'),
        # even when minTol / maxTol hold '0.00'. Such a row is an Attribute row.
        nominal
        and not is_number(nominal)
        # New Vincent Code End: October 29, 2026
    ):
        fill_attribute_row(ctx, row, idx, nominal)
    else:
        fill_variable_row(ctx, row, idx, nominal, inspection)
    # New Vincent Code End: October 23, 2026


def run_form3(page, part, status):
    if not FILL_FORM3:
        print("  Form 3 NOT run: FILL_FORM3 is off. Add FILL_FORM3=true to .env.")
        return status
    part_number = FORM3_PART_NUMBER or part.number

    # New Vincent Code Start: October 23, 2026
    try:
        total = count_characteristics(part_number)
    except Exception as e:
        print(f"  Form 3: database error: {type(e).__name__}: {str(e).strip()[:300]}")
        return "FORM3_FAILED"
    if not total:
        print(
            f"  Form 3: no row found in {INSPECT_TABLE} for part {part_number} "
            f"(no resultId in {DB_TABLE}, or no inspection row for it). Skipping."
        )
        return "FORM3_SKIPPED"
    # New Vincent Code End: October 23, 2026

    ctx = open_form_3(page, part.number)
    if ctx is None:
        return "FORM3_FAILED"

    # New Allen Code Start: October 26, 2026
    # Raise the grid page size so all rows stay on one page (a row past the 10th would go to page 2).
    capacity = set_page_size(ctx, total)
    how_many = min(total, capacity)
    if how_many < total:
        print(
            f"  WARNING: the grid holds only {capacity} rows per page but there are {total}; "
            f"only the first {how_many} will be filled."
        )
    try:
        rows = fetch_characteristics(part_number, how_many)
    except Exception as e:
        print(f"  Form 3: database error: {type(e).__name__}: {str(e).strip()[:300]}")
        return "FORM3_FAILED"
    print(
        f"  Form 3: {total} different characteristic(s) (distinct {COL_INSPECT_CHARNO}) for part {part_number}; "
        f"filling {len(rows)} (grid page size {capacity}, keeping the {'highest' if DUP_KEEP == 'MAX' else 'lowest'} "
        f"{COL_INSPECT_LIST} when a {COL_INSPECT_CHARNO} repeats)."
    )
    # New Allen Code End: October 26, 2026

    done = 0
    next_idx = None
    _PENDING_RANGE.clear()
    try:
        # New Vincent Code Start: October 23, 2026
        for n, row in enumerate(rows, 1):
            print(f"  --- Characteristic {n} of {len(rows)} ---")
            t_row = time.time()
            ctx, idx = add_characteristic_row(page, ctx, next_idx)
            next_idx = idx + 1
            fill_characteristic(ctx, row, idx)
            done += 1
            print(f"  [time] characteristic {n}: {time.time() - t_row:.1f}s")
        # New Vincent Code End: October 23, 2026
    except Exception as e:
        print(
            f"  Form 3 failed on characteristic {done + 1} of {len(rows)} "
            f"({done} done): {type(e).__name__}: {str(e).strip()[:400]}"
        )
        save_screenshot(page, f"form3_error_{part.number}")
        try:
            for fr in page.frames:
                # New Fix Code: also dump the frame that has the Add Characteristic button
                if (
                    fr.locator(NOMINAL).count()
                    or fr.get_by_text(re.compile(ADD_CHARACTERISTIC_RE, re.I)).count()
                ):
                    save_html("form3_characteristic.html", fr.content())
                    print_form_controls(fr)
                    break
        except Exception:
            pass
        return "FORM3_FAILED"
    # New Allen Code Start: October 27, 2026
    if _PENDING_RANGE and not apply_deferred_range(page, ctx, part.number):
        save_screenshot(page, f"form3_range_{part.number}")
        return "FORM3_FAILED"
    # New Allen Code End: October 27, 2026
    save_screenshot(page, f"form3_filled_{part.number}")
    # Final step: SAVE FAIR once more so everything typed in Form 3 is stored (the Checklist comes next).
    if FORM3_FINAL_SAVE:
        print("  Form 3: all rows filled, clicking SAVE FAIR (final save)...")
        if not save_fair_and_wait(page, f"{part.number}_form3_final"):
            save_screenshot(page, f"form3_final_save_failed_{part.number}")
            print(
                f"  Form 3: {done} characteristic(s) filled but the final SAVE FAIR failed. Check the page."
            )
            return "FORM3_FAILED"
        save_screenshot(page, f"form3_saved_{part.number}")
        print(f"  Form 3: {done} characteristic(s) filled and saved.")
        # Next: the Checklist tab (checklist.py). It only runs when FILL_CHECKLIST=true in .env;
        # otherwise it just returns the status it was given.
        return run_documents(page, part, "FORM3_FILLED")
    print(f"  Form 3: {done} characteristic(s) filled (not saved; check the page).")
    return "FORM3_FILLED"
