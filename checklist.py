import re
import time

from playwright.sync_api import TimeoutError as PWTimeout

from form1 import (
    SAVE_FAIR_DELAY,
    click_nav,
    click_unsaved_modal_save,
    find_clickable,
    track_requests,
    unsaved_modal_shown,
    wait_ready_to_leave,
)
from login import (
    COL_FINISH_STAMP,
    COL_PART_NUMBER,
    DB_TABLE,
    UPDATE_STAMPS,
    mark_finish_stamp,
    T,
    any_frame_shows,
    env_bool,
    env_str,
    error_shown,
    find_frame_with_text,
    ident,
    patient,
    poll,
    run_sql,
    SAVE_ERROR_WORDS,
    save_screenshot,
    settle,
)

# --- Checklist settings (all optional, read from .env) ---
FILL_CHECKLIST = env_bool("FILL_CHECKLIST", False)
# Last step: click SIGN CHECKLIST, then COMPLETE CHECKLIST in the confirmation box.
# NOTE: after this no further edits can be made to the checklist.
# Default is ON: signing is a required step, and the script does not go on to the next part until it succeeded.
CHECKLIST_SIGN = env_bool("CHECKLIST_SIGN", True)

# New Vincent Code Start: November 4, 2026
# After the checklist is signed and the script is back on Form 1:
#   SIGN_ALL_FORMS: click SIGN ALL FORMS, then OK in its popup.
#   SUBMIT_FAIR: click SUBMIT, then OK in the "Submit FAIR to Customer" popup.
# NOTE: SUBMIT_FAIR sends the FAIR to the customer. It can NOT be edited afterwards.
# SUBMIT_FAIR only runs when SIGN_ALL_FORMS succeeded. Set either to false in .env to stop at that step.
SIGN_ALL_FORMS = env_bool("SIGN_ALL_FORMS", True)
SUBMIT_FAIR = env_bool("SUBMIT_FAIR", True)
SIGN_ALL_BUTTON_RE = r"^\s*sign\s+all\s+forms\s*$"
SUBMIT_BUTTON_RE = r"^\s*submit\s*$"
OK_BUTTON_RE = r"^\s*ok\s*$"
# Dynamic waiting: keep waiting while the page is busy (saving request or spinner). Give up only after
# WAIT_IDLE seconds with NO activity, or after WAIT_HARD_CAP seconds in total (safety against hanging).
WAIT_IDLE = int(env_str("SIGN_WAIT_IDLE", "10"))
WAIT_HARD_CAP = int(env_str("SIGN_WAIT_HARD_CAP", "120"))
# New Vincent Code Start: November 14, 2026
# If the checklist is not locked after COMPLETE (no UNLOCK CHECKLIST button), SIGN -> COMPLETE is tried
# again. SIGN_TRIES = total number of tries (1 = no retry). SIGN_RETRY_WAIT = seconds between tries.
SIGN_TRIES = max(1, int(env_str("CHECKLIST_SIGN_TRIES", "3")))
SIGN_RETRY_WAIT = float(env_str("CHECKLIST_SIGN_RETRY_WAIT", "3"))
# New Vincent Code End: November 14, 2026
# Text that identifies each popup (so OK is never clicked in the wrong one).
SIGN_ALL_DIALOG_RE = (
    r"sign/lock all forms|not been signed/locked|proceed with this request"
)
SUBMIT_DIALOG_RE = r"Submit FAIR to Customer|submit FAIR to the customer"
# The submit step has TWO popups with the same title. Tell them apart by their body text.
SUBMIT_WARN_DIALOG_RE = r"not be able to edit it (any )?further"  # popup 1 (warning)
SUBMIT_ACK_DIALOG_RE = r"I acknowledge"  # popup 2 (Comments + checkbox + final OK)
# Optional text for the Comments box of the "Submit FAIR to Customer" popup. Empty = leave Comments empty.
SUBMIT_COMMENT = env_str("SUBMIT_COMMENT", "")
# False = find the final OK in the Submit popup but do NOT click it. True = click it.
SUBMIT_CLICK_OK = env_bool("SUBMIT_CLICK_OK", False)
# New Vincent Code End: November 4, 2026


# Questions that are answered N/A (comma separated). Every other question is answered Yes.
def _numbers(text):
    return {int(x) for x in str(text).replace(" ", "").split(",") if x.isdigit()}


# XA (Main Part): CHECKLIST_NA (default 5).
CHECKLIST_NA = _numbers(env_str("CHECKLIST_NA", "5"))
# New Vincent Code Start: November 5, 2026
# Sub-part = isSub = 1 in DB_TABLE (it_netInspectData), NOT the XP prefix.
# isSub = 1 -> CHECKLIST_NA_SUB (has sub-parts). isSub = 0 -> CHECKLIST_NA (no sub-parts, even if XP).
CHECKLIST_NA_SUB = _numbers(env_str("CHECKLIST_NA_SUB", "3,5"))
COL_IS_SUB = env_str("COL_IS_SUB", "isSub")


def lookup_is_sub(part_number):
    """True / False from isSub in DB_TABLE. None when the part has no row (do not guess)."""
    net = ident(DB_TABLE, "DB_TABLE")
    c_part = ident(COL_PART_NUMBER, "COL_PART_NUMBER")
    c_sub = ident(COL_IS_SUB, "COL_IS_SUB")
    row = run_sql(
        f"SELECT MAX({c_sub} = 1) AS is_sub FROM {net} WHERE TRIM({c_part}) = %s",
        (str(part_number or "").strip(),),
        fetch="one",
    )
    if not row or row.get("is_sub") is None:
        return None
    return bool(row["is_sub"])


def na_questions_for(part_number):
    is_sub = lookup_is_sub(part_number)
    if is_sub is None:
        return None, None
    return is_sub, (CHECKLIST_NA_SUB if is_sub else CHECKLIST_NA)


# New Vincent Code End: November 5, 2026
CHECKLIST_QUESTIONS = 8  # the First Article Review Checklist has 8 questions
SIGN_BUTTON_RE = r"sign\s+checklist"
COMPLETE_BUTTON_RE = r"complete\s+checklist"
CONFIRM_TITLE = "Net-Inspect Confirmation"


def open_checklist(page, part_number):
    # Do not leave the previous tab while it is still saving or uploading.
    if not wait_ready_to_leave(page, "-> Checklist"):
        print("  The Checklist tab was NOT clicked (the previous page is still busy).")
        return None
    ctx = find_frame_with_text(page, "Checklist", timeout=patient(15))
    if ctx is None:
        print("The Checklist tab was not found.")
        save_screenshot(page, f"checklist_tab_missing_{part_number}")
        return None
    tab = ctx.locator("a, [role=tab]").filter(has_text=re.compile(r"^\s*Checklist\s*$"))
    if tab.count():
        tab.first.click(timeout=T(5000))
    else:
        click_nav(ctx, "Checklist", roles=("tab", "link"), timeout=5000)
    print("Clicked the Checklist tab.")
    settle(page)
    ctx = find_frame_with_text(
        page, "First Article Review Checklist", timeout=patient(15)
    )
    if ctx is None:
        print(
            "Clicked Checklist, but the question list did not appear. Check the page."
        )
        save_screenshot(page, f"checklist_unconfirmed_{part_number}")
        return None
    print("Checklist is open.")
    return ctx


def question_row(ctx, number):
    # The row of question `number`: the closest parent of the question text that holds radio buttons.
    # Only a parent with 2 to 4 radios counts as a question row (Yes, N/A, Accept, Reject). That skips
    # the header boxes such as "4. FAIR Identifier", whose nearest radio parent is the whole page.
    text = ctx.get_by_text(re.compile(rf"^\s*{number}\.\s+\S"))
    row_xpath = (
        "xpath=ancestor::*[count(.//input[@type='radio'])>=2 "
        "and count(.//input[@type='radio'])<=4][1]"
    )
    for i in range(text.count()):
        row = text.nth(i).locator(row_xpath)
        if row.count():
            return row.first
    return None


def answer_question(ctx, number, answer):
    # answer = "Yes" or "N/A". The Supplier radios are the first two radios of the row (Yes, N/A).
    row = question_row(ctx, number)
    if row is None:
        raise RuntimeError(f"question {number}: its row was not found")
    radios = row.locator("input[type=radio]")
    if radios.count() < 2:
        raise RuntimeError(
            f"question {number}: expected Yes / N/A radio buttons, found {radios.count()}"
        )
    index = 0 if answer == "Yes" else 1
    radio = radios.nth(index)
    if not radio.is_checked():
        # Click the label text first (the radio itself is often hidden by the page's styling).
        try:
            row.get_by_text(answer, exact=True).first.click(timeout=T(3000))
        except Exception:
            pass
    if not radio.is_checked():
        radio.click(timeout=T(3000), force=True)
    if not radio.is_checked():
        raise RuntimeError(f"question {number}: could not tick {answer}")
    print(f"Question {number}: {answer}")


def answers_still_set(ctx, na_set):
    """Re-read all answers right before signing (the page may have reverted one).
    Also makes sure there is no extra question beyond the expected ones.
    Returns a list of problems (empty = all good)."""
    problems = []
    for n in range(1, CHECKLIST_QUESTIONS + 1):
        row = question_row(ctx, n)
        if row is None:
            problems.append(f"question {n}: row not found")
            continue
        want = 1 if n in na_set else 0
        try:
            if not row.locator("input[type=radio]").nth(want).is_checked():
                problems.append(
                    f"question {n}: {'N/A' if want else 'Yes'} is not ticked"
                )
        except Exception as e:
            problems.append(f"question {n}: could not read it ({type(e).__name__})")
    if question_row(ctx, CHECKLIST_QUESTIONS + 1) is not None:
        problems.append(
            f"question {CHECKLIST_QUESTIONS + 1} exists: the checklist has more "
            f"than {CHECKLIST_QUESTIONS} questions"
        )
    return problems


# New Vincent Code Start: November 4, 2026
SAVE_BUTTON_SELECTOR = 'button[title="Save Fair"]'


def save_checklist(page, part_number):
    """Click Save (the button titled 'Save Fair') on the Checklist tab and wait until the save is finished.
    Runs before SIGN CHECKLIST. Returns True when it is safe to go on and sign; False = do NOT sign.
    """
    track_requests(page)
    found = []

    def check():
        for fr in page.frames:
            try:
                loc = fr.locator(SAVE_BUTTON_SELECTOR)
                for i in range(loc.count()):
                    el = loc.nth(i)
                    if el.is_visible() and el.is_enabled():
                        found.append(el)
                        return True
            except Exception:
                pass
        return False

    if not wait_dynamic(page, check):
        print(
            "  Checklist: the Save button was not found (or stayed disabled); NOT signing."
        )
        save_screenshot(page, f"checklist_save_missing_{part_number}")
        return False
    target = found[-1]

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
            target.scroll_into_view_if_needed(timeout=T(5000))
            target.click(timeout=T(5000))
            clicked = True
        got = info.value
    except PWTimeout:
        if not clicked:
            print("  Checklist: could not click Save; NOT signing.")
            save_screenshot(page, f"checklist_save_click_failed_{part_number}")
            return False
        print("  (no save request seen within 8 s; checking the page instead)")
    except Exception as e:
        print(f"  Checklist: could not click Save ({type(e).__name__}); NOT signing.")
        save_screenshot(page, f"checklist_save_click_failed_{part_number}")
        return False
    print("Clicked Save.")
    # New Vincent Code Start: November 4, 2026
    if SAVE_FAIR_DELAY:
        print(f"  Waiting {SAVE_FAIR_DELAY:g} s so the save can finish...")
        page.wait_for_timeout(int(SAVE_FAIR_DELAY * 1000))
    handle_unsaved_modal(page, "after clicking Save on the Checklist")
    # New Vincent Code End: November 4, 2026

    if got is not None:
        print(f"  Save request: {got.request.method} HTTP {got.status}")
        if got.status >= 400:
            print(
                f"  Checklist: Save was REJECTED by the server (HTTP {got.status}); NOT signing."
            )
            save_screenshot(page, f"checklist_save_rejected_{part_number}")
            return False

    # Wait (no fixed delay) until nothing is saving any more.
    if not wait_ready_to_leave(page, "Checklist Save", seconds=WAIT_HARD_CAP):
        print("  Checklist: the save did not finish; NOT signing.")
        return False
    if error_shown(page, SAVE_ERROR_WORDS):
        print("  Checklist: an error message is showing after Save; NOT signing.")
        save_screenshot(page, f"checklist_save_error_{part_number}")
        return False
    save_screenshot(page, f"checklist_saved_{part_number}")
    print("  Checklist: saved.")
    return True


# New Vincent Code End: November 4, 2026


def run_checklist(page, part, status):
    if not FILL_CHECKLIST:
        print(
            "  Checklist NOT run: FILL_CHECKLIST is off. Add FILL_CHECKLIST=true to .env."
        )
        return status

    ctx = open_checklist(page, part.number)
    if ctx is None:
        return "CHECKLIST_FAILED"

    # New Vincent Code Start: November 5, 2026
    is_sub, na_set = na_questions_for(part.number)
    if is_sub is None:
        # Signing cannot be undone, so do not guess.
        print(
            f"  Part {part.number!r}: isSub not found in the database; NOT filling the checklist."
        )
        save_screenshot(page, f"checklist_issub_unknown_{part.number}")
        return "CHECKLIST_FAILED"
    print(
        f"  Part {part.number!r} isSub={int(is_sub)}"
        f" -> {'has sub-parts' if is_sub else 'no sub-parts'}"
        f" -> N/A questions: {sorted(na_set)}; all others Yes"
    )
    # New Vincent Code End: November 5, 2026
    done = 0
    try:
        for n in range(1, CHECKLIST_QUESTIONS + 1):
            answer_question(ctx, n, "N/A" if n in na_set else "Yes")
            done += 1
    except Exception as e:
        print(
            f"  Checklist failed on question {done + 1} ({done} done): "
            f"{type(e).__name__}: {str(e).strip()[:300]}"
        )
        save_screenshot(page, f"checklist_error_{part.number}")
        return "CHECKLIST_FAILED"

    save_screenshot(page, f"checklist_filled_{part.number}")

    if CHECKLIST_SIGN:
        # New Vincent Code Start: November 4, 2026
        # Click Save first, so the answers are stored before SIGN CHECKLIST.
        if not save_checklist(page, part.number):
            return "CHECKLIST_FAILED"
        # The page may have re-rendered after saving: take the checklist frame again.
        ctx = (
            find_frame_with_text(
                page, "First Article Review Checklist", timeout=patient(5)
            )
            or ctx
        )
        # New Vincent Code End: November 4, 2026
        return sign_and_complete(page, ctx, part, done, na_set)

    print(f"  Checklist: {done} questions answered (not signed; check the page).")
    return "CHECKLIST_FILLED"


def confirmation_open(page):
    for fr in page.frames:
        try:
            t = fr.get_by_text(CONFIRM_TITLE, exact=False)
            if t.count() and t.first.is_visible():
                return fr
        except Exception:
            pass
    return None


# New Vincent Code Start: November 14, 2026
def checklist_is_locked(page):
    """After COMPLETE the checklist tab shows 'UNLOCK CHECKLIST'."""
    for fr in page.frames:
        try:
            if find_clickable(fr, r"unlock\s+checklist").count():
                return True
        except Exception:
            pass
    return False


# New Vincent Code End: November 14, 2026


def go_to_form_1(page, part_number):
    """After the checklist is signed (the tab shows a lock and 'UNLOCK CHECKLIST'), click the Form 1 tab.
    It never changes the result: the checklist is already completed, so a problem here is only reported.
    """
    ctx = find_frame_with_text(page, "Form 1", timeout=patient(15))
    if ctx is None:
        print("  The Form 1 tab was not found.")
        save_screenshot(page, f"form1_tab_missing_{part_number}")
        return False
    # New Vincent Code Start: November 4, 2026
    handle_unsaved_modal(page, "before clicking Form 1")
    # New Vincent Code End: November 4, 2026
    tab = ctx.locator("a, [role=tab]").filter(has_text=re.compile(r"^\s*Form 1\s*$"))
    try:
        if tab.count():
            tab.first.click(timeout=T(5000))
        else:
            click_nav(ctx, "Form 1", roles=("tab", "link"), timeout=5000)
    except Exception as e:
        print(f"  Could not click the Form 1 tab: {type(e).__name__}")
        save_screenshot(page, f"form1_click_failed_{part_number}")
        return False
    print("Clicked the Form 1 tab.")
    settle(page)
    # New Vincent Code Start: November 4, 2026
    # Dynamic wait; also clicks SAVE if the unsaved-changes box appears after clicking Form 1.
    if wait_dynamic(
        page,
        lambda: any_frame_shows(page, "Serial Number"),
        label="after clicking Form 1",
    ):
        # New Vincent Code End: November 4, 2026
        print("Form 1 is open.")
        save_screenshot(page, f"form1_after_sign_{part_number}")
        return True
    print("  Clicked Form 1, but its fields did not appear. Check the page.")
    save_screenshot(page, f"form1_unconfirmed_{part_number}")
    return False


# New Vincent Code Start: November 4, 2026
DIALOG_SELECTOR = "[role=dialog], .k-window, .k-dialog, .modal, .modal-dialog"


_SHOWN_JS = (
    "e => { const s = getComputedStyle(e); const r = e.getBoundingClientRect();"
    " return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'"
    " && parseFloat(s.opacity) > 0; }"
)
BUSY_SELECTOR = "i.loading, .k-loading-mask, .k-loading-image, .k-i-loading"


def page_busy(page):
    """True while a save request is running or a loading spinner is visible."""
    if getattr(page, "_ni_inflight", None):
        return True
    for fr in page.frames:
        try:
            loc = fr.locator(BUSY_SELECTOR)
            for i in range(loc.count()):
                if loc.nth(i).evaluate(_SHOWN_JS):
                    return True
        except Exception:
            pass
    return False


def handle_unsaved_modal(page, label=""):
    """If Net-Inspect's 'WARNING: You have unsaved changes' box (Cancel / Save / Discard) is open,
    click SAVE in it. `label` says at which step it appeared (written in the log). True if clicked.
    """
    try:
        if unsaved_modal_shown(page):
            print(
                f"  The unsaved-changes box appeared{(' ' + label) if label else ''}: clicking SAVE."
            )
            return bool(click_unsaved_modal_save(page))
    except Exception as e:
        print(f"  Could not handle the unsaved-changes box: {type(e).__name__}")
    return False


def wait_dynamic(page, condition, idle=WAIT_IDLE, hard_cap=WAIT_HARD_CAP, label=""):
    """Wait until condition() is true. Returns as soon as it is (no fixed delay).
    While the page is busy the idle timer restarts, so a slow save just keeps waiting.
    Gives up after `idle` seconds with no activity, or after `hard_cap` seconds in total.
    If the unsaved-changes box shows up while waiting, SAVE is clicked in it.
    """
    track_requests(page)
    start = last_activity = time.time()
    while True:
        if handle_unsaved_modal(page, label):
            last_activity = time.time()
        try:
            if condition():
                return True
        except Exception:
            pass
        now = time.time()
        if page_busy(page):
            last_activity = now
        if now - last_activity >= idle or now - start >= hard_cap:
            return False
        page.wait_for_timeout(200)


def wait_button(page, regex):
    """Wait (dynamically) for a visible and enabled button matching `regex` in any frame. Returns it, or None."""
    found = []

    def check():
        for fr in page.frames:
            try:
                btns = find_clickable(fr, regex)
                for i in range(btns.count()):
                    el = btns.nth(i)
                    if el.is_visible() and el.is_enabled():
                        found.append(el)
                        return True
            except Exception:
                pass
        return False

    return found[-1] if wait_dynamic(page, check) else None


def find_dialog(page, text_re):
    """The open popup whose text matches `text_re` (outermost match, so its buttons are inside), or None."""
    for fr in page.frames:
        try:
            box = fr.locator(DIALOG_SELECTOR).filter(has_text=re.compile(text_re, re.I))
            if box.count() and box.first.is_visible():
                return box.first
        except Exception:
            pass
    return None


# New Vincent Code Start: November 5, 2026
# The "Submit FAIR to Customer" popup has an acknowledgement checkbox (export authorization). OK does
# nothing until it is ticked, so the popup stayed open. The visible box is a styled <span>; the real
# <input type=checkbox> is hidden behind it.
_SCROLL_DIALOG_JS = """d => { for (const e of [d, ...d.querySelectorAll('*')])
  if (e.scrollHeight > e.clientHeight + 5) e.scrollTop = e.scrollHeight; }"""


def tick_dialog_checkboxes(page, box, tag):
    """Tick every checkbox inside the popup `box`. Returns True when all of them are ticked
    (also True when the popup has no checkbox, e.g. the SIGN ALL FORMS popup)."""
    boxes = box.locator('input[type="checkbox"]')
    total = boxes.count()
    if total == 0:
        return True
    all_ok = True
    for i in range(total):
        cb = boxes.nth(i)
        for how in ("check", "span", "label", "js"):
            try:
                if cb.is_checked():
                    break
                if how == "check":
                    cb.check(force=True, timeout=3000)
                elif how == "span":
                    cb.locator(
                        "xpath=following-sibling::span[1] | ../span[1]"
                    ).first.click(timeout=3000)
                elif how == "label":
                    cb.locator("xpath=ancestor::label[1]").first.click(timeout=3000)
                else:
                    cb.evaluate("e => e.click()")
            except Exception:
                pass
        try:
            ticked = cb.is_checked()
        except Exception:
            ticked = False
        print(
            f"  {tag}: acknowledgement {i + 1} of {total}: "
            f"{'ticked' if ticked else 'NOT ticked'}"
        )
        all_ok = all_ok and ticked
    return all_ok


# New Vincent Code End: November 5, 2026


def click_ok_in_dialog(page, text_re, tag, part_number, comment="", click_ok=True):
    """Wait for the popup, tick its acknowledgement checkbox(es), click OK inside it only,
    then check it closed without an error."""
    found = []
    if not wait_dynamic(
        page,
        lambda: found.append(find_dialog(page, text_re)) or found[-1] is not None,
    ):
        print(f"  {tag}: the popup did not appear.")
        save_screenshot(page, f"{tag}_popup_missing_{part_number}")
        return False
    dlg = found[-1]

    # New Vincent Code Start: November 5, 2026
    if comment:
        try:
            ta = dlg.locator("textarea").first
            if ta.count():
                ta.fill(comment)
                print(f"  {tag}: Comments filled: {comment}")
        except Exception as e:
            print(f"  {tag}: could not fill Comments ({type(e).__name__}).")

    if not tick_dialog_checkboxes(page, dlg, tag):
        print(f"  {tag}: WARNING: a checkbox could not be ticked.")
        if click_ok:
            print(f"  {tag}: OK NOT clicked.")
            save_screenshot(page, f"{tag}_checkbox_failed_{part_number}")
            return False
    try:
        # the acknowledgement text scrolls; some popups enable OK only at the bottom
        dlg.evaluate(_SCROLL_DIALOG_JS)
    except Exception:
        pass
    page.wait_for_timeout(300)
    # New Vincent Code End: November 5, 2026

    ok = find_clickable(dlg, OK_BUTTON_RE)
    if ok.count() == 0:
        print(f"  {tag}: the OK button was not found in the popup.")
        save_screenshot(page, f"{tag}_ok_missing_{part_number}")
        return False
    try:
        print(
            f"  {tag}: FOUND the OK button ({ok.count()} match). "
            f"visible={ok.last.is_visible()} enabled={ok.last.is_enabled()}"
        )
    except Exception:
        print(f"  {tag}: FOUND the OK button.")

    if tag == "submit" and UPDATE_STAMPS and COL_FINISH_STAMP:
        try:
            changed = mark_finish_stamp(part_number)
            print(
                f"  DATABASE UPDATED: {COL_FINISH_STAMP} = NOW() for part {part_number} "
                f"({changed} row(s) changed)."
            )
        except Exception as e:
            print(f"  FINISH STAMP FAILED: {type(e).__name__}: {str(e).strip()[:300]}")

    if not click_ok:
        save_screenshot(page, f"{tag}_ready_for_ok_{part_number}")
        print(f"  {tag}: OK was NOT clicked (SUBMIT_CLICK_OK is off). Popup left open.")
        return "DRY_RUN"
    # New Vincent Code Start: November 5, 2026
    try:
        ok.last.click(timeout=T(5000))
    except Exception as e:
        print(
            f"  {tag}: could not click OK ({type(e).__name__}); it is probably still disabled."
        )
        save_screenshot(page, f"{tag}_ok_disabled_{part_number}")
        return False
    # New Vincent Code End: November 5, 2026
    print(f"Clicked OK ({tag}).")
    settle(page)
    if not wait_dynamic(page, lambda: find_dialog(page, text_re) is None):
        print(f"  {tag}: the popup is still open after OK. Check the page.")
        save_screenshot(page, f"{tag}_popup_stuck_{part_number}")
        return False
    if error_shown(page):
        print(f"  {tag}: an error message is showing after OK.")
        save_screenshot(page, f"{tag}_error_{part_number}")
        return False
    return True


def sign_all_and_submit(page, part_number):
    """On Form 1: 1) SIGN ALL FORMS  2) OK in its popup  3) SUBMIT  4) OK in 'Submit FAIR to Customer'.
    After step 4 the FAIR is with the customer and can NOT be edited. A problem is only reported.
    Returns True when SIGN ALL FORMS succeeded (and, if SUBMIT_FAIR is on, SUBMIT succeeded too).
    """
    if not SIGN_ALL_FORMS:
        print("  Sign All Forms NOT run: SIGN_ALL_FORMS is off.")
        return False

    track_requests(
        page
    )  # so the requests started by the clicks below are seen as "busy"

    # Step 1: SIGN ALL FORMS
    btn = wait_button(page, SIGN_ALL_BUTTON_RE)
    if btn is None:
        print("  The SIGN ALL FORMS button was not found (or it is disabled).")
        save_screenshot(page, f"signall_missing_{part_number}")
        return False
    btn.scroll_into_view_if_needed(timeout=T(5000))
    btn.click(timeout=T(5000))
    print("Clicked SIGN ALL FORMS.")

    # Step 2: OK in the "Forms 2 and 3 have not been signed/locked" popup
    if not click_ok_in_dialog(page, SIGN_ALL_DIALOG_RE, "signall", part_number):
        return False
    save_screenshot(page, f"signall_done_{part_number}")
    print("  All forms signed/locked.")

    if not SUBMIT_FAIR:
        print("  Submit NOT run: SUBMIT_FAIR is off.")
        return True

    # Step 3: SUBMIT (it becomes clickable after the forms are signed)
    submit = wait_button(page, SUBMIT_BUTTON_RE)
    if submit is None:
        print("  The SUBMIT button was not found (or it is disabled).")
        save_screenshot(page, f"submit_missing_{part_number}")
        return False
    submit.scroll_into_view_if_needed(timeout=T(5000))
    submit.click(timeout=T(5000))
    print("Clicked SUBMIT.")

    # Step 4a: popup 1 (warning "You will not be able to edit it further"): click OK to reach popup 2.
    if not click_ok_in_dialog(page, SUBMIT_WARN_DIALOG_RE, "submit_warn", part_number):
        return False

    # Step 4b: popup 2 (Comments + acknowledgement + final OK). Dry run unless SUBMIT_CLICK_OK=true.
    result = click_ok_in_dialog(
        page,
        SUBMIT_ACK_DIALOG_RE,
        "submit",
        part_number,
        comment=SUBMIT_COMMENT,
        click_ok=SUBMIT_CLICK_OK,
    )
    if result == "DRY_RUN":
        print("  Dry run: final OK seen, NOT clicked. The FAIR was NOT submitted.")
        return "DRY_RUN"
    if not result:
        return False
    save_screenshot(page, f"submit_done_{part_number}")
    print("  FAIR submitted to the customer.")
    return True


# New Vincent Code End: November 4, 2026


# New Vincent Code Start: November 14, 2026
def _sign_complete_attempt(page, ctx, part, na_set):
    """ONE try at SIGN CHECKLIST -> COMPLETE CHECKLIST.
    Returns True only when the checklist is really locked (UNLOCK CHECKLIST detected) and nothing is
    pending. Returns False on any problem, so sign_and_complete can try again."""

    # Already locked (an earlier try worked but was slow to show it): never sign twice.
    if checklist_is_locked(page):
        print("  Checklist: already locked (UNLOCK CHECKLIST is showing).")
        return True

    fr = confirmation_open(page)
    if fr is None:
        # Safety 1: every answer must still be what the script set.
        problems = answers_still_set(ctx, na_set)
        if problems:
            print("  NOT signing: the answers on the page are not as expected:")
            for p in problems:
                print(f"    - {p}")
            save_screenshot(page, f"checklist_answers_changed_{part.number}")
            return False

        # Step 1: SIGN CHECKLIST (there are two: top right and at the bottom; the first is used).
        btn = find_clickable(ctx, SIGN_BUTTON_RE)
        if btn.count() == 0:
            print("  Checklist: the SIGN CHECKLIST button was not found.")
            save_screenshot(page, f"checklist_sign_missing_{part.number}")
            return False
        target = btn.first
        target.scroll_into_view_if_needed(timeout=T(5000))
        target.click(timeout=T(5000))
        print("Clicked SIGN CHECKLIST.")

        # Step 2: wait for the "Net-Inspect Confirmation" popup.
        found = []
        if not wait_dynamic(
            page,
            lambda: found.append(confirmation_open(page)) or found[-1] is not None,
            label="after clicking SIGN CHECKLIST",
        ):
            print(
                "  Checklist: the confirmation box did not appear after SIGN CHECKLIST."
            )
            save_screenshot(page, f"checklist_confirm_missing_{part.number}")
            return False
        fr = found[-1]
    else:
        print(
            "  Checklist: the confirmation box is already open; going straight to COMPLETE CHECKLIST."
        )

    # Step 3: COMPLETE CHECKLIST, searched inside the popup only (never the whole page).
    box = fr.locator("[role=dialog], .k-window, .modal, .modal-dialog").filter(
        has_text=CONFIRM_TITLE
    )
    if box.count() == 0:
        print("  Checklist: could not isolate the confirmation box; NOT clicking.")
        save_screenshot(page, f"checklist_box_unscoped_{part.number}")
        return False
    complete = find_clickable(box.first, COMPLETE_BUTTON_RE)
    if complete.count() == 0:
        print("  Checklist: COMPLETE CHECKLIST was not found in the confirmation box.")
        save_screenshot(page, f"checklist_complete_missing_{part.number}")
        return False

    # Click COMPLETE and watch the server call it triggers (a 4xx/5xx means it was NOT completed).
    def _is_write(r):
        try:
            return (
                r.request.method in ("POST", "PUT", "PATCH")
                and "net-inspect.com" in r.url
            )
        except Exception:
            return False

    got = None
    try:
        with page.expect_response(_is_write, timeout=10000) as info:
            complete.last.click(timeout=T(5000))
        got = info.value
    except PWTimeout:
        print("  (no request seen within 10 s after COMPLETE CHECKLIST)")
    print("Clicked COMPLETE CHECKLIST.")
    if got is not None:
        print(
            f"  Complete request: {got.request.method} {got.url} -> HTTP {got.status}"
        )
        if got.status >= 400:
            print("  Checklist: COMPLETE was REJECTED by the server.")
            save_screenshot(page, f"checklist_complete_rejected_{part.number}")
            return False
    settle(page)

    # Step 4: the popup must close and no error may be showing.
    if not wait_dynamic(
        page,
        lambda: confirmation_open(page) is None,
        label="after clicking COMPLETE CHECKLIST",
    ):
        print("  Checklist: the confirmation box is still open. Check the page.")
        save_screenshot(page, f"checklist_complete_stuck_{part.number}")
        return False
    if error_shown(page):
        print("  Checklist: an error message is showing after COMPLETE CHECKLIST.")
        save_screenshot(page, f"checklist_complete_error_{part.number}")
        return False

    # Do not leave the tab until the checklist is really locked and nothing is pending.
    if not wait_dynamic(
        page,
        lambda: checklist_is_locked(page),
        label="waiting for the checklist to lock",
    ):
        print("  Checklist: NOT locked after COMPLETE (no UNLOCK CHECKLIST button).")
        save_screenshot(page, f"checklist_not_locked_{part.number}")
        return False
    if not wait_ready_to_leave(page, "Checklist complete", seconds=WAIT_HARD_CAP):
        print("  Checklist: still busy after COMPLETE.")
        return False
    return True


# New Vincent Code End: November 14, 2026


def sign_and_complete(page, ctx, part, done, na_set):
    """1) click SIGN CHECKLIST  2) the 'Net-Inspect Confirmation' popup shows
    3) click COMPLETE CHECKLIST inside that popup  4) check the popup closed without an error
    5) check UNLOCK CHECKLIST is showing (locked). If 5 fails, the whole thing is tried again.
    After this Net-Inspect allows NO further edits."""

    # New Vincent Code Start: November 14, 2026
    # Fallback: repeat SIGN -> COMPLETE until UNLOCK CHECKLIST is detected (at most SIGN_TRIES tries).
    locked = False
    for attempt in range(1, SIGN_TRIES + 1):
        if attempt > 1:
            print(
                f"  Checklist: NOT locked yet. Try {attempt} of {SIGN_TRIES} "
                f"in {SIGN_RETRY_WAIT:g} s..."
            )
            page.wait_for_timeout(int(SIGN_RETRY_WAIT * 1000))
            # The page may have re-rendered: take the checklist frame again.
            ctx = (
                find_frame_with_text(
                    page, "First Article Review Checklist", timeout=patient(5)
                )
                or ctx
            )
        if _sign_complete_attempt(page, ctx, part, na_set):
            locked = True
            break
    if not locked:
        print(
            f"  Checklist: NOT locked after {SIGN_TRIES} tries; NOT going on to Form 1."
        )
        save_screenshot(page, f"checklist_lock_failed_{part.number}")
        return "CHECKLIST_FAILED"
    # New Vincent Code End: November 14, 2026

    save_screenshot(page, f"checklist_completed_{part.number}")
    print(f"  Checklist: {done} questions answered, signed and completed.")
    # Signed: now go back to the Form 1 tab.
    # New Vincent Code Start: November 4, 2026
    handle_unsaved_modal(page, "after COMPLETE CHECKLIST")
    # New Vincent Code Start: November 5, 2026
    # FAIR_SUBMITTED is returned only when SIGN ALL FORMS and SUBMIT both worked (login.py sets
    # isFinish = 1 and finishRemarks = RPA for that status). Any other outcome stays CHECKLIST_COMPLETED,
    # so isFinish stays 0.
    submitted = False
    if go_to_form_1(page, part.number):
        submitted = sign_all_and_submit(page, part.number)
    # New Vincent Code End: November 5, 2026
    # New Vincent Code End: November 4, 2026
    if submitted == "DRY_RUN":
        return "FAIR_READY_TO_SUBMIT"
    if submitted and SUBMIT_FAIR:
        return "FAIR_SUBMITTED"
    return "CHECKLIST_COMPLETED"
