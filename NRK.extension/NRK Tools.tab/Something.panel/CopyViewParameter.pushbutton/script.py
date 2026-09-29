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
import json
import datetime
import System
from System.Collections.Generic import List

from System.Windows.Forms import (
    Form, ListBox, CheckedListBox, Button, DialogResult, TextBox,
    FormBorderStyle, SelectionMode, FormStartPosition, MessageBox,
    MessageBoxButtons, MessageBoxIcon, AnchorStyles, CheckState,
    Label, ProgressBar
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


def _rotate_log_if_large(max_lines=3000):
    """Keep the debug log from growing without bound across many runs -
    trims to the most recent max_lines each time the tool starts."""
    try:
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, "r") as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                with open(LOG_PATH, "w") as f:
                    f.writelines(lines[-max_lines:])
    except Exception:
        pass


_rotate_log_if_large()

LAST_OPTIONS_PATH = os.path.join(
    os.environ.get("APPDATA", os.environ.get("TEMP", r"C:\Temp")),
    "CopyViewSettings_last_options.json"
)


def _load_options_store():
    """{"last": [...keys], "presets": {"Preset Name": [...keys], ...}}.
    Transparently upgrades the older format (a bare JSON list) that
    earlier versions of this tool wrote."""
    try:
        with open(LAST_OPTIONS_PATH, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {"last": data, "presets": {}}
        if isinstance(data, dict):
            data.setdefault("last", [])
            data.setdefault("presets", {})
            return data
    except Exception:
        pass
    return {"last": [], "presets": {}}


def _save_options_store(data):
    try:
        with open(LAST_OPTIONS_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def load_last_options(valid_keys):
    """Returns the list of option keys checked last time, filtered to keys
    that still exist (in case the option set changes between versions), or
    None if there's no saved selection / it can't be read."""
    data = _load_options_store()
    keys = data.get("last")
    if isinstance(keys, list):
        kept = [k for k in keys if k in valid_keys]
        return kept if kept else None
    return None


def save_last_options(keys):
    data = _load_options_store()
    data["last"] = list(keys)
    _save_options_store(data)


def list_presets():
    return sorted(_load_options_store().get("presets", {}).keys())


def load_preset(name):
    return _load_options_store().get("presets", {}).get(name, [])


def save_preset(name, keys):
    data = _load_options_store()
    data.setdefault("presets", {})[name] = list(keys)
    _save_options_store(data)


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


def prompt_text(message, title):
    """Small single-line text input dialog (WinForms has no built-in
    InputBox). Returns the trimmed text, or None if cancelled/blank."""
    form = Form()
    form.Text = title
    form.Width = 380
    form.Height = 150
    form.StartPosition = FormStartPosition.CenterScreen
    form.FormBorderStyle = FormBorderStyle.FixedDialog
    form.MinimizeBox = False
    form.MaximizeBox = False

    lbl = Label()
    lbl.Location = Point(12, 12)
    lbl.Size = Size(340, 24)
    lbl.Text = message
    form.Controls.Add(lbl)

    tb = TextBox()
    tb.Location = Point(12, 40)
    tb.Size = Size(340, 24)
    form.Controls.Add(tb)

    ok_btn = Button()
    ok_btn.Text = "OK"
    ok_btn.DialogResult = DialogResult.OK
    ok_btn.Location = Point(196, 78)
    ok_btn.Size = Size(80, 28)
    form.Controls.Add(ok_btn)

    cancel_btn = Button()
    cancel_btn.Text = "Cancel"
    cancel_btn.DialogResult = DialogResult.Cancel
    cancel_btn.Location = Point(280, 78)
    cancel_btn.Size = Size(80, 28)
    form.Controls.Add(cancel_btn)

    form.AcceptButton = ok_btn
    form.CancelButton = cancel_btn
    form.Shown += lambda s, a: tb.Focus()

    result = form.ShowDialog()
    if result == DialogResult.OK and tb.Text.strip():
        return tb.Text.strip()
    return None


def pick_checked(items, title, checked_default=None, show_presets=False):
    """Multi-selection checklist with a live search box.
    Returns a list of selected strings (in original order), or None if cancelled.
    show_presets: adds a 'Save as Preset...' / 'Load Preset...' row on top,
    backed by save_preset()/load_preset()/list_presets() (module-level,
    used only by the settings-picker call in main() today)."""
    checked_default = checked_default or []
    checked_state = dict((it, it in checked_default) for it in items)

    y0 = 30 if show_presets else 0  # extra vertical offset for the preset row

    form = Form()
    form.Text = title
    form.Width = 480
    form.Height = 560 + y0
    form.StartPosition = FormStartPosition.CenterScreen
    form.FormBorderStyle = FormBorderStyle.FixedDialog
    form.MinimizeBox = False
    form.MaximizeBox = False

    if show_presets:
        save_preset_btn = Button()
        save_preset_btn.Text = "Save as Preset..."
        save_preset_btn.Location = Point(12, 12)
        save_preset_btn.Size = Size(210, 24)
        form.Controls.Add(save_preset_btn)

        load_preset_btn = Button()
        load_preset_btn.Text = "Load Preset..."
        load_preset_btn.Location = Point(242, 12)
        load_preset_btn.Size = Size(210, 24)
        form.Controls.Add(load_preset_btn)

    search_box = TextBox()
    search_box.Location = Point(12, 12 + y0)
    search_box.Size = Size(348, 24)
    form.Controls.Add(search_box)

    select_all_btn = Button()
    select_all_btn.Text = "All"
    select_all_btn.Location = Point(364, 12 + y0)
    select_all_btn.Size = Size(42, 24)
    form.Controls.Add(select_all_btn)

    clear_all_btn = Button()
    clear_all_btn.Text = "None"
    clear_all_btn.Location = Point(410, 12 + y0)
    clear_all_btn.Size = Size(42, 24)
    form.Controls.Add(clear_all_btn)

    clb = CheckedListBox()
    clb.CheckOnClick = True
    clb.Location = Point(12, 42 + y0)
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

    def set_all_visible(value):
        # Only affects items currently shown (i.e. matching the search filter),
        # so "All"/"None" work as "select/clear what I'm looking at right now".
        for i in range(clb.Items.Count):
            clb.SetItemChecked(i, value)
            checked_state[clb.Items[i]] = value

    select_all_btn.Click += lambda s, a: set_all_visible(True)
    clear_all_btn.Click += lambda s, a: set_all_visible(False)

    if show_presets:
        def do_save_preset(sender, args):
            name = prompt_text("Preset name:", "Save as Preset")
            if not name:
                return
            current = [it for it in items if checked_state.get(it, False)]
            save_preset(name, current)
            alert("Saved preset '{}'.".format(name), "Preset Saved")

        def do_load_preset(sender, args):
            names = list_presets()
            if not names:
                alert("No saved presets yet.", "Load Preset")
                return
            chosen = pick_single(names, "Load which preset?")
            if not chosen:
                return
            preset_items = load_preset(chosen)
            for it in items:
                checked_state[it] = it in preset_items
            refresh_list(search_box.Text)

        save_preset_btn.Click += do_save_preset
        load_preset_btn.Click += do_load_preset

    search_box.TextChanged += on_search_changed
    clb.ItemCheck += on_item_check
    refresh_list("")

    ok_btn = Button()
    ok_btn.Text = "OK"
    ok_btn.DialogResult = DialogResult.OK
    ok_btn.Location = Point(292, 484 + y0)
    ok_btn.Size = Size(80, 28)
    form.Controls.Add(ok_btn)

    cancel_btn = Button()
    cancel_btn.Text = "Cancel"
    cancel_btn.DialogResult = DialogResult.Cancel
    cancel_btn.Location = Point(376, 484 + y0)
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


class ProgressWindow(object):
    """Small non-modal WinForms window showing 'Copying view X of Y...'.
    Uses .Show() (not ShowDialog) plus Application.DoEvents() so it stays
    responsive without blocking the main thread or needing a WPF/async
    setup - consistent with keeping this tool's whole UI WinForms-only."""

    def __init__(self, total):
        self.form = Form()
        self.form.Text = "Copy View Settings"
        self.form.Width = 380
        self.form.Height = 140
        self.form.StartPosition = FormStartPosition.CenterScreen
        self.form.FormBorderStyle = FormBorderStyle.FixedDialog
        self.form.MinimizeBox = False
        self.form.MaximizeBox = False
        self.form.ControlBox = False

        self.label = Label()
        self.label.Location = Point(12, 16)
        self.label.Size = Size(340, 24)
        self.label.Text = "Starting..."
        self.form.Controls.Add(self.label)

        self.bar = ProgressBar()
        self.bar.Location = Point(12, 48)
        self.bar.Size = Size(340, 24)
        self.bar.Minimum = 0
        self.bar.Maximum = max(total, 1)
        self.bar.Value = 0
        self.form.Controls.Add(self.bar)

        # Second line for what's happening INSIDE the current view (which
        # category/filter/link/workset it's on right now) - without this,
        # the window looked frozen during a single view's ~400-category
        # loop even though work was happening, since step() below only
        # fires once per target view.
        self.detail = Label()
        self.detail.Location = Point(12, 80)
        self.detail.Size = Size(340, 24)
        self.detail.ForeColor = System.Drawing.Color.Gray
        self.detail.Text = ""
        self.form.Controls.Add(self.detail)

        self.form.Show()
        System.Windows.Forms.Application.DoEvents()

    def step(self, index, view_name):
        self.label.Text = "Copying to: {}".format(view_name)
        self.bar.Value = min(index, self.bar.Maximum)
        self.detail.Text = ""
        System.Windows.Forms.Application.DoEvents()

    def sub_status(self, text):
        self.detail.Text = text
        System.Windows.Forms.Application.DoEvents()

    def close(self):
        try:
            self.form.Close()
        except Exception:
            pass


# ---------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------

# Whitelist of view types that actually support V/G category overrides.
# Safer than excluding schedule/report types by name, since a couple
# of those enum members (e.g. PresureLossReport) have inconsistent /
# misspelled names across API versions. Shared by both real views and
# view templates, since a View Template IS a View object (IsTemplate=True)
# of one of these same ViewTypes under the hood.
ALLOWED_VIEW_TYPES = (
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


def get_all_views(doc):
    views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
    result = []
    for v in views:
        if v.IsTemplate:
            continue
        if v.ViewType not in ALLOWED_VIEW_TYPES:
            continue
        result.append(v)
    return result


def get_all_view_templates(doc):
    """View Templates are View objects too (IsTemplate=True), so almost
    every read/write we do on a normal 'source' view (GetCategoryOverrides,
    GetFilters, Scale, DetailLevel, etc.) works the same way on a template -
    letting a template be picked directly as the copy-FROM source, no
    special-casing needed elsewhere."""
    views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
    result = []
    for v in views:
        if not v.IsTemplate:
            continue
        if v.ViewType not in ALLOWED_VIEW_TYPES:
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


CATEGORY_OPTION_KEYS = ("cat_model", "cat_annotation", "cat_analytical", "cat_imported")


def get_category_bucket(cat, cat_id_int):
    """Maps a Category to the option key that controls it, matching the
    tabs Revit's own V/G dialog uses: Model / Annotation / Analytical Model
    / Imported Categories. Built-in categories always have a negative
    ElementId (matching a BuiltInCategory enum value); a CategoryType.Model
    category with a POSITIVE id is a non-built-in one - in practice this
    is how imported CAD file categories/layers show up, so that's used to
    tell 'Imported Categories' apart from ordinary 'Model Categories'."""
    if cat.CategoryType == DB.CategoryType.Annotation:
        return "cat_annotation"
    if cat.CategoryType == DB.CategoryType.AnalyticalModel:
        return "cat_analytical"
    if cat.CategoryType == DB.CategoryType.Model:
        if cat_id_int is not None and cat_id_int > 0:
            return "cat_imported"
        return "cat_model"
    return None


def get_effective_view_for_categories(view):
    """Category Overrides cannot be set via API directly on a Dependent View
    (a view created with 'Duplicate as Dependent'). Revit throws even though
    the same edit is fully allowed through the V/G dialog UI on a dependent
    view - the UI just quietly applies it to (and shares it from) the
    Primary View. Confirmed via a real project: a dependent view's own
    Properties panel showed View Template = <None>, yet SetCategoryOverrides
    still failed for every single category, while Filters/Worksets (no such
    restriction) succeeded normally on the same dependent view.
    Returns the Primary View element if `view` is a dependent view,
    otherwise returns `view` itself unchanged."""
    try:
        primary_id = view.GetPrimaryViewId()
        if primary_id and primary_id != DB.ElementId.InvalidElementId:
            primary_view = doc.GetElement(primary_id)
            if primary_view is not None:
                return primary_view
    except Exception:
        pass
    return view


def copy_view_settings(source, target, options, stats, categories_cache=None, progress=None):
    """options: dict of bool flags controlling what gets copied.
    stats: mutable dict accumulating counts across all target views, shown
    in the summary message at the end of the run.
    categories_cache: pre-fetched list(doc.Settings.Categories), so a
    multi-target run doesn't re-collect it from the document on every
    single target view.
    progress: optional ProgressWindow - given a sub_status() call at
    regular intervals so the window keeps visibly updating during a single
    view's category/filter/link/workset loops instead of appearing frozen."""

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
    category_options_selected = [k for k in CATEGORY_OPTION_KEYS if options.get(k)]
    log("step: category_overrides (selected={})".format(category_options_selected))
    if category_options_selected:
        cat_target = get_effective_view_for_categories(target)
        if cat_target.Id != target.Id:
            log("category_overrides: target '{}' is a Dependent View - redirecting category writes to its Primary View '{}' (the change will still show up on '{}')".format(
                target.Name, cat_target.Name, target.Name))
            stats["categories_redirected_to_primary"] = cat_target.Name

        categories = categories_cache if categories_cache is not None else list(doc.Settings.Categories)
        log("category_overrides: total categories = {}".format(len(categories)))
        for i, cat in enumerate(categories):
            if progress is not None and i % 10 == 0:
                try:
                    progress.sub_status("Category {}/{}: {}".format(i, len(categories), cat.Name))
                except Exception:
                    pass
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

            bucket = get_category_bucket(cat, cat_id_int)
            if bucket is None or not options.get(bucket):
                continue

            log("cat[{}]: '{}' (bucket={})".format(i, cat_name, bucket))

            try:
                allows_source = allows_visibility_control(cat, source)
            except Exception as e:
                allows_source = False
                log("cat[{}] '{}': AllowsVisibilityControl(source) raised: {}".format(i, cat_name, e))
            try:
                allows_target = allows_visibility_control(cat, cat_target)
            except Exception as e:
                allows_target = False
                log("cat[{}] '{}': AllowsVisibilityControl(target) raised: {}".format(i, cat_name, e))
            allows_control = allows_source and allows_target

            # Graphic overrides (line/pattern/transparency colors etc.) - independent
            # try/except so a failure here never blocks the hidden-state copy below.
            try:
                ogs = source.GetCategoryOverrides(cat.Id)
                cat_target.SetCategoryOverrides(cat.Id, ogs)
                stats["categories_ok"] = stats.get("categories_ok", 0) + 1
            except Exception as e:
                stats["categories_failed"] = stats.get("categories_failed", 0) + 1
                stats.setdefault("categories_failed_names", [])
                if len(stats["categories_failed_names"]) < 15:
                    stats["categories_failed_names"].append(cat_name)
                log("cat[{}] '{}': SetCategoryOverrides failed: {}".format(i, cat_name, e))
                # A genuine assigned View Template with "Model/Annotation
                # Categories" included fails every category the same way - no
                # point retrying the other ~380 one by one. (Dependent views
                # are already redirected to their Primary View above, so this
                # should now only fire for a real View Template lock.)
                if "template" in str(e).lower():
                    stats["categories_locked_by_template"] = True
                    stats["categories_failed_remaining_skipped"] = len(categories) - i - 1
                    log("category_overrides: View Template lock detected on '{}' - aborting remaining {} categories".format(
                        cat_name, len(categories) - i - 1))
                    break

            # Hidden/visible checkbox state - independent try/except, and only
            # attempted when BOTH views allow visibility control for this category
            # (previously only 'source' was checked, so if 'target' didn't allow
            # it, SetCategoryHidden raised and - because it shared a try block
            # with SetCategoryOverrides above - the whole category was skipped
            # silently, leaving the hidden/shown state uncopied).
            if allows_control:
                try:
                    hidden = source.GetCategoryHidden(cat.Id)
                    cat_target.SetCategoryHidden(cat.Id, hidden)
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
        src_filter_ids = list(src_filter_ids)
        log("filters: count = {}".format(len(src_filter_ids)))
        for fi, fid in enumerate(src_filter_ids):
            if progress is not None:
                try:
                    filt_name = doc.GetElement(fid).Name
                except Exception:
                    filt_name = str(fid)
                try:
                    progress.sub_status("Filter {}/{}: {}".format(fi + 1, len(src_filter_ids), filt_name))
                except Exception:
                    pass
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
                stats["filters_ok"] = stats.get("filters_ok", 0) + 1
            except Exception as e:
                stats["filters_failed"] = stats.get("filters_failed", 0) + 1
                log("filter: failed: {}".format(e))
                if "template" in str(e).lower():
                    stats["filters_locked_by_template"] = True
                    log("filters: View Template lock detected - aborting remaining filters")
                    break
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
        for li, link in enumerate(top_level_links):
            try:
                link_name = link.Name
            except Exception:
                link_name = "<link>"
            try:
                link_id_int = int(link.Id.Value) if hasattr(link.Id, "Value") else int(link.Id.IntegerValue)
            except Exception:
                link_id_int = "<?>"
            if progress is not None:
                try:
                    progress.sub_status("RVT Link {}/{}: {}".format(li + 1, len(top_level_links), link_name))
                except Exception:
                    pass
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
                    stats["links_ok"] = stats.get("links_ok", 0) + 1

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
                stats["links_failed"] = stats.get("links_failed", 0) + 1
                log("revit_link '{}': SetLinkOverrides FAILED: {}".format(link_name, e))
                if "template" in str(e).lower():
                    stats["links_locked_by_template"] = True
                    log("revit_links: View Template lock detected - aborting remaining links")
                    break
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
        for wi, ws in enumerate(worksets):
            if progress is not None and wi % 10 == 0:
                try:
                    progress.sub_status("Workset {}/{}: {}".format(wi + 1, len(worksets), ws.Name))
                except Exception:
                    pass
            try:
                log("workset: '{}'".format(ws.Name))
                vis = source.GetWorksetVisibility(ws.Id)
                target.SetWorksetVisibility(ws.Id, vis)
                stats["worksets_ok"] = stats.get("worksets_ok", 0) + 1
            except Exception as e:
                stats["worksets_failed"] = stats.get("worksets_failed", 0) + 1
                log("workset '{}': failed: {}".format(ws.Name, e))
                if "template" in str(e).lower():
                    stats["worksets_locked_by_template"] = True
                    log("worksets: View Template lock detected - aborting remaining worksets")
                    break
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


def get_preselected_target_views(view_by_id):
    """Views the user already selected (in the Project Browser, or by
    clicking a Viewport on a sheet) before running the command. Lets you
    pick your problem view(s) first, then only search for the ONE
    reference view to copy FROM, instead of also picking targets from
    a big checklist every time."""
    try:
        sel_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        sel_ids = []
    views = []
    seen_ids = set()
    for eid in sel_ids:
        try:
            el = doc.GetElement(eid)
        except Exception:
            continue
        if isinstance(el, DB.Viewport):
            try:
                el = doc.GetElement(el.ViewId)
            except Exception:
                continue
        if isinstance(el, DB.View) and el.Id in view_by_id and el.Id not in seen_ids:
            views.append(el)
            seen_ids.add(el.Id)
    return views


def get_current_sheet_views(view_by_id):
    """If the user currently has a Sheet open/active, returns
    (sheet, [views placed on it]) - lets 'Copy From' offer a shortcut
    straight to the views on that sheet instead of searching the whole
    project, for the common case of referencing another viewport on the
    same sheet. Returns (None, []) if the active view isn't a sheet."""
    try:
        active = uidoc.ActiveView
    except Exception:
        active = None
    if not isinstance(active, DB.ViewSheet):
        return None, []
    try:
        placed_ids = list(active.GetAllPlacedViews())
    except Exception:
        placed_ids = []
    views = []
    for vid in placed_ids:
        v = view_by_id.get(vid)
        if v is not None:
            views.append(v)
    return active, views


def pick_source(all_views, all_templates, exclude_names=None, title_suffix="",
                 sheet=None, sheet_views=None):
    """Lets the user choose whether to copy FROM a View placed on the
    current sheet, any real View, or a View Template - then search/pick
    within whichever list. Returns the chosen View (or Template-as-View)
    element, or None if cancelled at any step."""
    exclude_names = exclude_names or set()
    sheet_views = sheet_views or []

    mode_options = []
    if sheet_views:
        mode_options.append("Views on Current Sheet")
    mode_options.append("Any View")
    mode_options.append("View Template")

    if len(mode_options) == 1:
        mode = mode_options[0]
    else:
        mode = pick_single(mode_options, "Copy settings FROM...")
        if not mode:
            return None

    if mode == "Views on Current Sheet":
        sheet_map = {view_display_name(v): v for v in sheet_views}
        names = sorted(n for n in sheet_map.keys() if n not in exclude_names)
        if not names:
            alert(
                "No other eligible views are placed on the current sheet ('{}').".format(sheet.Name),
                "Views on Current Sheet"
            )
            return None
        picked = pick_single(
            names,
            "Select REFERENCE view from sheet '{}'{}".format(sheet.Name, title_suffix)
        )
        if not picked:
            return None
        return sheet_map[picked]

    elif mode == "View Template":
        template_map = {view_display_name(v): v for v in all_templates}
        names = sorted(template_map.keys())
        picked = pick_single(names, "Select REFERENCE TEMPLATE (copy FROM){}".format(title_suffix))
        if not picked:
            return None
        return template_map[picked]

    else:  # "Any View"
        view_map = {view_display_name(v): v for v in all_views}
        names = sorted(n for n in view_map.keys() if n not in exclude_names)
        picked = pick_single(names, "Select REFERENCE VIEW (copy FROM){}".format(title_suffix))
        if not picked:
            return None
        return view_map[picked]


def main():
    all_views = get_all_views(doc)
    all_templates = get_all_view_templates(doc)
    view_map = {view_display_name(v): v for v in all_views}
    view_by_id = dict((v.Id, v) for v in all_views)
    names_sorted = sorted(view_map.keys())

    current_sheet, current_sheet_views = get_current_sheet_views(view_by_id)
    if current_sheet:
        log("main: active sheet '{}' has {} placed view(s) eligible as a reference".format(
            current_sheet.Name, len(current_sheet_views)))

    preselected_targets = get_preselected_target_views(view_by_id)

    if preselected_targets:
        target_views = preselected_targets
        log("main: using {} pre-selected view(s) as target(s): {}".format(
            len(target_views), [v.Name for v in target_views]))
        exclude_names = set(view_display_name(v) for v in target_views)
        target_list_str = ", ".join(v.Name for v in target_views)
        source_view = pick_source(
            all_views, all_templates, exclude_names,
            title_suffix=" -> target: {}".format(target_list_str),
            sheet=current_sheet, sheet_views=current_sheet_views
        )
        if not source_view:
            return
    else:
        source_view = pick_source(
            all_views, all_templates,
            sheet=current_sheet, sheet_views=current_sheet_views
        )
        if not source_view:
            return

        exclude_names = set([view_display_name(source_view)]) if not source_view.IsTemplate else set()
        target_names = pick_checked(
            [n for n in names_sorted if n not in exclude_names],
            "Select TARGET view(s) (copy TO)"
        )
        if not target_names:
            return
        target_views = [view_map[n] for n in target_names]

    OPTION_LABELS = [
        ("scale", "Scale (View Scale)"),
        ("detail_level", "Detail Level"),
        ("phase", "Phase / Phase Filter"),
        ("cat_model", "Visibility/Graphics - Model Categories"),
        ("cat_annotation", "Visibility/Graphics - Annotation Categories"),
        ("cat_analytical", "Visibility/Graphics - Analytical Model Categories"),
        ("cat_imported", "Visibility/Graphics - Imported Categories"),
        ("filters", "Visibility/Graphics - Filters"),
        ("revit_links", "Visibility/Graphics - RVT Links"),
        ("worksets", "Worksets Visibility"),
        ("crop", "Crop Region (View + Annotation Crop)"),
        ("parts_visibility", "Parts Visibility"),
    ]
    label_to_key = dict((label, key) for key, label in OPTION_LABELS)
    key_to_label = dict((key, label) for key, label in OPTION_LABELS)
    all_keys = [key for key, label in OPTION_LABELS]
    all_labels = [label for key, label in OPTION_LABELS]

    last_keys = load_last_options(all_keys)
    default_labels = [key_to_label[k] for k in last_keys] if last_keys else all_labels

    selected_labels = pick_checked(
        all_labels,
        "Which settings to copy?",
        checked_default=default_labels,
        show_presets=True
    )
    if not selected_labels:
        return
    opts = dict((label_to_key[l], True) for l in selected_labels)
    save_last_options([label_to_key[l] for l in selected_labels])

    # --- Preview: flag target views with special conditions BEFORE running,
    # instead of only finding out from the summary dialog afterwards. Only
    # interrupts the flow when there's actually something to flag.
    category_selected = any(opts.get(k) for k in CATEGORY_OPTION_KEYS)
    preview_lines = []
    for tv in target_views:
        flags = []
        if tv.ViewTemplateId != DB.ElementId.InvalidElementId:
            flags.append("has a View Template assigned")
        if category_selected:
            try:
                eff = get_effective_view_for_categories(tv)
                if eff.Id != tv.Id:
                    flags.append("Dependent View -> Categories will go to Primary '{}'".format(eff.Name))
            except Exception:
                pass
        if flags:
            preview_lines.append("- {}: {}".format(tv.Name, "; ".join(flags)))

    if preview_lines:
        preview_msg = (
            "Heads up - {} of {} target view(s) have special conditions:\n\n{}"
            "\n\nContinue anyway?"
        ).format(len(preview_lines), len(target_views), "\n".join(preview_lines))
        confirm = MessageBox.Show(preview_msg, "Before you run this...",
                                   MessageBoxButtons.YesNo, MessageBoxIcon.Warning)
        if confirm != DialogResult.Yes:
            log("main: cancelled by user after preview")
            return

    warnings = []
    stats = {}
    log("=" * 60)
    log("RUN START. source='{}' targets={} opts={}".format(
        view_display_name(source_view), [view_display_name(v) for v in target_views], opts))

    categories_cache = list(doc.Settings.Categories)

    progress = ProgressWindow(len(target_views))
    try:
        with revit.Transaction("Copy View Settings"):
            for idx, tv in enumerate(target_views, start=1):
                progress.step(idx - 1, view_display_name(tv))
                log("--- target view: '{}' ---".format(view_display_name(tv)))
                if tv.ViewTemplateId != DB.ElementId.InvalidElementId:
                    warnings.append(
                        "{} is controlled by a View Template - some settings were skipped."
                        .format(view_display_name(tv))
                    )
                copy_view_settings(source_view, tv, opts, stats, categories_cache, progress)
            progress.step(len(target_views), "Done")
    finally:
        progress.close()
    log("RUN COMPLETE. stats={}".format(stats))

    lines = [
        "Copied settings from '{}' to {} view(s).".format(
            view_display_name(source_view), len(target_views)
        ),
        "",
    ]
    if any(opts.get(k) for k in CATEGORY_OPTION_KEYS):
        lines.append("Categories: {} ok, {} failed".format(
            stats.get("categories_ok", 0), stats.get("categories_failed", 0)))
    if opts.get("filters"):
        lines.append("Filters: {} ok, {} failed".format(
            stats.get("filters_ok", 0), stats.get("filters_failed", 0)))
    if opts.get("revit_links"):
        lines.append("RVT Links: {} ok, {} failed".format(
            stats.get("links_ok", 0), stats.get("links_failed", 0)))
    if opts.get("worksets"):
        lines.append("Worksets: {} ok, {} failed".format(
            stats.get("worksets_ok", 0), stats.get("worksets_failed", 0)))

    msg = "\n".join(lines)

    failed_names = stats.get("categories_failed_names")
    if failed_names:
        shown = ", ".join(failed_names)
        more = stats.get("categories_failed", 0) - len(failed_names)
        if more > 0:
            shown += ", and {} more (see log)".format(more)
        msg += "\n\nCategories that failed: {}".format(shown)

    if stats.get("categories_redirected_to_primary"):
        msg += (
            "\n\nNote: one or more target views are Dependent Views, so their "
            "Category Overrides were applied to their Primary View instead "
            "(e.g. '{}') - this is normal, and the change appears on the "
            "dependent view(s) automatically.".format(stats["categories_redirected_to_primary"])
        )
    if stats.get("categories_locked_by_template"):
        msg += (
            "\n\nCategory Overrides could not be applied: the target view's "
            "assigned View Template has 'Model/Annotation Categories' included, "
            "which locks this setting entirely. To copy Category Overrides "
            "here, either set the view's View Template to <None>, or edit the "
            "template and untick 'Model Categories'/'Annotation Categories' "
            "in its 'Value Included' column."
        )
    if stats.get("filters_locked_by_template"):
        msg += (
            "\n\nFilters could not be applied: the target view's assigned "
            "View Template has 'Filters' included, which locks this setting."
        )
    if stats.get("worksets_locked_by_template"):
        msg += (
            "\n\nWorksets could not be applied: the target view's assigned "
            "View Template has 'Worksets' included, which locks this setting."
        )
    if stats.get("links_locked_by_template"):
        msg += (
            "\n\nRVT Links could not be applied: the target view's assigned "
            "View Template has 'RVT Links' included, which locks this setting."
        )
    if warnings:
        msg += "\n\n" + "\n".join(warnings)
    alert(msg, title="Done")


if __name__ == "__main__":
    main()