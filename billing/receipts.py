"""Thermal receipt rendering: plain text for the screen, ESC/POS for the printer."""
import io
import socket
from decimal import Decimal

from django.utils import timezone
from django.utils.translation import gettext as _

ESC = b'\x1b'
GS = b'\x1d'
INIT = ESC + b'@'
ALIGN_LEFT = ESC + b'a\x00'
ALIGN_CENTER = ESC + b'a\x01'
BOLD_ON = ESC + b'E\x01'
BOLD_OFF = ESC + b'E\x00'
DOUBLE_ON = GS + b'!\x11'
DOUBLE_OFF = GS + b'!\x00'
CUT = GS + b'V\x42\x00'
FEED = b'\n'


def receipt_lines(payment, printer=None, *, copy_label=''):
    """The receipt as a list of plain-text lines, fitted to the paper width."""
    width = printer.columns if printer else 48
    tenancy = payment.tenancy
    building = tenancy.building
    guest = tenancy.guest
    currency = payment.tenant.currency
    invoice = payment.invoice

    out = []
    add = out.append

    add(_center(building.name.upper(), width))
    if building.address:
        for chunk in _wrap(building.address.replace('\n', ', '), width):
            add(_center(chunk, width))
    if building.contact_phone:
        add(_center(building.contact_phone, width))
    if printer and printer.header_text:
        for chunk in _wrap(printer.header_text, width):
            add(_center(chunk, width))
    add('-' * width)
    add(_center(_('RENT RECEIPT'), width))
    if copy_label:
        add(_center(copy_label, width))
    add('-' * width)

    add(_pair(_('Receipt'), payment.receipt_number, width))
    add(_pair(_('Date'), timezone.localtime(payment.created_at).strftime('%d-%m-%Y %H:%M'), width))
    if invoice:
        add(_pair(_('Invoice'), invoice.number, width))
    add(_pair(_('Guest'), guest.full_name, width))
    add(_pair(_('Mobile'), guest.mobile, width))
    add(_pair(_('Floor'), tenancy.floor.name, width))
    if tenancy.apartment_id:
        add(_pair(_('Apartment'), tenancy.apartment.name, width))
    add(_pair(_('Room'), tenancy.room.number, width))
    if tenancy.bed_id:
        add(_pair(_('Bed'), tenancy.bed.label, width))
    if invoice:
        add(_pair(_('Period'), invoice.period_label(), width))
    add('-' * width)

    if invoice:
        add(_pair(_('Rent'), _money(invoice.rent_amount, currency), width))
        for line in invoice.lines.all():
            add(_pair(line.description[:width // 2], _money(line.amount, currency), width))
        if invoice.late_fee:
            add(_pair(_('Late fee'), _money(invoice.late_fee, currency), width))
        if invoice.discount_amount:
            add(_pair(_('Discount'), '-' + _money(invoice.discount_amount, currency), width))
        add(_pair(_('Invoice total'), _money(invoice.total, currency), width))
    add(_pair(_('AMOUNT PAID'), _money(payment.amount, currency), width))
    balance = invoice.balance if invoice else Decimal('0')
    add(_pair(_('Balance'), _money(balance, currency), width))
    add('-' * width)
    add(_pair(_('Mode'), str(payment.mode or '—'), width))
    if payment.reference:
        add(_pair(_('Reference'), payment.reference, width))
    add(_pair(_('Collected by'), payment.collected_by_name or '—', width))
    add('')
    add(_('Guest signature: ') + '_' * max(width - 20, 8))
    add('')
    footer = (printer.footer_text if printer and printer.footer_text else building.receipt_footer)
    if footer:
        for chunk in _wrap(footer, width):
            add(_center(chunk, width))
    add(_center('ZyDwell', width))
    return out


def deposit_receipt_lines(entry, printer=None, *, copy_label=''):
    """The same thermal layout, for money in or out of a guest's deposit."""
    width = printer.columns if printer else 48
    tenancy = entry.tenancy
    building = tenancy.building
    guest = tenancy.guest
    currency = entry.tenant.currency

    out = []
    add = out.append

    add(_center(building.name.upper(), width))
    if building.address:
        for chunk in _wrap(building.address.replace('\n', ', '), width):
            add(_center(chunk, width))
    if building.contact_phone:
        add(_center(building.contact_phone, width))
    if printer and printer.header_text:
        for chunk in _wrap(printer.header_text, width):
            add(_center(chunk, width))
    add('-' * width)
    add(_center(str(entry.receipt_title()), width))
    if copy_label:
        add(_center(copy_label, width))
    add('-' * width)

    add(_pair(_('Receipt'), entry.receipt_number, width))
    add(_pair(_('Date'), entry.happened_on.strftime('%d-%m-%Y'), width))
    add(_pair(_('Guest'), guest.full_name, width))
    add(_pair(_('Mobile'), guest.mobile, width))
    add(_pair(_('Floor'), tenancy.floor.name, width))
    if tenancy.apartment_id:
        add(_pair(_('Apartment'), tenancy.apartment.name, width))
    add(_pair(_('Room'), tenancy.room.number, width))
    if tenancy.bed_id:
        add(_pair(_('Bed'), tenancy.bed.label, width))
    add('-' * width)

    label = _('AMOUNT RECEIVED') if entry.is_money_in else _('AMOUNT PAID OUT')
    add(_pair(label, _money(entry.amount, currency), width))
    if entry.reason:
        add(_pair(_('Reason'), entry.reason[:width // 2], width))
    add(_pair(_('Deposit agreed'), _money(tenancy.deposit_amount, currency), width))
    add(_pair(_('Deposit now held'), _money(tenancy.deposit_held(), currency), width))
    add('-' * width)
    if entry.mode_id:
        add(_pair(_('Mode'), str(entry.mode), width))
    if entry.reference:
        add(_pair(_('Reference'), entry.reference, width))
    add(_pair(_('Recorded by'), (entry.recorded_by.display_name()
                                 if entry.recorded_by_id else '—'), width))
    add('')
    add(_('Guest signature: ') + '_' * max(width - 20, 8))
    add('')
    add(_center(_('A deposit is held, not rent. It is returned at check-out '
                  'less any agreed deduction.'), width)
        if width >= 48 else '')
    footer = (printer.footer_text if printer and printer.footer_text else building.receipt_footer)
    if footer:
        for chunk in _wrap(footer, width):
            add(_center(chunk, width))
    add(_center('ZyDwell', width))
    return [line for line in out if line != '' or True]


def render_text(payment, printer=None, *, copy_label=''):
    return '\n'.join(receipt_lines(payment, printer, copy_label=copy_label))


def render_escpos(payment, printer=None, *, copy_label='', lines=None):
    """ESC/POS byte stream for a 58 mm or 80 mm thermal printer."""
    width = printer.columns if printer else 48
    lines = lines if lines is not None else receipt_lines(payment, printer,
                                                          copy_label=copy_label)
    buf = bytearray()
    buf += INIT
    for index, line in enumerate(lines):
        if index == 0:
            buf += ALIGN_CENTER + DOUBLE_ON + BOLD_ON
            buf += line.strip().encode('utf-8', 'replace') + FEED
            buf += DOUBLE_OFF + BOLD_OFF + ALIGN_LEFT
            continue
        emphasise = (line.strip().startswith(str(_('AMOUNT PAID')))
                     or line.strip().startswith(str(_('AMOUNT RECEIVED')))
                     or line.strip().startswith(str(_('AMOUNT PAID OUT'))))
        if emphasise:
            buf += BOLD_ON
        buf += line.encode('utf-8', 'replace') + FEED
        if emphasise:
            buf += BOLD_OFF
    buf += FEED * 3
    buf += CUT
    return bytes(buf)


def send_to_printer(printer, payload, timeout=5):
    """Push bytes at the printer. Returns ``(ok, message)``; never raises."""
    if printer is None:
        return False, _('No printer configured.')
    if printer.connection == printer.CONNECTION_NETWORK:
        address = printer.address or ''
        if ':' not in address:
            return False, _('Network printer address must be IP:port.')
        host, _sep, port = address.partition(':')
        try:
            with socket.create_connection((host, int(port)), timeout=timeout) as sock:
                sock.sendall(payload)
            return True, _('Sent %(n)d bytes to %(addr)s') % {'n': len(payload), 'addr': address}
        except OSError as exc:
            return False, _('Printer unreachable: %(err)s') % {'err': exc}
    if printer.connection == printer.CONNECTION_USB:
        try:
            with open(printer.address, 'wb') as device:
                device.write(payload)
            return True, _('Sent to %(addr)s') % {'addr': printer.address}
        except OSError as exc:
            return False, _('USB device error: %(err)s') % {'err': exc}
    return False, _('Bluetooth printing is handled by the local print agent.')


# --- layout helpers ----------------------------------------------------
def _money(value, currency):
    return f'{currency} {Decimal(value or 0):,.2f}'


def _center(text, width):
    text = text[:width]
    pad = max((width - len(text)) // 2, 0)
    return ' ' * pad + text


def _pair(label, value, width):
    label, value = str(label), str(value)
    if len(label) + len(value) + 1 > width:
        value = value[: max(width - len(label) - 1, 4)]
    return label + ' ' * max(width - len(label) - len(value), 1) + value


def _wrap(text, width):
    words, line, out = str(text).split(), '', []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f'{line} {word}'.strip()
    if line:
        out.append(line)
    return out or ['']
