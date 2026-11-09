"""Generate clean, publication-grade PDF reports for daily, monthly, and yearly transactions."""
from __future__ import annotations

import html
from datetime import datetime

import config
from playwright.async_api import async_playwright

H = html.escape


def build_report_html(business: dict, summary: dict, payments: list[dict], period_title: str) -> str:
    biz_name = H(business.get("name") or "Business")
    curr = H(business.get("currency") or config.CURRENCY or "ETB")
    now_str = datetime.now().strftime("%b %d, %Y %I:%M %p")

    m = lambda val: f"{val:,.2f} {curr}" if val is not None else f"0.00 {curr}"

    # Build Waiter Rows
    waiter_rows = ""
    for w in summary.get("by_waiter", []):
        waiter_rows += f"""
        <tr>
            <td><strong>{H(w['name'])}</strong></td>
            <td style="text-align:center;">{w['count']}</td>
            <td style="text-align:right;"><strong>{m(w['total'])}</strong></td>
            <td style="text-align:right; color:#2e9e5b;">{m(w['verified'])}</td>
        </tr>
        """
    if not waiter_rows:
        waiter_rows = "<tr><td colspan='4' style='text-align:center; color:#888;'>No sales in this period</td></tr>"

    # Build Bank Rows
    bank_rows = ""
    for b in summary.get("by_bank", []):
        bank_rows += f"""
        <tr>
            <td><strong>{H(b['bank'])}</strong></td>
            <td style="text-align:center;">{b['count']}</td>
            <td style="text-align:right;"><strong>{m(b['total'])}</strong></td>
            <td style="text-align:right; color:#2e9e5b;">{m(b['verified'])}</td>
        </tr>
        """
    if not bank_rows:
        bank_rows = "<tr><td colspan='4' style='text-align:center; color:#888;'>No payment breakdown</td></tr>"

    # Build Top Customers / Payers Rows
    customer_rows = ""
    for c in summary.get("by_customer", [])[:10]:
        customer_rows += f"""
        <tr>
            <td>{H(c['name'])}</td>
            <td style="text-align:center;">{c['count']}</td>
            <td style="text-align:right;"><strong>{m(c['total'])}</strong></td>
        </tr>
        """
    if not customer_rows:
        customer_rows = "<tr><td colspan='3' style='text-align:center; color:#888;'>No customer names captured</td></tr>"

    # Build Payment Ledger Rows
    pay_rows = ""
    for p in payments:
        status_color = "#2e9e5b" if p["status"] == "verified" else ("#d64545" if p["status"] == "rejected" else "#d9911a")
        dup_tag = f"<span class='badge' style='background:#d64545;'>DUP #{p['duplicate_of']}</span> " if p.get("duplicate_of") else ""
        flags = H("; ".join(p.get("flags") or [])) if isinstance(p.get("flags"), list) else H(p.get("flags") or "")
        flag_div = f"<div style='color:#d64545; font-size:11px; margin-top:2px;'>🚩 {flags}</div>" if flags else ""
        bank_note = f"<div style='color:#555; font-size:11px;'>{H(p.get('bank_check_msg') or '')}</div>" if p.get("bank_check_msg") else ""

        pay_rows += f"""
        <tr>
            <td style="font-weight:600; color:#555;">#{p['id']}</td>
            <td style="white-space:nowrap;">{H(p['day'])} {H(p.get('time') or '')}</td>
            <td><strong>{H(p.get('waiter') or p.get('waiter_name') or '')}</strong></td>
            <td>{H(p.get('payer') or '—')}</td>
            <td><strong>{H(p.get('bank') or '—')}</strong></td>
            <td><code style="background:#f4f4f4; padding:2px 4px; border-radius:4px;">{H(p.get('tx_ref') or '—')}</code>{flag_div}{bank_note}</td>
            <td style="text-align:right; font-weight:700;">{m(p.get('amount'))}</td>
            <td style="text-align:center;">
                {dup_tag}<span class='badge' style='background:{status_color};'>{H(p['status'].upper())}</span>
            </td>
        </tr>
        """
    if not pay_rows:
        pay_rows = "<tr><td colspan='8' style='text-align:center; color:#888; padding:20px;'>No transactions found for this period.</td></tr>"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{biz_name} — {H(period_title)}</title>
<style>
    @page {{
        size: A4;
        margin: 12mm 12mm 12mm 12mm;
    }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        color: #222;
        background: #fff;
        margin: 0;
        padding: 0;
        font-size: 13px;
        line-height: 1.4;
    }}
    .header {{
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        border-bottom: 2px solid #2481cc;
        padding-bottom: 12px;
        margin-bottom: 16px;
    }}
    .title-area h1 {{
        margin: 0 0 4px 0;
        font-size: 22px;
        color: #111;
    }}
    .title-area h2 {{
        margin: 0;
        font-size: 14px;
        font-weight: 500;
        color: #555;
    }}
    .meta-area {{
        text-align: right;
        font-size: 12px;
        color: #666;
    }}
    .badge {{
        display: inline-block;
        padding: 2px 7px;
        border-radius: 4px;
        color: #fff;
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
    }}
    .kpi-grid {{
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 10px;
        margin-bottom: 18px;
    }}
    .kpi-card {{
        background: #f8f9fa;
        border: 1px solid #e9ecef;
        border-radius: 8px;
        padding: 10px;
    }}
    .kpi-val {{
        font-size: 18px;
        font-weight: 700;
        color: #111;
        margin-top: 2px;
    }}
    .kpi-lbl {{
        font-size: 11px;
        text-transform: uppercase;
        color: #666;
        font-weight: 600;
        letter-spacing: .02em;
    }}
    .section-title {{
        font-size: 14px;
        font-weight: 700;
        color: #222;
        margin: 16px 0 8px 0;
        border-left: 4px solid #2481cc;
        padding-left: 8px;
    }}
    .two-cols {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 16px;
        margin-bottom: 16px;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
        margin-bottom: 12px;
        page-break-inside: auto;
    }}
    tr {{
        page-break-inside: avoid;
        page-break-after: auto;
    }}
    thead {{
        display: table-header-group;
    }}
    th {{
        background: #f1f3f5;
        color: #495057;
        font-weight: 600;
        padding: 7px 8px;
        border-bottom: 1px solid #ced4da;
        text-align: left;
    }}
    td {{
        padding: 6px 8px;
        border-bottom: 1px solid #e9ecef;
        vertical-align: top;
    }}
    .footer {{
        margin-top: 24px;
        border-top: 1px solid #dee2e6;
        padding-top: 8px;
        display: flex;
        justify-content: space-between;
        font-size: 11px;
        color: #888;
    }}
</style>
</head>
<body>

<div class="header">
    <div class="title-area">
        <h1>{biz_name}</h1>
        <h2>{H(period_title)}</h2>
    </div>
    <div class="meta-area">
        <div><strong>Currency:</strong> {curr}</div>
        <div><strong>Generated:</strong> {now_str}</div>
        <div><strong>Shop Code:</strong> {H(business.get('code') or '')}</div>
    </div>
</div>

<div class="kpi-grid">
    <div class="kpi-card">
        <div class="kpi-lbl">Total Sales Claimed</div>
        <div class="kpi-val">{m(summary['claimed']['total'])}</div>
        <div style="font-size:11px; color:#555;">{summary['claimed']['count']} transactions</div>
    </div>
    <div class="kpi-card" style="border-left:3px solid #2e9e5b;">
        <div class="kpi-lbl" style="color:#2e9e5b;">Total Verified Real</div>
        <div class="kpi-val" style="color:#2e9e5b;">{m(summary['verified']['total'])}</div>
        <div style="font-size:11px; color:#555;">{summary['verified']['count']} verified</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-lbl">Pending Review</div>
        <div class="kpi-val" style="color:#d9911a;">{m(summary['pending']['total'])}</div>
        <div style="font-size:11px; color:#555;">{summary['pending']['count']} pending</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-lbl">Problems / Flagged</div>
        <div class="kpi-val" style="color:#d64545;">{summary['problems']['count']}</div>
        <div style="font-size:11px; color:#d64545;">{m(summary['problems']['total'])}</div>
    </div>
</div>

<div class="two-cols">
    <div>
        <div class="section-title">Sales by Waiter / Cashier</div>
        <table>
            <thead>
                <tr>
                    <th>Waiter / Counter</th>
                    <th style="text-align:center;">Sales</th>
                    <th style="text-align:right;">Total</th>
                    <th style="text-align:right;">Verified</th>
                </tr>
            </thead>
            <tbody>
                {waiter_rows}
            </tbody>
        </table>
    </div>
    <div>
        <div class="section-title">By Payment System / Bank</div>
        <table>
            <thead>
                <tr>
                    <th>Bank / Wallet</th>
                    <th style="text-align:center;">Sales</th>
                    <th style="text-align:right;">Total</th>
                    <th style="text-align:right;">Verified</th>
                </tr>
            </thead>
            <tbody>
                {bank_rows}
            </tbody>
        </table>
    </div>
</div>

<div class="section-title">Top Paying Customers</div>
<table>
    <thead>
        <tr>
            <th>Customer / Payer Name</th>
            <th style="text-align:center;">Transactions</th>
            <th style="text-align:right;">Total Amount Paid</th>
        </tr>
    </thead>
    <tbody>
        {customer_rows}
    </tbody>
</table>

<div class="section-title">Transaction Ledger</div>
<table>
    <thead>
        <tr>
            <th>#</th>
            <th>Date & Time</th>
            <th>Waiter</th>
            <th>Customer (Payer)</th>
            <th>Bank</th>
            <th>Transaction Code</th>
            <th style="text-align:right;">Amount</th>
            <th style="text-align:center;">Status</th>
        </tr>
    </thead>
    <tbody>
        {pay_rows}
    </tbody>
</table>

<div class="footer">
    <span>{biz_name} · Official Financial Reconciliation</span>
    <span>Ethiopian Transaction Checker Platform</span>
</div>

</body>
</html>
"""


async def generate_pdf(business: dict, summary: dict, payments: list[dict], period_title: str) -> bytes:
    html_content = build_report_html(business, summary, payments, period_title)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html_content, wait_until="load")
        pdf_bytes = await page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
        )
        await browser.close()
        return pdf_bytes

