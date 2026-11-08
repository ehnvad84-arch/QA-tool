"""Выпуск протоколов прогона в папку прогона: HTML, PDF, Excel по камере и сводные."""

from pathlib import Path

from qa_tool.core.store import RunStore
from qa_tool.report.html import render_protocol, render_summary, write_html
from qa_tool.report.image_compare import build_image_compare, publish_image_compare
from qa_tool.report.model import build_summary
from qa_tool.report.pdf import write_pdf
from qa_tool.report.xlsx import write_protocol_xlsx, write_summary_xlsx


def publish(store: RunStore) -> list[Path]:
    """Выпустить протоколы всех камер, сводку и, если есть, сравнение картинки в папку прогона.

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
    for p in summary.protocols:
        html = render_protocol(p)
        written += [
            write_html(out / f"protocol_{p.slot}.html", html),
            write_pdf(out / f"protocol_{p.slot}.pdf", html, f"Протокол {p.protocol_id}", out),
            write_protocol_xlsx(out / f"protocol_{p.slot}.xlsx", p),
        ]
    html = render_summary(summary)
    written += [
        write_html(out / "summary.html", html),
        write_pdf(out / "summary.pdf", html, f"Сводный отчёт {summary.run_id}"),
        write_summary_xlsx(out / "summary.xlsx", summary),
    ]
    return written
