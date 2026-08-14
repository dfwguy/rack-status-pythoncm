# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""PDF rendering of the Rack Status dataset — the executive view.

Same data as the CSV and JSON renderings (see :mod:`cluster_report`), ordered for someone who
reads the first page and stops: verdict, then what the cluster is, then which nodes need
attention, then the performance charts, and only then the full per-node table.

reportlab is imported lazily so a deployment without it keeps serving CSV and JSON instead of
failing at import. Charts use reportlab's own primitives rather than matplotlib: the four graphs
here are bars and lines, and matplotlib plus numpy would multiply the image size for that.

Landscape A4 throughout, because the node table is wide and splitting its columns across pages
makes it unreadable.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from cluster_report import CLUSTER_FIELDS, _dig

_log = logging.getLogger(__name__)

#: NVIDIA green, used for headers and the pass series.
BRAND = "#76B900"
INK = "#1A1A1A"
MUTED = "#6E6E6E"
FAIL = "#C8102E"
WARN = "#E8A33D"

#: Bars stop being readable long before a 112-rack fleet fits, so the charts show the weakest
#: racks (the actionable end) and say what they are showing. The numbers for every rack remain
#: in the tables and in the CSV/JSON renderings.
MAX_BARS = 28

#: A 2000-row table is ~40 pages. That is acceptable in a formal report and is what keeps the
#: three formats carrying the same information, but it is capped so a pathological fleet cannot
#: produce a document nobody can open.
MAX_NODE_ROWS = 4000


def available() -> bool:
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


def to_pdf(dataset: dict[str, Any]) -> bytes:
    """Render *dataset* as a PDF. Raises ``RuntimeError`` when reportlab is absent."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            KeepTogether,
            LongTable,
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:  # pragma: no cover - exercised only without the dependency
        raise RuntimeError("reportlab is not installed; PDF rendering unavailable") from exc

    import io

    meta = dataset.get("meta") or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"{meta.get('report', 'Rack Status report')} — {meta.get('cluster', '')}",
        author="NVIS Rack Status",
    )

    sheet = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=sheet["Heading1"], fontSize=20, leading=24,
                        textColor=colors.HexColor(INK), spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=sheet["Heading2"], fontSize=12, leading=15,
                        textColor=colors.HexColor(BRAND), spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=sheet["BodyText"], fontSize=8.5, leading=11,
                          alignment=TA_LEFT, textColor=colors.HexColor(INK))
    small = ParagraphStyle("small", parent=body, fontSize=7.5, textColor=colors.HexColor(MUTED))

    story: list[Any] = []

    # ---- Title + verdict ------------------------------------------------------------------
    title = meta.get("cluster") or meta.get("customer") or "Cluster"
    story.append(Paragraph(f"{title} — validation report", h1))
    story.append(Paragraph(
        f"{meta.get('report', '')} &nbsp;·&nbsp; generated {meta.get('generated_at', '')}"
        + (f" &nbsp;·&nbsp; head {meta.get('head')}" if meta.get("head") else ""), small))
    story.append(Spacer(1, 6))
    story.append(_verdict_band(dataset, Table, TableStyle, colors, mm, body))

    # ---- Cluster details -----------------------------------------------------------------
    story.append(Paragraph("Cluster", h2))
    story.append(_cluster_table(dataset, Table, TableStyle, colors, mm, body))

    # ---- Nodes needing attention ----------------------------------------------------------
    failures = [r for r in (dataset.get("nodes") or []) if r.get("overall") == "FAIL"]
    story.append(Paragraph(
        f"Nodes requiring attention ({len(failures)})" if failures
        else "Nodes requiring attention — none", h2))
    if failures:
        story.append(_failure_table(dataset, failures, Table, TableStyle, colors, mm, body))
    else:
        story.append(Paragraph("Every node that ran a test passed it.", body))

    # ---- Charts ---------------------------------------------------------------------------
    charts = _charts(dataset, colors)
    if charts:
        story.append(PageBreak())
        story.append(Paragraph("Performance", h2))
        for caption, drawing in charts:
            story.append(KeepTogether([Paragraph(caption, small), Spacer(1, 2), drawing,
                                       Spacer(1, 8)]))
    else:
        story.append(Paragraph("Performance", h2))
        story.append(Paragraph(
            "No analytics aggregate was available, so no rack-level charts are included. "
            "Run Analytics and regenerate to include them.", body))

    # ---- Full node table ------------------------------------------------------------------
    rows = dataset.get("nodes") or []
    shown = rows[:MAX_NODE_ROWS]
    for caption, table in _node_tables(dataset, shown, LongTable, TableStyle, colors, mm):
        story.append(PageBreak())
        suffix = f" ({len(shown)} of {len(rows)})" if len(shown) != len(rows) else f" ({len(rows)})"
        story.append(Paragraph(caption + suffix, h2))
        story.append(table)
        if len(shown) != len(rows):
            story.append(Paragraph(
                f"Truncated at {MAX_NODE_ROWS} rows; the CSV and JSON carry every node.", small))

    doc.build(buf and story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColorRGB(0.43, 0.43, 0.43)
    canvas.drawRightString(doc.pagesize[0] - 14 * 2.83465, 8 * 2.83465, f"Page {doc.page}")
    canvas.restoreState()


def _verdict_band(dataset, Table, TableStyle, colors, mm, body):
    """Four numbers, big: the answer to "is this cluster healthy" without reading further."""
    s = dataset.get("summary") or {}
    cells = [
        ("Nodes", s.get("nodes_total", 0), INK),
        ("Passed", s.get("nodes_passed", 0), BRAND),
        ("Failed", s.get("nodes_failed", 0), FAIL),
        ("Incomplete", s.get("nodes_incomplete", 0), WARN),
        ("Racks", s.get("racks_total", 0), INK),
    ]
    data = [[str(v) for _, v, _ in cells], [k for k, _, _ in cells]]
    t = Table(data, colWidths=[46 * mm] * len(cells))
    style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 18),
        ("FONT", (0, 1), (-1, 1), "Helvetica", 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor(MUTED)),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("LINEBELOW", (0, 1), (-1, 1), 0.75, colors.HexColor(BRAND)),
    ]
    for i, (_, _, colour) in enumerate(cells):
        style.append(("TEXTCOLOR", (i, 0), (i, 0), colors.HexColor(colour)))
    t.setStyle(TableStyle(style))
    return t


def _cluster_table(dataset, Table, TableStyle, colors, mm, body):
    """The software and hardware stack, two label/value pairs per row to use the width."""
    pairs = [(label, str(_dig(dataset, path))) for label, path in CLUSTER_FIELDS
             if str(_dig(dataset, path)) not in ("", "None")]
    rows = []
    for i in range(0, len(pairs), 2):
        left = pairs[i]
        right = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
        rows.append([left[0], left[1], right[0], right[1]])
    t = Table(rows, colWidths=[38 * mm, 92 * mm, 38 * mm, 92 * mm])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 8),
        ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 8),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor(MUTED)),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor(MUTED)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#F5F7F2")]),
    ]))
    return t


def _failure_table(dataset, failures, Table, TableStyle, colors, mm, body):
    """Which node, where it is, and which test failed — enough to act without another tool."""
    tests = list((dataset.get("summary") or {}).get("tests") or [])
    header = ["Node", "Rack", "Serial", "Failed tests"]
    rows = [header]
    for row in failures[:200]:
        failed = [t for t in tests if str(row.get(f"{t}_status", "")).upper() == "FAIL"]
        rows.append([row.get("node", ""), row.get("rack", ""), row.get("serial", ""),
                     ", ".join(failed) or "—"])
    t = Table(rows, colWidths=[70 * mm, 30 * mm, 30 * mm, 130 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(INK)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("TEXTCOLOR", (3, 1), (3, -1), colors.HexColor(FAIL)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAF2F3")]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


#: The per-node columns, split into groups that each fit the page.
#:
#: One table of everything is what a spreadsheet wants and a page cannot give: 25 columns across
#: 269 mm at 6 pt overprint each other whatever widths you choose, and an overlapping table
#: carries less information than three legible ones. Every column still appears; the node name
#: repeats as the key so the groups can be read together. The single wide table lives in the CSV.
NODE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Nodes — placement and verdict",
     ("node", "rack", "rack_unit", "serial", "ip", "bmc_ip", "gpu_count", "link_layer",
      "data_captured_at", "overall")),
    ("Nodes — firmware and software",
     ("node", "vbios_version", "driver_version", "cuda_runtime", "nic_firmware",
      "ofed_version", "bios_version", "bmc_firmware")),
)


def _node_tables(dataset, rows, LongTable, TableStyle, colors, mm) -> list[tuple[str, Any]]:
    """``(caption, table)`` for each group, plus one for the site's test results."""
    present = [c for c in (dataset.get("node_columns") or []) if c != "gpu_ids"]
    out = []
    for caption, group in NODE_GROUPS:
        columns = [c for c in group if c in present]
        if len(columns) > 1:
            out.append((caption, _node_table(dataset, rows, columns, LongTable, TableStyle,
                                             colors, mm)))
    tests = list((dataset.get("summary") or {}).get("tests") or [])
    result_cols = ["node", "overall"]
    for test in tests:
        result_cols += [f"{test}_status", f"{test}_metric"]
    if tests:
        out.append(("Nodes — test results",
                    _node_table(dataset, rows, result_cols, LongTable, TableStyle, colors, mm)))
    return out


def _node_table(dataset, rows, columns, LongTable, TableStyle, colors, mm):
    """One group of columns for every node. LongTable so it flows with the header repeated."""
    # gpu_ids is four 40-character UUIDs: it would dominate any layout, and it lives in the CSV
    # and JSON where a reader can actually use it.
    header = [c.replace("_", " ").replace("version", "ver").replace("firmware", "fw")
              for c in columns]
    data = [header] + [[_cell(c, r.get(c, "")) for c in columns] for r in rows]

    # Proportional widths, measured from the content. Equal widths look tidy in code and
    # unreadable on the page: 25 columns share the width evenly while a hostname needs twenty
    # characters and gpu_count needs one, so the long values overprint their neighbours.
    weights = []
    for i, name in enumerate(columns):
        longest = max([len(header[i])] + [len(str(row[i])) for row in data[1:]] or [1])
        # The hostname is not capped: it is never truncated, so its column must fit it.
        weights.append(max(4, longest if name == "node" else min(longest, 20)))
    total = float(sum(weights)) or 1.0
    usable = landscape_width() - 28 * mm
    widths = [usable * (w / total) for w in weights]

    t = LongTable(data, colWidths=widths, repeatRows=1)
    style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 6.5),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 6.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(INK)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F7F7")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("GRID", (0, 0), (-1, -1), 0.15, colors.HexColor("#DDDDDD")),
    ]
    if "overall" in columns:
        col = columns.index("overall")
        for i, row in enumerate(rows, start=1):
            verdict = row.get("overall")
            if verdict == "FAIL":
                style.append(("TEXTCOLOR", (col, i), (col, i), colors.HexColor(FAIL)))
            elif verdict == "PASS":
                style.append(("TEXTCOLOR", (col, i), (col, i), colors.HexColor(BRAND)))
    t.setStyle(TableStyle(style))
    return t


def landscape_width() -> float:
    from reportlab.lib.pagesizes import A4

    return A4[1]


def _short(value: Any, limit: int = 22) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cell(column: str, value: Any) -> str:
    """One table cell, shortened where the full string belongs in the CSV instead.

    The carried-forward timestamp is rendered as a date: the report only needs to say "this
    node's data is from the 10th", and the full ISO instant costs a fifth of the table width.
    The hostname is never shortened — it is the key a reader uses to act on a row, and
    ``ink50-dh01-wh1a…`` identifies nothing.
    """
    if column == "data_captured_at" and value:
        return str(value)[:10]
    if column == "node":
        return "" if value is None else str(value)
    return _short(value, 16)


def _num(value: Any) -> float | None:
    """A plottable float, or None. NaN is rejected: reportlab draws it as a broken axis."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def _charts(dataset: dict[str, Any], colors) -> list[tuple[str, Any]]:
    """The four graphs, skipping any whose data the analytics run did not produce."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib.units import mm

    racks = dataset.get("racks") or {}
    out: list[tuple[str, Any]] = []
    width, height = 250 * mm, 62 * mm

    def bar_chart(pairs: list[tuple[str, float]], unit: str):
        d = Drawing(width, height)
        chart = VerticalBarChart()
        chart.x, chart.y, chart.width, chart.height = 22 * mm, 16 * mm, width - 30 * mm, height - 24 * mm
        chart.data = [[v for _, v in pairs]]
        chart.categoryAxis.categoryNames = [k for k, _ in pairs]
        chart.categoryAxis.labels.angle = 90
        chart.categoryAxis.labels.dy = -6
        chart.categoryAxis.labels.fontSize = 5.5
        chart.categoryAxis.labels.boxAnchor = "e"
        chart.valueAxis.valueMin = 0
        chart.valueAxis.labels.fontSize = 6
        chart.bars[0].fillColor = colors.HexColor(BRAND)
        chart.bars[0].strokeColor = None
        chart.barSpacing = 0.5
        d.add(chart)
        d.add(String(2, height - 6, unit, fontSize=6, fillColor=colors.HexColor(MUTED)))
        return d

    # 1. Per-rack NCCL all-reduce, weakest first — the actionable end of the distribution.
    collectives = [(str(r.get("rack") or ""), _num(r.get("all_reduce_gbps")))
                   for r in (racks.get("nccl_collectives") or [])]
    collectives = [(k, v) for k, v in collectives if k and v is not None]
    if collectives:
        collectives.sort(key=lambda kv: kv[1])
        shown = collectives[:MAX_BARS]
        caption = (f"NCCL all-reduce bus bandwidth per rack (GB/s), lowest {len(shown)} of "
                   f"{len(collectives)}") if len(shown) < len(collectives) else \
            "NCCL all-reduce bus bandwidth per rack (GB/s)"
        out.append((caption, bar_chart(shown, "GB/s")))

    # 2. All-reduce against message size — the shape of the curve, not just its peak.
    series = racks.get("nccl_all_reduce_series") or {}
    sizes = series.get("sizes_bytes") or []
    median = series.get("median_bus_bw_gbps") or []
    points = [(s, m) for s, m in zip(sizes, median, strict=False) if _num(m) is not None]
    if len(points) >= 2:
        d = Drawing(width, height)
        line = HorizontalLineChart()
        line.x, line.y, line.width, line.height = 22 * mm, 14 * mm, width - 30 * mm, height - 22 * mm
        line.data = [[_num(m) for _, m in points]]
        line.categoryAxis.categoryNames = [_bytes_label(s) for s, _ in points]
        line.categoryAxis.labels.fontSize = 5.5
        line.valueAxis.valueMin = 0
        line.valueAxis.labels.fontSize = 6
        line.lines[0].strokeColor = colors.HexColor(BRAND)
        line.lines[0].strokeWidth = 1.4
        d.add(line)
        d.add(String(2, height - 6, "GB/s", fontSize=6, fillColor=colors.HexColor(MUTED)))
        out.append(("NCCL all-reduce bus bandwidth against message size (fleet median)", d))

    # 3. Per-rack NeMo throughput.
    nemo = [(str(r.get("rack") or ""), _num(r.get("tflops_per_gpu")))
            for r in (racks.get("nemotron") or [])]
    nemo = [(k, v) for k, v in nemo if k and v is not None]
    if nemo:
        nemo.sort(key=lambda kv: kv[1])
        shown = nemo[:MAX_BARS]
        caption = (f"NeMo training throughput per rack (TFLOP/s per GPU), lowest {len(shown)} of "
                   f"{len(nemo)}") if len(shown) < len(nemo) else \
            "NeMo training throughput per rack (TFLOP/s per GPU)"
        out.append((caption, bar_chart(shown, "TFLOP/s/GPU")))

    # 4. Scaling: does throughput hold as the job grows?
    scaling = [(int(r.get("n_racks") or 0), _num(r.get("mean_tflops_per_gpu")))
               for r in (racks.get("nemotron_scaling") or [])]
    scaling = [(n, v) for n, v in scaling if n and v is not None]
    if len(scaling) >= 2:
        scaling.sort(key=lambda kv: kv[0])
        d = Drawing(width, height)
        line = HorizontalLineChart()
        line.x, line.y, line.width, line.height = 22 * mm, 14 * mm, width - 30 * mm, height - 22 * mm
        line.data = [[v for _, v in scaling]]
        line.categoryAxis.categoryNames = [f"{n} rack{'s' if n != 1 else ''}" for n, _ in scaling]
        line.categoryAxis.labels.fontSize = 6
        line.valueAxis.valueMin = 0
        line.valueAxis.labels.fontSize = 6
        line.lines[0].strokeColor = colors.HexColor(BRAND)
        line.lines[0].strokeWidth = 1.4
        d.add(line)
        d.add(String(2, height - 6, "TFLOP/s/GPU", fontSize=6, fillColor=colors.HexColor(MUTED)))
        out.append(("NeMo throughput as the job scales (mean TFLOP/s per GPU by rack count)", d))

    return out


def _bytes_label(size: Any) -> str:
    value = _num(size)
    if value is None:
        return str(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{value:.0f}{unit}" if value >= 10 or unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return str(size)
