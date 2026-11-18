"""Part Number Index (Form 1): add sub-parts of an XA main part.

The list of sub-parts comes from the BOM (cadcam_parts / cadcam_subparts), the same logic as
makeTree() in the PHP code (see make_tree()). The lot tables (ppic_lotlist) are only used to find
the lot of a non-XP sub-part, so that its Batch Number (FAIR Identifier) can be traced.
No JOINs are used anywhere; related tables are reached with subqueries.

Which sub-parts are added:
  * XP sub-part (AUTO_FAIR_PREFIX): added only when it has isSub = 1 in the
    Net-Inspect data table. The form fills its FAIR Identifier by itself once
    the Part Number is chosen, so the script does not touch it.
  * Every other sub-part: taken from the BOM tree, no isSub check. Its FAIR Identifier is the Batch
    Number of its lot, traced with the same steps as vlt_getMaterials().
"""

import difflib
import re
import time


from login import (
    COL_ID,
    COL_PART_NAME,
    COL_PART_NUMBER,
    DB_TABLE,
    T,
    env_bool,
    env_str,
    find_frame_with_text,
    get_conn,
    ident,
    patient,
    poll,
    save_html,
    save_screenshot,
)

# --- settings (all optional, read from .env) ---

FILL_PART_INDEX = env_bool("FILL_PART_INDEX", False)

COL_IS_SUB = env_str("COL_IS_SUB", "isSub")  # 1 = the part is a sub-part (XP only)

MAIN_PART_PREFIX = env_str(
    "MAIN_PART_PREFIX", env_str("PART_PREFIX", "XA")
)  # main parts start with XA

AUTO_FAIR_PREFIX = env_str(
    "AUTO_FAIR_PREFIX", "XP"
)  # sub-parts starting with this need isSub = 1 and get their FAIR Identifier from the form itself

LOT_TABLE = env_str("LOT_TABLE", "ppic_lotlist")

LOT_PART_TABLE = env_str("LOT_PART_TABLE", "cadcam_parts")

LOT_ACCESSORY_TABLE = env_str(
    "LOT_ACCESSORY_TABLE", "cadcam_accessories"
)  # identifier 2 lots (accessories) get their number from here

LOT_SUBPART_TABLE = env_str(
    "LOT_SUBPART_TABLE", "cadcam_subparts"
)  # BOM: partId (main part) -> childId, identifier 2 = accessory

PO_LIST_TABLE = env_str("PO_LIST_TABLE", "sales_polist")  # used by mother_lot()

PART_INDEX_LOT = env_str(
    "PART_INDEX_LOT"
)  # for testing: use this lot instead of the part's own lot

# Part Type: fixed value for every NON-XP sub-part (leave empty in .env to skip it).
PART_TYPE_NON_XP = env_str("PART_TYPE_NON_XP", "COTS (or equivalent)")

PART_TYPE_HEADER = env_str("PART_TYPE_HEADER", "Part Type")  # text of the column header

PART_TYPE_COL_ID = env_str(
    "PART_TYPE_COL_ID", ""
)  # optional: ag-Grid col-id, skips the header lookup

SUPPLIER_COL_ID = env_str("SUPPLIER_COL_ID", "supplierName")

try:

    SUPPLIER_MATCH_MIN = float(
        env_str("SUPPLIER_MATCH_MIN", "0.5")
    )  # 0..1, lower = looser match

except (TypeError, ValueError):

    SUPPLIER_MATCH_MIN = 0.5


def _int_env(name, default):

    try:

        return max(1, int(env_str(name, str(default))))

    except (TypeError, ValueError):

        return default


INDEX_CELL_WAIT = _int_env(
    "PART_INDEX_CELL_WAIT", 5
)  # seconds to keep trying to open a cell

INDEX_MAX_PARTS = _int_env("PART_INDEX_MAX_PARTS", 10)  # most sub-parts to add

MAX_DEPTH = 6

# New Vincent Code Start: October 29, 2026
# No Enter and no Tab in the Part Number Index. Each field is: click it, type the value, then click the
# next field (clicking away commits the typed value). See click_field().
# While the index is filled, any click on SAVE FAIR is cancelled (finish_fair() clicks it later, after
# the guard is off). Every cancelled click is listed in the log with the key that caused it.
BLOCK_SAVE_FAIR = env_bool("PART_INDEX_BLOCK_SAVE_FAIR", True)
# New Vincent Code End: October 28, 2026

# New Vincent Code Start: October 28, 2026
# Also add the accessories (cadcam_subparts.identifier = 2 -> cadcam_accessories) of the main part and of
# its sub-parts. makeTree() only follows identifier = 1, so without this they are missing from the index.
INDEX_ACCESSORIES = env_bool("PART_INDEX_ACCESSORIES", True)
# New Vincent Code End: October 28, 2026

MAX_TRACE_STEPS = 50  # safety cap for the material trace loop


OPTION_SEL = (
    "[role='option'], ngb-typeahead-window button, .dropdown-menu .dropdown-item, "
    ".k-list-item, li.k-item, .autocomplete-item"
)

FAIR_NO_SRC = (
    r"FAIR\s*#\s*(\d+)"  # dropdown line: 'Not Submitted | FAIR # 2594 | 10/28/26'
)

ICON_WORDS = {
    "warning",
    "error",
    "info",
    "report_problem",
    "priority_high",
    "help",
    "help_outline",
    "close",
    "cancel",
}  # Material-icon ligatures that show up as text in an empty/invalid cell

# Finds the FIRST suggestion of the Part Number dropdown: the smallest visible element that
# contains the part number and exactly one "FAIR # n" (an option has one; the list has many).
FIND_FIRST_OPTION_JS = r"""([num, reSrc]) => {
  document.querySelectorAll('[data-pi-first-option]').forEach(e => e.removeAttribute('data-pi-first-option'));
  const re = new RegExp(reSrc, 'g');
  const shown = (e) => {
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const needle = num.toLowerCase();
  const txt = (e) => (e.innerText !== undefined ? e.innerText : e.textContent) || '';
  const cands = [];
  for (const e of document.querySelectorAll('body *')) {
    const t = txt(e);
    if (!t.toLowerCase().includes(needle)) continue;
    const m = t.match(re);
    if (m && m.length === 1 && shown(e)) cands.push(e);
  }
  const leaves = cands.filter(e => !cands.some(o => o !== e && e.contains(o)));
  if (!leaves.length) return null;
  const first = leaves[0];
  first.setAttribute('data-pi-first-option', '1');
  const mm = new RegExp(reSrc).exec(txt(first));
  return {fair: mm ? mm[1] : '', text: txt(first).replace(/\s+/g, ' ').trim()};
}"""

DUMP_DROPDOWN_JS = r"""() => {
  const leaf = [...document.querySelectorAll('body *')].find(
    e => e.children.length === 0 && /FAIR\s*#\s*\d+/.test(e.textContent || ''));
  if (!leaf) return '';
  let e = leaf;
  for (let i = 0; i < 5 && e.parentElement; i++) e = e.parentElement;
  return e.outerHTML;
}"""

CELL_OFFSETS = (0, -6, 6, -10, 10)  # rows are only ~33px tall


def _text(el):

    try:

        return " ".join((el.inner_text() or "").split())

    except Exception:

        return ""


# ----------------------------------------------------------------------------------------------

# Database

# ----------------------------------------------------------------------------------------------


_CONN = None


def _conn():
    # One connection for the whole lookup instead of one per query.
    global _CONN
    if _CONN is None or not _CONN.is_connected():
        _CONN = get_conn()
        _CONN.autocommit = True
    return _CONN


def _close_conn():
    global _CONN
    try:
        if _CONN is not None:
            _CONN.close()
    except Exception:
        pass
    _CONN = None


def _sql(sql, params=None, fetch="all"):
    cur = _conn().cursor(dictionary=True, buffered=True)
    try:
        cur.execute(sql, params or ())
        return cur.fetchone() if fetch == "one" else cur.fetchall()
    finally:
        cur.close()


def _val(row, key):
    return str((row or {}).get(key) or "").strip()


def lookup_part_info(numbers):
    # One query for all candidate sub-parts -> {part_number.lower(): (is_sub, name)}.
    numbers = list(dict.fromkeys(n for n in numbers if n))
    if not numbers:
        return {}
    net = ident(DB_TABLE, "DB_TABLE")
    c_part = ident(COL_PART_NUMBER, "COL_PART_NUMBER")
    c_sub = ident(COL_IS_SUB, "COL_IS_SUB")
    c_name = ident(COL_PART_NAME, "COL_PART_NAME")
    marks = ", ".join(["%s"] * len(numbers))
    rows = _sql(
        f"SELECT TRIM({c_part}) AS part, MAX({c_sub} = 1) AS is_sub, MIN({c_name}) AS name "
        f"FROM {net} WHERE TRIM({c_part}) IN ({marks}) GROUP BY TRIM({c_part})",
        tuple(numbers),
    )
    return {
        str(r["part"]).strip().lower(): (bool(r["is_sub"]), _val(r, "name"))
        for r in rows
    }


def lot_of_part(part_number):

    net = ident(DB_TABLE, "DB_TABLE")

    c_part = ident(COL_PART_NUMBER, "COL_PART_NUMBER")

    c_id = ident(COL_ID, "COL_ID")

    from login import COL_LOT_NUMBER

    if not COL_LOT_NUMBER:

        return ""

    c_lot = ident(COL_LOT_NUMBER, "COL_LOT_NUMBER")

    row = _sql(
        f"SELECT {c_lot} AS lot FROM {net} WHERE TRIM({c_part}) = %s ORDER BY {c_id} ASC LIMIT 1",
        (part_number,),
        fetch="one",
    )

    return str(row.get("lot") or "").strip() if row else ""


def lot3(lot):

    # 26-06-22554-1 -> 26-06-22554 (the lot without its split suffix)

    return "-".join(str(lot).strip().split("-")[:3])


def find_main_lot(lot):

    # Same steps as vlt_buildForLot(): the main lot of the PO of this lot.

    lots = ident(LOT_TABLE, "LOT_TABLE")

    row = _sql(
        f"SELECT poId, identifier FROM {lots} WHERE lotNumber LIKE %s LIMIT 1",
        (lot,),
        fetch="one",
    )

    po_id = int(row["poId"] or 0) if row else 0

    kind = int(row["identifier"] or 0) if row else 0

    main = ""

    if po_id > 0 and kind == 1:

        r = _sql(
            f"SELECT lotNumber FROM {lots} WHERE poId = %s AND partLevel = 1 AND identifier = 1 LIMIT 1",
            (po_id,),
            fetch="one",
        )

        main = r["lotNumber"] if r else ""

    elif po_id > 0 and kind in (2, 4):

        r = _sql(
            f"SELECT lotNumber FROM {lots} WHERE poId = %s AND identifier = %s LIMIT 1",
            (po_id, kind),
            fetch="one",
        )

        main = r["lotNumber"] if r else ""

    return lot3(main or lot)


def mother_lot(lot):
    """Port of lotMainDetails() (rose_prodfunctions.php), written with subqueries only.

    The PHP INNER JOINs were:
        ppic_lotlist a  ->  sales_polist b ON a.poId = b.poId
                        ->  ppic_lotlist c ON b.poId = c.poId AND b.partId = c.partId
                                          AND c.identifier = 1
    i.e. the identifier = 1 lot(s) of the PO line(s) that the given lot belongs to.
    The PHP keeps the LAST row returned and cuts it to 3 segments; so does this.
    Returns "" when nothing matches (the PHP would return "--").
    """
    lots = ident(LOT_TABLE, "LOT_TABLE")
    po_list = ident(PO_LIST_TABLE, "PO_LIST_TABLE")
    rows = _sql(
        f"SELECT lotNumber FROM {lots} WHERE identifier = 1 AND EXISTS ("
        f"SELECT 1 FROM {po_list} WHERE {po_list}.poId = {lots}.poId "
        f"AND {po_list}.partId = {lots}.partId "
        f"AND {po_list}.poId IN ("
        f"SELECT poId FROM {lots} WHERE lotNumber LIKE %s OR lotNumber LIKE %s))",
        # `lot` is already cut to 3 segments, so also match stored split suffixes (-1, -2 ...)
        (lot, lot + "-%"),
    )
    return lot3(rows[-1]["lotNumber"]) if rows else ""


def part_number_of_lot(kind, part_id):

    # Same as vlt_getPartNumber(): identifier 1 = part, identifier 2 = accessory.
    # Any other identifier has no part number (the PHP page shows it blank too).

    kind = int(kind or 0)

    if kind == 1:

        parts = ident(LOT_PART_TABLE, "LOT_PART_TABLE")

        r = _sql(
            f"SELECT partNumber FROM {parts} WHERE partId = %s LIMIT 1",
            (int(part_id or 0),),
            fetch="one",
        )

        return str(r["partNumber"] or "").strip() if r else ""

    if kind == 2:

        acc = ident(LOT_ACCESSORY_TABLE, "LOT_ACCESSORY_TABLE")

        # Number and name come from the same row, so the Part Name costs no extra query.

        r = _sql(
            f"SELECT accessoryNumber, accessoryName FROM {acc} WHERE accessoryId = %s LIMIT 1",
            (int(part_id or 0),),
            fetch="one",
        )

        number = _val(r, "accessoryNumber")

        if number:

            _ACC_NAMES[number.lower()] = _val(r, "accessoryName")

        return number

    return ""


_ACC_NAMES = (
    {}
)  # accessoryNumber.lower() -> accessoryName, filled while the lot tree is read

# New Vincent Code Start: November 13, 2026
# lot -> inventoryId found by _trace_batch_number(); supplier_alias_of() reads the Supplier Alias with it.
_INV_IDS = {}
# New Vincent Code End: November 13, 2026


def accessory_names_from_bom(main_part_number):
    """Fallback Part Name lookup through the BOM, one query, subqueries only (no JOINs).

    main part (cadcam_parts.partId) -> cadcam_subparts.childId (identifier = 2)
    -> cadcam_accessories.accessoryId -> accessoryName.
    Returns {accessoryNumber.lower(): accessoryName}.
    """
    try:
        parts = ident(LOT_PART_TABLE, "LOT_PART_TABLE")
        subs = ident(LOT_SUBPART_TABLE, "LOT_SUBPART_TABLE")
        acc = ident(LOT_ACCESSORY_TABLE, "LOT_ACCESSORY_TABLE")
        rows = _sql(
            f"SELECT accessoryNumber, accessoryName FROM {acc} WHERE accessoryId IN ("
            f"SELECT childId FROM {subs} WHERE identifier = 2 AND partId IN ("
            f"SELECT partId FROM {parts} WHERE partNumber = %s))",
            (main_part_number,),
        )
        return {
            _val(r, "accessoryNumber").lower(): _val(r, "accessoryName")
            for r in rows
            if _val(r, "accessoryNumber")
        }
    except Exception as e:
        print(
            f"    ! accessory name lookup (BOM) failed: {type(e).__name__}: {str(e).strip()[:200]}"
        )
        return {}


def _collect_sub_parts(parent_lot, depth, found, seen):

    if depth > MAX_DEPTH or len(found) >= 50:

        return

    lots = ident(LOT_TABLE, "LOT_TABLE")

    rows = _sql(
        f"SELECT lotNumber, poId, partId, identifier FROM {lots} WHERE parentLot LIKE %s "
        f"GROUP BY poId, partId ORDER BY lotNumber",
        (parent_lot,),
    )

    for r in rows:

        sub_lot = lot3(r["lotNumber"])

        pn = part_number_of_lot(r["identifier"], r["partId"])

        print(
            f"    tree: parent={parent_lot} lot={sub_lot} "
            f"identifier={r['identifier']} part={pn or '(none)'}"
        )

        if sub_lot in seen:

            continue

        seen.add(sub_lot)

        found.append((sub_lot, pn))

        _collect_sub_parts(sub_lot, depth + 1, found, seen)


def trace_batch_number(lot):
    """Material Batch Number of a lot (port of vlt_getMaterials() in vincent_lotTreeV2.php).

    Follows the lot back to the material withdrawal / booking to get the
    inventory id, then reads batchNumber from warehouse_inventory (or its
    history table). Returns "" when nothing is found (the page shows N/A).
    """
    lot = str(lot or "").strip()
    if not lot:
        return ""
    try:
        return _trace_batch_number(lot)
    except Exception as e:
        print(
            f"    ! batch lookup failed for {lot}: {type(e).__name__}: {str(e).strip()[:200]}"
        )
        return ""


def _trace_batch_number(lot):
    source_lot, inv_id = lot, ""
    _INV_IDS.pop(
        lot, None
    )  # New Vincent Code: November 13, 2026 (never reuse an old inventoryId)

    head = lot.split("-")[0]
    if head.isdigit() and int(head) <= 20:
        ws_table = f"`ppic_workschedule20{head}`"
    else:
        ws_table = "`ppic_workschedule`"

    # Step 1: processCode 254 remarks point to the source lot / withdrawal record.
    row = _sql(
        f"SELECT processRemarks FROM {ws_table} WHERE lotNumber LIKE %s AND processCode = 254 LIMIT 1",
        (lot,),
        fetch="one",
    )
    if row:
        source_lot = _val(row, "processRemarks")
        if not source_lot:
            r = _sql(
                "SELECT finishGoodproductionId FROM warehouse_finishgoodwithdrawaldetail "
                "WHERE finishGoodproductionLotNo LIKE %s LIMIT 1",
                (lot,),
                fetch="one",
            )
            if r:
                source_lot = _val(r, "finishGoodproductionId")
            else:
                r = _sql(
                    "SELECT suppliesproductionLotNo, suppliesproductionId FROM warehouse_supplieswithdrawaldetail "
                    "WHERE suppliesproductionLotNo LIKE %s LIMIT 1",
                    (lot,),
                    fetch="one",
                )
                if r:
                    source_lot = _val(r, "suppliesproductionLotNo")
                    inv_id = _val(r, "suppliesproductionId")
                else:
                    r = _sql(
                        "SELECT accessoryproductionLotNo, accessoryproductionId FROM warehouse_accessorywithdrawaldetails "
                        "WHERE accessoryproductionLotNo LIKE %s LIMIT 1",
                        (lot,),
                        fetch="one",
                    )
                    if r:
                        source_lot = _val(r, "accessoryproductionLotNo")
                        inv_id = _val(r, "accessoryproductionId")
                    else:
                        source_lot = lot
        if source_lot:
            r = _sql(
                "SELECT lotNumber FROM ppic_lotlist WHERE productionTag LIKE %s LIMIT 1",
                (source_lot,),
                fetch="one",
            )
            if r:
                source_lot = _val(r, "lotNumber")

    # Step 2: walk back until a booking / material withdrawal gives the inventory id.
    visited = set()
    for _ in range(MAX_TRACE_STEPS):
        if not source_lot or source_lot in visited:
            break
        visited.add(source_lot)
        again = False

        r = _sql(
            "SELECT bookingId FROM engineering_bookingdetails WHERE lotNumber LIKE %s LIMIT 1",
            (source_lot,),
            fetch="one",
        )
        if r:
            b = _sql(
                "SELECT inventoryId FROM engineering_booking WHERE bookingId LIKE %s LIMIT 1",
                (_val(r, "bookingId"),),
                fetch="one",
            )
            if b:
                inv_id = _val(b, "inventoryId")
        else:
            r = _sql(
                "SELECT materialsInProductionMPRS FROM warehouse_materialproductiondetails "
                "WHERE materialsInProductionLotNo LIKE %s LIMIT 1",
                (source_lot,),
                fetch="one",
            )
            if r:
                mprs = _val(r, "materialsInProductionMPRS")
                w = _sql(
                    "SELECT withdrawMaterialId FROM warehouse_materialwithdrawal "
                    "WHERE TRIM(withdrawMaterialMPRS) LIKE %s LIMIT 1",
                    (mprs,),
                    fetch="one",
                )
                if w:
                    inv_id = _val(w, "withdrawMaterialId")
                else:
                    w = _sql(
                        "SELECT oldWithdrawMaterialId FROM warehouse_materialwithdrawalhistory "
                        "WHERE TRIM(oldWithdrawMaterialMPRS) LIKE %s LIMIT 1",
                        (mprs,),
                        fetch="one",
                    )
                    if w:
                        inv_id = _val(w, "oldWithdrawMaterialId")
            else:
                w = _sql(
                    "SELECT processRemarks FROM ppic_workschedule WHERE lotNumber LIKE %s AND processCode = 254 LIMIT 1",
                    (source_lot,),
                    fetch="one",
                )
                if w:
                    source_lot = _val(w, "processRemarks")
                    again = True
                else:
                    w = _sql(
                        "SELECT id FROM ppic_workschedule WHERE lotNumber LIKE %s "
                        "AND processCode IN (312, 430, 431, 432) AND status = 0 LIMIT 1",
                        (source_lot,),
                        fetch="one",
                    )
                    if not w:
                        w = _sql(
                            "SELECT sourceLotNumber FROM ppic_prslog WHERE lotNumber LIKE %s LIMIT 1",
                            (source_lot,),
                            fetch="one",
                        )
                        if w:
                            source_lot = _val(w, "sourceLotNumber")
                            again = True
                if not again:
                    w = _sql(
                        "SELECT lotNumber FROM ppic_lotlist WHERE parentLot LIKE %s AND identifier = 1 LIMIT 1",
                        (source_lot,),
                        fetch="one",
                    )
                    if w:
                        source_lot = _val(w, "lotNumber")
                        again = True
        if not again:
            break

    if not inv_id:
        return ""

    # New Vincent Code Start: November 13, 2026
    _INV_IDS[lot] = inv_id
    # New Vincent Code End: November 13, 2026
    # Step 3: the batch number is on the inventory record (or its history).
    for table in ("warehouse_inventory", "warehouse_inventoryhistory"):
        r = _sql(
            f"SELECT batchNumber FROM {table} WHERE inventoryId LIKE %s LIMIT 1",
            (inv_id,),
            fetch="one",
        )
        if r:
            return _val(r, "batchNumber")
    return ""


# New Vincent Code Start: November 13, 2026
def supplier_alias_of(batch, lot):
    """Supplier Alias of a sub-part: warehouse_inventory where inventoryId = the inventory id traced from
    the lot (see _trace_batch_number) AND batchNumber = batch (falls back to warehouse_inventoryhistory).
    The part number (cadcam_accessories.accessoryNumber) is NOT compared with dataOne anymore.
    """
    batch, lot = str(batch or "").strip(), str(lot or "").strip()
    inv_id = _INV_IDS.get(lot, "")
    if not batch or not inv_id:
        return ""
    try:
        for table in ("warehouse_inventory", "warehouse_inventoryhistory"):
            r = _sql(
                f"SELECT supplierAlias FROM {table} "
                f"WHERE TRIM(inventoryId) = %s AND TRIM(batchNumber) = %s "
                f"AND TRIM(IFNULL(supplierAlias, '')) <> '' LIMIT 1",
                (inv_id, batch),
                fetch="one",
            )
            if r:
                return _val(r, "supplierAlias")
    except Exception as e:
        print(
            f"    ! supplier lookup failed for lot {lot} / inventory {inv_id} / batch {batch}: "
            f"{type(e).__name__}: {str(e).strip()[:200]}"
        )
    return ""


# New Vincent Code End: November 13, 2026


_COMPANY_WORDS = {
    "co",
    "ltd",
    "limited",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "llc",
    "gmbh",
    "sdn",
    "bhd",
    "pte",
    "plc",
    "the",
    "and",
    "of",
}


def _tokens(text):
    return [t for t in re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split() if t]


def _sig_tokens(text):
    # Words that identify a company (without Co., Ltd., Inc. ...).
    sig = [t for t in _tokens(text) if t not in _COMPANY_WORDS]
    return sig or _tokens(text)


def _tok_match(a, b):
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return len(a) >= 5 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def supplier_score(alias, option):
    """0..1 similarity between the database Supplier Alias and a dropdown option."""
    a, b = _sig_tokens(alias), _sig_tokens(option)
    if not a or not b:
        return 0.0
    na, nb = " ".join(_tokens(alias)), " ".join(_tokens(option))
    if na and nb and min(len(na), len(nb)) >= 4 and (na in nb or nb in na):
        return 1.0
    hit = sum(1 for x in a if any(_tok_match(x, y) for y in b))
    return max(hit / len(a), difflib.SequenceMatcher(None, na, nb).ratio())


# New Vincent Code Start: October 28, 2026
def bom_part_number(part_id):
    # getPartDetails(): the part number of one cadcam_parts row.
    parts = ident(LOT_PART_TABLE, "LOT_PART_TABLE")
    r = _sql(
        f"SELECT partNumber FROM {parts} WHERE partId = %s LIMIT 1",
        (int(part_id or 0),),
        fetch="one",
    )
    return _val(r, "partNumber")


def make_tree(part_id, acc=None, depth=0, seen=None):
    """Port of makeTree($partId, $acc) (PHP). Returns {} when the part has no sub-parts.

    * partTypeFlag = 2          -> {}
    * partTypeFlag != 0 (a sub-part itself) -> the tree of its parent (cadcam_subparts, identifier = 1)
    * partTypeFlag = 0 (main)   -> {"main": partId, "subparts": {childId: <tree of the child>}, "partNumber": ...}
    Only identifier = 1 rows of cadcam_subparts are followed (identifier 2 = accessories are not).
    `depth` / `seen` only guard against a BOM that points back to itself.
    """
    parts = ident(LOT_PART_TABLE, "LOT_PART_TABLE")
    subs = ident(LOT_SUBPART_TABLE, "LOT_SUBPART_TABLE")
    seen = set() if seen is None else seen
    tree = {}

    if not acc:
        row = _sql(
            f"SELECT partNumber, partTypeFlag FROM {parts} WHERE partId = %s",
            (int(part_id or 0),),
            fetch="one",
        )
        if not row:
            return {}
        flag = int(row.get("partTypeFlag") or 0)
        if flag == 2:
            return {}
        if flag != 0:
            child = _sql(
                f"SELECT parentId FROM {subs} WHERE childId = %s AND identifier = 1",
                (int(part_id or 0),),
                fetch="one",
            )
            if child and depth < MAX_DEPTH:
                return make_tree(child["parentId"], None, depth + 1, seen)
            return {}

    current = acc if acc else part_id
    key = "submain" if acc else "main"
    tree[key] = "end"
    seen.add(int(current or 0))

    rows = _sql(
        f"SELECT childId FROM {subs} WHERE parentId = %s AND identifier = 1",
        (int(current or 0),),
    )
    if rows:
        tree[key] = current
        tree["subparts"] = {}
        for r in rows:
            child_id = int(r["childId"] or 0)
            if depth >= MAX_DEPTH or child_id in seen:
                continue  # safety: a BOM loop or an unreasonably deep tree
            tree["subparts"][child_id] = make_tree(part_id, child_id, depth + 1, seen)

    tree["partNumber"] = bom_part_number(current)
    return {} if tree.get("main") == "end" else tree


def flatten_tree(tree, out=None):
    # [(partNumber, partId)] of every sub-part under the main part, depth first.
    out = [] if out is None else out
    for child_id, sub in (tree.get("subparts") or {}).items():
        number = (sub or {}).get("partNumber") or bom_part_number(child_id)
        if number:
            out.append((number, child_id))
        flatten_tree(sub or {}, out)
    return out


def bom_children_report(part_id):
    """One log line with every cadcam_subparts row of a part, grouped by identifier (for debugging)."""
    subs = ident(LOT_SUBPART_TABLE, "LOT_SUBPART_TABLE")
    rows = _sql(
        f"SELECT childId, identifier FROM {subs} WHERE parentId = %s ORDER BY identifier, childId",
        (int(part_id or 0),),
    )
    groups = {}
    for r in rows:
        groups.setdefault(int(r["identifier"] or 0), []).append(int(r["childId"] or 0))
    text = (
        "; ".join(f"identifier {k}: {v}" for k, v in sorted(groups.items()))
        or "no rows"
    )
    print(f"    BOM rows of partId {part_id} in {LOT_SUBPART_TABLE}: {text}")


def bom_accessories(part_id):
    """Accessories (identifier = 2) of one part -> [(accessoryNumber, accessoryName)]."""
    subs = ident(LOT_SUBPART_TABLE, "LOT_SUBPART_TABLE")
    acc = ident(LOT_ACCESSORY_TABLE, "LOT_ACCESSORY_TABLE")
    rows = _sql(
        f"SELECT accessoryNumber, accessoryName FROM {acc} WHERE accessoryId IN ("
        f"SELECT childId FROM {subs} WHERE parentId = %s AND identifier = 2) ORDER BY accessoryId",
        (int(part_id or 0),),
    )
    return [
        (_val(r, "accessoryNumber"), _val(r, "accessoryName"))
        for r in rows
        if _val(r, "accessoryNumber")
    ]


def bom_sub_parts(part_number):
    """Sub-parts of a main part number through the BOM -> [(partNumber, partId or 0 for accessories)]."""
    parts = ident(LOT_PART_TABLE, "LOT_PART_TABLE")
    ids = _sql(
        f"SELECT partId FROM {parts} WHERE TRIM(partNumber) = %s ORDER BY partId",
        (part_number.strip(),),
    )
    if not ids:
        print(f"  ! {part_number} was not found in {LOT_PART_TABLE}.")
        return []
    found, main_id = [], 0
    for r in ids:
        tree = make_tree(r["partId"])
        if tree:
            main_id = int(tree["main"])
            found = flatten_tree(tree)
            break
    if not main_id:
        main_id = int(ids[0]["partId"])
    try:
        bom_children_report(main_id)
    except Exception as e:
        print(f"    ! BOM report failed: {type(e).__name__}: {str(e).strip()[:200]}")
    if INDEX_ACCESSORIES:
        try:
            have = {n.lower() for n, _ in found}
            for pid in [main_id] + [i for _, i in found]:
                for number, name in bom_accessories(pid):
                    if number.lower() in have:
                        continue
                    have.add(number.lower())
                    _ACC_NAMES[number.lower()] = name
                    found.append((number, 0))
        except Exception as e:
            print(
                f"    ! accessory lookup failed: {type(e).__name__}: {str(e).strip()[:200]}"
            )
    return found


def _lots_by_part_number(lot):
    """{partNumber.lower(): sub_lot} from the lot tree. Only used to find the lot (Batch Number)
    of the non-XP sub-parts; the list of sub-parts itself comes from the BOM."""
    main_lot = find_main_lot(lot)
    root = main_lot
    try:
        mother = mother_lot(main_lot)
        if mother:
            root = mother
    except Exception as e:
        print(
            f"  ! mother lot lookup failed ({type(e).__name__}: {str(e).strip()[:200]}); "
            f"using the main lot as tree root."
        )
    found, seen = [], {main_lot, root}
    _collect_sub_parts(root, 1, found, seen)
    return main_lot, {pn.lower(): sub_lot for sub_lot, pn in found if pn}


def find_sub_parts(part_number, lot):
    """Sub-parts of the main part from the BOM (make_tree), like the PHP makeTree().

    Returns (main_lot, [(part_number, sub_lot), ...]). sub_lot is "" for XP sub-parts (not needed)
    and for sub-parts whose lot cannot be found (their Batch Number then stays empty).
    """
    bom = bom_sub_parts(part_number)
    needs_lot = [
        n for n, _ in bom if not (AUTO_FAIR_PREFIX and n.startswith(AUTO_FAIR_PREFIX))
    ]
    main_lot, lots = "", {}
    if needs_lot and lot:
        try:
            main_lot, lots = _lots_by_part_number(lot)
        except Exception as e:
            print(
                f"  ! lot lookup for the Batch Number failed ({type(e).__name__}: {str(e).strip()[:200]})."
            )
    elif needs_lot:
        print(
            "  ! No lot number for this part: the Batch Number of the non-XP sub-parts stays empty."
        )
    return main_lot, [(number, lots.get(number.lower(), "")) for number, _ in bom]


# New Vincent Code End: October 28, 2026


def open_index_section(page):

    ctx = find_frame_with_text(page, "Part Number Index", timeout=patient(15))

    if ctx is None:

        raise RuntimeError("the Part Number Index section was not found")

    heading = ctx.get_by_text("Part Number Index").first

    heading.scroll_into_view_if_needed(timeout=T(5000))

    return ctx, heading


def set_page_size(ctx, heading, size="100"):

    picker = heading.locator(
        "xpath=following::div[contains(@class,'ag-picker-field-wrapper') and @aria-label='Page Size'][1]"
    )

    picker.scroll_into_view_if_needed(timeout=T(8000))

    shown = picker.locator(".ag-picker-field-display")

    if _text(shown) == size:

        print(f"  Page Size is already {size}.")

        return

    picker.click(timeout=T(5000))

    option = ctx.locator("[role='option']").filter(
        has_text=re.compile(r"^\s*" + re.escape(size) + r"\s*$")
    )

    option.first.wait_for(state="visible", timeout=T(5000))

    option.first.click(timeout=T(5000))

    if not poll(ctx.page, lambda: _text(shown) == size, 5):

        raise RuntimeError(
            f"Page Size did not change to {size} (it shows {_text(shown)!r})"
        )

    print(f"  Page Size set to {size}.")


def click_add_part(ctx, heading):

    btn = heading.locator("xpath=following::button[contains(@class,'popup-btn')][1]")

    btn.scroll_into_view_if_needed(timeout=T(5000))

    btn.wait_for(state="visible", timeout=T(8000))

    if "add part" not in _text(btn).lower():

        raise RuntimeError(
            f"the button after the Part Number Index heading is {_text(btn)!r}, not '+ Add Part'"
        )

    btn.click(timeout=T(5000))

    print("  Clicked + Add Part.")


def _find_dialog(page):

    for fr in page.frames:

        try:

            loc = fr.locator(
                "kendo-dialog:visible, .k-dialog:visible, [role='dialog']:visible, [role='alertdialog']:visible"
            )

            if loc.count():

                return fr, loc.first

        except Exception:

            pass

    return None


def click_modal_ok(page, seconds=5, quiet=False):

    # Wait (up to `seconds`) for a modal in any frame and click its Ok button.

    end = time.time() + seconds

    hit = None

    while hit is None and time.time() < end:

        hit = _find_dialog(page)

        if hit is None:

            page.wait_for_timeout(250)

    if hit is None:

        if not quiet:

            print("  No modal appeared after + Add Part.")

        return False

    fr, dlg = hit

    print(f"  Modal: {_text(dlg)[:150]!r}")

    ok = dlg.locator(
        "button[aria-label='Ok'], button:has-text('Ok'), button:has-text('OK')"
    ).first

    if ok.count() == 0:

        ok = fr.locator("button[aria-label='Ok']:visible").first

    ok.click(timeout=T(5000))

    poll(page, lambda: _find_dialog(page) is None, 8)

    print("  Clicked Ok in the modal.")

    return True


def rows_of(grid):

    return grid.locator(".ag-center-cols-container .ag-row")


def row_by_index(grid, i):

    one = grid.locator(f".ag-center-cols-container .ag-row[row-index='{i}']")

    return one.first if one.count() else rows_of(grid).nth(i)


def header_x(ctx, text):

    heads = ctx.get_by_text(text)

    for i in range(heads.count()):

        try:

            if heads.nth(i).is_visible():

                b = heads.nth(i).bounding_box()

                if b:

                    return b["x"] + b["width"] / 2

        except Exception:

            pass

    return None


def find_cell_input(ctx, grid, ri, placeholder):
    # The editor can be inside the row or in a popup outside the grid (Angular editor),
    # so look in the row first, then anywhere in the frame (only one editor is open at a time).
    vis = f"input[placeholder='{placeholder}']:visible"
    in_row = grid.locator(f".ag-row[row-index='{ri}'] {vis}")
    if in_row.count() > 0:
        return in_row.first
    anywhere = ctx.locator(vis)
    if anywhere.count() > 0:
        return anywhere.first
    return None


def open_cell_input(ctx, grid, ri, header_text, placeholder, label):

    # Click the cell of this column on row ri (x from the column header, y from the row) until its input shows.
    # Odd tries use a double-click, because some ag-Grid setups only start editing on double-click.

    deadline = time.time() + INDEX_CELL_WAIT

    tries = 0

    while tries == 0 or time.time() < deadline:

        row = row_by_index(grid, ri)

        row.scroll_into_view_if_needed(timeout=T(5000))

        x = header_x(ctx, header_text)

        b = row.bounding_box()

        if x is None or not b:

            raise RuntimeError(
                f"could not find where the {label} cell of row {ri + 1} is on the page"
            )

        y = b["y"] + b["height"] / 2 + CELL_OFFSETS[tries % len(CELL_OFFSETS)]

        if tries % 2 == 1:

            ctx.page.mouse.dblclick(x, y)

        else:

            ctx.page.mouse.click(x, y)

        tries += 1

        if poll(
            ctx.page, lambda: find_cell_input(ctx, grid, ri, placeholder) is not None, 2
        ):

            return find_cell_input(ctx, grid, ri, placeholder)

        print(
            f"  {label} box not showing after click {tries}; clicking the cell again..."
        )

    raw = f"input[placeholder='{placeholder}']"
    row_sel = f".ag-row[row-index='{ri}'] {raw}"
    raise RuntimeError(
        f"the {label} box of row {ri + 1} was not found "
        f"(in row={grid.locator(row_sel).count()}, in frame={ctx.locator(raw).count()}, "
        f"visible in frame={ctx.locator(raw + ':visible').count()})"
    )


# New Vincent Code Start: October 29, 2026
def click_field(ctx, grid, ri, field):
    """Click one cell of row ri. Clicking a cell commits the value typed in the cell that was open before.

    field: "partNumber" | "partName" | "partType" | "supplier" | "fair"
    """
    row = row_by_index(grid, ri)
    row.scroll_into_view_if_needed(timeout=T(5000))
    col_ids = {"partName": "partName", "supplier": SUPPLIER_COL_ID}
    if field == "partType":
        col_ids["partType"] = PART_TYPE_COL_ID or _col_id_by_header(
            grid, PART_TYPE_HEADER
        )
    cid = col_ids.get(field)
    if cid:
        cell = _row_cell(grid, ri, cid)
        if cell.count():
            cell.click(timeout=T(5000))
            return
    header = {"partNumber": "15. Part Number", "fair": "18. FAIR Identifier"}.get(field)
    x = header_x(ctx, header) if header else None
    b = row.bounding_box()
    if x is None or not b:
        raise RuntimeError(f"could not find the {field} cell of row {ri + 1} to click")
    ctx.page.mouse.click(x, b["y"] + b["height"] / 2)


# New Vincent Code End: October 29, 2026


def choose_option(ctx, box, text=None, strict=False):

    # Pick the suggestion that contains `text` (or the first suggestion when text is None).

    # strict=True: if no suggestion contains `text`, stop with an error instead of taking the first one.

    opts = ctx.locator(OPTION_SEL)

    if text:

        opts = opts.filter(has_text=re.compile(re.escape(text), re.I))

    try:

        opts.first.wait_for(state="visible", timeout=T(5000))

        opts.first.click(timeout=T(5000))

    except Exception:

        if strict:

            raise RuntimeError(f"no suggestion contains {text!r}")

        raise RuntimeError(
            f"no suggestion list found to choose {text or 'the first option'!r} from"
        )


def _filled(cell):
    # True when the cell has real text (an icon word such as "warning" does not count).
    try:
        if cell.count() == 0:
            return False
    except Exception:
        return False
    t = _text(cell).strip().lower()
    # New Vincent Code Start: October 29, 2026
    # An open Part Name editor shows its character counter ("250 / 250 remaining"); that is not a value.
    if re.fullmatch(r"\d+\s*/\s*\d+\s*remaining", t):
        return False
    # New Vincent Code End: October 29, 2026
    return bool(t) and t not in ICON_WORDS


def selection_done(ctx, grid, ri, fair_no, wait=8):
    # After picking a suggestion the form fills Part Name and FAIR Identifier by itself.
    name_cell = grid.locator(f".ag-row[row-index='{ri}'] [col-id='partName']").first
    cells = grid.locator(f".ag-row[row-index='{ri}'] .ag-cell")

    def ok():
        if not _filled(name_cell):
            return False
        if fair_no:
            return (
                cells.filter(
                    has_text=re.compile(rf"^\s*{re.escape(fair_no)}\s*$")
                ).count()
                > 0
            )
        return True

    return poll(ctx.page, ok, wait)


def dump_dropdown(ctx):
    try:
        html = ctx.evaluate(DUMP_DROPDOWN_JS)
        if html:
            save_html("part_index_dropdown.html", html)
            print(
                "  (saved the dropdown HTML to screenshots\\part_index_dropdown.html)"
            )
    except Exception:
        pass


def pick_first_option(ctx, grid, ri, box, number):
    # XP sub-part: choose the FIRST suggestion of the dropdown, then make sure the row really filled in.
    info = None
    end = time.time() + 8
    while info is None and time.time() < end:
        info = ctx.evaluate(FIND_FIRST_OPTION_JS, [number, FAIR_NO_SRC])
        if info is None:
            ctx.page.wait_for_timeout(300)

    fair_no = ""
    done = False
    if info:
        fair_no = info.get("fair") or ""
        print(f"  First suggestion: {info.get('text', '')[:140]!r}")
        try:
            ctx.locator("[data-pi-first-option='1']").first.click(timeout=T(5000))
        except Exception as e:
            print(f"  Click on the first suggestion failed ({type(e).__name__}).")
        done = selection_done(ctx, grid, ri, fair_no)
    else:
        print("  No suggestion with a FAIR # appeared in the dropdown.")

    if not done:
        print("  Clicking the first suggestion once more...")
        try:
            ctx.locator("[data-pi-first-option='1']").first.click(
                timeout=T(5000), force=True
            )
        except Exception:
            pass
        done = selection_done(ctx, grid, ri, fair_no)

    if not done:
        dump_dropdown(ctx)
        row = row_by_index(grid, ri)
        raise RuntimeError(
            f"the first suggestion for {number} was not selected"
            f"{' (expected FAIR # ' + fair_no + ')' if fair_no else ''}; row {ri + 1} shows {_text(row)[:200]!r}"
        )
    return fair_no


def _first_option_showing(ctx, number, seconds):

    def shown():

        return ctx.evaluate(FIND_FIRST_OPTION_JS, [number, FAIR_NO_SRC]) is not None

    return poll(ctx.page, shown, seconds, 100)


def fill_part_number(ctx, grid, ri, number, pick_first=False):

    box = open_cell_input(
        ctx, grid, ri, "15. Part Number", "Select Part Number", "Part Number"
    )

    box.click(timeout=T(5000))

    box.press("Control+A")

    box.press("Delete")

    # Paste the whole value at once (same as form2.py), instead of key by key.
    box.fill(number)

    if pick_first:

        # XP: the suggestion list must open from the pasted value; if it does not, type it instead.
        if not _first_option_showing(ctx, number, 1.5):

            box.fill("")

            box.press_sequentially(number, delay=20)

        fair_no = pick_first_option(ctx, grid, ri, box, number)

        print(
            f"  Selected Part Number: {number}"
            f"{' (FAIR Identifier ' + fair_no + ' set by the form)' if fair_no else ''}"
        )

        return

    # Non-XP sub-parts have no suggestions at all: the value (from the database) is
    # typed as-is; clicking the next field commits it (no Enter). Nothing waits for a dropdown.

    click_field(
        ctx, grid, ri, "partName"
    )  # clicking the next field commits the typed Part Number

    row = row_by_index(grid, ri)

    if not poll(ctx.page, lambda: number.lower() in _text(row).lower(), 3):

        raise RuntimeError(
            f"Part Number: typed {number!r} but row {ri + 1} shows {_text(row)!r}"
        )

    print(f"  Typed Part Number: {number}")


# New Vincent Code Start: October 29, 2026
def _part_name_editor(ctx, cell, seconds=2.5):
    """The Part Name editor. In ag-Grid the CELL ITSELF carries the 'ag-cell-inline-editing' class, so the
    old search for '.ag-cell-inline-editing input' INSIDE the cell never matched; the script then fell back to
    typing key by key. Look for the textarea / input directly inside the cell (or in a popup editor).
    """
    places = (
        cell.locator("textarea:visible, input:visible"),
        ctx.locator(
            ".ag-popup-editor textarea:visible, .ag-popup-editor input:visible"
        ),
    )
    found = []

    def seen():
        for loc in places:
            try:
                if loc.count() > 0:
                    found.append(loc.first)
                    return True
            except Exception:
                pass
        return False

    return found[0] if poll(ctx.page, seen, seconds, 100) else None


# New Vincent Code End: October 29, 2026


def fill_part_name(ctx, grid, ri, name, wait=8):

    cell = grid.locator(f".ag-row[row-index='{ri}'] [col-id='partName']").first

    if wait:

        poll(
            ctx.page, lambda: _filled(cell), wait
        )  # XP only: the name fills in by itself after the part is chosen

    if _filled(cell):

        print(f"  Part Name is already filled: {_text(cell)}")

        return

    if not name:

        print("  No Part Name in the database for this part; skipped.")

        return

    for attempt in (1, 2, 3):

        try:

            cell.scroll_into_view_if_needed(timeout=T(3000))

            cell.dblclick(timeout=T(5000))

            # The editor is inside the cell (inline editing) or in an ag-Grid popup.
            editor = _part_name_editor(ctx, cell)

            if editor is not None:

                editor.fill(
                    name
                )  # sets the whole value at once (a paste, not key by key)

            else:

                # No editor found: insert the whole text into whatever got focus (a paste, not key by key).
                ctx.page.keyboard.insert_text(name)

            # Click ANOTHER field to commit (Part Number cell). The Part Type cell is not touched here.
            click_field(ctx, grid, ri, "partNumber")

        except Exception as e:

            print(
                f"  Part Name attempt {attempt} error: {type(e).__name__}: {str(e).strip()[:120]}"
            )

        if poll(ctx.page, lambda: _filled(cell), 3):

            print(f"  Typed Part Name: {_text(cell)}")

            return

        print(
            f"  Part Name attempt {attempt} did not stick; cell shows {_text(cell)!r}."
        )

    try:

        print(f"  Part Name cell HTML: {cell.evaluate('e => e.outerHTML')[:300]}")

    except Exception:

        pass

    print("  Part Name still looks empty; check the page.")


def fill_fair(ctx, grid, ri, batch):

    # The FAIR Identifier is the FAIR whose identifier matches the sub-part's Batch Number.

    if not batch:

        print(
            "  No Batch Number for this sub-part, so the FAIR Identifier was left empty."
        )

        return

    row = row_by_index(grid, ri)

    before = _text(row)

    box = open_cell_input(
        ctx, grid, ri, "18. FAIR Identifier", "Select FAIR", "FAIR Identifier"
    )

    box.click(timeout=T(5000))

    box.press("Control+A")

    box.press("Delete")

    box.fill(batch)  # paste

    # No suggestions for these sub-parts: the Batch Number is typed as-is; clicking the next field commits it (no Enter).

    click_field(
        ctx, grid, ri, "supplier"
    )  # clicking the next field commits the typed value

    if not poll(ctx.page, lambda: _text(row) != before, 3):

        raise RuntimeError(
            f"FAIR Identifier: nothing was selected in row {ri + 1} (it still shows {_text(row)!r})"
        )

    print(f"  Typed FAIR Identifier (batch {batch}); row now shows: {_text(row)!r}")


def _col_id_by_header(grid, header_text):
    heads = grid.locator(".ag-header-cell").filter(
        has_text=re.compile(re.escape(header_text), re.I)
    )
    for i in range(heads.count()):
        cid = heads.nth(i).get_attribute("col-id")
        if cid:
            return cid
    return ""


def _row_cell(grid, ri, col_id):
    return grid.locator(f".ag-row[row-index='{ri}'] [col-id='{col_id}']").first


# New Vincent Code Start: October 29, 2026
def _cell_editing(ctx, grid, ri, col_id):
    """True while the cell of THIS column is still being edited. An editor that Tab opened in the
    NEXT cell does not count (that is what made a correct Supplier look 'not committed').
    """
    sel = (
        f".ag-row[row-index='{ri}'] [col-id='{col_id}'].ag-cell-inline-editing, "
        f".ag-row[row-index='{ri}'] [col-id='{col_id}'] .ag-cell-inline-editing"
    )
    try:
        if grid.locator(sel).count() > 0:
            return True
        return ctx.locator(".ag-popup-editor input:visible").count() > 0
    except Exception:
        return False


# New Vincent Code End: October 29, 2026


def _find_editor(ctx, grid, ri, col_id):
    for scope, sel in (
        (grid, f".ag-row[row-index='{ri}'] [col-id='{col_id}'] input:visible"),
        (grid, f".ag-row[row-index='{ri}'] .ag-cell-inline-editing input:visible"),
        (ctx, ".ag-popup-editor input:visible"),
    ):
        loc = scope.locator(sel)
        if loc.count() > 0:
            return loc.first
    return None


def open_col_editor(ctx, grid, ri, col_id, label):
    # Click the cell of this column (by col-id) on row ri until its editor input shows.
    # Before the first click, remember everything that is visible, so the dropdown list
    # that opens afterwards can be recognised as "new" (no need to know its CSS classes).
    _mark_baseline(ctx)
    deadline = time.time() + INDEX_CELL_WAIT
    tries = 0
    while tries == 0 or time.time() < deadline:
        row = row_by_index(grid, ri)
        row.scroll_into_view_if_needed(timeout=T(5000))
        cell = _row_cell(grid, ri, col_id)
        b = None
        if cell.count():
            cell.scroll_into_view_if_needed(timeout=T(3000))
            b = cell.bounding_box()
        if b:
            x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        else:
            head = grid.locator(f".ag-header-cell[col-id='{col_id}']").first
            hb = head.bounding_box() if head.count() else None
            rb = row.bounding_box()
            if not hb or not rb:
                raise RuntimeError(
                    f"could not find where the {label} cell of row {ri + 1} is on the page"
                )
            x, y = hb["x"] + hb["width"] / 2, rb["y"] + rb["height"] / 2
        if tries % 2 == 1:
            ctx.page.mouse.dblclick(x, y)
        else:
            ctx.page.mouse.click(x, y)
        tries += 1
        if poll(ctx.page, lambda: _find_editor(ctx, grid, ri, col_id) is not None, 1.2):
            return _find_editor(ctx, grid, ri, col_id)
        print(
            f"  {label} editor not showing after click {tries}; clicking the cell again..."
        )
    raise RuntimeError(
        f"the {label} editor of row {ri + 1} was not found (col-id {col_id!r})"
    )


MARK_BASELINE_JS = r"""() => {
  document.querySelectorAll('[data-pi-base]').forEach(e => e.removeAttribute('data-pi-base'));
  document.querySelectorAll('[data-pi-opt]').forEach(e => e.removeAttribute('data-pi-opt'));
  const shown = (e) => {
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  for (const e of document.querySelectorAll('body *')) {
    if (shown(e)) e.setAttribute('data-pi-base', '1');
  }
}"""

# The dropdown list = visible elements with text that were NOT visible at the baseline.
# Only the innermost ones are returned (one per option line) and tagged data-pi-opt="n".
NEW_ITEMS_JS = r"""() => {
  document.querySelectorAll('[data-pi-opt]').forEach(e => e.removeAttribute('data-pi-opt'));
  const shown = (e) => {
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const txt = (e) => (e.innerText || '').replace(/\s+/g, ' ').trim();
  const cands = [];
  for (const e of document.querySelectorAll('body *')) {
    if (e.hasAttribute('data-pi-base')) continue;
    if (['SCRIPT', 'STYLE', 'INPUT', 'TEXTAREA'].includes(e.tagName)) continue;
    if (!shown(e) || !txt(e)) continue;
    cands.push(e);
  }
  const leaves = cands.filter(e => !cands.some(o => o !== e && e.contains(o)));
  return leaves.map((e, i) => { e.setAttribute('data-pi-opt', String(i)); return {i: i, t: txt(e)}; });
}"""

DUMP_POPUP_JS = r"""() => {
  let e = document.querySelector('[data-pi-opt]');
  if (!e) return '';
  for (let i = 0; i < 4 && e.parentElement; i++) e = e.parentElement;
  return e.outerHTML;
}"""


def _mark_baseline(ctx):
    try:
        ctx.evaluate(MARK_BASELINE_JS)
    except Exception:
        pass


def _new_items(ctx):
    # [(index, text)] of the dropdown options currently showing.
    try:
        rows = ctx.evaluate(NEW_ITEMS_JS) or []
    except Exception:
        return []
    return [(int(r["i"]), str(r["t"])) for r in rows]


def _wait_items(ctx, seconds):
    end = time.time() + seconds
    items = _new_items(ctx)
    while not items and time.time() < end:
        ctx.page.wait_for_timeout(250)
        items = _new_items(ctx)
    return items


def _click_item(ctx, idx):
    ctx.locator(f"[data-pi-opt='{idx}']").first.click(timeout=T(3000))


def _dump_popup(ctx, name):
    try:
        html = ctx.evaluate(DUMP_POPUP_JS)
        if html:
            save_html(name, html)
            print(f"  (saved the dropdown HTML to screenshots\\{name})")
    except Exception:
        pass


def fill_part_type(ctx, grid, ri, text):
    # Part Type is a read-only dropdown that opens by itself when the cell is edited.
    # Do NOT click the box first (that would close the list): wait for the option, click it.
    cid = PART_TYPE_COL_ID or _col_id_by_header(grid, PART_TYPE_HEADER)
    if not cid:
        raise RuntimeError(
            f"no column header containing {PART_TYPE_HEADER!r} was found in the Part Number Index "
            f"(set PART_TYPE_COL_ID in .env)"
        )
    box = open_col_editor(ctx, grid, ri, cid, "Part Type")
    want = text.lower()

    def find(seconds):
        end = time.time() + seconds
        while True:
            items = _new_items(ctx)
            hit = [i for i, t in items if t.lower() == want] or [
                i for i, t in items if want in t.lower()
            ]
            if hit:
                return hit[0], items
            if time.time() >= end:
                return None, items
            ctx.page.wait_for_timeout(250)

    idx, items = find(3)
    if idx is None:  # the list is closed: open it once and look again
        box.click(timeout=T(3000))
        idx, items = find(3)
    if idx is None:
        _dump_popup(ctx, "part_index_parttype.html")
        raise RuntimeError(
            f"Part Type: no option contains {text!r}; new items seen: {[t for _, t in items][:8]}"
        )
    _click_item(ctx, idx)
    cell = _row_cell(grid, ri, cid)
    if not poll(ctx.page, lambda: want in _text(cell).lower(), 3):
        raise RuntimeError(
            f"Part Type: {text!r} was clicked but the cell shows {_text(cell)!r}"
        )
    print(f"  Selected Part Type: {text}")


def fill_supplier(ctx, grid, ri, alias):
    # Pick the dropdown option that is most similar to the Supplier Alias from the database.
    if not alias:
        print(
            "  No Supplier Alias in the database for this part/batch; Supplier skipped."
        )
        return
    box = open_col_editor(ctx, grid, ri, SUPPLIER_COL_ID, "Supplier")
    typed = False
    if box.get_attribute("readonly") is None:
        # searchable: type the main word of the alias to narrow the list
        box.press("Control+A")
        box.press("Delete")
        word = _sig_tokens(alias)[0]
        box.fill(word)  # paste
        typed = True
        ctx.page.wait_for_timeout(800)  # let the list filter itself
    else:
        box.click(timeout=T(3000))
    items = _wait_items(ctx, 4)
    if not items and typed:
        # the pasted word did not open the list: type it key by key once
        box.fill("")
        box.press_sequentially(word, delay=30)
        ctx.page.wait_for_timeout(500)
        items = _wait_items(ctx, 3)
    if not items and typed:
        box.press(
            "Control+A"
        )  # nothing matched the typed word: show the whole list instead
        box.press("Delete")
        ctx.page.wait_for_timeout(500)
        items = _wait_items(ctx, 3)
    if not items:
        _dump_popup(ctx, "part_index_supplier.html")
        ctx.page.keyboard.press("Escape")
        print(f"  Supplier: no dropdown options appeared for {alias!r}; skipped.")
        return
    scored = sorted(
        ((supplier_score(alias, t), i, t) for i, t in items),
        key=lambda x: (-x[0], x[1]),
    )
    score, idx, option = scored[0]
    if score < SUPPLIER_MATCH_MIN:
        ctx.page.keyboard.press("Escape")
        print(
            f"  Supplier: nothing similar to {alias!r} (best {score:.2f}: {option[:60]!r}); skipped. "
            f"Options: {[t[:40] for _, _, t in scored[:6]]}"
        )
        return
    _click_item(ctx, idx)
    # Clicking the option only sets the combobox value. The grid cell stays in edit mode
    # (and looks empty) until the edit is committed, so commit it by clicking another cell of the row.
    cell = _row_cell(grid, ri, SUPPLIER_COL_ID)

    def committed():
        return _filled(cell) and not _cell_editing(ctx, grid, ri, SUPPLIER_COL_ID)

    ctx.page.wait_for_timeout(300)
    for target in (
        "fair",
        "partName",
    ):  # click another cell of the row to commit (no Enter / Tab)
        try:
            click_field(ctx, grid, ri, target)
        except Exception:
            continue
        if poll(ctx.page, committed, 2):
            break
    else:
        raise RuntimeError(
            f"Supplier: picked {option!r} but it was not committed; cell shows {_text(cell)!r}"
        )
    print(
        f"  Selected Supplier: {_text(cell)!r} (database alias {alias!r}, match {score:.2f})"
    )


def _close_editor(ctx, grid, ri):
    # Escape only if an editor is still open in this row (it would block the next step).
    try:
        if (
            grid.locator(f".ag-row[row-index='{ri}'] .ag-cell-inline-editing").count()
            > 0
        ):
            ctx.page.keyboard.press("Escape")
    except Exception:
        pass


_NET_STAGE = {"now": "starting"}


def _watch_saves(page):
    # Prints every POST/PUT/PATCH/DELETE sent to net-inspect.com and the step it happened in.
    # part_index.py never clicks SAVE FAIR; if the page itself saves after each edit, it shows here.
    def on_request(req):
        try:
            if (
                req.method in ("POST", "PUT", "PATCH", "DELETE")
                and "net-inspect.com" in req.url
            ):
                print(
                    f"    [net] {req.method} {req.url.split('?')[0][-80:]}  <- during: {_NET_STAGE['now']}"
                )
        except Exception:
            pass

    page.on("request", on_request)
    return on_request


# New Vincent Code Start: October 28, 2026
SAVE_GUARD_JS = r"""(on) => {
  const w = window;
  if (!w.__sfGuard) {
    const g = { on: false, blocked: 0, lastKey: '', log: [] };
    w.__sfGuard = g;
    const isSave = (el) => {
      const b = el && el.closest
        ? el.closest('button, input[type=button], input[type=submit], a, [role=button]') : null;
      if (!b) return false;
      const t = (b.value || b.innerText || b.textContent || '').replace(/\s+/g, ' ').trim();
      return /^save\s*fair$/i.test(t);
    };
    document.addEventListener('keydown', (e) => { g.lastKey = e.key; }, true);
    document.addEventListener('click', (e) => {
      if (g.on && isSave(e.target)) {
        e.preventDefault();
        e.stopImmediatePropagation();
        g.blocked++;
        g.log.push('after key ' + (g.lastKey || '(none)') + (e.isTrusted ? '' : ' [synthetic click]'));
      }
    }, true);
  }
  w.__sfGuard.on = on;
  const out = [w.__sfGuard.blocked, w.__sfGuard.log.slice()];
  if (!on) { w.__sfGuard.blocked = 0; w.__sfGuard.log = []; }
  return out;
}"""


def _set_save_guard(page, on):
    """Switch the SAVE FAIR click guard on/off in every frame. Returns (blocked_count, reasons)."""
    blocked, reasons = 0, []
    for fr in page.frames:
        try:
            n, log = fr.evaluate(SAVE_GUARD_JS, on)
            blocked += n
            reasons += log
        except Exception:
            pass
    return blocked, reasons


# New Vincent Code End: October 28, 2026


def fill_part_index(page, subs):
    watcher = _watch_saves(page)
    # New Vincent Code Start: October 28, 2026
    if BLOCK_SAVE_FAIR:
        _set_save_guard(page, True)
        print(
            "  SAVE FAIR guard ON: any click on SAVE FAIR is cancelled while the index is filled."
        )
    # New Vincent Code End: October 28, 2026
    try:
        return _fill_rows(page, subs)
    finally:
        # New Vincent Code Start: October 28, 2026
        if BLOCK_SAVE_FAIR:
            blocked, reasons = _set_save_guard(page, False)
            print(
                f"  SAVE FAIR guard OFF: {blocked} click(s) on SAVE FAIR were cancelled."
            )
            for r in reasons:
                print(f"    - cancelled click {r}")
        # New Vincent Code End: October 28, 2026
        try:
            page.remove_listener("request", watcher)
        except Exception:
            pass


def _fill_rows(page, subs):

    ctx, heading = open_index_section(page)

    set_page_size(ctx, heading)

    grid = heading.locator(
        "xpath=following::div[contains(@class,'ag-root-wrapper')][1]"
    )

    failed = []

    for n, (number, name, batch, supplier) in enumerate(subs, 1):

        print(f"  --- Sub-part {n} of {len(subs)}: {number} ---")

        before = rows_of(grid).count()

        _NET_STAGE["now"] = f"{number}: + Add Part"

        click_add_part(ctx, heading)

        # The Detail FAIR confirmation shows only on the first Add Part, so later rows
        # only do a very short check (in case it shows again) instead of waiting.

        click_modal_ok(page, seconds=5 if n == 1 else 0.5, quiet=n > 1)

        if not poll(page, lambda: rows_of(grid).count() > before, 10):

            raise RuntimeError(
                "clicked + Add Part but no new row appeared in the Part Number Index"
            )

        ri = before  # new rows are added at the bottom

        auto = bool(AUTO_FAIR_PREFIX and number.startswith(AUTO_FAIR_PREFIX))

        _NET_STAGE["now"] = f"{number}: Part Number"

        if auto:

            # XP: choose the first suggestion; the form fills Part Name and FAIR Identifier.

            fill_part_number(ctx, grid, ri, number, pick_first=True)

            fill_part_name(ctx, grid, ri, name)

            print(f"  {number}: FAIR Identifier is automatic; left as the form set it.")

            continue

        # Other sub-parts: no suggestions, everything is typed from the database values.

        try:

            fill_part_number(ctx, grid, ri, number, pick_first=False)

            _NET_STAGE["now"] = f"{number}: after Part Number"

        except Exception as e:

            print(
                f"  ! {number}: Part Number failed: {type(e).__name__}: {str(e).strip()[:200]}"
            )

            failed.append(f"{number} (Part Number)")

            _close_editor(ctx, grid, ri)

            continue

        # The rest: each field on its own. One failing field never skips the next ones
        # (Supplier is last, so a Supplier problem can not stop the FAIR Identifier).

        steps = [
            ("Part Name", lambda: fill_part_name(ctx, grid, ri, name, wait=0)),
            (
                "Part Type",
                lambda: (
                    fill_part_type(ctx, grid, ri, PART_TYPE_NON_XP)
                    if PART_TYPE_NON_XP
                    else None
                ),
            ),
            ("FAIR Identifier", lambda: fill_fair(ctx, grid, ri, batch)),
            ("Supplier", lambda: fill_supplier(ctx, grid, ri, supplier)),
        ]

        problems = []

        for label, step in steps:

            try:

                _NET_STAGE["now"] = f"{number}: {label}"

                step()

            except Exception as e:

                print(
                    f"  ! {number}: {label} failed: {type(e).__name__}: {str(e).strip()[:200]}"
                )

                problems.append(label)

                _close_editor(ctx, grid, ri)

        _close_editor(
            ctx, grid, ri
        )  # the last click may have opened an editor; close it (Escape)

        if problems:

            failed.append(f"{number} ({', '.join(problems)})")

    if failed:

        raise RuntimeError("these sub-parts were not filled: " + ", ".join(failed))

    return ctx


def run_part_index(page, part, status):

    if not FILL_PART_INDEX:

        return status

    try:

        if not (MAIN_PART_PREFIX and part.number.startswith(MAIN_PART_PREFIX)):

            print(
                f"  Part Number Index: {part.number} is not a main part ({MAIN_PART_PREFIX}...). Skipped."
            )

            return status

        # New Vincent Code Start: October 28, 2026
        # The lot is only needed for the Batch Number of non-XP sub-parts; the sub-parts come from the BOM.
        lot = PART_INDEX_LOT or part.lot or lot_of_part(part.number)

        source = "BOM: cadcam_parts / cadcam_subparts (makeTree)"

        main_lot, tree = find_sub_parts(part.number, lot)
        # New Vincent Code End: October 28, 2026

        print(
            f"  Part Number Index: {part.number}, lot {lot}, main lot {main_lot or '?'}: "
            f"{len(tree)} sub-part(s) in the database query ({source})."
        )

        info = lookup_part_info([number for number, _ in tree])

        subs, seen = [], set()

        bom_names = None  # loaded only if an accessory has no name yet

        for number, sub_lot in tree:

            if number in seen:

                continue

            seen.add(number)

            if number == part.number:

                # the main part can show up under its mother lot; never add it to its own index
                print(f"    - {number} skipped: it is the main part itself.")

                continue

            is_xp = bool(AUTO_FAIR_PREFIX and number.startswith(AUTO_FAIR_PREFIX))

            is_sub, name = info.get(number.lower(), (False, ""))

            # isSub = 1 is required for XP sub-parts only; the others come from the BOM tree.
            if is_xp and not is_sub:

                print(
                    f"    - {number} skipped: no row with {COL_IS_SUB} = 1 in {DB_TABLE}"
                )

                continue

            if not is_xp:

                # Part Name of an accessory = cadcam_accessories.accessoryName
                name = _ACC_NAMES.get(number.lower()) or name

                if not name:

                    if bom_names is None:

                        bom_names = accessory_names_from_bom(part.number)

                    name = bom_names.get(number.lower(), "")

            if len(subs) >= INDEX_MAX_PARTS:

                print(f"    - {number} skipped: more than {INDEX_MAX_PARTS} sub-parts.")

                continue

            if is_xp:

                batch = ""  # the form fills the FAIR Identifier by itself

                supplier = ""

                print(
                    f"    + {number}  ({COL_IS_SUB} = 1; FAIR Identifier is automatic)"
                )

            else:

                batch = trace_batch_number(sub_lot) if sub_lot else ""

                supplier = supplier_alias_of(
                    batch, sub_lot
                )  # New Vincent Code: November 13, 2026

                print(
                    f"    + {number}  lot {sub_lot}  batch {batch or '-'}  name {name or '-'}  supplier {supplier or '-'}  (from the BOM tree)"
                )

            subs.append((number, name, batch, supplier))

    except Exception as e:

        print(
            f"  Part Number Index: lookup error: {type(e).__name__}: {str(e).strip()[:300]}"
        )

        return "FAIR_INDEX_FAILED"

    finally:

        _close_conn()

    if not subs:

        print("  Part Number Index: no sub-parts to add.")

        return status

    try:

        fill_part_index(page, subs)

    except Exception as e:

        print(f"  Part Number Index failed: {type(e).__name__}: {str(e).strip()[:400]}")

        save_screenshot(page, f"part_index_error_{part.number}")

        try:

            for fr in page.frames:

                if fr.get_by_text("Part Number Index").count():

                    save_html(
                        "part_index.html",
                        fr.locator("div.ag-root-wrapper").first.evaluate(
                            "e => e.outerHTML"
                        ),
                    )

                    break

        except Exception:

            pass

        return "FAIR_INDEX_FAILED"

    save_screenshot(page, f"part_index_done_{part.number}")

    print(
        f"  Part Number Index: {len(subs)} sub-part(s) added (not saved yet; SAVE FAIR comes next)."
    )

    return status
