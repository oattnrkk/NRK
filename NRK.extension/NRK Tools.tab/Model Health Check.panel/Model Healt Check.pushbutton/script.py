#! python2
# -*- coding: utf-8 -*-
"""Model Health Check
Reads a BIM Interoperability Tools (Model Checker) check set XML as its
settings and runs every check in Revit: P0 / P1 / P2 / MONITORING.
Put the XML in the same folder as this script, or browse to it."""
__title__ = 'Model Health\nCheck'
__author__ = 'NRK'

import clr
import os
import re
import time
import xml.etree.ElementTree as ET

clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')
clr.AddReference('RevitAPI')

import System
from System.Collections.Generic import List, HashSet
import System.Windows.Forms as WF
import System.Drawing as SD
import Autodesk.Revit.DB as DB
from pyrevit import revit

doc = revit.doc
uidoc = revit.uidoc
app = doc.Application

MAX_ROWS = 5000
FT_TO_MM = 304.8


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def eid_val(i):
    """ElementId -> integer, works on old and new Revit APIs."""
    try:
        return i.Value
    except AttributeError:
        return i.IntegerValue


def is_valid_id(i):
    try:
        return i is not None and eid_val(i) != -1
    except Exception:
        return False


def safe_name(el):
    try:
        return el.Name or ''
    except Exception:
        return ''


def clr_type(cls):
    return clr.GetClrType(cls)


def truthy(v):
    return str(v).strip().lower() == 'true'


def file_size_mb(path):
    try:
        return os.path.getsize(path) / 1048576.0
    except Exception:
        return None


def model_file_path():
    """Central file path for workshared models, else the saved path."""
    try:
        if doc.IsWorkshared:
            mp = doc.GetWorksharingCentralModelPath()
            if mp is not None:
                p = DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(mp)
                if p:
                    return p
    except Exception:
        pass
    return doc.PathName or ''


# ----------------------------------------------------------------------------
# check-set XML
# ----------------------------------------------------------------------------
class CheckDef(object):
    def __init__(self):
        self.heading = ''
        self.section = ''
        self.name = ''
        self.type = 'Custom'
        self.result_condition = 'FailMatchingElements'
        self.required = True
        self.checked = True
        self.failure_message = ''
        self.description = ''
        self.filters = []       # list of dict
        self.options = {}       # PrebuiltOptions attributes


def parse_checkset(path):
    root = ET.parse(path).getroot()
    checks = []
    for h in root.findall('Heading'):
        ht = (h.get('HeadingText') or '').strip()
        if ht.startswith('<--'):          # "<--Unused-->" headings
            continue
        for s in h.findall('Section'):
            for c in s.findall('Check'):
                d = CheckDef()
                d.heading = ht
                d.section = (s.get('SectionName') or '').strip()
                d.name = c.get('CheckName') or ''
                d.type = c.get('CheckType') or 'Custom'
                d.result_condition = c.get('ResultCondition') or 'FailMatchingElements'
                d.required = truthy(c.get('IsRequired', 'True'))
                d.checked = truthy(c.get('IsChecked', 'True'))
                d.failure_message = c.get('FailureMessage') or ''
                d.description = c.get('Description') or ''
                for f in c.findall('Filter'):
                    d.filters.append({
                        'op': f.get('Operator') or 'And',
                        'cat': f.get('Category') or '',
                        'prop': f.get('Property') or '',
                        'cond': f.get('Condition') or '',
                        'val': f.get('Value') or '',
                        'ci': truthy(f.get('CaseInsensitive', 'False')),
                    })
                po = c.find('PrebuiltOptions')
                if po is not None:
                    d.options = dict(po.attrib)
                checks.append(d)
    return checks, root.get('Name') or os.path.basename(path)


def split_groups(filters):
    """First group = include filters. Every 'Exclude' filter starts a new
    exclusion group; the And-filters after it belong to that group."""
    groups = [[]]
    for f in filters:
        if f['op'] == 'Exclude':
            groups.append([f])
        else:
            groups[-1].append(f)
    return groups


# ----------------------------------------------------------------------------
# results
# ----------------------------------------------------------------------------
class Result(object):
    def __init__(self, status='INFO', message='', items=None):
        self.status = status          # PASS FAIL WARN INFO SKIP ERROR
        self.message = message
        self.items = items or []      # tuples: (text, ElementId or None, extra)

    @property
    def count(self):
        return len(self.items)


def flag_status(chk):
    return 'FAIL' if chk.required else 'WARN'


# ----------------------------------------------------------------------------
# run context (caches)
# ----------------------------------------------------------------------------
class Ctx(object):
    def __init__(self, progress=None, cancelled=None):
        self._instances = None
        self._ws_names = {}
        self.progress = progress or (lambda t: None)
        self.cancelled = cancelled or (lambda: False)
        self.workshared = doc.IsWorkshared

    def instances(self):
        if self._instances is None:
            self.progress('Collecting model elements...')
            self._instances = list(
                DB.FilteredElementCollector(doc).WhereElementIsNotElementType().ToElements())
        return self._instances

    def workset_name(self, el):
        if not self.workshared:
            return None
        try:
            wid = el.WorksetId
            key = wid.IntegerValue
            if key not in self._ws_names:
                ws = doc.GetWorksetTable().GetWorkset(wid)
                self._ws_names[key] = ws.Name if ws is not None else ''
            return self._ws_names[key]
        except Exception:
            return None


# ----------------------------------------------------------------------------
# Custom (filter based) checks
# ----------------------------------------------------------------------------
def bic_from_name(name):
    return getattr(DB.BuiltInCategory, name, None)


def bip_from_name(name):
    return getattr(DB.BuiltInParameter, name, None)


def param_text(el, name):
    """Text of a parameter (built-in or by name), falling back to the type."""
    targets = [el]
    try:
        tid = el.GetTypeId()
        if is_valid_id(tid):
            t = doc.GetElement(tid)
            if t is not None:
                targets.append(t)
    except Exception:
        pass
    bip = bip_from_name(name)
    for t in targets:
        p = None
        try:
            if bip is not None:
                p = t.get_Parameter(bip)
            if p is None:
                p = t.LookupParameter(name)
        except Exception:
            p = None
        if p is None:
            continue
        try:
            if p.StorageType == DB.StorageType.String:
                v = p.AsString()
            else:
                v = p.AsValueString()
                if v is None and p.HasValue:
                    if p.StorageType == DB.StorageType.Integer:
                        v = str(p.AsInteger())
                    elif p.StorageType == DB.StorageType.Double:
                        v = str(p.AsDouble())
            if v is not None:
                return v
        except Exception:
            continue
    return None


def family_of(el):
    if isinstance(el, DB.FamilyInstance):
        return el.Symbol.Family
    if isinstance(el, DB.FamilySymbol):
        return el.Family
    return None


def prop_value(ctx, el, cat, prop):
    if cat == 'TypeOrInstance':
        return str(isinstance(el, DB.ElementType))
    if cat == 'APIType':
        return el.GetType().FullName
    if cat == 'APIParameter':
        return str(getattr(el, prop))
    if cat == 'Parameter':
        if prop == 'Name':
            return safe_name(el)
        return param_text(el, prop)
    if cat == 'Family':
        fam = family_of(el)
        if prop == 'Is In Place':
            return str(fam.IsInPlace) if fam is not None else 'False'
        if fam is not None:
            return fam.Name
        return param_text(el, 'ELEM_FAMILY_PARAM')
    if cat == 'Workset':
        return ctx.workset_name(el)
    if cat == 'Type':
        if isinstance(el, DB.ElementType):
            return safe_name(el)
        t = doc.GetElement(el.GetTypeId())
        return safe_name(t) if t is not None else None
    return None


def compare(actual, cond, val, ci, cat=''):
    if cond == 'HasValue' or cond == 'HasNoValue':
        has = actual is not None and str(actual) != ''
        want = truthy(val)
        return (has == want) if cond == 'HasValue' else (has != want)
    if actual is None:
        if cat == 'Workset':
            return False
        actual = ''
    actual = str(actual)
    flags = re.IGNORECASE if ci else 0
    if cond == 'Equal':
        return actual.lower() == val.lower()
    if cond == 'NotEqual':
        return actual.lower() != val.lower()
    if cond == 'Contains':
        return val.lower() in actual.lower()
    if cond == 'DoesNotContain':
        return val.lower() not in actual.lower()
    if cond in ('WildCard', 'WildCardNoMatch'):
        try:
            m = re.search(val, actual, flags) is not None
        except re.error:
            m = False
        return m if cond == 'WildCard' else (not m)
    raise ValueError('Unsupported condition: ' + cond)


def category_match(el, f):
    b = bic_from_name(f['prop'])
    if b is None or el.Category is None:
        return False
    try:
        hit = el.Category.BuiltInCategory == b
    except Exception:
        hit = False
    return hit if truthy(f['val']) else (not hit)


def eval_group(ctx, el, group):
    cat_f = [f for f in group if f['cat'] == 'Category' and f['cond'] == 'Included']
    others = [f for f in group if not (f['cat'] == 'Category' and f['cond'] == 'Included')]
    if cat_f:
        # several category filters = the element may be in any of them
        if not any(category_match(el, f) for f in cat_f):
            return False
    res = True
    first = True
    for f in others:
        try:
            v = compare(prop_value(ctx, el, f['cat'], f['prop']),
                        f['cond'], f['val'], f['ci'], f['cat'])
        except Exception:
            v = False
        if first:
            res = v
            first = False
        elif f['op'] == 'Or':
            res = res or v
        else:
            res = res and v
    return res


def collect_candidates(inc):
    bics = []
    want_type = None
    class_name = None
    for f in inc:
        if f['cat'] == 'Category' and f['cond'] == 'Included' and truthy(f['val']):
            b = bic_from_name(f['prop'])
            if b is not None:
                bics.append(b)
        elif f['cat'] == 'TypeOrInstance' and f['cond'] == 'Equal':
            want_type = truthy(f['val'])
        elif f['cat'] == 'APIType' and f['cond'] == 'Equal':
            if f['val'].startswith('Autodesk.Revit.DB.'):
                class_name = f['val'].split('.')[-1]
    out = []
    kinds = [want_type] if want_type is not None else [False, True]
    for as_type in kinds:
        col = DB.FilteredElementCollector(doc)
        if bics:
            lst = List[DB.BuiltInCategory]()
            for b in bics:
                lst.Add(b)
            col = col.WherePasses(DB.ElementMulticategoryFilter(lst))
        cls = getattr(DB, class_name, None) if class_name else None
        if cls is not None:
            col = col.OfClass(clr_type(cls))
        col = col.WhereElementIsElementType() if as_type else col.WhereElementIsNotElementType()
        out.extend(list(col.ToElements()))
    return out


def describe(ctx, el):
    try:
        cat = el.Category.Name if el.Category is not None else ''
    except Exception:
        cat = ''
    fam = family_of(el)
    if fam is not None:
        text = '%s | %s : %s' % (cat, fam.Name, safe_name(el))
    else:
        text = '%s | %s' % (cat, safe_name(el))
    extra = []
    ws = ctx.workset_name(el)
    if ws:
        extra.append('Workset: ' + ws)
    try:
        lid = el.LevelId
        if is_valid_id(lid):
            extra.append('Level: ' + safe_name(doc.GetElement(lid)))
    except Exception:
        pass
    return text, ' | '.join(extra)


def run_custom(ctx, chk):
    groups = split_groups(chk.filters)
    inc, excl = groups[0], groups[1:]
    ctx.progress('Scanning: ' + chk.name)
    matches = []
    for el in collect_candidates(inc):
        try:
            if not eval_group(ctx, el, inc):
                continue
            if any(eval_group(ctx, el, g) for g in excl):
                continue
            matches.append(el)
        except Exception:
            continue
    items = []
    for el in matches:
        text, extra = describe(ctx, el)
        items.append((text, el.Id, extra))
    items.sort(key=lambda x: x[0])
    rc = chk.result_condition
    if rc == 'CountAndList':
        return Result('INFO', '%d element(s)' % len(items), items)
    if rc == 'FailNoElements':
        if items:
            return Result('PASS', '%d matching element(s) found' % len(items), items)
        return Result(flag_status(chk), chk.failure_message or 'No matching elements found', [])
    # FailMatchingElements
    if items:
        return Result(flag_status(chk), chk.failure_message or '%d element(s) found' % len(items), items)
    return Result('PASS', 'No issues found', [])


# ----------------------------------------------------------------------------
# prebuilt checks
# ----------------------------------------------------------------------------
def chk_design_options(ctx, chk):
    opts = list(DB.FilteredElementCollector(doc).OfClass(clr_type(DB.DesignOption)).ToElements())
    if not opts:
        return Result('PASS', 'No design options in the model')
    counts = {}
    if chk.options.get('ShowCount', 'False') == 'True':
        for el in ctx.instances():
            try:
                d = el.DesignOption
                if d is not None:
                    counts[d.Name] = counts.get(d.Name, 0) + 1
            except Exception:
                pass
    items = []
    for o in opts:
        n = counts.get(o.Name)
        items.append((o.Name, o.Id, ('%d element(s)' % n) if n is not None else ''))
    return Result('WARN', '%d design option(s) in the model' % len(items), items)


def chk_project_coordinates(ctx, chk):
    items = []
    for bp in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.BasePoint)).ToElements():
        label = 'Survey Point' if bp.IsShared else 'Project Base Point'
        vals = []
        for title, name in (('E/W', 'BASEPOINT_EASTWEST_PARAM'), ('N/S', 'BASEPOINT_NORTHSOUTH_PARAM'),
                            ('Elev', 'BASEPOINT_ELEVATION_PARAM'), ('Angle to True North', 'BASEPOINT_ANGLETON_PARAM')):
            v = param_text(bp, name)
            if v is not None:
                vals.append('%s = %s' % (title, v))
        items.append((label, bp.Id, ' | '.join(vals)))
    try:
        loc = doc.ActiveProjectLocation
        pos = loc.GetProjectPosition(DB.XYZ.Zero)
        items.append(('Active project location', None, '%s | True North angle = %.4f deg'
                      % (loc.Name, pos.Angle * 180.0 / 3.141592653589793)))
    except Exception:
        pass
    return Result('INFO', 'Base point / survey point / true north', items)


def chk_warnings(ctx, chk):
    warns = list(doc.GetWarnings())
    if not warns:
        return Result('PASS', 'No warnings')
    groups = {}
    for w in warns:
        d = w.GetDescriptionText()
        groups.setdefault(d, []).append(w)
    items = []
    for d, ws in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ids = list(ws[0].GetFailingElements())
        items.append(('%d x %s' % (len(ws), d), ids[0] if ids else None, ''))
    return Result('WARN', '%d warning(s), %d type(s)' % (len(warns), len(groups)), items)


def chk_file_name(ctx, chk):
    pattern = chk.options.get('FileNamePattern', '')
    path = model_file_path()
    if path:
        name = os.path.splitext(os.path.basename(path))[0]
    else:
        name = os.path.splitext(doc.Title)[0]
    try:
        ok = re.search(pattern, name) is not None
    except re.error:
        return Result('ERROR', 'Invalid FileNamePattern in XML')
    items = [(name, None, 'Checked name')]
    if ok:
        return Result('PASS', 'File name matches the standard', items)
    return Result(flag_status(chk), chk.failure_message or 'File name does not match', items)


def chk_revit_version(ctx, chk):
    items = [('Version', None, app.VersionName), ('Version number', None, app.VersionNumber),
             ('Build', None, app.VersionBuild)]
    return Result('INFO', '%s (build %s)' % (app.VersionName, app.VersionBuild), items)


def chk_file_size(ctx, chk):
    path = model_file_path()
    mb = file_size_mb(path)
    if mb is None:
        return Result('INFO', 'File size not available (file not saved / cloud model)',
                      [(path or doc.Title, None, '')])
    return Result('INFO', '%.1f MB' % mb, [(path, None, '%.1f MB' % mb)])


VIEW_SKIP = ('Schedule', 'Legend', 'DrawingSheet', 'ProjectBrowser', 'SystemBrowser',
             'Internal', 'Undefined', 'Report', 'CostReport', 'LoadsReport',
             'PressureLossReport', 'PanelSchedule', 'ColumnSchedule')


def chk_views_not_on_sheets(ctx, chk):
    placed = set()
    for vp in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.Viewport)).ToElements():
        placed.add(eid_val(vp.ViewId))
    items = []
    for v in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.View)).ToElements():
        try:
            if v.IsTemplate or str(v.ViewType) in VIEW_SKIP:
                continue
            if eid_val(v.Id) in placed:
                continue
            items.append((v.Name, v.Id, str(v.ViewType)))
        except Exception:
            continue
    items.sort(key=lambda x: x[0])
    if items:
        return Result('WARN', '%d view(s) not placed on a sheet' % len(items), items)
    return Result('PASS', 'All views are on sheets')


def chk_phase_elements(ctx, chk):
    counts = {}
    for el in ctx.instances():
        try:
            pid = el.CreatedPhaseId
            if is_valid_id(pid):
                n = safe_name(doc.GetElement(pid))
                counts[n] = counts.get(n, 0) + 1
        except Exception:
            pass
    items = [(k, None, '%d element(s)' % v) for k, v in sorted(counts.items())]
    return Result('INFO', 'Elements created per phase', items)


def _import_instances(linked):
    out = []
    for ii in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.ImportInstance)).ToElements():
        try:
            if ii.IsLinked == linked:
                out.append(ii)
        except Exception:
            pass
    return out


def chk_linked_cad(ctx, chk):
    items = []
    for ii in _import_instances(True):
        t = doc.GetElement(ii.GetTypeId())
        items.append((safe_name(t) or safe_name(ii), ii.Id, ''))
    if items:
        return Result(flag_status(chk), '%d linked CAD file(s)' % len(items), items)
    return Result('PASS', 'No linked CAD files')


def chk_pinned_links(ctx, chk):
    items = []
    for li in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.RevitLinkInstance)).ToElements():
        if not li.Pinned:
            items.append((li.Name, li.Id, 'Revit link'))
    for ii in _import_instances(True):
        if not ii.Pinned:
            items.append((safe_name(ii), ii.Id, 'CAD link'))
    if items:
        return Result(flag_status(chk), '%d link(s) not pinned' % len(items), items)
    return Result('PASS', 'All links are pinned')


def chk_views_hidden(ctx, chk):
    views = []
    for v in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.View)).ToElements():
        try:
            if v.IsTemplate:
                continue
            if str(v.ViewType) in ('FloorPlan', 'CeilingPlan', 'Elevation', 'Section',
                                   'ThreeD', 'EngineeringPlan', 'AreaPlan', 'Detail'):
                views.append(v)
        except Exception:
            pass
    cands = []
    for el in ctx.instances():
        try:
            c = el.Category
            if c is not None and c.CategoryType == DB.CategoryType.Model and not el.ViewSpecific:
                cands.append(el)
        except Exception:
            pass
    items = []
    for n, v in enumerate(views):
        if ctx.cancelled():
            break
        ctx.progress('Hidden elements: view %d / %d' % (n + 1, len(views)))
        hidden = 0
        for el in cands:
            try:
                if el.CanBeHidden(v) and el.IsHidden(v):
                    hidden += 1
            except Exception:
                pass
        if hidden:
            items.append((v.Name, v.Id, '%d hidden element(s)' % hidden))
    items.sort(key=lambda x: x[0])
    if items:
        return Result('WARN', '%d view(s) with hidden elements' % len(items), items)
    return Result('PASS', 'No views with permanently hidden elements')


def chk_workset_placement(ctx, chk):
    if not ctx.workshared:
        return Result('INFO', '*Not workshared*')
    wanted = set()
    for ws in DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset):
        n = ws.Name.lower()
        if 'level' in n and 'grid' in n:
            wanted.add(ws.Id.IntegerValue)
    if not wanted:
        return Result('INFO', "No workset with both 'level' and 'grid' in its name")
    items = []
    for el in ctx.instances():
        try:
            if el.WorksetId.IntegerValue in wanted and not isinstance(el, (DB.Level, DB.Grid)):
                text, extra = describe(ctx, el)
                items.append((text, el.Id, extra))
        except Exception:
            pass
    if items:
        return Result(flag_status(chk), '%d element(s) on the Levels and Grids workset that are not levels/grids' % len(items), items)
    return Result('PASS', 'Only levels and grids on that workset')


def chk_line_styles(ctx, chk):
    cat = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    items = [(s.Name, None, '') for s in cat.SubCategories]
    items.sort(key=lambda x: x[0])
    return Result('INFO', '%d line style(s)' % len(items), items)


def chk_legends(ctx, chk):
    items = []
    for v in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.View)).ToElements():
        try:
            if str(v.ViewType) == 'Legend':
                items.append((v.Name, v.Id, ''))
        except Exception:
            pass
    items.sort(key=lambda x: x[0])
    return Result('INFO', '%d legend(s)' % len(items), items)


def chk_raster_images(ctx, chk):
    items = []
    for t in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.ImageType)).ToElements():
        items.append((safe_name(t), t.Id, ''))
    items.sort(key=lambda x: x[0])
    return Result('INFO', '%d raster image(s)' % len(items), items)


def chk_keynotes(ctx, chk):
    counts = {}
    for t in DB.FilteredElementCollector(doc).WhereElementIsElementType().ToElements():
        try:
            k = param_text(t, 'KEYNOTE_PARAM')
            if k:
                counts[k] = counts.get(k, 0) + 1
        except Exception:
            pass
    items = [(k, None, '%d type(s)' % v) for k, v in sorted(counts.items())]
    return Result('INFO', '%d keynote(s) used on types' % len(items), items)


def chk_worksets(ctx, chk):
    if not ctx.workshared:
        return Result('INFO', '*Not Workshared*')
    counts = {}
    if chk.options.get('ShowCount', 'False') == 'True':
        for el in ctx.instances():
            try:
                k = el.WorksetId.IntegerValue
                counts[k] = counts.get(k, 0) + 1
            except Exception:
                pass
    items = []
    for ws in DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset):
        n = counts.get(ws.Id.IntegerValue)
        items.append((ws.Name, None, ('%d element(s)' % n) if n is not None else ''))
    items.sort(key=lambda x: x[0])
    return Result('INFO', '%d user workset(s)' % len(items), items)


def chk_unused(ctx, chk):
    ctx.progress('Finding purgeable elements...')
    ids = doc.GetUnusedElements(HashSet[DB.ElementId]())
    items = []
    for i in ids:
        el = doc.GetElement(i)
        if el is None:
            continue
        try:
            cat = el.Category.Name if el.Category is not None else type(el).__name__
        except Exception:
            cat = ''
        items.append(('%s | %s' % (cat, safe_name(el)), i, ''))
    items.sort(key=lambda x: x[0])
    if items:
        return Result('WARN', '%d element(s) can be purged' % len(items), items)
    return Result('PASS', 'Nothing to purge')


def chk_largest_families(ctx, chk):
    fams = []
    for f in DB.FilteredElementCollector(doc).OfClass(clr_type(DB.Family)).ToElements():
        try:
            if not f.IsInPlace and f.IsEditable:
                fams.append(f)
        except Exception:
            pass
    tmp = System.IO.Path.GetTempPath()
    sizes = []
    opts = DB.SaveAsOptions()
    opts.OverwriteExistingFile = True
    for n, f in enumerate(fams):
        if ctx.cancelled():
            break
        ctx.progress('Family size %d / %d: %s' % (n + 1, len(fams), f.Name))
        path = os.path.join(tmp, 'mhc_%d.rfa' % n)
        try:
            fd = doc.EditFamily(f)
            try:
                fd.SaveAs(path, opts)
            finally:
                fd.Close(False)
            sizes.append((os.path.getsize(path), f))
        except Exception:
            pass
        finally:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
    sizes.sort(key=lambda x: -x[0])
    items = [(f.Name, f.Id, '%.2f MB' % (s / 1048576.0)) for s, f in sizes[:10]]
    return Result('INFO', 'Ten largest families (of %d measured)' % len(sizes), items)


CHECK_FUNCS = {
    'Custom': run_custom,
    'DesignOptions': chk_design_options,
    'ProjectCoordinates': chk_project_coordinates,
    'WarningCount': chk_warnings,
    'FileName': chk_file_name,
    'RevitVersion': chk_revit_version,
    'FileSize': chk_file_size,
    'ViewsNotOnSheets': chk_views_not_on_sheets,
    'PhaseElements': chk_phase_elements,
    'LinkedCad': chk_linked_cad,
    'PinnedLinks': chk_pinned_links,
    'ViewsWithHiddenElements': chk_views_hidden,
    'WorksetPlacement': chk_workset_placement,
    'LineStyles': chk_line_styles,
    'Legends': chk_legends,
    'RasterImages': chk_raster_images,
    'Keynotes': chk_keynotes,
    'Worksets': chk_worksets,
    'UnusedElements': chk_unused,
    'TenLargestFamilies': chk_largest_families,
}
SLOW_TYPES = ('ViewsWithHiddenElements', 'UnusedElements', 'TenLargestFamilies')


def run_check(ctx, chk):
    fn = CHECK_FUNCS.get(chk.type)
    if fn is None:
        return Result('SKIP', 'Check type "%s" is not supported by this tool' % chk.type)
    return fn(ctx, chk)


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
STATUS_COLORS = {
    'PASS': SD.Color.FromArgb(198, 239, 206),
    'FAIL': SD.Color.FromArgb(255, 199, 206),
    'WARN': SD.Color.FromArgb(255, 235, 156),
    'INFO': SD.Color.FromArgb(221, 235, 247),
    'SKIP': SD.Color.FromArgb(230, 230, 230),
    'ERROR': SD.Color.FromArgb(230, 200, 240),
}

LAST_XML_FILE = os.path.join(os.environ.get('APPDATA', ''), 'pyRevit', 'NRK_ModelHealthCheck_lastxml.txt')


def default_xml():
    here = os.path.dirname(os.path.abspath(__file__))
    for f in sorted(os.listdir(here)):
        if f.lower().endswith('.xml'):
            return os.path.join(here, f)
    try:
        if os.path.exists(LAST_XML_FILE):
            p = open(LAST_XML_FILE).read().strip()
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return ''


def csv_cell(v):
    v = '' if v is None else str(v)
    return '"' + v.replace('"', '""') + '"'


class HealthForm(WF.Form):
    def __init__(self):
        WF.Form.__init__(self)
        self.checks = []
        self.results = {}
        self.cancel = False
        self.select_ids = None
        self.Text = 'Model Health Check'
        self.Width = 1280
        self.Height = 780
        self.StartPosition = WF.FormStartPosition.CenterScreen
        self.Font = SD.Font('Segoe UI', 9.0)

        # --- main split
        self.split = WF.SplitContainer()
        self.split.Dock = WF.DockStyle.Fill
        self.split.SplitterDistance = 560

        self.lv = WF.ListView()
        self.lv.Dock = WF.DockStyle.Fill
        self.lv.View = WF.View.Details
        self.lv.CheckBoxes = True
        self.lv.FullRowSelect = True
        self.lv.GridLines = True
        self.lv.HideSelection = False
        self.lv.MultiSelect = False
        self.lv.Columns.Add('Check', 250)
        self.lv.Columns.Add('Section', 80)
        self.lv.Columns.Add('Status', 70)
        self.lv.Columns.Add('Count', 55)
        self.lv.SelectedIndexChanged += self.on_select
        self.split.Panel1.Controls.Add(self.lv)

        self.dv = WF.ListView()
        self.dv.Dock = WF.DockStyle.Fill
        self.dv.View = WF.View.Details
        self.dv.FullRowSelect = True
        self.dv.GridLines = True
        self.dv.MultiSelect = True
        self.dv.Columns.Add('Item', 430)
        self.dv.Columns.Add('Element Id', 80)
        self.dv.Columns.Add('Info', 300)
        self.split.Panel2.Controls.Add(self.dv)

        self.msg = WF.TextBox()
        self.msg.Dock = WF.DockStyle.Top
        self.msg.Multiline = True
        self.msg.ReadOnly = True
        self.msg.Height = 70
        self.msg.ScrollBars = WF.ScrollBars.Vertical
        self.split.Panel2.Controls.Add(self.msg)

        # --- top bar
        top = WF.Panel()
        top.Dock = WF.DockStyle.Top
        top.Height = 38
        lbl = WF.Label()
        lbl.Text = 'Check set XML:'
        lbl.Left = 8
        lbl.Top = 11
        lbl.AutoSize = True
        top.Controls.Add(lbl)
        self.txt_xml = WF.TextBox()
        self.txt_xml.Left = 105
        self.txt_xml.Top = 8
        self.txt_xml.Width = 900
        self.txt_xml.Anchor = WF.AnchorStyles.Top | WF.AnchorStyles.Left | WF.AnchorStyles.Right
        top.Controls.Add(self.txt_xml)
        btn_browse = WF.Button()
        btn_browse.Text = 'Browse...'
        btn_browse.Left = 1015
        btn_browse.Top = 6
        btn_browse.Anchor = WF.AnchorStyles.Top | WF.AnchorStyles.Right
        btn_browse.Click += self.on_browse
        top.Controls.Add(btn_browse)
        btn_load = WF.Button()
        btn_load.Text = 'Reload'
        btn_load.Left = 1100
        btn_load.Top = 6
        btn_load.Anchor = WF.AnchorStyles.Top | WF.AnchorStyles.Right
        btn_load.Click += self.on_reload
        top.Controls.Add(btn_load)

        # --- bottom bar
        bottom = WF.Panel()
        bottom.Dock = WF.DockStyle.Bottom
        bottom.Height = 70
        self.btn_run = self._button(bottom, 'Run checks', 8, 6, self.on_run)
        self.btn_stop = self._button(bottom, 'Stop', 100, 6, self.on_stop)
        self.btn_stop.Enabled = False
        self._button(bottom, 'All', 190, 6, lambda s, e: self.tick_all(True), 45)
        self._button(bottom, 'None', 240, 6, lambda s, e: self.tick_all(False), 45)
        self._button(bottom, 'Select in Revit && close', 300, 6, self.on_select_revit, 160)
        self._button(bottom, 'Export CSV', 470, 6, self.on_export)
        self._button(bottom, 'Close', 562, 6, lambda s, e: self.Close(), 70)
        self.lbl_sum = WF.Label()
        self.lbl_sum.Left = 8
        self.lbl_sum.Top = 40
        self.lbl_sum.AutoSize = True
        self.lbl_sum.Text = ''
        bottom.Controls.Add(self.lbl_sum)

        self.Controls.Add(self.split)
        self.Controls.Add(top)
        self.Controls.Add(bottom)

        self.Shown += self.on_shown

    def _button(self, parent, text, left, top, handler, width=85):
        b = WF.Button()
        b.Text = text
        b.Left = left
        b.Top = top
        b.Width = width
        b.Height = 28
        b.Click += handler
        parent.Controls.Add(b)
        return b

    # ---- loading
    def on_shown(self, s, e):
        self.txt_xml.Text = default_xml()
        self.load_checks()

    def on_browse(self, s, e):
        dlg = WF.OpenFileDialog()
        dlg.Filter = 'Check set XML (*.xml)|*.xml'
        if dlg.ShowDialog() == WF.DialogResult.OK:
            self.txt_xml.Text = dlg.FileName
            self.load_checks()

    def on_reload(self, s, e):
        self.load_checks()

    def load_checks(self):
        path = self.txt_xml.Text.strip()
        self.lv.Items.Clear()
        self.dv.Items.Clear()
        self.results = {}
        self.lbl_sum.Text = ''
        if not path or not os.path.exists(path):
            self.msg.Text = 'Select the check set XML file first.'
            return
        try:
            self.checks, name = parse_checkset(path)
        except Exception as ex:
            self.msg.Text = 'Could not read XML: %s' % ex
            return
        try:
            open(LAST_XML_FILE, 'w').write(path)
        except Exception:
            pass
        self.Text = 'Model Health Check  -  %s' % name
        groups = {}
        self.lv.BeginUpdate()
        for chk in self.checks:
            if chk.heading not in groups:
                g = WF.ListViewGroup(chk.heading, chk.heading)
                self.lv.Groups.Add(g)
                groups[chk.heading] = g
            label = chk.name + ('  (slow)' if chk.type in SLOW_TYPES else '')
            it = WF.ListViewItem(label)
            it.SubItems.Add(chk.section)
            it.SubItems.Add('Not run')
            it.SubItems.Add('')
            it.Checked = chk.checked
            it.Tag = chk
            it.Group = groups[chk.heading]
            self.lv.Items.Add(it)
        self.lv.EndUpdate()
        self.msg.Text = '%d checks loaded from "%s". Untick any you want to skip, then press Run.' % (len(self.checks), name)

    def tick_all(self, state):
        for it in self.lv.Items:
            it.Checked = state

    # ---- run
    def on_stop(self, s, e):
        self.cancel = True

    def set_progress(self, text):
        self.lbl_sum.Text = text
        WF.Application.DoEvents()

    def on_run(self, s, e):
        if not self.checks:
            return
        self.cancel = False
        self.btn_run.Enabled = False
        self.btn_stop.Enabled = True
        self.results = {}
        t0 = time.time()
        ctx = Ctx(progress=self.set_progress, cancelled=lambda: self.cancel)
        for it in self.lv.Items:
            it.SubItems[2].Text = 'Not run' if not it.Checked else '...'
            it.SubItems[3].Text = ''
            it.BackColor = SD.Color.White
        WF.Application.DoEvents()
        for it in list(self.lv.Items):
            if self.cancel:
                break
            if not it.Checked:
                continue
            chk = it.Tag
            it.SubItems[2].Text = 'Running'
            self.set_progress('Running: ' + chk.name)
            try:
                res = run_check(ctx, chk)
            except Exception as ex:
                res = Result('ERROR', str(ex))
            self.results[id(chk)] = res
            it.SubItems[2].Text = res.status
            it.SubItems[3].Text = str(res.count) if res.count else ''
            it.BackColor = STATUS_COLORS.get(res.status, SD.Color.White)
        self.btn_run.Enabled = True
        self.btn_stop.Enabled = False
        self.update_summary(time.time() - t0)
        self.on_select(None, None)

    def update_summary(self, secs):
        tally = {}
        for r in self.results.values():
            tally[r.status] = tally.get(r.status, 0) + 1
        parts = ['%s %d' % (k, tally[k]) for k in ('FAIL', 'WARN', 'PASS', 'INFO', 'SKIP', 'ERROR') if k in tally]
        self.lbl_sum.Text = '%s   (%.0f s)%s' % ('   |   '.join(parts), secs, '   - stopped' if self.cancel else '')

    # ---- details
    def current_check(self):
        if self.lv.SelectedItems.Count == 0:
            return None, None
        chk = self.lv.SelectedItems[0].Tag
        return chk, self.results.get(id(chk))

    def on_select(self, s, e):
        chk, res = self.current_check()
        self.dv.Items.Clear()
        if chk is None:
            return
        head = chk.description or ''
        if res is None:
            self.msg.Text = head + '\r\n\r\nNot run yet.'
            return
        self.msg.Text = '%s: %s\r\n%s' % (res.status, res.message, head)
        self.dv.BeginUpdate()
        for text, eid, extra in res.items[:MAX_ROWS]:
            li = WF.ListViewItem(text)
            li.SubItems.Add(str(eid_val(eid)) if eid is not None else '')
            li.SubItems.Add(extra or '')
            li.Tag = eid
            self.dv.Items.Add(li)
        if len(res.items) > MAX_ROWS:
            self.dv.Items.Add(WF.ListViewItem('... %d more (see CSV export)' % (len(res.items) - MAX_ROWS)))
        self.dv.EndUpdate()

    def on_select_revit(self, s, e):
        chk, res = self.current_check()
        if res is None:
            return
        picked = [li.Tag for li in self.dv.SelectedItems if li.Tag is not None]
        if not picked:
            picked = [i for _, i, _ in res.items if i is not None]
        if not picked:
            WF.MessageBox.Show('This check has no elements to select.', 'Model Health Check')
            return
        self.select_ids = picked
        self.Close()

    # ---- export
    def on_export(self, s, e):
        if not self.results:
            WF.MessageBox.Show('Run the checks first.', 'Model Health Check')
            return
        dlg = WF.SaveFileDialog()
        dlg.Filter = 'CSV (*.csv)|*.csv'
        dlg.FileName = 'ModelHealthCheck_%s_%s.csv' % (
            re.sub(r'[^\w\-]+', '_', os.path.splitext(doc.Title)[0]), time.strftime('%Y%m%d_%H%M'))
        if dlg.ShowDialog() != WF.DialogResult.OK:
            return
        lines = [','.join(csv_cell(h) for h in
                          ('Priority', 'Section', 'Check', 'Status', 'Message', 'Item', 'ElementId', 'Info'))]
        for it in self.lv.Items:
            chk = it.Tag
            res = self.results.get(id(chk))
            if res is None:
                continue
            if not res.items:
                lines.append(','.join(csv_cell(x) for x in
                                      (chk.heading, chk.section, chk.name, res.status, res.message, '', '', '')))
            for text, eid, extra in res.items:
                lines.append(','.join(csv_cell(x) for x in
                                      (chk.heading, chk.section, chk.name, res.status, res.message,
                                       text, eid_val(eid) if eid is not None else '', extra)))
        System.IO.File.WriteAllText(dlg.FileName, '\r\n'.join(lines), System.Text.UTF8Encoding(True))
        WF.MessageBox.Show('Saved: ' + dlg.FileName, 'Model Health Check')


def main():
    form = HealthForm()
    form.ShowDialog()
    if form.select_ids:
        ids = List[DB.ElementId]()
        for i in form.select_ids:
            ids.Add(i)
        try:
            uidoc.Selection.SetElementIds(ids)
        except Exception:
            pass


if __name__ == '__main__':
    main()
