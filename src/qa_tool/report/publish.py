"""Выпуск протоколов прогона в папку прогона: HTML, PDF, Excel по камере и сводные."""

from pathlib import Path

from qa_tool.core.store import RunStore
from qa_tool.report.api_doc import build_api_document
from qa_tool.report.codec_compare import build_codec_compare, publish_codec_compare
from qa_tool.report.html import render_protocol, render_summary, render_template, write_html
from qa_tool.report.image_compare import build_image_compare, publish_image_compare
from qa_tool.report.model import build_summary
from qa_tool.report.pdf import write_pdf
from qa_tool.report.xlsx import write_api_map_xlsx, write_protocol_xlsx, write_summary_xlsx


def publish(store: RunStore) -> list[Path]:
    """Выпустить протоколы всех камер, сводку и, если есть, сравнения картинки и кодеков.

    Returns:
        Пути записанных файлов.
    """
    summary = build_summary(store)
    out = store.run_dir
    written = []
    compare = build_image_compare(summary, out)
    if compare is not None:
        written += publish_image_compare(compare, out)
        summary.image_compare = True
    codecs = build_codec_compare(summary, out)
    if codecs is not None:
        written += publish_codec_compare(codecs, out)
        summary.codec_compare = True
    for p in summary.protocols:
        html = render_protocol(p)
        written += [
            write_html(out / f"protocol_{p.slot}.html", html),
            write_pdf(out / f"protocol_{p.slot}.pdf", html, f"Протокол {p.protocol_id}", out),
            write_protocol_xlsx(out / f"protocol_{p.slot}.xlsx", p),
        ]
        doc = build_api_document(p, out)
        if doc is not None:
            # «Карта API» — рядом с протоколом своей камеры (api-discovery, T18)
            html = render_template("api_map.html", d=doc)
            written += [
                write_html(out / f"api_map_{p.slot}.html", html),
                write_pdf(out / f"api_map_{p.slot}.pdf", html, doc.title, out),
                write_api_map_xlsx(out / f"api_map_{p.slot}.xlsx", doc),
            ]
    html = render_summary(summary)
    written += [
        write_html(out / "summary.html", html),
        write_pdf(out / "summary.pdf", html, f"Сводный отчёт {summary.run_id}"),
        write_summary_xlsx(out / "summary.xlsx", summary),
    ]
    return written
