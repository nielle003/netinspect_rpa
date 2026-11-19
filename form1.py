import re
import time

from playwright.sync_api import TimeoutError as PWTimeout

from login import (
    AUTO_REQUEST_YES,
    CLICK_CREATE_FAIR,
    CUSTOMER,
    DEBUG_FIELDS,
    DIVISION,
    FAIR_DETAIL_ASSEMBLY,
    FAIR_FULL_PARTIAL,
    FAIR_FULL_PARTIAL_REASON,
    FAIR_NONCONFORMANCE,
    FAIR_PROGRAM,
    FAIR_SERIAL_NUMBER,
    FAIR_SUPPLIER_CODE,
    FILL_CUSTOMER_PART,
    FILL_PART_REVISION,
    FILL_PO_NUMBER,
    PART_CHECK_SECONDS,
    SAVE_ERROR_WORDS,
    SAVE_SUCCESS_TEXT,
    SHOW_REQUEST_DIALOG,
    SUBMIT_REQUEST_FORM,
    T,
    any_frame_shows,
    ask,
    dump_open_popup,
    env_str,
    error_shown,
    find_frame_with_text,
    patient,
    poll,
    print_form_controls,
    safe_name,
    save_html,
    save_screenshot,
    settle,
    wait_enabled,
    get_conn,
)

from notify_not_registered import send_notification

DIALOG_TEXT = "is not set up within"
# New Vincent Code Start: November 4, 2026
# After SAVE FAIR is clicked, wait this many seconds before going on, so the save is fully done.
# Change it with SAVE_FAIR_DELAY in .env (0 = no extra wait).
try:
    SAVE_FAIR_DELAY = max(0.0, float(env_str("SAVE_FAIR_DELAY", "5")))
except ValueError:
    SAVE_FAIR_DELAY = 5.0
# New Vincent Code End: November 4, 2026
NOT_AT_CUSTOMER_MARKERS = ("does not exist at customer", DIALOG_TEXT)
ALREADY_REQUESTED = "has already been requested"
SUBMIT_NAMES = r"(?i)(submit|send|send request|submit request|request|request part|send part request)"


# New Vincent Code Start: November 13, 2026
# Set to True once CREATE FAIR was clicked for the current part. login.main() reads it before restarting a
# part from the start (a restart after CREATE FAIR could create a second FAIR).
CREATE_STATE = {"clicked": False}
# New Vincent Code End: November 13, 2026


def click_nav(ctx, label, roles=("link",), timeout=15000):
    pat = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    loc = None
    for role in roles:
        loc = ctx.get_by_role(role, name=pat)
        if loc.count():
            break
    else:
        loc = ctx.get_by_text(pat)
    loc.first.click(timeout=T(timeout))


def go_to_fai(page):
    click_nav(page, "FAI")
    settle(page)
    print(f"On FAI tab. Current page: {page.url}")


def open_create_fair_tab(page):
    click_nav(page, "Create FAIR")
    settle(page)
    print(f"On Create FAIR tab. Current page: {page.url}")


def fill_field(ctx, label, placeholder, value):
    # The page wording changed ("Form 1, Field 1" placeholders are gone), so try several ways.
    candidates = [
        ctx.get_by_label(label, exact=True),
        ctx.get_by_label(label),
        ctx.get_by_placeholder(placeholder),
        ctx.get_by_placeholder(label, exact=True),
        ctx.locator(
            f'xpath=(//label[starts-with(normalize-space(.), "{label}")])[1]/following::input[1]'
        ),
        ctx.locator(
            f'xpath=(//*[starts-with(normalize-space(text()[1]), "{label}")])[1]/following::input[1]'
        ),
    ]
    loc = next((c for c in candidates if c.count() > 0), candidates[-1])
    loc.first.fill(value, timeout=T(5000))
    print(f"Filled {label}: {value}")
    return loc.first


def fill_after_label(ctx, label, value):
    loc = ctx.get_by_label(label, exact=True)
    if loc.count() == 0:
        loc = ctx.locator(
            f'xpath=(//*[normalize-space(text())="{label}"])[last()]/following::input[1]'
        )
    loc.last.fill(value, timeout=T(5000))
    print(f"Filled request form {label}: {value}")


def not_at_customer_shown(page):
    return any(any_frame_shows(page, t) for t in NOT_AT_CUSTOMER_MARKERS)


def part_exists_at_customer(page, box):
    box.press("Tab")
    # Let the part-number validation request finish, then give the "not set up" dialog a short
    # grace period. (Before: always waited a flat 8 s whenever the part DID exist.)
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except PWTimeout:
        pass
    return not poll(page, lambda: not_at_customer_shown(page), PART_CHECK_SECONDS, 150)


LABEL_ALIASES = {"FAIR Program": ("FAIR Program", "Program")}


def find_dropdown(ctx, label_text, fallback_index):
    names = LABEL_ALIASES.get(label_text, (label_text,))
    cond = " or ".join(f'starts-with(normalize-space(text()[1]), "{n}")' for n in names)
    dd = ctx.locator(f"xpath=(//*[{cond}]/following::kendo-dropdownlist[1])")
    return (
        dd.first
        if dd.count()
        else ctx.locator("kendo-dropdownlist").nth(fallback_index)
    )


def dropdown_enabled(dd):
    return (
        "k-disabled" not in (dd.get_attribute("class") or "")
        and dd.get_attribute("aria-disabled") != "true"
    )


def select_kendo_option(ctx, dd, label, option_text):
    last = ""
    for attempt in range(1, 4):
        try:
            dd.click(timeout=T(5000))
            items = ctx.locator("li.k-item:visible, [role=option]:visible")
            items.first.wait_for(timeout=T(8000))
            filt = ctx.locator(".k-list-filter input:visible")
            if filt.count():
                filt.first.fill(option_text)
                ctx.wait_for_timeout(400)
            exact = items.filter(
                has_text=re.compile(rf"^\s*{re.escape(option_text)}\s*$")
            )
            (
                exact if exact.count() else items.filter(has_text=option_text)
            ).first.click(timeout=T(5000))
        except Exception as e:
            last = f"(could not pick: {type(e).__name__})"
        for _ in range(40):
            shown = dd.inner_text().strip()
            if option_text in shown:
                print(f"Selected {label}: {shown}")
                return
            last = shown
            ctx.wait_for_timeout(125)
        print(f"  {label}: attempt {attempt} shows {last!r}, retrying...")
        try:
            dd.press("Escape")
        except Exception:
            pass
        ctx.wait_for_timeout(800)
    raise RuntimeError(f"{label}: expected {option_text!r} but the box shows {last!r}")


def choose_option(ctx, label_text, option_text, fallback_index):
    dd = find_dropdown(ctx, label_text, fallback_index)
    for _ in range(patient(20) * 4):
        if dropdown_enabled(dd):
            break
        ctx.wait_for_timeout(250)
    else:
        raise RuntimeError(f"{label_text}: dropdown stayed disabled")
    select_kendo_option(ctx, dd, label_text, option_text)


def request_form_open(rctx):
    try:
        return rctx.get_by_text("Supplier Contact Email").last.is_visible()
    except Exception:
        return False


def find_submit_button(rctx):
    lab = rctx.locator('label[for="partNumberRequest"]').filter(
        has_text=re.compile(r"^\s*submit\s*$", re.I)
    )
    if lab.count():
        return lab.last
    btns = rctx.locator(
        'xpath=(//*[normalize-space(text())="Comments"])[last()]'
        '/following::*[self::button or self::input[@type="submit" or @type="button"]]'
    )
    seen = []
    for i in range(btns.count()):
        b = btns.nth(i)
        try:
            if not b.is_visible():
                continue
            name = " ".join((b.inner_text() or b.get_attribute("value") or "").split())
        except Exception:
            continue
        seen.append(name)
        if re.fullmatch(SUBMIT_NAMES, name):
            return b
    alt = rctx.get_by_role(
        "button", name=re.compile(r"^\s*(submit|send|send request)\s*$", re.I)
    )
    if alt.count():
        return alt.last
    raise RuntimeError(
        f"Could not find the submit button of the request form. Buttons seen: {seen}"
    )


def submit_request_form(page, rctx):
    find_submit_button(rctx).click(timeout=T(5000))
    print("Clicked 'Submit' on the request form.")

    deadline = time.time() + patient(15)
    while time.time() < deadline:
        if any_frame_shows(page, ALREADY_REQUESTED):
            print(
                "Net-Inspect says this part was ALREADY REQUESTED and is Pending. No new request was created."
            )
            return "ALREADY_PENDING"
        if not request_form_open(rctx):
            page.wait_for_timeout(1000)
            if any_frame_shows(page, ALREADY_REQUESTED):
                print(
                    "Net-Inspect says this part was ALREADY REQUESTED and is Pending."
                )
                return "ALREADY_PENDING"
            if error_shown(page):
                print(
                    "The form closed but an error message is showing. Treating it as UNCONFIRMED."
                )
                save_screenshot(page, "submit_error")
                return "UNCONFIRMED"
            print(
                "Request form closed: the part request looks submitted "
                "(inferred from the form closing; confirm in Net-Inspect if unsure)."
            )
            return "SUBMITTED"
        page.wait_for_timeout(250)
    print("The form is still open after submitting. Check the page for a message.")
    save_screenshot(page, "submit_unconfirmed")
    return "UNCONFIRMED"


def fill_request_form(page, part):
    rctx = find_frame_with_text(page, "Supplier Contact Email", timeout=patient(15))
    if rctx is None:
        raise RuntimeError("The Request Part form did not open after YES.")
    fill_after_label(rctx, "Part Name", part.name)
    if part.po:
        fill_after_label(rctx, "P.O. Number", part.po)

    label = '(//*[normalize-space(text())="Division"])[last()]'
    dd = rctx.locator(f"xpath={label}/following::kendo-dropdownlist[1]")
    if dd.count():
        select_kendo_option(rctx, dd.first, "request form Division", DIVISION)
    else:
        rctx.locator(f"xpath={label}/following::select[1]").select_option(
            label=DIVISION, timeout=T(5000)
        )
        print(f"Selected request form Division: {DIVISION}")
    print("Request form filled (Comments left empty).")
    if not SUBMIT_REQUEST_FORM:
        print("SUBMIT_REQUEST_FORM is off: NOT submitting the request form.")
        return "NOT_SUBMITTED"
    return submit_request_form(page, rctx)


def click_dialog_button(dlg, name):
    dlg.get_by_role("button", name=re.compile(rf"^\s*{name}\s*$", re.I)).first.click(
        timeout=T(5000)
    )


def run_request_dialog(page, box, part):
    dlg = find_frame_with_text(page, DIALOG_TEXT, timeout=1)
    if dlg is None:
        box.focus()
        box.press("Enter")
        dlg = find_frame_with_text(page, DIALOG_TEXT, timeout=8)
    if dlg is None:
        print("Pressed Enter, but the Request Part dialog did not appear.")
        return "NOT_AT_CUSTOMER"

    print("REQUEST PART dialog is showing.")
    answer = (
        "YES"
        if AUTO_REQUEST_YES
        else ask(
            f"Type YES to open the part request for '{part.number}' (anything else = cancel): "
        ).upper()
    )
    if answer != "YES":
        click_dialog_button(dlg, "cancel")
        print("Cancelled the dialog: no request sent.")
        return "NOT_AT_CUSTOMER"

    click_dialog_button(dlg, "yes")
    try:
        dlg.get_by_text(DIALOG_TEXT).first.wait_for(state="hidden", timeout=T(15000))
    except PWTimeout:
        pass
    print("Clicked YES: the Request Part form opened.")
    return {
        "SUBMITTED": "REQUEST_SUBMITTED",
        "ALREADY_PENDING": "REQUEST_ALREADY_PENDING",
        "UNCONFIRMED": "REQUEST_UNCONFIRMED",
        "NOT_SUBMITTED": "REQUEST_FORM_FILLED",
    }[fill_request_form(page, part)]


def handle_unknown_part(page, ctx, box, part):
    print(f"NOT AT CUSTOMER: '{part.number}' does not exist at the customer.")
    status = (
        run_request_dialog(page, box, part)
        if SHOW_REQUEST_DIALOG
        else "NOT_AT_CUSTOMER"
    )
    if status == "NOT_AT_CUSTOMER":
        try:
            fill_field(ctx, "Part Name", "Form 1, Field 2", part.name)
        except Exception as e:
            print(f"Could not fill Part Name ({type(e).__name__}).")
    #nielle added notfication
    notification = send_notification(get_conn(), part)
    if notification:
        print("Notification sent successfully.")
    else:
        print("Notification not sent.")
    print("CREATE FAIR not clicked (the part is not at the customer).")
    return status


def find_create_fair_button(ctx):
    btn = ctx.get_by_role("button", name=re.compile(r"^\s*create\s*fair\s*$", re.I))
    if btn.count() == 0:
        btn = ctx.locator("button, label.btn, a.btn").filter(
            has_text=re.compile(r"create\s*fair", re.I)
        )
    if btn.count() == 0:
        btn = ctx.locator('input[value*="create fair" i]')
    if btn.count() == 0:
        raise RuntimeError("The CREATE FAIR button was not found on the form.")
    return btn.last


def click_create_fair(page, ctx, part_number):
    target = find_create_fair_button(ctx)
    if not wait_enabled(page, target, patient(10)):
        raise RuntimeError("The CREATE FAIR button stayed disabled (form incomplete?).")

    url_before = page.url
    CREATE_STATE["clicked"] = True  # New Vincent Code: November 13, 2026
    target.click(timeout=T(5000))
    print("Clicked CREATE FAIR.")

    deadline = time.time() + patient(20)
    while time.time() < deadline:
        page.wait_for_timeout(300)
        if page.url != url_before or not any_frame_shows(
            page, "Select AS9102 Revision"
        ):
            settle(page)
            print(f"CREATE FAIR went through. Page is now: {page.url}")
            save_screenshot(page, f"fair_created_{part_number}")
            return "FAIR_CREATED"
        if error_shown(page):
            break
    print("CREATE FAIR was clicked but the page did not change. Check Net-Inspect.")
    save_screenshot(page, f"fair_unconfirmed_{part_number}")
    return "FAIR_UNCONFIRMED"


def fill_by_control(ctx, control, value):
    loc = ctx.locator(
        f'input[formcontrolname="{control}"], textarea[formcontrolname="{control}"]'
    ).first
    loc.fill(value, timeout=10000)
    got = loc.input_value()
    if got != value:
        raise RuntimeError(f"{control}: typed {value!r} but the box shows {got!r}")
    print(f"Filled {control}: {value}")


def click_radio(ctx, text):
    loc = ctx.get_by_label(text, exact=True)
    if loc.count() == 0:
        loc = ctx.get_by_text(text, exact=True)
    el = loc.first
    try:
        el.check(timeout=5000)
    except Exception:
        el.click(timeout=5000)
    try:
        if not el.is_checked():
            raise RuntimeError(f"radio {text!r} is still not checked after clicking it")
    except PWTimeout:
        pass
    except Exception as e:
        if "still not checked" in str(e):
            raise
    print(f"Selected radio: {text}")


# New Vincent Code Start: October 28, 2026
def click_radio_in_section(ctx, section_label, text):
    """Select a radio inside one labelled block (e.g. '13. Detail/Assembly').

    Scoped to the block so another 'Assembly' on the page cannot match, and it
    tries several click methods because the real <input> is often covered by a
    styled label (a plain click then times out).
    """
    section = ctx.locator(
        f'xpath=(//*[contains(normalize-space(text()[1]), "{section_label}")])[1]'
        "/ancestor::*[self::td or self::div or self::section][1]"
    )
    if section.count() == 0 or section.locator('input[type="radio"]').count() == 0:
        # climb one more level if the label sits in a narrow wrapper
        section = ctx.locator(
            f'xpath=(//*[contains(normalize-space(text()[1]), "{section_label}")])[1]'
            "/ancestor::*[self::td or self::div or self::section][2]"
        )

    radio = section.get_by_label(text, exact=True)
    if radio.count() == 0:
        radio = section.locator(
            f'xpath=.//label[normalize-space(.)="{text}"]//input[@type="radio"]'
            f' | .//input[@type="radio"][@value="{text}"]'
        )
    label = section.locator(f'xpath=.//label[normalize-space(.)="{text}"]')
    text_el = section.get_by_text(text, exact=True)

    def is_on():
        try:
            return radio.first.is_checked()
        except Exception:
            return False

    attempts = [
        ("check", lambda: radio.first.check(timeout=3000)),
        ("click label", lambda: label.first.click(timeout=3000)),
        ("click text", lambda: text_el.first.click(timeout=3000)),
        ("forced check", lambda: radio.first.check(force=True, timeout=3000)),
        ("forced click", lambda: radio.first.click(force=True, timeout=3000)),
        ("JS click", lambda: radio.first.evaluate("el => el.click()")),
    ]
    last = None
    for name, fn in attempts:
        try:
            fn()
        except Exception as e:
            last = e
            continue
        if is_on():
            print(f"Selected radio: {text} (via {name})")
            return
    raise RuntimeError(
        f"radio {text!r} in {section_label!r} could not be selected"
        + (f" (last error: {str(last).splitlines()[0][:150]})" if last else "")
    )


# New Vincent Code End: October 28, 2026


def set_question_radio(ctx, question, answer):
    base = f'(//*[contains(normalize-space(text()), "{question}")])[1]'
    radios = ctx.locator(f'xpath={base}/following::input[@type="radio"]')
    n = min(radios.count(), 2)
    if n == 0:
        raise RuntimeError(f"no radio buttons found after the question {question!r}")
    seen = []
    for i in range(n):
        r = radios.nth(i)
        label = r.evaluate(
            """e => { const l = e.id ? document.querySelector('label[for="' + e.id + '"]') : null;
                      return ((l && l.innerText) || (e.parentElement && e.parentElement.innerText) || '').trim(); }"""
        )
        seen.append(label)
        if label.lower() != answer.lower():
            continue
        if r.is_checked():
            print(f"{question}: already {answer}")
            return
        try:
            r.check(timeout=5000)
        except Exception:
            r.evaluate("e => e.click()")
        if not r.is_checked():
            raise RuntimeError(f"{question}: clicked {answer!r} but it is not checked")
        print(f"{question}: set to {answer}")
        return
    raise RuntimeError(
        f"{question}: no radio labelled {answer!r} (labels seen: {seen})"
    )


def pick_supplier_code(ctx, value):
    inp = ctx.locator(
        'supplier-code-picker kendo-combobox input[role="combobox"]'
    ).first
    if value == (inp.input_value() or "").strip():
        print(f"Supplier Code already shows: {inp.input_value()}")
        return
    last = ""
    for attempt in range(1, 4):
        try:
            inp.click(timeout=5000)
            if attempt == 1:
                inp.fill(
                    value
                )  # paste the whole code at once (fires the input event the list needs)
            else:
                # retries: type it key by key in case the paste did not open the suggestion list
                inp.fill("")
                inp.press_sequentially(value, delay=10)
            real = (
                ctx.locator(
                    'kendo-popup [role="option"]:visible, kendo-popup li.k-list-item:visible'
                )
                .filter(has_text=value)
                .filter(has_not_text=re.compile(r"^\s*Use\b"))
            )
            use_row = ctx.get_by_text(
                re.compile(rf'Use\s*[\u201c"]?{re.escape(value)}')
            )
            poll(
                ctx.page,
                lambda: real.count() > 0
                or (use_row.count() > 0 and use_row.first.is_visible()),
                1.2,
                60,
            )
            if real.count():
                real.first.click(timeout=5000)
                how = "list entry"
            elif use_row.count() and use_row.first.is_visible():
                use_row.first.click(timeout=5000)
                how = "Use row"
            else:
                inp.press("Enter")
                how = "Enter"
            inp.press("Tab")
            poll(ctx.page, lambda: (inp.input_value() or "").strip() == value, 0.8, 60)
            last = (inp.input_value() or "").strip()
            if last == value:
                print(f"Selected Supplier Code: {last} (via {how})")
                return
        except Exception as e:
            last = f"(could not pick: {type(e).__name__})"
        print(f"  Supplier Code: attempt {attempt} shows {last!r}, retrying...")
        dump_open_popup(ctx, ctx.page, "Supplier_Code")
        try:
            inp.press("Escape")
        except Exception:
            pass
    raise RuntimeError(f"Supplier Code: expected {value!r} but the box shows {last!r}")


def shown_text(el):
    try:
        if el.evaluate("e => e.tagName.toLowerCase()") == "input":
            return (el.input_value() or "").strip()
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def pick_flexible(ctx, label_text, option_text):
    base = f'(//*[contains(normalize-space(text()), "{label_text}")])[1]'
    stage = "start"
    try:
        stage = "find native select"
        sel = ctx.locator(f"xpath={base}/following::select[1]")
        if sel.count():
            opts = sel.first.locator("option").all_inner_texts()
            chosen = (
                sel.first.evaluate(
                    "e => e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : ''"
                )
                or ""
            )
            if option_text in chosen:
                print(f"{label_text} already shows: {chosen.strip()}")
                return
            match = next((o for o in opts if option_text in o), None)
            if match is None:
                raise RuntimeError(f"option {option_text!r} not in {opts}")
            stage = "select_option"
            sel.first.select_option(label=match, timeout=5000)
            print(f"Selected {label_text}: {match.strip()}")
            return

        stage = "find custom dropdown"
        dd = ctx.locator(
            f"xpath={base}/following::*[self::kendo-dropdownlist or self::kendo-combobox "
            "or @role='combobox' or contains(@class,'k-dropdown') or contains(@class,'k-picker')][1]"
        )
        if dd.count() == 0:
            raise RuntimeError("no dropdown found after the label")
        shown = shown_text(dd.first)
        if option_text in shown:
            print(f"{label_text} already shows: {shown}")
            return
        stage = "open dropdown (click)"
        dd.first.click(timeout=5000)
        stage = "wait for option list"
        items = ctx.locator(
            "kendo-popup [role=option]:visible, kendo-popup li.k-list-item:visible, "
            "li.k-item:visible, li.k-list-item:visible"
        )
        items.first.wait_for(timeout=8000)
        stage = "click option"
        target = items.filter(has_text=option_text)
        for how in ("normal click", "forced click", "type + Enter"):
            try:
                if how == "normal click":
                    target.first.click(timeout=3000)
                elif how == "forced click":
                    target.first.click(timeout=3000, force=True)
                else:
                    dd.first.focus()
                    ctx.page.keyboard.type(option_text, delay=40)
                    ctx.wait_for_timeout(400)
                    ctx.page.keyboard.press("Enter")
                ctx.wait_for_timeout(500)
                if option_text in shown_text(dd.first):
                    print(f"Selected {label_text}: {shown_text(dd.first)} (via {how})")
                    return
                print(
                    f"  {label_text}: {how} did not change the value, trying next way..."
                )
            except Exception as e:
                print(
                    f"  {label_text}: {how} failed ({type(e).__name__}), trying next way..."
                )
        raise RuntimeError(
            f"none of the click methods set {option_text!r}; box shows {shown_text(dd.first)!r}"
        )
    except Exception as e:
        try:
            html = ctx.locator(
                f"xpath={base}/ancestor::*[self::div or self::td or self::section][2]"
            ).first.evaluate("e => e.outerHTML")
            save_html(f"debug_{safe_name(label_text)}.html", html, 20000)
        except Exception:
            pass
        raise RuntimeError(
            f"{label_text} failed at stage '{stage}': {type(e).__name__}: {str(e).splitlines()[0][:200]}"
        ) from e


def fill_extra_fields(page, part):
    track_requests(page)  # New Vincent Code: October 29, 2026
    ctx = find_frame_with_text(page, "Serial Number", timeout=patient(20))
    if ctx is None:
        raise RuntimeError(f"FAIR page not found after CREATE FAIR. URL: {page.url}")

    if DEBUG_FIELDS:
        print_form_controls(ctx)

    print(
        f"  Values for this FAIR -> drawing number: {part.number!r}, revision: {part.rev!r}, "
        f"lot: {part.lot!r}, P.O.: {part.po!r}"
    )
    failed = []

    def step(name, fn):
        try:
            fn()
        except Exception as e:
            failed.append(name)
            print(
                f"  Could not set {name}: {type(e).__name__}: {str(e).splitlines()[0][:250] if str(e) else ''}"
            )

    controls = [
        ("Serial Number", "serialNumber", FAIR_SERIAL_NUMBER),
        (
            "Customer Part Number",
            "customerPartNumber",
            part.number if FILL_CUSTOMER_PART else "",
        ),
        ("Drawing Number", "drawingNumber", part.number),
        ("Drawing Revision Level", "drawingRevLevel", part.rev),
        ("Part Revision Level", "partRevLevel", part.rev if FILL_PART_REVISION else ""),
        ("Manufacturing Process Reference", "manufacturingProcessReference", part.lot),
        ("Purchase Order Number", "poNumber", part.po if FILL_PO_NUMBER else ""),
    ]
    for name, control, value in controls:
        if value:
            step(name, lambda c=control, v=value: fill_by_control(ctx, c, v))

    if not part.rev:
        print(
            "  No drawing revision available for this part: Drawing Revision Level left empty. "
            "Check COL_DRAWING_REV in .env and the revision value in the database."
        )
    if not part.lot:
        print("  No lot number available: Manufacturing Process Reference left empty.")
    if FILL_PO_NUMBER and not part.po:
        print(
            "  No P.O. number available: Purchase Order Number left empty "
            "(check COL_PO_NUMBER in .env and the poNumber value in the database)."
        )

    if FAIR_SUPPLIER_CODE:
        step("Supplier Code", lambda: pick_supplier_code(ctx, FAIR_SUPPLIER_CODE))
    # New Vincent Code Start: October 28, 2026
    # XA (Main Part) -> Assembly. XP (Sub-part) / anything else -> FAIR_DETAIL_ASSEMBLY (Detail).
    is_main_part = str(part.number or "").strip().upper().startswith("XA")
    detail_assembly = "Assembly" if is_main_part else FAIR_DETAIL_ASSEMBLY
    print(
        f"  Part {part.number!r} is {'XA (Main Part)' if is_main_part else 'XP (Sub-part)'}"
        f" -> Detail/Assembly: {detail_assembly!r}"
    )
    if detail_assembly:
        step(
            "Detail/Assembly",
            lambda: click_radio_in_section(ctx, "Detail/Assembly", detail_assembly),
        )
    # New Vincent Code End: October 28, 2026
    if FAIR_FULL_PARTIAL:
        step("Full/Partial", lambda: click_radio(ctx, FAIR_FULL_PARTIAL))
    if FAIR_FULL_PARTIAL_REASON:
        step(
            "Reason for Full/Partial FAI",
            lambda: pick_flexible(
                ctx, "Reason for Full/Partial FAI", FAIR_FULL_PARTIAL_REASON
            ),
        )
    if FAIR_NONCONFORMANCE:
        step(
            "Documented Nonconformance (19)",
            lambda: set_question_radio(
                ctx, "Documented Nonconformance", FAIR_NONCONFORMANCE
            ),
        )

    save_screenshot(page, f"fair_filled_{part.number}")
    if failed:
        print(f"Extra fields NOT set: {', '.join(failed)}. Check the page.")
        return "FAIR_FILLED_PARTIAL"
    print("Extra fields filled. Nothing was saved or submitted.")
    return "FAIR_FILLED"


def find_clickable(ctx, name_regex):
    pat = re.compile(name_regex, re.I)
    loc = ctx.get_by_role("button", name=pat)
    if loc.count() == 0:
        loc = ctx.locator(
            "button, a.btn, label.btn, input[type=button], input[type=submit]"
        ).filter(has_text=pat)
    if loc.count() == 0:
        loc = ctx.locator(
            'input[type=button][value*="save fair" i], input[type=submit][value*="save fair" i]'
        )
    return loc


_INVALID_JS = """() => [...document.querySelectorAll(
    'input.ng-invalid, textarea.ng-invalid, select.ng-invalid, [formcontrolname].ng-invalid')]
  .filter(e => !e.disabled && e.offsetParent !== null)
  .map(e => (e.getAttribute('formcontrolname') || e.id || e.name || e.tagName)
            + '=' + JSON.stringify((e.value || '').slice(0, 40)))"""


def invalid_controls(page):
    """Visible Form fields that Angular marks invalid (these can stop SAVE FAIR from saving)."""
    out = []
    for fr in page.frames:
        try:
            out += fr.evaluate(_INVALID_JS) or []
        except Exception:
            pass
    return out


def save_fair(page, part_number):
    ctx = find_frame_with_text(page, "SAVE FAIR", timeout=patient(15))
    btn = None
    if ctx:
        btn = ctx.locator('button[title="Save Fair"]')  # the SAVE FAIR button itself
        if btn.count() == 0:
            btn = find_clickable(ctx, r"^\s*save\s*fair\s*$")
    if btn is None or btn.count() == 0:
        print("SAVE FAIR button not found on the page.")
        return "FAIR_SAVE_FAILED"
    target = btn.first
    if not wait_enabled(page, target, 20):
        print("SAVE FAIR stayed disabled for 20 seconds, so nothing was saved.")
        bad = invalid_controls(page)
        if bad:
            print(f"  Fields marked invalid on the page: {bad}")
        save_screenshot(page, f"fair_save_disabled_{part_number}")
        return "FAIR_SAVE_FAILED"

    def is_save_call(r):
        try:
            return (
                r.request.method in ("POST", "PUT", "PATCH")
                and "net-inspect.com" in r.url
            )
        except Exception:
            return False

    clicked, got = False, None
    try:
        with page.expect_response(is_save_call, timeout=8000) as info:
            target.click(timeout=T(5000))
            clicked = True
        got = info.value
    except PWTimeout:
        if not clicked:
            raise
        print("  (no save request seen within 8 s; falling back to checking the page)")
        bad = invalid_controls(page)
        if bad:
            print(
                f"  No save request was sent and these fields are marked invalid: {bad}. "
                "Net-Inspect does not save while a field is invalid."
            )
            save_screenshot(page, f"fair_save_invalid_{part_number}")
            return "FAIR_SAVE_FAILED"
    print("Clicked SAVE FAIR.")
    # New Vincent Code Start: November 4, 2026
    if SAVE_FAIR_DELAY:
        print(f"  Waiting {SAVE_FAIR_DELAY:g} s so the save can finish...")
        page.wait_for_timeout(int(SAVE_FAIR_DELAY * 1000))
    # New Vincent Code End: November 4, 2026

    if got is not None:
        print(
            f"  Save request: {got.request.method} HTTP {got.status} "
            f"{got.url.split('?')[0][-90:]}"
        )
        if got.status >= 400:
            try:
                body = " ".join(got.text().split())[:400]
            except Exception:
                body = "(could not read the answer)"
            print(f"SAVE FAIR was REJECTED by the server (HTTP {got.status}): {body}")
            save_screenshot(page, f"fair_save_rejected_{part_number}")
            return "FAIR_SAVE_FAILED"
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PWTimeout:
            pass
        if error_shown(page, SAVE_ERROR_WORDS):
            print(
                "SAVE FAIR was clicked but an error message is showing. Check the page."
            )
            save_screenshot(page, f"fair_save_error_{part_number}")
            return "FAIR_SAVE_FAILED"
        if not SAVE_SUCCESS_TEXT:
            print(
                f"SAVE FAIR clicked, the server answered (HTTP {got.status}) and no error message appeared "
                "(set SAVE_SUCCESS_TEXT to also require Net-Inspect's success message)."
            )
            save_screenshot(page, f"fair_saved_{part_number}")
            return "FAIR_SAVED_UNVERIFIED"

    # With no SAVE_SUCCESS_TEXT nothing can confirm the save, so only watch for errors briefly.
    deadline = time.time() + (12 if SAVE_SUCCESS_TEXT else 3)
    while time.time() < deadline:
        page.wait_for_timeout(300)
        if SAVE_SUCCESS_TEXT and any_frame_shows(page, SAVE_SUCCESS_TEXT):
            settle(page)
            print(f"Save confirmed: the page shows {SAVE_SUCCESS_TEXT!r}.")
            save_screenshot(page, f"fair_saved_{part_number}")
            return "FAIR_SAVED"
        if error_shown(page, SAVE_ERROR_WORDS):
            print(
                "SAVE FAIR was clicked but an error message is showing. Check the page."
            )
            save_screenshot(page, f"fair_save_error_{part_number}")
            return "FAIR_SAVE_FAILED"
    settle(page)
    if SAVE_SUCCESS_TEXT:
        print(
            f"SAVE FAIR clicked, but {SAVE_SUCCESS_TEXT!r} never appeared: save NOT confirmed. Check the page."
        )
    else:
        print(
            "SAVE FAIR clicked and no error message appeared (set SAVE_SUCCESS_TEXT to also require "
            "Net-Inspect's success message). Check the page to be sure."
        )
    save_screenshot(page, f"fair_saved_{part_number}")
    return "FAIR_SAVED_UNVERIFIED"


# New Vincent Code Start: October 29, 2026
# "Ready to leave" checker. Run it before clicking the Form 2 / Form 3 tab (and before SAVE FAIR),
# so Net-Inspect's Save / Cancel (unsaved changes) modal does not appear.
READY_SECONDS = 30  # how long to wait before giving up and NOT clicking the tab
SAVE_LOADING = 'button[title="Save Fair"] i.loading'
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_SHOWN_JS = (
    "e => { const s = getComputedStyle(e); const r = e.getBoundingClientRect();"
    " return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'"
    " && parseFloat(s.opacity) > 0; }"
)


def track_requests(page):
    """Counts save/upload requests (POST/PUT/PATCH/DELETE to net-inspect.com) that have started
    but not finished. Installed once per page; safe to call again."""
    if getattr(page, "_ni_inflight", None) is not None:
        return
    inflight = set()

    def started(req):
        try:
            if req.method in WRITE_METHODS and "net-inspect.com" in req.url:
                inflight.add(req)
        except Exception:
            pass

    def ended(req):
        inflight.discard(req)

    page.on("request", started)
    page.on("requestfinished", ended)
    page.on("requestfailed", ended)
    page._ni_inflight = inflight


def save_spinner_visible(page):
    for fr in page.frames:
        try:
            loc = fr.locator(SAVE_LOADING)
            for i in range(loc.count()):
                if loc.nth(i).evaluate(_SHOWN_JS):
                    return True
        except Exception:
            pass
    return False


def grid_editor_open(page):
    """A grid cell (Part Number Index, Materials, Processes) is still being typed into."""
    for fr in page.frames:
        try:
            if fr.locator(".ag-cell-inline-editing, .ag-cell-popup-editing").count():
                return True
        except Exception:
            pass
    return False


def unsaved_modal_shown(page):
    """A dialog that has both a Save and a Cancel button (the unsaved-changes modal)."""
    cancel = re.compile(r"^\s*cancel\s*$", re.I)
    save = re.compile(r"^\s*save\b", re.I)
    for fr in page.frames:
        try:
            dlg = (
                fr.locator(
                    '[role="dialog"], .modal.show, .k-dialog, .k-window, .cdk-overlay-pane'
                )
                .filter(has=fr.get_by_role("button", name=cancel))
                .filter(has=fr.get_by_role("button", name=save))
            )
            if dlg.count() and dlg.first.is_visible():
                return True
        except Exception:
            pass
    return False


def not_ready_reasons(page):
    reasons = []
    if save_spinner_visible(page):
        reasons.append("the SAVE FAIR spinner is still showing")
    pending = len(getattr(page, "_ni_inflight", None) or ())
    if pending:
        reasons.append(f"{pending} save/upload request(s) still running")
    if grid_editor_open(page):
        reasons.append("a grid cell is still being edited")
    if unsaved_modal_shown(page):
        reasons.append("a Save / Cancel modal is already open")
    return reasons


def wait_ready_to_leave(page, label, seconds=READY_SECONDS, calm_ms=700):
    """True once nothing is saving, uploading or being typed, and it stayed that way for calm_ms
    (the calm period covers the short gap between clicking SAVE FAIR and the spinner starting).
    False after `seconds`; the caller must then NOT click the next tab."""
    track_requests(page)
    settle(page)
    deadline = time.time() + seconds
    calm_since = None
    while True:
        reasons = not_ready_reasons(page)
        now = time.time()
        if not reasons:
            calm_since = calm_since or now
            if (now - calm_since) * 1000 >= calm_ms:
                print(f"  [ready] {label}: nothing saving, uploading or being edited.")
                return True
        else:
            calm_since = None
        if now >= deadline:
            print(f"  NOT READY ({label}) after {seconds}s: " + "; ".join(reasons))
            save_screenshot(page, f"not_ready_{safe_name(label)}")
            return False
        page.wait_for_timeout(200)


# New Vincent Code End: October 29, 2026


def click_unsaved_modal_save(page):
    """Clicks SAVE in the 'You have unsaved changes' box (Save / Discard / Cancel). True if clicked."""
    cancel = re.compile(r"^\s*cancel\s*$", re.I)
    save = re.compile(r"^\s*save\s*$", re.I)
    for fr in page.frames:
        try:
            dlg = (
                fr.locator(
                    '[role="dialog"], .modal.show, .k-dialog, .k-window, .cdk-overlay-pane'
                )
                .filter(has=fr.get_by_role("button", name=cancel))
                .filter(has=fr.get_by_role("button", name=save))
            )
            if dlg.count() and dlg.first.is_visible():
                dlg.first.get_by_role("button", name=save).first.click(timeout=5000)
                print("  Clicked SAVE in the unsaved-changes box.")
                poll(page, lambda: not unsaved_modal_shown(page), 15)
                settle(page)
                return True
        except Exception as e:
            print(f"  Could not click SAVE in the box: {type(e).__name__}")
    return False


def open_form_2(page, part_number):
    # New Vincent Code Start: October 29, 2026
    if not wait_ready_to_leave(page, "Form 1 -> Form 2"):
        print(
            "The Form 2 tab was NOT clicked (Form 1 is not finished). Check the page."
        )
        return False
    # New Vincent Code End: October 29, 2026
    ctx = find_frame_with_text(page, "Form 2", timeout=patient(15))
    if ctx is None:
        print("The Form 2 tab was not found.")
        return False
    click_nav(ctx, "Form 2", roles=("tab", "link"), timeout=5000)
    print("Clicked the Form 2 tab.")
    if poll(page, lambda: any_frame_shows(page, "Form 2:"), 10):
        settle(page)
        print("Form 2 is open.")
        save_screenshot(page, f"form2_{part_number}")
        return True
    # New Vincent Code Start: October 29, 2026
    if unsaved_modal_shown(page):
        print(
            "Clicked Form 2, but the Save / Cancel modal is showing (Form 1 had unsaved changes)."
        )
        # Click SAVE in the modal (keeps what the script filled), then make sure Form 2 opens.
        if click_unsaved_modal_save(page):
            if not poll(page, lambda: any_frame_shows(page, "Form 2:"), 15):
                try:
                    ctx = find_frame_with_text(page, "Form 2", timeout=patient(10))
                    if ctx is not None:
                        click_nav(ctx, "Form 2", roles=("tab", "link"), timeout=5000)
                except Exception:
                    pass
            if poll(page, lambda: any_frame_shows(page, "Form 2:"), 15):
                settle(page)
                print(
                    "Form 2 is open (after clicking SAVE in the unsaved-changes box)."
                )
                save_screenshot(page, f"form2_{part_number}")
                return True
    # New Vincent Code End: October 29, 2026
    print("Clicked Form 2, but its heading did not appear. Check the page.")
    bad = invalid_controls(page)
    print(
        "  Fields marked invalid on Form 1 (Net-Inspect will not save while these are invalid): "
        f"{bad if bad else 'none found'}"
    )
    print(f"  Save / Cancel box still showing: {unsaved_modal_shown(page)}")
    save_screenshot(page, f"form2_unconfirmed_{part_number}")
    return False


def save_and_open_form_2(page, part_number):
    # New Vincent Code Start: October 29, 2026
    # Nothing may still be saving or being typed (Part Number Index) when SAVE FAIR is clicked.
    if not wait_ready_to_leave(page, "before SAVE FAIR (Form 1)"):
        print("SAVE FAIR NOT clicked: Form 1 is still being filled or saved.")
        return "FAIR_SAVE_SKIPPED"
    # New Vincent Code End: October 29, 2026
    status = save_fair(page, part_number)
    if status == "FAIR_SAVE_FAILED":
        print("Form 2 NOT opened (the save failed).")
        return status
    return (
        "FAIR_SAVED_FORM2"
        if open_form_2(page, part_number)
        else "FAIR_SAVED_FORM2_UNCONFIRMED"
    )


CREATE_FORM_MARKERS = ("Select AS9102 Revision", "Create a New FAIR", "Customer Info")


def find_create_form(page, seconds=60):
    """Find the Create FAIR form by any of several texts, so a wording change on the page
    does not make the script wait for minutes."""
    for _ in range(int(seconds * 4)):
        for text in CREATE_FORM_MARKERS:
            ctx = find_frame_with_text(page, text, timeout=0)
            if ctx is not None:
                if text != CREATE_FORM_MARKERS[0]:
                    print(
                        f"  (Create FAIR form found by '{text}', not by 'Select AS9102 Revision')"
                    )
                return ctx
        page.wait_for_timeout(250)
    return None


def fill_form(page, part):
    ctx = find_create_form(page)
    if ctx is None:
        save_screenshot(page, f"create_fair_not_found_{part.number}")
        try:
            save_html(f"debug_create_fair_{part.number}.html", page.content(), 80000)
        except Exception:
            pass
        raise RuntimeError(
            f"Create FAIR form not found after 60 s. URL: {page.url}. "
            "See screenshots/create_fair_not_found_*.png"
        )

    choose_option(ctx, "Customer", CUSTOMER, 1)
    ctx.get_by_text("To Division").first.wait_for(timeout=T(15000))
    choose_option(ctx, "To Division", DIVISION, 2)
    box = fill_field(ctx, "Part Number", "Form 1, Field 1", part.number)

    exists = part_exists_at_customer(page, box)
    if exists:
        fill_field(ctx, "Part Name", "Form 1, Field 2", part.name)
        try:
            choose_option(ctx, "FAIR Program", FAIR_PROGRAM, 3)
        except RuntimeError as e:
            if not_at_customer_shown(page):
                exists = False
            else:
                raise RuntimeError(
                    f"Could not confirm that '{part.number}' exists at the customer: {e}"
                ) from e

    if not exists:
        return handle_unknown_part(page, ctx, box, part)

    print("Part exists at the customer. Form filled.")
    if CLICK_CREATE_FAIR:
        return click_create_fair(page, ctx, part.number)
    print("CLICK_CREATE_FAIR is off: NOT clicking CREATE FAIR.")
    return "EXISTS"
