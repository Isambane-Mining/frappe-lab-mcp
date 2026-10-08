"""Runs INSIDE the bench virtualenv (bench/env/bin/python), cwd = bench/sites.

Reads one JSON request on stdin, connects to the site, performs the action,
commits (or rolls back), and prints one marker-prefixed JSON line with the
result. Deliberately self-contained: it must not import frappe_lab_mcp.

Unlike `bench console`, this commits for real.
"""

import ast
import contextlib
import io
import json
import sys
import traceback

MARKER = "__LABMCP_RESULT__:"


def _emit(payload):
    sys.__stdout__.write(MARKER + json.dumps(payload, default=str) + "\n")
    sys.__stdout__.flush()


def _module_app(frappe, module):
    if not module:
        return None
    return frappe.local.module_app.get(frappe.scrub(module))


def _exec_code(frappe, code, namespace):
    """Exec code; if the last statement is an expression, return its repr (REPL-like)."""
    tree = ast.parse(code, mode="exec")
    last_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)
    exec(compile(tree, "<run_python>", "exec"), namespace)
    if last_expr is not None:
        value = eval(compile(last_expr, "<run_python>", "eval"), namespace)
        if value is not None:
            return value
    return namespace.get("result")


def action_exec(frappe, req):
    ns = {"frappe": frappe, "__name__": "__labmcp__"}
    value = _exec_code(frappe, req["code"], ns)
    try:
        json.dumps(value)
        return {"value": value}
    except (TypeError, ValueError):
        return {"value": repr(value)}


def action_sql(frappe, req):
    max_rows = int(req.get("max_rows") or 200)
    rows = frappe.db.sql(req["query"], as_dict=True)
    if not isinstance(rows, (list, tuple)):
        return {"rows": [], "rowcount": 0}
    return {"rows": list(rows[:max_rows]), "rowcount": len(rows), "truncated": len(rows) > max_rows}


FIELD_KEYS = (
    "label", "fieldtype", "options", "reqd", "unique", "read_only", "hidden", "default",
    "depends_on", "mandatory_depends_on", "read_only_depends_on", "fetch_from", "in_list_view",
    "in_standard_filter", "set_only_once", "allow_on_submit", "permlevel", "is_virtual",
    "no_copy", "length", "precision", "insert_after",
)


def action_meta(frappe, req):
    doctype = req["doctype"]
    if not frappe.db.exists("DocType", doctype):
        matches = frappe.get_all("DocType", filters={"name": ["like", f"%{doctype}%"]}, pluck="name", limit=15)
        raise ValueError(f"DocType '{doctype}' not found. Similar: {matches}")
    meta = frappe.get_meta(doctype)
    fields = []
    for df in meta.fields:
        f = {"fieldname": df.fieldname}
        for k in FIELD_KEYS:
            v = df.get(k)
            if v not in (None, "", 0):
                f[k] = v
        if df.get("is_custom_field"):
            f["custom"] = 1
        fields.append(f)
    out = {
        "name": meta.name,
        "module": meta.module,
        "app": _module_app(frappe, meta.module),
        "custom": meta.custom,
        "istable": meta.istable,
        "issingle": meta.issingle,
        "is_submittable": meta.is_submittable,
        "is_tree": meta.is_tree,
        "autoname": meta.autoname,
        "naming_rule": meta.get("naming_rule"),
        "title_field": meta.title_field,
        "track_changes": meta.track_changes,
        "fields": fields,
        "permissions": [
            {k: p.get(k) for k in ("role", "permlevel", "read", "write", "create", "delete", "submit",
                                   "cancel", "amend", "report", "export", "if_owner") if p.get(k)}
            for p in meta.permissions
        ],
        "links_from": [{"link_doctype": l.link_doctype, "link_fieldname": l.link_fieldname}
                       for l in (meta.links or [])],
        "property_setters": frappe.get_all(
            "Property Setter", filters={"doc_type": doctype},
            fields=["field_name", "property", "value"], limit=100),
    }
    if req.get("include_controller", True):
        import os
        app = out["app"]
        if app and not meta.custom:
            from frappe.modules import get_doc_path
            path = get_doc_path(meta.module, "DocType", doctype)
            out["files_dir"] = os.path.relpath(path, frappe.get_app_path(app, "..", ".."))
    return out


def action_find_doctypes(frappe, req):
    filters = {}
    if req.get("query"):
        filters["name"] = ["like", f"%{req['query']}%"]
    if req.get("module"):
        filters["module"] = req["module"]
    rows = frappe.get_all("DocType", filters=filters,
                          fields=["name", "module", "custom", "istable", "issingle"],
                          order_by="name", limit=int(req.get("limit") or 100))
    if req.get("app"):
        mods = set(frappe.get_module_list(req["app"]) or [])
        rows = [r for r in rows if frappe.scrub(r.module) in {frappe.scrub(m) for m in mods}]
    for r in rows:
        r["app"] = _module_app(frappe, r.module)
    return {"doctypes": rows}


def action_save_doc(frappe, req):
    doctype = req["doctype"]
    values = dict(req.get("values") or {})
    name = req.get("name")
    writable = set(req.get("writable_apps") or [])

    created = not (name and frappe.db.exists(doctype, name))
    if created:
        values["doctype"] = doctype
        doc = frappe.get_doc(values)
        apps = set()
    else:
        doc = frappe.get_doc(doctype, name)
        apps = {_module_app(frappe, doc.get("module"))}
        doc.update(values)

    # Docs with a module (DocType, Workspace, Report, Print Format, ...) are exported to that
    # module's app folder in developer mode, so the module must belong to a writable app.
    if doc.meta.has_field("module"):
        apps.add(_module_app(frappe, doc.get("module")))
        for a in apps - {None}:
            if a not in writable:
                raise PermissionError(
                    f"{doctype} '{name or doc.name}' belongs to app '{a}', which is not writable "
                    f"on its current branch. Writable apps: {sorted(writable)}")

    if created:
        doc.insert(set_name=name) if name else doc.insert()
    else:
        doc.save()
    return {"doctype": doctype, "name": doc.name, "created": created,
            "modified": doc.modified, "module": doc.get("module")}


ACTIONS = {
    "exec": action_exec,
    "sql": action_sql,
    "meta": action_meta,
    "find_doctypes": action_find_doctypes,
    "save_doc": action_save_doc,
}


def main():
    req = json.loads(sys.stdin.read())
    captured = io.StringIO()
    import frappe

    try:
        frappe.init(site=req["site"], sites_path=".")
        frappe.connect()
        frappe.set_user(req.get("as_user") or "Administrator")
    except Exception:
        _emit({"ok": False, "error": "connect failed", "traceback": traceback.format_exc(limit=5)})
        return

    ok = True
    result = None
    error = None
    tb = None
    try:
        with contextlib.redirect_stdout(captured):
            result = ACTIONS[req["action"]](frappe, req)
        if req.get("dry_run"):
            frappe.db.rollback()
        else:
            frappe.db.commit()
    except Exception as e:
        ok = False
        error = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc(limit=12)
        frappe.db.rollback()
    messages = _messages(frappe)
    try:
        frappe.destroy()
    except Exception:
        pass

    _emit({"ok": ok, "result": result, "error": error, "traceback": tb,
           "stdout": captured.getvalue(), "messages": messages})


def _messages(frappe):
    try:
        return [m if isinstance(m, str) else m.get("message") for m in (frappe.local.message_log or [])]
    except Exception:
        return []


if __name__ == "__main__":
    main()
