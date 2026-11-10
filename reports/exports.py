"""Excel and PDF export helpers shared by every report."""
import io

from django.http import HttpResponse
from django.utils import timezone


def excel_response(filename, sheets):
    """``sheets`` is a list of ``(title, headers, rows)``."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    book.remove(book.active)
    head_fill = PatternFill('solid', fgColor='123447')
    head_font = Font(color='FFFFFF', bold=True, size=10)

    for title, headers, rows in sheets:
        sheet = book.create_sheet(str(title)[:31])
        sheet.append(list(headers))
        for cell in sheet[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(vertical='center')
        for row in rows:
            sheet.append(list(row))
        widths = [len(str(h)) + 2 for h in headers]
        for row in rows:
            for index, value in enumerate(row):
                if index < len(widths):
                    widths[index] = max(widths[index], min(len(str(value)) + 2, 48))
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.freeze_panes = 'A2'

    buffer = io.BytesIO()
    book.save(buffer)
    response = HttpResponse(
        buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    stamp = timezone.localdate().strftime('%Y%m%d')
    response['Content-Disposition'] = f'attachment; filename="{filename}-{stamp}.xlsx"'
    return response


def pdf_table_response(filename, title, subtitle, headers, rows, landscape_mode=True):
    """A simple branded table PDF for rent roll / collection / expense reports."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    pagesize = landscape(A4) if landscape_mode else A4
    doc = SimpleDocTemplate(buffer, pagesize=pagesize, title=title,
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm)
    styles = getSampleStyleSheet()
    heading = ParagraphStyle('zy-h', parent=styles['Title'], fontSize=16, spaceAfter=2,
                             textColor=colors.HexColor('#123447'), alignment=0)
    sub = ParagraphStyle('zy-s', parent=styles['Normal'], fontSize=9,
                         textColor=colors.HexColor('#5d6b78'))
    cell = ParagraphStyle('zy-c', parent=styles['Normal'], fontSize=8, leading=10)

    story = [Paragraph(title, heading), Paragraph(subtitle, sub), Spacer(1, 8)]
    data = [[Paragraph(f'<b>{h}</b>', cell) for h in headers]]
    for row in rows:
        data.append([Paragraph(str(value), cell) for value in row])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#123447')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#dfe5ea')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f9fa')]),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f'Generated {timezone.localtime():%d %b %Y %H:%M} · ZyDwell', sub))
    doc.build(story)

    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    stamp = timezone.localdate().strftime('%Y%m%d')
    response['Content-Disposition'] = f'attachment; filename="{filename}-{stamp}.pdf"'
    return response
