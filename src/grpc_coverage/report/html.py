"""`index.html`: one static file with CSS, JS and data inline. No CDN, no frontend build; opens from disk.

Everything is rendered here with `html.escape`; the inline script only expands rows, sorts and filters.
"""

import json
from html import escape
from importlib import resources
from string import Template
from typing import Any

from grpc_coverage.report.compute import pct


def _level(value: dict[str, Any]) -> str:
    percent = value["percent"]
    if percent is None:
        return "na"
    return "good" if percent >= 80 else "warn" if percent >= 50 else "bad"


def _bar(value: dict[str, Any], *, big: bool = False) -> str:
    percent = value["percent"]
    width = 0 if percent is None else percent
    sort = -1 if percent is None else percent
    counts = f"{value['covered']}/{value['total']}"
    return (
        f'<span class="cov {_level(value)}{" big" if big else ""}" data-value="{sort}">'
        f'<span class="bar" aria-hidden="true"><span style="width:{width:.1f}%"></span></span>'
        f'<span class="pct">{escape(pct(value))}</span> <span class="counts">{counts}</span></span>'
    )


def _codes(codes: dict[str, int]) -> str:
    if not codes:
        return '<span class="muted">—</span>'
    return " ".join(
        f'<span class="code {"ok" if code == "OK" else "err"}">{escape(code)}&nbsp;×{count}</span>'
        for code, count in codes.items()
    )


def _mark(covered: bool) -> str:
    return '<span class="yes" title="covered">✓</span>' if covered else '<span class="no" title="not covered">✗</span>'


def _schema_label(schema: dict[str, Any]) -> str:
    source = schema["source"]
    if schema["error"]:
        return f'<span class="badge bad">error: {escape(schema["error"]["code"])}</span>'
    label = {"reflection-v1": "reflection v1", "reflection-v1alpha": "reflection v1alpha", "proto": ".proto"}.get(
        source, source
    )
    return f'<span class="badge">{escape(label)}</span>'


def _toggle(label: str, controls: str) -> str:
    return (
        f'<button type="button" class="toggle" aria-expanded="false" aria-controls="{escape(controls)}">'
        f'<span class="chev" aria-hidden="true">▸</span>{label}</button>'
    )


def _sort_header(columns: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<th scope="col"><button type="button" class="sort" data-type="{kind}">{escape(name)}'
        f'<span class="arrow" aria-hidden="true"></span></button></th>'
        for name, kind in columns
    )
    return f"<thead><tr>{cells}</tr></thead>"


def _method_card(method: dict[str, Any]) -> str:
    parts = []
    if method["fields"]:
        items = []
        for field in method["fields"]:
            depth = field["path"].count(".")
            name = field["path"].rsplit(".", 1)[-1]
            note = ""
            if field["implicit_default"] and not field["covered"]:
                note = (
                    ' <span class="badge warn" title="proto3 field without `optional`: a default value (0, &quot;&quot;'
                    ', false) is never sent, so it can not be seen">proto3 default?</span>'
                )
            items.append(
                f'<li style="--depth:{depth}" class="{"c" if field["covered"] else "u"}">{_mark(field["covered"])} '
                f'<code>{escape(name)}</code> <span class="muted">{escape(field["type"])}</span>{note}</li>'
            )
        parts.append(
            f"<div><h4>Request fields <code>{escape(method['request_type'])}</code></h4>"
            f'<ul class="tree">{"".join(items)}</ul></div>'
        )
    else:
        parts.append(
            f"<div><h4>Request fields <code>{escape(method['request_type'])}</code></h4>"
            f'<p class="muted">no fields</p></div>'
        )
    side = []
    if method["enum_values"]:
        side.append(
            "<h4>Enum values</h4><ul>"
            + "".join(
                f"<li>{_mark(e['covered'])} <code>{escape(e['value'])}</code></li>" for e in method["enum_values"]
            )
            + "</ul>"
        )
    if method["oneofs"]:
        side.append(
            "<h4>Oneof branches</h4><ul>"
            + "".join(f"<li>{_mark(o['covered'])} <code>{escape(o['branch'])}</code></li>" for o in method["oneofs"])
            + "</ul>"
        )
    side.append(
        "<h4>Expected codes</h4><ul>"
        + "".join(f"<li>{_mark(c['covered'])} <code>{escape(c['code'])}</code></li>" for c in method["expected_codes"])
        + "</ul>"
    )
    side.append(f"<h4>Received codes</h4><p>{_codes(method['codes'])}</p>")
    if method["unexpected_codes"]:
        side.append(
            '<p class="muted">Not in <code>expected-codes</code> (shown, not counted): '
            + ", ".join(f"<code>{escape(code)}</code>" for code in method["unexpected_codes"])
            + "</p>"
        )
    parts.append(f"<div>{''.join(side)}</div>")
    return (
        f'<div class="card"><h3><code>{escape(method["full_name"])}</code> {_bar(method["contracts"])}</h3>'
        f'<div class="cols">{"".join(parts)}</div></div>'
    )


def _methods_table(server: str, service: dict[str, Any]) -> str:
    rows = []
    for method in service["method_list"]:
        detail = f"{server}{method['full_name']}"  # account/account.v1.Account/Create: a link, index.html#...
        flags = ""
        if not method["covered"]:
            flags = ' <span class="badge bad">not called</span>'
        elif method["only_ok"]:
            flags = ' <span class="badge warn" title="no negative checks">only OK</span>'
        uncovered = "" if method["covered"] else " uncovered"
        rows.append(
            f'<tr class="row{uncovered}" data-covered="{int(method["covered"])}">'
            f'<td data-value="{escape(method["name"])}">{_toggle(escape(method["name"]), detail)}{flags}</td>'
            f'<td data-value="{method["kind"]}"><span class="kind">{method["kind"].replace("_", " → ")}</span></td>'
            f'<td data-value="{method["calls"]}" class="num">{method["calls"]}</td>'
            f"<td>{_codes(method['codes'])}</td>"
            f"<td>{_bar(method['contracts'])}</td></tr>"
            f'<tr class="detail" id="{escape(detail)}" hidden><td colspan="5">{_method_card(method)}</td></tr>'
        )
    head = _sort_header(
        [("Method", "text"), ("Kind", "text"), ("Calls", "num"), ("Codes", "none"), ("Contract", "num")]
    )
    return f'<table class="methods">{head}<tbody>{"".join(rows)}</tbody></table>'


def _services_table(server: dict[str, Any]) -> str:
    if server["schema"]["error"]:
        error = server["schema"]["error"]
        return (
            f'<div class="error"><strong>Schema error {escape(error["code"])}</strong>: {escape(error["message"])}'
            "</div>"
        )
    if not server["services"]:
        return '<p class="muted">No proto services in the schema (all excluded?).</p>'
    rows = []
    for service in server["services"]:
        detail = f"{server['name']}/{service['name']}"
        full = service["methods"]["covered"] == service["methods"]["total"]
        rows.append(
            f'<tr class="row{"" if full else " uncovered"}" data-covered="{int(full)}">'
            f'<td data-value="{escape(service["name"])}">{_toggle(escape(service["name"]), detail)}</td>'
            f"<td>{_bar(service['methods'])}</td><td>{_bar(service['contracts'])}</td>"
            f'<td data-value="{service["calls"]}" class="num">{service["calls"]}</td></tr>'
            f'<tr class="detail" id="{escape(detail)}" hidden><td colspan="4">'
            f"{_methods_table(server['name'], service)}</td></tr>"
        )
    head = _sort_header([("Proto service", "text"), ("Methods", "num"), ("Contracts", "num"), ("Calls", "num")])
    return f'<table class="services">{head}<tbody>{"".join(rows)}</tbody></table>'


def _servers_table(report: dict[str, Any]) -> str:
    rows = []
    for server in report["servers"]:
        detail = f"server-{server['name']}"
        full = server["methods"]["covered"] == server["methods"]["total"] and not server["schema"]["error"]
        note = "" if server["called"] else ' <span class="badge bad">not called</span>'
        rows.append(
            f'<tr class="row{"" if full else " uncovered"}" data-covered="{int(full)}">'
            f'<td data-value="{escape(server["name"])}">{_toggle(escape(server["name"]), detail)}{note}</td>'
            f'<td data-value="{escape(server["target"] or "")}"><code>{escape(server["target"] or "—")}</code></td>'
            f'<td data-value="{escape(server["schema"]["source"])}">{_schema_label(server["schema"])}</td>'
            f"<td>{_bar(server['methods'])}</td><td>{_bar(server['contracts'])}</td>"
            f'<td data-value="{server["calls"]}" class="num">{server["calls"]}</td></tr>'
            f'<tr class="detail" id="{escape(detail)}" hidden><td colspan="6">{_services_table(server)}</td></tr>'
        )
    head = _sort_header(
        [
            ("Server", "text"),
            ("Target", "text"),
            ("Schema", "text"),
            ("Methods", "num"),
            ("Contracts", "num"),
            ("Calls", "num"),
        ]
    )
    return f'<table class="servers">{head}<tbody>{"".join(rows)}</tbody></table>'


def _list(title: str, items: list[str], empty: str = "none") -> str:
    body = "".join(f"<li>{item}</li>" for item in items) if items else f'<li class="muted">{empty}</li>'
    return (
        f'<section class="list"><h3>{escape(title)} <span class="muted">({len(items)})</span></h3><ul>{body}</ul>'
        "</section>"
    )


def _lists(report: dict[str, Any]) -> str:
    servers = report["servers"]
    uncalled_servers = [f"<code>{escape(s['name'])}</code>" for s in servers if not s["called"]]
    uncalled_methods = [
        f"<code>{escape(s['name'])}</code> <code>{escape(m['full_name'])}</code>"
        for s in servers
        for service in s["services"]
        for m in service["method_list"]
        if not m["covered"]
    ]
    out_of_schema = [
        f"<code>{escape(s['name'])}</code> <code>{escape(o['method'])}</code> × {o['calls']} {_codes(o['codes'])}"
        for s in servers
        for o in s["out_of_schema"]
    ]
    out_of_schema += [
        f"<code>{escape(u['name'])}</code>: {u['calls']} call(s) to a server that is not declared in "
        f"<code>[tool.grpc-coverage]</code>"
        for u in report["unknown_servers"]
    ]
    excluded = [
        f"<code>{escape(s['name'])}</code> <code>{escape(name)}</code>"
        for s in servers
        for name in s["excluded_services"]
    ]
    errors = [
        f"<code>{escape(s['name'])}</code> <strong>{escape(s['schema']['error']['code'])}</strong>: "
        f"{escape(s['schema']['error']['message'])}"
        for s in servers
        if s["schema"]["error"]
    ]
    return "".join(
        [
            _list("Servers not called", uncalled_servers),
            _list("Methods not called", uncalled_methods),
            _list("Calls outside the schema", out_of_schema),
            _list("Excluded proto services", excluded),
            _list("Schema errors", errors),
            *([_list("Warnings", [escape(w) for w in report["warnings"]])] if report["warnings"] else []),
        ]
    )


_PAGE = Template((resources.files("grpc_coverage.report") / "page.html").read_text(encoding="utf-8"))


def render(report: dict[str, Any]) -> str:
    failures = ""
    if report["failures"]:
        failures = (
            '<div class="failures"><strong>Failed checks</strong><ul>'
            + "".join(f"<li>{escape(failure)}</li>" for failure in report["failures"])
            + "</ul></div>"
        )
    data = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    return _PAGE.substitute(
        title=escape(report["title"]),
        generated_at=escape(report["generated_at"]),
        calls=report["calls"],
        result_files=report["result_files"],
        version=escape(report["grpc_coverage"]),
        methods_bar=_bar(report["totals"]["methods"], big=True),
        contracts_bar=_bar(report["totals"]["contracts"], big=True),
        failures=failures,
        servers=_servers_table(report),
        lists=_lists(report),
        data=data,
    )
