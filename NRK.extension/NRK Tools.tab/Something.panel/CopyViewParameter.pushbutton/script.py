# -*- coding: utf-8 -*-
"""Copy View Settings (no Template)
Copies Scale, Detail Level, V/G category overrides, V/G filters,
Phase, Phase Filter, Crop settings, and Parts Visibility
from a source view to one or more target views, directly via API
(no View Template is created or applied).

UI uses System.Windows.Forms (WinForms) instead of pyrevit.forms,
because the WPF/XAML-based pyrevit.forms popups conflict with the
legacy System.Data.OleDb assembly pyRevit loads in this Revit 2026 /
IronPython environment, which can crash Revit outright instead of
raising a catchable exception.

Works in pyRevit (CPython3 or IronPython 2.7 engine).
"""

import clr
clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")

import os
import gc
import datetime
import System
from System.Collections.Generic import List

from System.Windows.Forms import (
    Form, ListBox, CheckedListBox, Button, DialogResult, TextBox,
    FormBorderStyle, SelectionMode, FormStartPosition, MessageBox,
    MessageBoxButtons, MessageBoxIcon, AnchorStyles, CheckState
)
from System.Drawing import Size, Point

from pyrevit import revit, DB

doc = revit.doc

# ---------------------------------------------------------------------
# Debug log: written to disk BEFORE each risky API call, and flushed
# immediately, so that if Revit hard-crashes (no catchable exception),
# the last line in this file tells us exactly what it was doing.
# ---------------------------------------------------------------------
LOG_PATH = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "CopyViewSettings_debug.log")


def log(msg):
    try:
        with open(LOG_PATH, "a") as f:
            f.write("{}  {}\n".format(datetime.datetime.now().strftime("%H:%M:%S"), msg))
            f.flush()
    except Exception:
        pass


# Known Revit categories that have a history of crashing the API when
# V/G overrides are applied to them programmatically. Skip these outright.
SKIP_CATEGORY_BUILTINS = set()
for _name in ("OST_RvtLinks", "OST_PointClouds", "OST_Materials"):
    _bic = getattr(DB.BuiltInCategory, _name, None)
    if _bic is not None:
        SKIP_CATEGORY_BUILTINS.add(int(_bic))
uidoc = revit.uidoc


# ---------------------------------------------------------------------
# WinForms pickers (avoid pyrevit.forms / WPF entirely)
# ---------------------------------------------------------------------

def pick_single(items, title):
    """Single-selection list with a live search box. Returns the selected string or None."""
    form = Form()
    form.Text = title
    form.Width = 480
    form.Height = 560
    form.StartPosition = FormStartPosition.CenterScreen
    form.FormBorderStyle = FormBorderStyle.FixedDialog
    form.MinimizeBox = False
    form.MaximizeBox = False

    search_box = TextBox()
    search_box.Location = Point(12, 12)
    search_box.Size = Size(440, 24)
    form.Controls.Add(search_box)

    lb = ListBox()
    lb.SelectionMode = SelectionMode.One
    lb.Location = Point(12, 42)
    lb.Size = Size(440, 430)
    for it in items:
        lb.Items.Add(it)
    form.Controls.Add(lb)

    def refresh_list(sender, args):
        filter_text = search_box.Text.lower()
        lb.BeginUpdate()
        lb.Items.Clear()
        for it in items:
            if filter_text in it.lower():
                lb.Items.Add(it)
        lb.EndUpdate()
        if lb.Items.Count > 0:
            lb.SelectedIndex = 0

    search_box.TextChanged += refresh_list

    ok_btn = Button()
    ok_btn.Text = "OK"
    ok_btn.DialogResult = DialogResult.OK
    ok_btn.Location = Point(292, 484)
    ok_btn.Size = Size(80, 28)
    form.Controls.Add(ok_btn)

    cancel_btn = Button()
    cancel_btn.Text = "Cancel"
    cancel_btn.DialogResult = DialogResult.Cancel
    cancel_btn.Location = Point(376, 484)
    cancel_btn.Size = Size(80, 28)
    form.Controls.Add(cancel_btn)

    form.AcceptButton = ok_btn
    form.CancelButton = cancel_btn
    form.Shown += lambda s, a: search_box.Focus()

    result = form.ShowDialog()
    if result == DialogResult.OK and lb.SelectedItem is not None:
        return lb.SelectedItem
    return None


def pick_checked(items, title, checked_default=None):
    """Multi-selection checklist with a live search box.
    Returns a list of selected strings (in original order), or None if cancelled."""
    checked_default = checked_default or []
    checked_state = dict((it, it in checked_default) for it in items)

    form = Form()
    form.Text = title
    form.Width = 480
    form.Height = 560
    form.StartPosition = FormStartPosition.CenterScreen
    form.FormBorderStyle = FormBorderStyle.FixedDialog
    form.MinimizeBox = False
    form.MaximizeBox = False

    search_box = TextBox()
    search_box.Location = Point(12, 12)
    search_box.Size = Size(440, 24)
    form.Controls.Add(search_box)

    clb = CheckedListBox()
    clb.CheckOnClick = True
    clb.Location = Point(12, 42)
    clb.Size = Size(440, 430)
    form.Controls.Add(clb)

    def on_item_check(sender, e):
        # e.Index is into the CURRENTLY DISPLAYED (filtered) list
        item_text = clb.Items[e.Index]
        checked_state[item_text] = (e.NewValue == CheckState.Checked)

    def refresh_list(filter_text):
        clb.ItemCheck -= on_item_check
        clb.BeginUpdate()
        clb.Items.Clear()
        ft = filter_text.lower()
        for it in items:
            if ft in it.lower():
                clb.Items.Add(it, checked_state.get(it, False))
        clb.EndUpdate()
        clb.ItemCheck += on_item_check

    def on_search_changed(sender, args):
        refresh_list(search_box.Text)

    search_box.TextChanged += on_search_changed
    clb.ItemCheck += on_item_check
    refresh_list("")

    ok_btn = Button()
    ok_btn.Text = "OK"
    ok_btn.DialogResult = DialogResult.OK
    ok_btn.Location = Point(292, 484)
    ok_btn.Size = Size(80, 28)
    form.Controls.Add(ok_btn)

    cancel_btn = Button()
    cancel_btn.Text = "Cancel"
    cancel_btn.DialogResult = DialogResult.Cancel
    cancel_btn.Location = Point(376, 484)
    cancel_btn.Size = Size(80, 28)
    form.Controls.Add(cancel_btn)

    form.AcceptButton = ok_btn
    form.CancelButton = cancel_btn
    form.Shown += lambda s, a: search_box.Focus()

    result = form.ShowDialog()
    if result == DialogResult.OK:
        return [it for it in items if checked_state.get(it, False)]
    return None


def alert(message, title="Info"):
    MessageBox.Show(message, title, MessageBoxButtons.OK, MessageBoxIcon.Information)


# ---------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------

def get_all_views(doc):
    views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
    # Whitelist of view types that actually support V/G category overrides.
    # Safer than excluding schedule/report types by name, since a couple
    # of those enum members (e.g. PresureLossReport) have inconsistent /
    # misspelled names across API versions.
    allowed_types = (
        DB.ViewType.FloorPlan,
        DB.ViewType.CeilingPlan,
        DB.ViewType.EngineeringPlan,
        DB.ViewType.AreaPlan,
        DB.ViewType.Elevation,
        DB.ViewType.Section,
        DB.ViewType.Detail,
        DB.ViewType.ThreeD,
        DB.ViewType.DraftingView,
        DB.ViewType.Walkthrough,
        DB.ViewType.Rendering,
        DB.ViewType.Legend,
    )
    result = []
    for v in views:
        if v.IsTemplate:
            continue
        if v.ViewType not in allowed_types:
            continue
        result.append(v)
    return result


def view_display_name(v):
    try:
        return "{} - {}".format(v.ViewType, v.Name)
    except Exception:
        return v.Name


def allows_visibility_control(cat, view):
    """Category.AllowsVisibilityControl is exposed by the Revit API as a
    parameterized property (like get_Parameter). Under IronPython this MUST
    be called as get_AllowsVisibilityControl(view) - calling it directly as
    AllowsVisibilityControl(view) raises 'indexer# is not callable' and is
    silently swallowed by any surrounding except-Exception block. Try the
    IronPython-correct form first, then fall back for other engines."""
    try:
        return cat.get_AllowsVisibilityControl(view)
    except AttributeError:
        return cat.AllowsVisibilityControl(view)


def is_top_level_link(link):
    """FilteredElementCollector(doc).OfClass(RevitLinkInstance) returns BOTH
    top-level Revit links AND nested links (links loaded inside another
    link) - the latter show up in the V/G 'Revit Links' tab as the unnamed
    numeric sub-rows under each top-level link. HideElements/UnhideElements
    and SetLinkOverrides were being called identically for both, which is
    what caused a parent link to end up visible in the target while an
    unrelated nested child link ended up hidden instead - the two are not
    interchangeable and must be told apart first. A nested link instance
    has a SuperComponent (the element it's nested inside); a top-level link
    does not."""
    try:
        return link.SuperComponent is None
    except Exception:
        # If SuperComponent isn't available for some reason, fall back to
        # treating it as top-level (previous behavior) rather than silently
        # dropping it.
        return True


def copy_view_settings(source, target, options):
    """options: dict of bool flags controlling what gets copied"""

    log("copy_view_settings: ENTER (target='{}')".format(target.Name))
    try:
        src_vt = source.ViewTemplateId
        tgt_vt = target.ViewTemplateId
        log("view_templates: source.ViewTemplateId={} target.ViewTemplateId={}".format(
            src_vt.Value if hasattr(src_vt, "Value") else src_vt.IntegerValue,
            tgt_vt.Value if hasattr(tgt_vt, "Value") else tgt_vt.IntegerValue,
        ))
    except Exception as e:
        log("view_templates: failed to read ViewTemplateId: {}".format(e))

    # --- Scale ---
    log("step: scale (selected={})".format(bool(options.get("scale"))))
    if options.get("scale") and target.ViewTemplateId == DB.ElementId.InvalidElementId:
        try:
            log("scale: about to set target.Scale = {}".format(source.Scale))
            target.Scale = source.Scale
            log("scale: done")
        except Exception:
            pass

    # --- Detail Level ---
    log("step: detail_level (selected={})".format(bool(options.get("detail_level"))))
    if options.get("detail_level"):
        try:
            log("detail_level: about to set target.DetailLevel = {}".format(source.DetailLevel))
            target.DetailLevel = source.DetailLevel
            log("detail_level: done")
        except Exception:
            pass

    # --- Phase / Phase Filter ---
    log("step: phase (selected={})".format(bool(options.get("phase"))))
    if options.get("phase"):
        for bip in (DB.BuiltInParameter.VIEW_PHASE,
                    DB.BuiltInParameter.VIEW_PHASE_FILTER):
            sp = source.get_Parameter(bip)
            tp = target.get_Parameter(bip)
            if sp and tp and not tp.IsReadOnly:
                try:
                    log("phase: about to set {}".format(bip))
                    tp.Set(sp.AsElementId())
                    log("phase: done {}".format(bip))
                except Exception:
                    pass

    # --- Category V/G overrides + visibility (Model + Annotation + Analytical Model categories) ---
    # Merged into a single pass over categories (previously two separate passes,
    # which doubled the number of AllowsVisibilityControl calls). Also runs a
    # periodic GC to release accumulated COM/interop references, since a crash
    # was observed partway through a ~400-category double pass with no single
    # category responsible - consistent with native resource buildup rather
    # than a specific bad category.
    log("step: category_overrides (selected={})".format(bool(options.get("category_overrides"))))
    if options.get("category_overrides"):
        categories = list(doc.Settings.Categories)
        log("category_overrides: total categories = {}".format(len(categories)))
        for i, cat in enumerate(categories):
            if i % 50 == 0 and i > 0:
                log("category_overrides: periodic GC at index {}".format(i))
                try:
                    gc.collect()
                    System.GC.Collect()
                    System.GC.WaitForPendingFinalizers()
                except Exception:
                    pass

            try:
                cat_name = cat.Name
            except Exception:
                cat_name = "<unnamed>"
            try:
                cat_id_int = int(cat.Id.Value) if hasattr(cat.Id, "Value") else int(cat.Id.IntegerValue)
            except Exception:
                cat_id_int = None
            if cat_id_int in SKIP_CATEGORY_BUILTINS:
                continue
            if cat.CategoryType not in (
                DB.CategoryType.Model,
                DB.CategoryType.Annotation,
                DB.CategoryType.AnalyticalModel,
            ):
                continue

            log("cat[{}]: '{}'".format(i, cat_name))

            try:
                allows_source = allows_visibility_control(cat, source)
            except Exception as e:
                allows_source = False
                log("cat[{}] '{}': AllowsVisibilityControl(source) raised: {}".format(i, cat_name, e))
            try:
                allows_target = allows_visibility_control(cat, target)
            except Exception as e:
                allows_target = False
                log("cat[{}] '{}': AllowsVisibilityControl(target) raised: {}".format(i, cat_name, e))
            allows_control = allows_source and allows_target

            # Graphic overrides (line/pattern/transparency colors etc.) - independent
            # try/except so a failure here never blocks the hidden-state copy below.
            try:
                ogs = source.GetCategoryOverrides(cat.Id)
                target.SetCategoryOverrides(cat.Id, ogs)
            except Exception as e:
                log("cat[{}] '{}': SetCategoryOverrides failed: {}".format(i, cat_name, e))

            # Hidden/visible checkbox state - independent try/except, and only
            # attempted when BOTH views allow visibility control for this category
            # (previously only 'source' was checked, so if 'target' didn't allow
            # it, SetCategoryHidden raised and - because it shared a try block
            # with SetCategoryOverrides above - the whole category was skipped
            # silently, leaving the hidden/shown state uncopied).
            if allows_control:
                try:
                    hidden = source.GetCategoryHidden(cat.Id)
                    target.SetCategoryHidden(cat.Id, hidden)
                except Exception as e:
                    log("cat[{}] '{}': SetCategoryHidden failed: {}".format(i, cat_name, e))
            else:
                log("cat[{}] '{}': visibility control not allowed - AllowsVisibilityControl(source)={} AllowsVisibilityControl(target)={} - hidden state skipped".format(
                    i, cat_name, allows_source, allows_target))
        log("category_overrides: loop complete")

    # --- V/G Filters (added filters + their overrides + visibility) ---
    log("step: filters (selected={})".format(bool(options.get("filters"))))
    if options.get("filters"):
        try:
            src_filter_ids = source.GetFilters()
        except Exception:
            src_filter_ids = []
        log("filters: count = {}".format(len(list(src_filter_ids))))
        for fid in src_filter_ids:
            try:
                log("filter: processing id {}".format(fid.Value if hasattr(fid, "Value") else fid.IntegerValue))
                if fid not in target.GetFilters():
                    log("filter: AddFilter")
                    target.AddFilter(fid)
                log("filter: GetFilterOverrides")
                ogs = source.GetFilterOverrides(fid)
                log("filter: SetFilterOverrides")
                target.SetFilterOverrides(fid, ogs)
                log("filter: SetFilterVisibility")
                target.SetFilterVisibility(fid, source.GetFilterVisibility(fid))
                log("filter: done")
            except Exception:
                continue

    # --- RVT Links (per-link graphic overrides + hide/show state) ---
    # Uses the dedicated link-override API (GetLinkOverrides/SetLinkOverrides)
    # rather than the generic category-override path, since OST_RvtLinks is
    # kept out of the category loop above for safety.
    log("step: revit_links (selected={})".format(bool(options.get("revit_links"))))
    if options.get("revit_links"):
        try:
            link_instances = list(DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance).ToElements())
        except Exception:
            link_instances = []
        log("revit_links: count = {}".format(len(link_instances)))
        top_level_links = [l for l in link_instances if is_top_level_link(l)]
        nested_count = len(link_instances) - len(top_level_links)
        log("revit_links: top-level = {}, nested (skipped) = {}".format(len(top_level_links), nested_count))
        for link in top_level_links:
            try:
                link_name = link.Name
            except Exception:
                link_name = "<link>"
            try:
                link_id_int = int(link.Id.Value) if hasattr(link.Id, "Value") else int(link.Id.IntegerValue)
            except Exception:
                link_id_int = "<?>"
            log("revit_link: id={} '{}'".format(link_id_int, link_name))
            try:
                ogs = source.GetLinkOverrides(link.Id)
                if ogs is None:
                    # No override configured on the source view at all (matches
                    # Display Settings = 'By Host View' and Halftone/Underlay
                    # both unticked in the V/G dialog) - there is nothing to
                    # copy, and passing None into SetLinkOverrides throws a
                    # null-argument error ("linkDisplaySettings ... is null").
                    # Skip cleanly instead of letting that exception fire.
                    log("revit_link '{}': source has no link-graphics override set (GetLinkOverrides returned None) - nothing to copy".format(link_name))
                else:
                    log("revit_link '{}': source overrides - Halftone={} Underlay={} DetailLevel={} Category={}".format(
                        link_name,
                        getattr(ogs, "Halftone", "<n/a>"),
                        getattr(ogs, "Underlay", "<n/a>"),
                        getattr(ogs, "DetailLevel", "<n/a>"),
                        getattr(ogs, "Category", "<n/a>"),
                    ))
                    target.SetLinkOverrides(link.Id, ogs)
                    log("revit_link '{}': SetLinkOverrides call completed (no exception)".format(link_name))

                    try:
                        verify_ogs = target.GetLinkOverrides(link.Id)
                        log("revit_link '{}': target overrides AFTER set - Halftone={} Underlay={} DetailLevel={} Category={}".format(
                            link_name,
                            getattr(verify_ogs, "Halftone", "<n/a>"),
                            getattr(verify_ogs, "Underlay", "<n/a>"),
                            getattr(verify_ogs, "DetailLevel", "<n/a>"),
                            getattr(verify_ogs, "Category", "<n/a>"),
                        ))
                    except Exception as e:
                        log("revit_link '{}': could not verify target override properties: {}".format(link_name, e))
            except Exception as e:
                log("revit_link '{}': SetLinkOverrides FAILED: {}".format(link_name, e))
            try:
                src_hidden = link.IsHidden(source)
                tgt_hidden_before = link.IsHidden(target)
                log("revit_link '{}': IsHidden - source={} target(before)={}".format(link_name, src_hidden, tgt_hidden_before))
                if src_hidden and not tgt_hidden_before:
                    target.HideElements(List[DB.ElementId]([link.Id]))
                    log("revit_link '{}': called HideElements".format(link_name))
                elif not src_hidden and tgt_hidden_before:
                    target.UnhideElements(List[DB.ElementId]([link.Id]))
                    log("revit_link '{}': called UnhideElements".format(link_name))
                else:
                    log("revit_link '{}': hidden state already matches - no change".format(link_name))
            except Exception as e:
                log("revit_link '{}': Hide/UnhideElements FAILED: {}".format(link_name, e))
        log("revit_links: done")

    # --- Worksets visibility (Visible / Hidden / Use Global Setting) ---
    log("step: worksets (selected={})".format(bool(options.get("worksets"))))
    if options.get("worksets") and doc.IsWorkshared:
        try:
            worksets = list(DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset).ToWorksets())
        except Exception:
            worksets = []
        log("worksets: count = {}".format(len(worksets)))
        for ws in worksets:
            try:
                log("workset: '{}'".format(ws.Name))
                vis = source.GetWorksetVisibility(ws.Id)
                target.SetWorksetVisibility(ws.Id, vis)
            except Exception:
                continue
        log("worksets: done")

    # --- Crop Box / Annotation Crop visibility settings ---
    log("step: crop (selected={})".format(bool(options.get("crop"))))
    if options.get("crop"):
        try:
            target.CropBoxActive = source.CropBoxActive
            target.CropBoxVisible = source.CropBoxVisible
        except Exception:
            pass
        try:
            target.AnnotationCropActive = source.AnnotationCropActive
        except Exception:
            pass

    # --- Parts Visibility ---
    log("step: parts_visibility (selected={})".format(bool(options.get("parts_visibility"))))
    if options.get("parts_visibility"):
        p_src = source.get_Parameter(DB.BuiltInParameter.VIEW_PARTS_VISIBILITY)
        p_tgt = target.get_Parameter(DB.BuiltInParameter.VIEW_PARTS_VISIBILITY)
        if p_src and p_tgt and not p_tgt.IsReadOnly:
            try:
                p_tgt.Set(p_src.AsInteger())
            except Exception:
                pass

    log("copy_view_settings: EXIT (target='{}')".format(target.Name))


def main():
    all_views = get_all_views(doc)
    view_map = {view_display_name(v): v for v in all_views}
    names_sorted = sorted(view_map.keys())

    source_name = pick_single(names_sorted, "Select SOURCE view (copy FROM)")
    if not source_name:
        return
    source_view = view_map[source_name]

    target_names = pick_checked(
        [n for n in names_sorted if n != source_name],
        "Select TARGET view(s) (copy TO)"
    )
    if not target_names:
        return
    target_views = [view_map[n] for n in target_names]

    OPTION_LABELS = [
        ("scale", "Scale (View Scale)"),
        ("detail_level", "Detail Level"),
        ("phase", "Phase / Phase Filter"),
        ("category_overrides", "Visibility/Graphics - Category Overrides"),
        ("filters", "Visibility/Graphics - Filters"),
        ("revit_links", "Visibility/Graphics - RVT Links"),
        ("worksets", "Worksets Visibility"),
        ("crop", "Crop Region (View + Annotation Crop)"),
        ("parts_visibility", "Parts Visibility"),
    ]
    label_to_key = dict((label, key) for key, label in OPTION_LABELS)
    all_labels = [label for key, label in OPTION_LABELS]

    selected_labels = pick_checked(
        all_labels,
        "Which settings to copy?",
        checked_default=all_labels
    )
    if not selected_labels:
        return
    opts = dict((label_to_key[l], True) for l in selected_labels)

    warnings = []
    log("=" * 60)
    log("RUN START. source='{}' targets={} opts={}".format(
        view_display_name(source_view), [view_display_name(v) for v in target_views], opts))
    with revit.Transaction("Copy View Settings"):
        for tv in target_views:
            log("--- target view: '{}' ---".format(view_display_name(tv)))
            if tv.ViewTemplateId != DB.ElementId.InvalidElementId:
                warnings.append(
                    "{} is controlled by a View Template - some settings were skipped."
                    .format(view_display_name(tv))
                )
            copy_view_settings(source_view, tv, opts)
    log("RUN COMPLETE")

    msg = "Copied settings from '{}' to {} view(s).".format(
        view_display_name(source_view), len(target_views)
    )
    if warnings:
        msg += "\n\n" + "\n".join(warnings)
    alert(msg, title="Done")


if __name__ == "__main__":
    main()