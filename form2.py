import os
import re
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout

from form1 import save_fair
from login import (
    CERT_FOLDER,
    COL_QC_LOT,
    DEBUG_FORM2,
    FILL_FORM2_MATERIALS,
    FILL_FORM2_PROCESSES,
    FORM2_LOT_OVERRIDE,
    FORM2_OVERWRITE_FIRST_ROW,
    PROCESS_ROOT,
    QC_TABLE,
    SAVE_FORM2_AT_END,  # no longer used: Form 2 is now always saved after the writes (kept so the import still works)
    T,
    dump_open_popup,
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
    timed,
)

MATERIAL_ROOT = "ni-fair-materials"
ATTACHED_PDFS = set()  # PDFs already typed for an earlier row in this FAIR

QC_MATERIAL_COLUMNS = [
    ("5. Material Name", "materialnameS", "material"),
    ("6. Specification Number", "specificationS", "specificationNumber"),
    ("7. Code", "codeS", "customerCode"),
    ("8. Supplier", "suppliernameS", "supplierCode"),
    (
        "9. Customer Approval Verification",
        "customerapprovalS",
        "customerApprovalVerification",
    ),
    ("10. Certificate of Conformance Number", "cocnumberS", "certificationNumber"),
]


def split_items(value):
    if value is None:
        return []
    return [" ".join(p.split()) for p in str(value).split("`")]


def item_at(value, i):
    items = split_items(value)
    return items[i] if i < len(items) else ""


# --- How the QC row is turned into Form 2 rows -------------------------------------------------
# The QC columns hold one backtick-separated list each. Item 0 is the Material, items 1.. are the
# Processes. The QC "code" list (7`K`I`K`...) is NOT the Form 2 "7. Code" column: it is the suffix of
# the Specification Number (MS2003 + 7 -> "MS2003 - 7"), and "7. Code" is "N/A".
F2_SPEC = "6. Specification Number"
F2_CODE = "7. Code"
F2_NA_LABELS = (
    "7. Code",
    "8. Supplier",
    "9. Customer Approval Verification",
    "10. Certificate of Conformance Number",
)
JOIN_CODE_INTO_SPEC = True
NA_VALUE = "N/A"
# Specs starting with these prefixes are left out of Form 2 (e.g. TS1100, TS1300 are not shown).
# Set FORM2_SKIP_SPEC_PREFIXES= (empty) in .env to keep every item, or "TS,XX" for several.
SKIP_SPEC_PREFIXES = tuple(
    p.strip().upper()
    for p in str(env_str("FORM2_SKIP_SPEC_PREFIXES", "TS")).split(",")
    if p.strip()
)


# XA (main part / assembly): Form 2 has NO Material row, every item goes to Processes.
# Set FORM2_NO_MATERIAL_PREFIX= (empty) in .env to turn this rule off.
NO_MATERIAL_PREFIX = env_str("FORM2_NO_MATERIAL_PREFIX", "XA").upper()


def has_no_material(part_number):
    return bool(NO_MATERIAL_PREFIX) and str(
        part_number or ""
    ).strip().upper().startswith(NO_MATERIAL_PREFIX)


def build_items(raw, first_is_material=True):
    """One dict per Form 2 row (label -> value), in order.
    Normal part: items[0] = Material, items[1:] = Processes.
    XA part (first_is_material=False): there is no Material, so ALL items are Processes.
    """
    cols = QC_MATERIAL_COLUMNS
    total = max((len(split_items(raw[c])) for _, c, _cid in cols), default=0)
    items = []
    for i in range(total):
        vals = {label: item_at(raw[col], i) for label, col, _cid in cols}
        if not any(vals.values()):  # blank item from the trailing backtick
            continue
        spec, code = vals[F2_SPEC], vals[F2_CODE]
        # Normal part: the first item (items is still empty) is ALWAYS the Material, so the skip rule is
        # never applied to it; otherwise the next item would be promoted to Material by mistake.
        # XA part: there is no Material, so the skip rule (e.g. TS) applies to every item.
        if (
            (items or not first_is_material)
            and spec
            and SKIP_SPEC_PREFIXES
            and spec.upper().startswith(SKIP_SPEC_PREFIXES)
        ):
            print(
                f"  Item {i + 1} ({spec}) skipped: spec prefix is in FORM2_SKIP_SPEC_PREFIXES."
            )
            continue
        if JOIN_CODE_INTO_SPEC:
            if spec and code:
                vals[F2_SPEC] = f"{spec} - {code}"
            vals[F2_CODE] = ""
        for label in F2_NA_LABELS:
            if not vals[label]:
                vals[label] = "N/A"
        items.append(vals)
    return items


_QC_CACHE = {}


def get_qc_raw(lot_number):
    key = lot_number.strip()
    if key not in _QC_CACHE:
        _QC_CACHE[key] = _get_qc_raw(lot_number)
    return _QC_CACHE[key]


def _get_qc_raw(lot_number):
    cols = ", ".join(ident(c, "qc column") for _, c, _cid in QC_MATERIAL_COLUMNS)
    return run_sql(
        f"SELECT {cols} FROM {ident(QC_TABLE, 'QC_TABLE')} "
        f"WHERE TRIM({ident(COL_QC_LOT, 'COL_QC_LOT')}) = %s ORDER BY `cnt` DESC LIMIT 1",
        (lot_number.strip(),),
        fetch="one",
    )


def rows_of(ctx, root):
    return ctx.locator(f"{root} .ag-center-cols-container .ag-row")


def grid_row(ctx, root, idx):
    base = f"{root} .ag-center-cols-container .ag-row"
    by_attr = ctx.locator(f'{base}[row-index="{idx}"]')
    return by_attr.first if by_attr.count() else ctx.locator(base).nth(idx)


def cell_shows(cell):
    try:
        inp = cell.locator(
            "textarea, input:not([type=checkbox]):not([type=radio])"
        ).first
        if inp.count():
            v = (inp.input_value() or "").strip()
            if v:
                return v
    except Exception:
        pass
    try:
        return " ".join(cell.inner_text().split())
    except Exception:
        return ""


def same_text(a, b):
    return " ".join((a or "").split()).lower() == " ".join((b or "").split()).lower()


# Reads every cell of a row with ONE browser call (col-id -> text shown), instead of one call per cell.
_ROW_TEXT_JS = r"""(row) => {
  const out = {};
  for (const c of row.querySelectorAll('.ag-cell')) {
    const id = c.getAttribute('col-id');
    if (!id) continue;
    const inp = c.querySelector('textarea, input:not([type=checkbox]):not([type=radio])');
    let v = inp ? (inp.value || '').trim() : '';
    if (!v) v = (c.innerText || '').replace(/\s+/g, ' ').trim();
    out[id] = v;
  }
  return out;
}"""


def row_cell_texts(row):
    try:
        return row.evaluate(_ROW_TEXT_JS, timeout=3000) or {}
    except Exception:
        return {}


def dump_form2_section(ctx, tag, heading="Materials", stop="Processes"):
    try:
        html = ctx.evaluate(
            """([heading, stop]) => {
                const all = [...document.querySelectorAll('*')];
                const h = all.find(e => e.children.length === 0 && e.textContent.trim() === heading)
                       || all.find(e => e.textContent.trim() === heading);
                if (!h) return 'NO ' + heading + ' HEADING FOUND';
                let el = h;
                while (el.parentElement && !el.parentElement.innerText.includes(stop)) el = el.parentElement;
                return el.outerHTML;
            }""",
            [heading, stop],
        )
        save_html(f"debug_form2_{heading.lower()}_{tag}.html", html, 80000)
        popups = ctx.evaluate("""() => [...document.querySelectorAll(
                '.k-dialog, .k-window, .modal, [role=dialog], .ag-popup, kendo-popup, .k-popup')]
                .map(e => e.outerHTML).join('\\n\\n=====\\n\\n')""")
        if popups:
            save_html(f"debug_form2_popup_{tag}.html", popups)
    except Exception as e:
        print(f"  (could not save the {heading} section HTML: {type(e).__name__})")


def open_cell_editor(ctx, cell, inline, popup, label):
    page = ctx.page

    def current():
        for ed in (inline, popup):
            try:
                if ed.is_visible():
                    return ed
            except Exception:
                pass
        return None

    ed = current()
    if ed is not None:
        return ed
    for how in ("double-click", "forced double-click", "Enter key"):
        try:
            if how == "double-click":
                cell.dblclick(timeout=3000)
            elif how == "forced double-click":
                cell.dblclick(timeout=3000, force=True)
            else:
                cell.click(timeout=3000, force=True)
                page.keyboard.press("Enter")
        except Exception as e:
            print(f"    {label}: {how} failed ({type(e).__name__})")
        poll(page, lambda: current() is not None, 0.8, 50)
        ed = current()
        if ed is None:
            try:
                inline.wait_for(state="visible", timeout=1500)
                ed = inline
            except PWTimeout:
                pass
        if ed is not None:
            return ed
    raise RuntimeError(
        "could not open the cell editor "
        "(double-click, forced double-click and Enter all failed)"
    )


# The commit method that worked last time for each column (tried first on the next row).
_LAST_HOW = {}
# N/A cells: col-id -> "plain" (type + Tab works) or "full" (needs the normal method).
_NA_MODE = {}

# Paste (fast) or type key by key (fallback) into the grid cells.
# Set FORM2_PASTE=0 in .env to go back to typing every value key by key (the old behaviour).
_PASTE = {
    "mode": "type" if str(env_str("FORM2_PASTE", "1")).strip() == "0" else "paste",
    "ok": False,
    "probes": 2,
}


def fill_grid_cell(ctx, row, col_id, label, value):
    page = ctx.page
    cell = row.locator(f'.ag-cell[col-id="{col_id}"]').first
    cell.scroll_into_view_if_needed(timeout=5000)
    if same_text(cell_shows(cell), value):
        print(f"    {label}: already shows {value}, nothing typed.")
        return
    page.wait_for_timeout(50)
    inline = cell.locator(
        "input:not([type=checkbox]):not([type=radio]), textarea"
    ).first
    popup = ctx.locator(".ag-popup input:visible, .ag-popup textarea:visible").first
    opts = ctx.locator(
        ".ag-popup [role=option]:visible, kendo-popup [role=option]:visible, "
        "kendo-popup li.k-list-item:visible, .ag-rich-select-row:visible, .ag-popup li:visible"
    )
    use_row = ctx.get_by_text(re.compile(rf'Use\s*[\u201c"]?{re.escape(value)}'))
    real = opts.filter(has_text=value).filter(has_not_text=re.compile(r"^\s*Use\b"))

    def suggestions_shown():
        return opts.count() > 0 or (use_row.count() > 0 and use_row.first.is_visible())

    def type_value(editor):
        editor.click(timeout=3000)
        if _PASTE["mode"] == "paste":
            # Paste the whole value at once (fires the input event the suggestion list listens to).
            editor.fill(value)
            # wait up to 0.6 s, but stop as soon as a suggestion list or the 'Use "..."' row shows
            if poll(page, suggestions_shown, 0.6, 50):
                _PASTE["ok"] = True
                return
            # Nothing showed. Only the first few cells of the run are double-checked by typing the value
            # key by key; after that a cell with no suggestions is simply left as pasted (no retyping).
            if _PASTE["ok"] or _PASTE["probes"] <= 0:
                return
            _PASTE["probes"] -= 1
            editor.fill("")
            editor.press_sequentially(value, delay=8)
            if poll(page, suggestions_shown, 0.6, 50):
                _PASTE["mode"] = "type"
                print(
                    "    Paste did not open the suggestion list; typing is used from now on."
                )
            return
        editor.fill("")
        editor.press_sequentially(value, delay=8)
        poll(page, suggestions_shown, 0.6, 50)

    ed = open_cell_editor(ctx, cell, inline, popup, label)
    type_value(ed)
    texts = [" ".join(t.split())[:60] for t in opts.all_inner_texts()][:6]
    use_visible = bool(use_row.count() and use_row.first.is_visible())
    if texts:
        print(f"    {label}: options shown: {texts}")
    order = (
        ("ArrowDown+Enter", "list entry", "Use row", "Tab")
        if (texts or use_visible)
        else ("Tab", "Enter")
    )
    if use_visible and not real.count():
        # Nothing in the list matches the typed text (e.g. "MS2003 - 7" when the list only has "MS2003"),
        # so ArrowDown+Enter would pick the wrong entry and the value would be typed again.
        order = ("Use row", "Tab")
    pref = _LAST_HOW.get(col_id)
    if pref in order:
        order = (pref,) + tuple(h for h in order if h != pref)

    last_shown, last_how, dirty = "", "", False
    for how in order:
        try:
            if how == "list entry" and not real.count():
                continue
            if how == "Use row" and not use_visible:
                continue
            if dirty:
                ed = open_cell_editor(ctx, cell, inline, popup, label)
                type_value(ed)
                dirty = False

            if how == "ArrowDown+Enter":
                ed.press("ArrowDown")
                page.wait_for_timeout(200)
                ed.press("Enter")
            elif how == "list entry":
                real.first.click(timeout=3000)
            elif how == "Use row":
                use_row.first.click(timeout=3000)
            elif how == "Enter":
                ed.press("Enter")
            else:
                ed.press("Tab")
            dirty = True

            poll(page, lambda: value.lower() in cell_shows(cell).lower(), 0.8, 60)
            last_shown, last_how = cell_shows(cell), how
            if value.lower() in last_shown.lower():
                print(f"Filled {label}: {value} (via {how})")
                _LAST_HOW[col_id] = how
                return
            print(
                f"    {label}: {how} left the cell as {last_shown!r}, trying next way..."
            )
            page.keyboard.press("Escape")
        except Exception as e:
            print(f"    {label}: {how} failed ({type(e).__name__}), trying next way...")
            dirty = True

    dump_open_popup(ctx, page, f"form2_{col_id}")
    raise RuntimeError(
        f"typed {value!r} but the cell shows {last_shown!r} (last try: {last_how})"
    )


def fill_na_cell(ctx, row, col_id, label, value):
    """N/A is plain text, so it does not need the suggestion-list handling: type it and press Tab once.
    If that does not work for a column, that column uses the normal method from then on.
    """
    page = ctx.page
    cell = row.locator(f'.ag-cell[col-id="{col_id}"]').first
    cell.scroll_into_view_if_needed(timeout=5000)
    if same_text(cell_shows(cell), value):
        print(f"    {label}: already shows {value}, nothing typed.")
        return
    if _NA_MODE.get(col_id) != "full":
        inline = cell.locator(
            "input:not([type=checkbox]):not([type=radio]), textarea"
        ).first
        popup = ctx.locator(".ag-popup input:visible, .ag-popup textarea:visible").first
        try:
            ed = open_cell_editor(ctx, cell, inline, popup, label)
            ed.click(timeout=3000)
            ed.fill(value)
            ed.press("Tab")
            if poll(page, lambda: value.lower() in cell_shows(cell).lower(), 0.8, 60):
                _NA_MODE[col_id] = "plain"
                print(f"Filled {label}: {value} (type + Tab)")
                return
            print(
                f"    {label}: type + Tab left the cell as {cell_shows(cell)!r}; "
                "the normal method is used for this column from now on."
            )
        except Exception as e:
            print(
                f"    {label}: type + Tab failed ({type(e).__name__}); "
                "the normal method is used for this column from now on."
            )
            press_escape(page)
        _NA_MODE[col_id] = "full"
    fill_grid_cell(ctx, row, col_id, label, value)


def fill_cell(ctx, row, col_id, label, value):
    if value == NA_VALUE:
        fill_na_cell(ctx, row, col_id, label, value)
    else:
        fill_grid_cell(ctx, row, col_id, label, value)


def fill_row(ctx, row, cells, heading, stop, tag):
    page = ctx.page
    failed = []
    for label, value, col_id in cells:
        if not value:
            continue
        if not col_id:
            failed.append(label)
            print(f"  No column id found for {label}.")
            continue
        t_cell = time.time()
        try:
            fill_cell(ctx, row, col_id, label, value)
            print(f"    [time] {label}: {time.time() - t_cell:.1f}s")
        except Exception as e:
            failed.append(label)
            print(
                f"  Could not set {label}: {type(e).__name__}: {' | '.join(str(e).splitlines()[:3])[:400]}"
            )
            if DEBUG_FORM2:
                dump_form2_section(ctx, f"fail_{tag}_{col_id}", heading, stop)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    return failed


def find_add_button(ctx, root, name):
    add = ctx.locator(f"{root} ni-grid-add-button button.popup-btn")
    if add.count() == 0:
        add = ctx.get_by_role("button", name=re.compile(name, re.I))
    return add.first


def add_grid_row(ctx, rows, add, dump_args, error):
    page = ctx.page
    before = rows.count()
    add.click(timeout=T(5000))
    if not poll(page, lambda: rows.count() > before, 10, 80):
        dump_form2_section(ctx, *dump_args)
        raise RuntimeError(error)
    return before


def press_escape(page):
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


_CERT_NAMES = {"names": None}


def cert_names(refresh=False):
    """File list of CERT_FOLDER, read once per run (listing a network share for every row was slow).
    A miss triggers one fresh listing, so PDFs added while the script runs are still found.
    """
    if refresh or _CERT_NAMES["names"] is None:
        _CERT_NAMES["names"] = os.listdir(Path(CERT_FOLDER))
    return _CERT_NAMES["names"]


def find_cert_pdf(spec, cert_no):
    base = re.sub(r"\.pdf$", "", (cert_no or "").strip(), flags=re.I)
    if not base or base.upper() == "N/A":
        return None
    folder = Path(CERT_FOLDER)
    low = base.lower()
    for refresh in (False, True):
        try:
            names = cert_names(refresh)
        except OSError as e:
            print(f"    Cannot read {CERT_FOLDER}: {e}")
            return None
        exact = [n for n in names if n.lower() == f"{low}.pdf"]
        tail = [n for n in names if n.lower().endswith(f"_{low}.pdf")]
        hits = (
            (tail + exact) if (spec or "").upper().startswith("MS") else (exact + tail)
        )
        if hits:
            return folder / hits[0]
    return None


def row_index_of(ctx, root, row):
    attr = row.get_attribute("row-index")
    if attr is not None and attr.strip().isdigit():
        return int(attr)
    all_rows = rows_of(ctx, root)
    for i in range(all_rows.count()):
        try:
            if all_rows.nth(i).evaluate("(a, b) => a === b", row.element_handle()):
                return i
        except Exception:
            pass
    return max(all_rows.count() - 1, 0)


def attach_cert(ctx, root, row, spec, cert_no, label):
    if not cert_no or cert_no.strip().upper() == "N/A":
        return False
    pdf = find_cert_pdf(spec, cert_no)
    if pdf is None:
        print(f"    {label}: no PDF found in {CERT_FOLDER} for {cert_no!r} ({spec}).")
        return False
    if pdf.name.lower() in ATTACHED_PDFS:
        print(
            f"    {label}: {pdf.name} is already used by an earlier row; "
            "leaving this row's Reference Document empty."
        )
        return False
    try:
        inp = row.locator("input.value-container")
        if inp.count() == 0:
            every = ctx.locator(f"{root} input.value-container")
            if every.count() == 0:
                raise RuntimeError("no input.value-container found for this row")
            idx = int(row.get_attribute("row-index") or 0)
            inp = every.nth(min(idx, every.count() - 1))
        inp = inp.first
        inp.scroll_into_view_if_needed(timeout=5000)
        inp.click(timeout=3000)
        inp.fill(pdf.name)
        inp.press("Tab")
        if inp.input_value() != pdf.name:
            raise RuntimeError(f"box shows {inp.input_value()!r} (maxlength 100?)")
        ATTACHED_PDFS.add(pdf.name.lower())
        print(f"    {label}: file name typed: {pdf.name}")
        return True
    except Exception as e:
        print(
            f"    {label}: could not type file name: {type(e).__name__}: {str(e).splitlines()[0][:200]}"
        )
        return False


def fill_form2_materials(page, part_number, lot_number):
    if has_no_material(part_number):
        print(
            f"  {part_number} starts with {NO_MATERIAL_PREFIX}: no Materials row on Form 2 "
            "(all items go to Processes)."
        )
        return "FORM2_MATERIALS_SKIPPED"
    if not lot_number:
        print("  No lot number for this part: Form 2 Materials skipped.")
        return "FORM2_MATERIALS_SKIPPED"
    raw = get_qc_raw(lot_number)
    if not raw:
        print(
            f"  No row in {QC_TABLE} for lot {lot_number!r}: Form 2 Materials skipped."
        )
        return "FORM2_MATERIALS_SKIPPED"
    items = build_items(raw)
    if not items:
        print(f"  Lot {lot_number!r} has no usable items: Form 2 Materials skipped.")
        return "FORM2_MATERIALS_SKIPPED"
    data = items[0]
    print(f"  Form 2 Materials values (lot {lot_number}):")
    for label, val in data.items():
        print(f"    {label}: {val!r}")

    ctx = find_frame_with_text(page, "ADD MATERIAL", timeout=patient(20))
    if ctx is None:
        raise RuntimeError("The Materials section of Form 2 was not found.")

    rows = rows_of(ctx, MATERIAL_ROOT)
    if DEBUG_FORM2:
        print_form_controls(ctx)
        dump_form2_section(ctx, "before_add")
        print(f"  Materials data rows found before adding: {rows.count()}")
        if rows.count():
            try:
                save_html(
                    "debug_form2_material_row.html",
                    rows.first.evaluate("e => e.outerHTML"),
                    20000,
                )
            except Exception as e:
                print(f"  (could not save the row HTML: {type(e).__name__})")

    if rows.count() and FORM2_OVERWRITE_FIRST_ROW:
        row = rows.first
        print(
            f"  A Materials row already exists and will be overwritten: {cell_shows(row)!r}"
        )
    else:
        add = find_add_button(ctx, MATERIAL_ROOT, "add material")
        add_grid_row(
            ctx,
            rows,
            add,
            ("after_add",),
            f"No new Materials row appeared after + ADD MATERIAL (rows seen: {rows.count()}). "
            "See screenshots/debug_form2_materials_after_add.html",
        )
        print("  Clicked + ADD MATERIAL.")
        row = rows.last

    cells = [
        (label, data[label], col_id) for label, _col, col_id in QC_MATERIAL_COLUMNS
    ]
    failed = fill_row(ctx, row, cells, "Materials", "Processes", "materials")
    # attach_cert(
    #     ctx,
    #     MATERIAL_ROOT,
    #     row,
    #     data[F2_SPEC],
    #     data["10. Certificate of Conformance Number"],
    #     "Materials",
    # )
    press_escape(page)
    save_screenshot(page, f"form2_materials_{part_number}")
    if failed:
        print(
            f"Form 2 Materials NOT set: {', '.join(failed)}. Check the page (try DEBUG_FORM2=true)."
        )
        return "FORM2_MATERIALS_PARTIAL"
    print("Form 2 Materials row filled.")
    return "FORM2_MATERIALS_FILLED"


def header_col_ids(ctx, root):
    out = {}
    hdrs = ctx.locator(f"{root} .ag-header-cell")
    for i in range(hdrs.count()):
        h = hdrs.nth(i)
        m = re.match(r"\s*(\d+)\.", " ".join(h.inner_text().split()))
        cid = h.get_attribute("col-id")
        if m and cid:
            out[m.group(1)] = cid
    return out


def verify_process_cells(ctx, expected):
    page = ctx.page
    for rnd in (1, 2, 3):
        bad = []
        for idx, cells in expected:
            row = grid_row(ctx, PROCESS_ROOT, idx)
            texts = row_cell_texts(row)  # one browser call per row
            for lab, val, col_id in cells:
                if not val or not col_id:
                    continue
                shown = texts.get(col_id, "")
                if val.lower() not in shown.lower():
                    bad.append((idx, lab, val, col_id, shown))
        if not bad:
            print("  Check: every Processes cell shows the expected value.")
            return []
        print(f"  Check round {rnd}: {len(bad)} Processes cell(s) are missing a value:")
        for idx, lab, val, col_id, shown in bad:
            print(f"    row {idx + 1} {lab}: shows {shown!r}, expected {val!r}")
        still_bad = [f"{lab}@row{idx + 1}" for idx, lab, _v, _c, _s in bad]
        if rnd == 3:
            return still_bad
        refilled = 0
        for idx, lab, val, col_id, shown in bad:
            if val == NA_VALUE and rnd > 1:
                continue  # N/A was already retyped once; do not keep retyping it
            try:
                fill_cell(ctx, grid_row(ctx, PROCESS_ROOT, idx), col_id, lab, val)
                refilled += 1
            except Exception as e:
                print(
                    f"    could not refill row {idx + 1} {lab}: {type(e).__name__}: "
                    f"{' | '.join(str(e).splitlines()[:2])[:200]}"
                )
                press_escape(page)
        if not refilled:
            return still_bad
        settle(page)
        page.wait_for_timeout(500)
    return []


ADD_SLIDER_MAX = 10  # the "Number of processes to Add" slider goes from 1 to 10


def _open_add_count_popup(ctx, root):
    # The small arrow on the right of the + ADD button opens "Number of processes to Add".
    host = ctx.locator(f"{root} ni-grid-add-button").first
    main = ctx.locator(f"{root} ni-grid-add-button button.popup-btn").first
    btns = ctx.locator(f"{root} ni-grid-add-button button")
    for i in range(btns.count()):
        b = btns.nth(i)
        cls = b.get_attribute("class") or ""
        if "popup-btn" not in cls:
            b.click(timeout=T(4000))
            return
    # No separate arrow button found: click the right edge of the add button (where the arrow is).
    box = host.bounding_box(timeout=3000)
    if not box:
        raise RuntimeError("the arrow next to the add button was not found")
    ctx.page.mouse.click(box["x"] + box["width"] - 12, box["y"] + box["height"] / 2)


def _set_slider(ctx, n):
    page = ctx.page
    native = ctx.locator('input[type="range"]:visible')
    if native.count():
        sl = native.first
        sl.evaluate(
            "(e, v) => { e.value = v; e.dispatchEvent(new Event('input', {bubbles: true}));"
            " e.dispatchEvent(new Event('change', {bubbles: true})); }",
            str(n),
        )
        return str(sl.input_value()).strip() == str(n)
    handle = ctx.locator('[role="slider"]:visible').first
    if handle.count() == 0:
        handle = ctx.locator(".k-draghandle:visible").first
    if handle.count() == 0:
        raise RuntimeError("the number slider was not found")
    handle.focus()
    page.keyboard.press("Home")
    for _ in range(n - 1):
        page.keyboard.press("ArrowRight")
    now = (handle.get_attribute("aria-valuenow") or "").strip()
    return now == str(n)


def add_grid_rows(ctx, rows, root, name, n, dump_args):
    # Add `n` rows with as few clicks as possible: set the slider to the number of rows (up to 10 at a
    # time), click the add button once, and check that the rows appeared. If the slider cannot be used,
    # or fewer rows appeared, the missing rows are added one click at a time (the old way).
    # Returns the index of the first new row.
    page = ctx.page
    first = rows.count()
    target = first + n
    while rows.count() < target:
        before = rows.count()
        chunk = min(target - before, ADD_SLIDER_MAX)
        if chunk > 1:
            try:
                _open_add_count_popup(ctx, root)
                page.wait_for_timeout(300)
                ok = _set_slider(ctx, chunk)
                add = find_add_button(ctx, root, name)
                add.click(timeout=T(5000))
                if not poll(page, lambda: rows.count() > before, 3, 80):
                    add.click(
                        timeout=T(5000)
                    )  # the first click may only have closed the popup
                poll(page, lambda: rows.count() >= before + chunk, 8, 80)
                print(
                    f"  Slider set to {chunk}: {rows.count() - before} row(s) added with one click "
                    f"(slider shows the right number: {ok})."
                )
            except Exception as e:
                print(
                    f"  Could not use the number slider ({type(e).__name__}: "
                    f"{str(e).splitlines()[0][:150] if str(e) else ''}); adding one by one."
                )
                try:
                    dump_form2_section(ctx, "slider", *dump_args)
                except Exception:
                    pass
            press_escape(page)
        if rows.count() == before:
            # one row with a normal click of the add button
            add = find_add_button(ctx, root, name)
            add_grid_row(
                ctx,
                rows,
                add,
                (f"after_add_{rows.count() + 1}", *dump_args),
                f"No new row appeared after clicking add ({rows.count() - first} of {n} added).",
            )
    if rows.count() > target:
        print(
            f"  WARNING: {rows.count() - target} more row(s) were added than needed; delete the extra row(s)."
        )
    return first


def fill_form2_processes(page, part_number, lot_number):
    if not lot_number:
        print("  No lot number: Processes skipped.")
        return "FORM2_PROCESSES_SKIPPED"
    raw = get_qc_raw(lot_number)
    if not raw:
        print(f"  No row in {QC_TABLE} for lot {lot_number!r}: Processes skipped.")
        return "FORM2_PROCESSES_SKIPPED"

    cols = QC_MATERIAL_COLUMNS
    if has_no_material(part_number):
        procs = build_items(
            raw, first_is_material=False
        )  # XA: no Material, all are Processes
    else:
        procs = build_items(raw)[1:]  # item 0 is the Material
    if not procs:
        if has_no_material(part_number):
            print("  No usable items for this XA part: no Processes to add.")
        else:
            print("  Only the Materials item exists: no Processes to add.")
        return "FORM2_PROCESSES_SKIPPED"
    print(f"  {len(procs)} Process row(s) to add.")
    for k, item in enumerate(procs, 1):
        print(f"    #{k}: " + " | ".join(item[lab] or "-" for lab, _c, _cid in cols))

    ctx = find_frame_with_text(page, "ADD PROCESS", timeout=patient(20))
    if ctx is None:
        raise RuntimeError("The Processes section of Form 2 was not found.")
    ids = header_col_ids(ctx, PROCESS_ROOT)
    print(f"  Processes col-ids from the header: {ids}")
    if not ids:
        dump_form2_section(ctx, "header", "Processes", "Inspections")
        raise RuntimeError(
            f"Could not read the Processes column ids (is the root tag {PROCESS_ROOT!r}?). "
            "See screenshots/debug_form2_processes_header.html"
        )

    rows = rows_of(ctx, PROCESS_ROOT)
    failed, expected = [], []
    # Work out all the Processes rows first, so the right number of rows can be added at once.
    todo = []
    for pos, item in enumerate(procs, 1):
        values = [
            (lab, item[lab], ids.get(lab.split(".")[0])) for lab, _c, _cid in cols
        ]
        todo.append((pos, values))
    print(f"  Adding {len(todo)} Processes row(s)...")
    _STAGE["now"] = "Processes add rows"
    first_idx = add_grid_rows(
        ctx,
        rows,
        PROCESS_ROOT,
        "add process",
        len(todo),
        ("Processes", "Inspections"),
    )
    for k, (pos, values) in enumerate(todo):
        before = first_idx + k
        row = grid_row(ctx, PROCESS_ROOT, before)
        expected.append((before, values))
        _STAGE["now"] = f"Process row {pos}"
        print(f"  Process row {pos}:")
        failed += [
            f"{lab}@{pos}"
            for lab in fill_row(ctx, row, values, "Processes", "Inspections", pos)
        ]
        # attach_cert(
        #     ctx, PROCESS_ROOT, row, values[1][1], values[5][1], f"Process {pos}"
        # )
        page.wait_for_timeout(150)

    failed = verify_process_cells(ctx, expected)
    press_escape(page)
    save_screenshot(page, f"form2_processes_{part_number}")
    if failed:
        print(
            f"Processes NOT set: {', '.join(failed)}. Check the page (DEBUG_FORM2=true saves the HTML)."
        )
        return "FORM2_PROCESSES_PARTIAL"
    print("Processes rows filled.")
    return "FORM2_PROCESSES_FILLED"


def resolve_form2_lot(part):
    """Lot used for Form 2. Stops if FORM2_LOT_OVERRIDE belongs to a different lot than the part."""
    own = str(part.lot or "").strip()
    override = str(FORM2_LOT_OVERRIDE or "").strip()
    if not override:
        print(f"  Form 2 lot: {own!r} (the part's own lot)")
        return own
    if own and override != own:
        if os.getenv("FORM2_ALLOW_LOT_MISMATCH", "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            print(
                f"  WARNING: FORM2_LOT_OVERRIDE={override!r} differs from the part's lot {own!r} "
                "(allowed by FORM2_ALLOW_LOT_MISMATCH)."
            )
            return override
        raise RuntimeError(
            f"FORM2_LOT_OVERRIDE={override!r} does not match the lot of part {part.number} ({own!r}). "
            "Form 2 would be filled with another lot's data. Clear FORM2_LOT_OVERRIDE in .env, "
            "or set FORM2_ALLOW_LOT_MISMATCH=true if this is intended."
        )
    print(f"  Form 2 lot: {override!r} (override, matches the part)")
    return override


_STAGE = {"now": "starting"}


def watch_saves(page):
    """Prints every POST/PUT/PATCH sent to net-inspect.com with the script stage it happened in,
    so we can see what is really triggering a save."""

    def on_request(req):
        try:
            if req.method in ("POST", "PUT", "PATCH") and "net-inspect.com" in req.url:
                print(
                    f"    [net] {req.method} {req.url.split('?')[0][-90:]}  <- during: {_STAGE['now']}"
                )
        except Exception:
            pass

    page.on("request", on_request)
    return on_request


def _remove_watcher(page, watcher):
    try:
        page.remove_listener("request", watcher)
    except Exception:
        pass


_SAVED = ("FAIR_SAVED", "FAIR_SAVED_UNVERIFIED")


def run_form2(page, part, status):
    lot = resolve_form2_lot(part)
    _watcher = watch_saves(page)
    mat = proc = None
    ATTACHED_PDFS.clear()
    if FILL_FORM2_MATERIALS:
        _STAGE["now"] = "Materials fill"
        with timed("Form 2 Materials fill"):
            mat = fill_form2_materials(page, part.number, lot)
        print(f"  Materials result: {mat}")
    if FILL_FORM2_PROCESSES:
        _STAGE["now"] = "Processes fill"
        with timed("Form 2 Processes fill"):
            proc = fill_form2_processes(page, part.number, lot)
        print(f"  Processes result: {proc}")
    else:
        print(
            "  Processes NOT filled: FILL_FORM2_PROCESSES is off. Add FILL_FORM2_PROCESSES=true to .env."
        )
    status = proc or mat or status

    if mat or proc:
        partial = any(r and r.endswith("PARTIAL") for r in (mat, proc))
        if partial:
            print(
                "  Some Form 2 cells failed. Saving what was filled anyway; check the page afterwards."
            )

        # Save 1: after the Form 2 writes.
        print("  Clicking SAVE FAIR after the Form 2 writes...")
        _STAGE["now"] = "SAVE FAIR #1"
        with timed("SAVE FAIR #1"):
            save_status = save_fair(page, part.number)
        print(f"  Save after writes: {save_status}")
        if save_status not in _SAVED:
            status = "FORM2_SAVE_FAILED"
            print(
                "  Form 2 was NOT saved, so Form 3 is not opened (nothing is lost; check the page)."
            )
            _remove_watcher(page, _watcher)
            return status
        status = "FORM2_SAVED_PARTIAL" if partial else "FORM2_SAVED"
    _remove_watcher(page, _watcher)
    # Do not click the Form 3 tab while Form 2 is unsaved or saving.
    from form1 import wait_ready_to_leave

    if not wait_ready_to_leave(page, "Form 2 -> Form 3"):
        print("  Form 3 NOT opened: Form 2 is still saving/uploading (check the page).")
        return "FORM2_NOT_READY"
    import form3

    return form3.run_form3(page, part, status)
