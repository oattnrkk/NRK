# -*- coding: utf-8 -*-
"""Go To Reference View.

Finds the Section / Elevation / Callout marker that references the
CURRENT active view, opens the sheet it's placed on (if any), activates
the view, and selects/zooms to the marker.

Converted from a single-node Dynamo Python script ("test.dyn") into a
pyRevit pushbutton. Logic is unchanged; only the IN[0]/IN[1] Dynamo
inputs and OUT output were replaced with pyRevit-native equivalents:

  - IN[0] (bool "run")        -> not needed, the button click IS the run.
  - IN[1] (manual marker id)  -> OPTIONAL: pre-select the section/elevation/
                                  callout head/arrow graphic in its host
                                  view BEFORE clicking this button, and the
                                  script will use that selection instead of
                                  auto-searching.
  - OUT (message, view)       -> message is shown via pyRevit's output
                                  window / TaskDialog; the view is simply
                                  activated in the Revit UI.
"""

import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
from Autodesk.Revit.DB import *

from System.Collections.Generic import List

from pyrevit import revit, script, forms

doc = revit.doc
uidoc = revit.uidoc
uiapp = __revit__  # pyRevit injects this global: the UIApplication instance

# --- OPTIONAL manual override -------------------------------------------
# If something is pre-selected when the button is clicked, treat the first
# selected element as the marker to use directly (equivalent of Dynamo's
# IN[1] manual override).
manual_marker_id = None
try:
    sel_ids = uidoc.Selection.GetElementIds()
    if sel_ids and sel_ids.Count > 0:
        for _id in sel_ids:
            manual_marker_id = _id
            break
except Exception:
    pass

message = ""
result_view = None


def get_sheet_by_number(sheet_no):
    """Find a ViewSheet by its Sheet Number (string match). Shared helper --
    scans ViewSheet elements only (a small collection compared to scanning
    every Viewport/Element in the model)."""
    if not sheet_no:
        return None
    sheet_no = sheet_no.strip()
    if not sheet_no:
        return None
    try:
        for s in FilteredElementCollector(doc).OfClass(ViewSheet).ToElements():
            try:
                if s.SheetNumber == sheet_no:
                    return s
            except Exception:
                continue
    except Exception:
        pass
    return None


def find_sheet_for_view(view):
    """Look up the sheet 'view' is placed on via its own read-only
    'Sheet Number' parameter (BuiltInParameter.VIEWPORT_SHEET_NUMBER,
    auto-populated once a view is placed on a sheet) -- NOT by scanning
    every Viewport in the model. Only the one matching sheet's own (small)
    viewport list is checked afterwards, to pin down the exact Viewport
    for zoom/select.
    Returns (ViewSheet, Viewport) or (None, None)."""
    sheet_no = None
    try:
        p = view.get_Parameter(BuiltInParameter.VIEWPORT_SHEET_NUMBER)
        if p is not None:
            sheet_no = p.AsString()
    except Exception:
        pass

    target_sheet = get_sheet_by_number(sheet_no)
    if target_sheet is None:
        return None, None

    # scoped: only this one sheet's own viewports, not doc-wide
    try:
        for vp_id in target_sheet.GetAllViewports():
            vp = doc.GetElement(vp_id)
            if vp is not None and vp.ViewId == view.Id:
                return target_sheet, vp
    except Exception:
        pass

    # sheet matched by parameter but the exact viewport couldn't be
    # pinned down -- still usable for opening/activating the sheet+view
    return target_sheet, None


def legacy_id_param_view_id(marker):
    """Old unofficial trick: some markers store the referenced view's
    ElementId on BuiltInParameter.ID_PARAM. Not guaranteed, kept only
    as a fallback for older files."""
    try:
        p = marker.get_Parameter(BuiltInParameter.ID_PARAM)
        if p is not None:
            vid = p.AsElementId()
            if vid is not None and vid != ElementId.InvalidElementId:
                return vid
    except Exception:
        pass
    return None


def documented_referenced_view_id(marker):
    """Official/documented parameter: ParameterTypeId.ReferencedView
    -- 'The view referenced by a section or callout.'"""
    try:
        p = marker.GetParameter(ParameterTypeId.ReferencedView)
        if p is not None:
            vid = p.AsElementId()
            if vid is not None and vid != ElementId.InvalidElementId:
                return vid
    except Exception:
        pass
    return None


def find_marker_for_view(target_view, scope_view_id=None):
    """Find the OST_Viewers marker (or ElevationMarker) that refers to
    target_view. Returns (marker_element, scanned_count, elems).

    Matching order:
      1) documented ReferencedView parameter (reference sections/callouts)
      2) legacy ID_PARAM trick (some older files)
      3) NAME MATCH -- Revit keeps a plain section/callout marker's own
         Name in sync with the Name of the view it points to. This is
         the one technique confirmed to work for ordinary (non-reference)
         sections and is used here as the primary real-world fallback.
    """
    target_view_id = target_view.Id
    try:
        target_name = target_view.Name
    except Exception:
        target_name = None

    try:
        if scope_view_id is None:
            col = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Viewers).WhereElementIsNotElementType()
        else:
            col = FilteredElementCollector(doc, scope_view_id).OfCategory(BuiltInCategory.OST_Viewers).WhereElementIsNotElementType()
    except Exception:
        return None, 0, []

    elems = col.ToElements()
    scanned = 0
    name_match = None  # keep a weaker name-based candidate as last resort

    for m in elems:
        scanned += 1

        # --- Elevation markers: one element can point to up to 4 views ---
        if isinstance(m, ElevationMarker):
            try:
                for i in range(m.CurrentViewCount):
                    vid = m.GetViewId(i)
                    if vid == target_view_id:
                        return m, scanned, elems
            except Exception:
                pass
            continue

        # --- Section / Callout markers: parameter-based match first ---
        vid = documented_referenced_view_id(m)
        if vid is None or vid == ElementId.InvalidElementId:
            vid = legacy_id_param_view_id(m)

        if vid == target_view_id:
            return m, scanned, elems

        # --- Name-based fallback (works for plain, non-reference sections) ---
        if name_match is None and target_name:
            try:
                if m.Name == target_name:
                    name_match = m
            except Exception:
                pass

    if name_match is not None:
        return name_match, scanned, elems

    return None, scanned, elems


def find_host_via_sheet_scan(view):
    """USER-PREFERRED APPROACH: read the view's own read-only 'Referencing
    Sheet' / 'Referencing Detail' parameters (same values shown in its
    Properties panel), open that ONE sheet, then scan only the views
    actually placed on that sheet (a small set -- fast) for the marker
    that really matches this view (by the same param/legacy/name checks
    as find_marker_for_view, just scoped to each candidate view).

    Detail Number is used only as a TIE-BREAKER if more than one placed
    view on the sheet turns out to have a matching marker -- we don't
    trust it alone, since Autodesk has documented cases where it can be
    stale/wrong after copying views.

    Returns (host_view, marker_elem, sheet_number_str, detail_number_str,
    host_sheet, host_viewport). The last two are the ViewSheet/Viewport we
    already found along the way, so the caller can open+activate them
    directly instead of looking the sheet up a second time.
    host_view/marker_elem are None if nothing on that sheet matched.
    """
    sheet_no, detail_no = None, None
    try:
        p_sheet = view.GetParameter(ParameterTypeId.ViewReferencingSheet)
        if p_sheet is not None:
            sheet_no = p_sheet.AsString()
    except Exception:
        pass
    try:
        p_detail = view.GetParameter(ParameterTypeId.ViewReferencingDetail)
        if p_detail is not None:
            detail_no = p_detail.AsString()
    except Exception:
        pass

    if not sheet_no or sheet_no.strip() == "":
        return None, None, sheet_no, detail_no, None, None
    sheet_no = sheet_no.strip()
    if detail_no:
        detail_no = detail_no.strip()

    target_sheet = get_sheet_by_number(sheet_no)
    if target_sheet is None:
        return None, None, sheet_no, detail_no, None, None

    try:
        vp_ids = target_sheet.GetAllViewports()
    except Exception:
        try:
            vp_ids = FilteredElementCollector(doc, target_sheet.Id).OfClass(Viewport).ToElementIds()
        except Exception:
            vp_ids = []

    candidates = []  # (host_view, marker_elem, viewport_detail_no, viewport)
    for vp_id in vp_ids:
        try:
            vp = doc.GetElement(vp_id)
            if vp is None:
                continue
            host_view = doc.GetElement(vp.ViewId)
            if host_view is None:
                continue
            # small, scoped marker search -- only elements owned by this
            # one placed view, so this stays fast even on huge projects
            m, _, _ = find_marker_for_view(view, host_view.Id)
            if m is not None:
                vp_dn = None
                try:
                    p_dn = vp.get_Parameter(BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
                    if p_dn is not None:
                        vp_dn = p_dn.AsString()
                except Exception:
                    pass
                candidates.append((host_view, m, vp_dn, vp))
        except Exception:
            continue

    if not candidates:
        return None, None, sheet_no, detail_no, None, None

    if len(candidates) == 1:
        hv, m, _, vp = candidates[0]
        return hv, m, sheet_no, detail_no, target_sheet, vp

    # multiple matches on this sheet -- break the tie with Detail Number
    if detail_no:
        for hv, m, vp_dn, vp in candidates:
            if vp_dn is not None and vp_dn.strip() == detail_no:
                return hv, m, sheet_no, detail_no, target_sheet, vp

    hv, m, _, vp = candidates[0]
    return hv, m, sheet_no, detail_no, target_sheet, vp


# ============================================================
#  MAIN
# ============================================================
av = uidoc.ActiveView

if av is None:
    message = "No active view."
else:
    parent_view = None
    marker_elem = None
    search_view = av   # the view whose marker we look for -- may be
                        # swapped to the primary view below
    used_primary = False
    used_manual = False
    used_ref_sheet = False
    ref_sheet_no, ref_detail_no = None, None
    ref_host_sheet, ref_host_viewport = None, None
    scanned_count = 0

    # --- 0) Manual override: something was pre-selected before running ---
    if manual_marker_id is not None:
        try:
            manual_elem = doc.GetElement(manual_marker_id)
            if manual_elem is not None:
                host_id = manual_elem.OwnerViewId
                if host_id is not None and host_id != ElementId.InvalidElementId:
                    parent_view = doc.GetElement(host_id)
                    marker_elem = manual_elem
                    used_manual = True
        except Exception:
            pass

    # --- 0b) Dependent views have NO marker of their own -- only the
    #        primary view does. Search using the primary view instead.
    if parent_view is None:
        try:
            primary_id = av.GetPrimaryViewId()
            if primary_id is not None and primary_id != ElementId.InvalidElementId and primary_id != av.Id:
                search_view = doc.GetElement(primary_id)
                used_primary = True
        except Exception:
            pass

        # --- 1) FASTEST + verified: Referencing Sheet, scanned -------
        # Works whenever the section's marker is shown on a sheet
        # (the normal real-world workflow). Verifies against the
        # actual marker on that sheet instead of trusting Detail
        # Number blindly.
        hv, verified_marker, ref_sheet_no, ref_detail_no, ref_host_sheet, ref_host_viewport = find_host_via_sheet_scan(search_view)
        if hv is not None:
            parent_view = hv
            marker_elem = verified_marker
            used_ref_sheet = True

        # --- 2) documented param / legacy / name match, doc-wide ---
        if parent_view is None:
            marker_elem, scanned_count, scanned_elems = find_marker_for_view(search_view)
            if marker_elem is not None:
                # ElevationMarker's own OwnerViewId is the plan view it sits on.
                # Section/Callout marker's OwnerViewId is the host view.
                host_id = marker_elem.OwnerViewId
                if host_id is not None and host_id != ElementId.InvalidElementId:
                    parent_view = doc.GetElement(host_id)

        # --- 3) Fallback for true callouts: official API -------------
        if parent_view is None:
            try:
                if search_view.IsCallout:
                    pid = search_view.GetCalloutParentId()
                    if pid is not None and pid != ElementId.InvalidElementId:
                        parent_view = doc.GetElement(pid)
                        marker_elem, _, _ = find_marker_for_view(search_view, parent_view.Id)
            except Exception:
                pass

    # --- Result ---------------------------------------------------
    if parent_view is None:
        extra = (" (this is a dependent view -- searched using its primary "
                  "view '{0}' instead, since dependents don't have their own "
                  "marker)").format(search_view.Name) if used_primary else ""
        ref_note = ""
        if ref_sheet_no and ref_detail_no:
            ref_note = (" View reports Referencing Sheet '{0}' / Detail '{1}', but no "
                        "matching viewport was found on that sheet -- the sheet may "
                        "have been renamed/deleted or the parameter is stale.").format(
                        ref_sheet_no, ref_detail_no)
        elif not ref_sheet_no:
            ref_note = (" The view's own 'Referencing Sheet' field is empty, meaning "
                        "its marker currently isn't shown on any sheet.")
        message = ("No referring view found for '{0}'{1} (scanned {2} OST_Viewers "
                   "marker(s) doc-wide, none matched by parameter or name).{3} This is "
                   "a known Revit API gap for plain Sections that aren't 'Reference "
                   "Sections' -- there's no documented link from the view back to "
                   "its marker. If you know where the marker/arrow is, select it "
                   "while standing in its host view, then click this button again "
                   "to bypass the search entirely.").format(
                   av.Name, extra, scanned_count, ref_note)
    else:
        # --- Preferred: if the reference view is placed on a sheet, open
        #     that SHEET and activate the view on it (same as double-
        #     clicking the viewport) instead of opening the view alone.
        #     Reuse the sheet/viewport find_host_via_sheet_scan already
        #     found (path 1) instead of looking it up again from scratch.
        if used_ref_sheet and ref_host_sheet is not None:
            host_sheet, host_viewport = ref_host_sheet, ref_host_viewport
        else:
            host_sheet, host_viewport = find_sheet_for_view(parent_view)

        # Detail Number of the viewport we're activating, if we have it --
        # shown alongside the sheet number the way Revit itself labels
        # callout/section tags ("Sheet / Detail").
        host_detail_no = None
        if host_viewport is not None:
            try:
                p_dn = host_viewport.get_Parameter(BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
                if p_dn is not None:
                    host_detail_no = p_dn.AsString()
            except Exception:
                pass

        sheet_label = None
        if host_sheet is not None:
            sheet_label = (host_sheet.SheetNumber + " / Detail " + host_detail_no.strip()
                            if host_detail_no and host_detail_no.strip() else host_sheet.SheetNumber)

        # Diagnostic note on HOW parent_view/marker was located -- kept
        # separate from host_sheet (WHERE parent_view itself lives),
        # since those can be two different sheets.
        if used_manual:
            method_note = " (marker located via manual pre-selection)"
        elif used_ref_sheet:
            method_note = (" (marker located via Referencing Sheet '{0}' / "
                          "Detail '{1}')").format(ref_sheet_no, ref_detail_no)
        else:
            method_note = ""

        def _get_active_uiview_for(view):
            """Find the open UIView showing 'view', if any."""
            try:
                for uv in uidoc.GetOpenUIViews():
                    if uv.ViewId == view.Id:
                        return uv
            except Exception:
                pass
            return None

        def _zoom_to_marker():
            """Best-effort zoom/select. Safe to call more than once.

            Deliberately does NOT use UIDocument.ShowElements(): that method
            can pop up Revit's own "There is no open view that shows any of
            the highlighted elements... Continue?" TaskDialog, which needs a
            DialogBoxShowing handler to auto-dismiss -- and if that handler
            is left subscribed even briefly, it can intercept and silently
            auto-close OTHER unrelated dialogs too (e.g. the Filter dialog
            no longer responding to clicks). Using UIView.ZoomAndCenterRectangle
            directly avoids that dialog entirely, so no such handler is needed.
            """
            if marker_elem is None:
                return
            try:
                uidoc.Selection.SetElementIds(List[ElementId]([marker_elem.Id]))
            except Exception:
                pass
            try:
                bbox = marker_elem.get_BoundingBox(parent_view)
                if bbox is None:
                    return
                uv = _get_active_uiview_for(parent_view)
                if uv is None:
                    return
                pad = 2.0  # feet, so the marker isn't glued to the view edge
                pmin = XYZ(bbox.Min.X - pad, bbox.Min.Y - pad, bbox.Min.Z)
                pmax = XYZ(bbox.Max.X + pad, bbox.Max.Y + pad, bbox.Max.Z)
                uv.ZoomAndCenterRectangle(pmin, pmax)
            except Exception:
                pass

        # --- Switch the active view -----------------------------------
        # Try a SYNCHRONOUS switch (doc.ActiveView) first: this is the only
        # way that lets the zoom below take effect within the same command.
        # uidoc.RequestViewChange is only a last-resort fallback, because
        # it's DEFERRED (applied only after this command finishes).
        switched = False
        try:
            if host_sheet is not None:
                # doc.ActiveView must be set to the SHEET first, then to
                # the view, so the second assignment activates that
                # view's viewport on the now-open sheet (same as
                # double-clicking it) instead of just opening it alone.
                doc.ActiveView = host_sheet
                doc.ActiveView = parent_view
            else:
                doc.ActiveView = parent_view
            switched = True
        except Exception:
            switched = False

        if not switched:
            try:
                uidoc.RequestViewChange(parent_view)
                switched = True
            except Exception as e:
                message = "Found '{0}' but could not switch to it: {1}".format(
                    parent_view.Name, str(e))

        if switched:
            result_view = parent_view

            # Zoom attempt #1: right now. Works when Revit has already
            # applied the view switch above (usually true for the
            # doc.ActiveView / sheet-activation path).
            _zoom_to_marker()

            # Zoom attempt #2: queued on Idling, a ONE-SHOT handler that
            # fires after this command finishes and Revit has fully
            # redrawn the new active view (so GetOpenUIViews() actually
            # includes it). This is what fixes "switches view but doesn't
            # zoom in" -- the UI doesn't finish updating until the command
            # returns control to Revit, so a zoom call made too early can
            # silently do nothing.
            def _zoom_on_idle(sender, args):
                try:
                    _zoom_to_marker()
                finally:
                    try:
                        uiapp.Idling -= _zoom_on_idle
                    except Exception:
                        pass

            try:
                uiapp.Idling += _zoom_on_idle
            except Exception:
                pass

            if host_sheet is not None:
                message = "Opened sheet {0} and activated view '{1}'.{2}".format(
                    sheet_label, parent_view.Name, method_note)
            else:
                message = "Switched to '{0}'.{1}".format(parent_view.Name, method_note)

# --- Report result to the user -------------------------------------------
# Silent on success (the view switch itself is the feedback). Only pop up
# an alert when nothing was found or something went wrong, so no console/
# output window appears on a normal, successful click.
if result_view is None:
    forms.alert(message, title="Go To Reference")
