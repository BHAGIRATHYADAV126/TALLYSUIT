"""
Tally Suite — Converter API
Handles Excel → Tally XML conversion server-side.
Customer browser only sends the file — Python logic never leaves server.

Routes:
  POST /api/convert          — upload Excel, get XML back
  POST /api/convert/push     — upload Excel, convert AND push to Tally
"""

import io, json
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import Response, JSONResponse
import pandas as pd

router = APIRouter(prefix="/api/convert", tags=["converter"])


# ── Core conversion logic (runs server-side only) ─────────────────────────────

def clean_amount(series):
    return pd.to_numeric(
        series.astype(str)
              .str.replace(",", "", regex=False)
              .str.strip()
              .replace("", "0")
              .replace("nan", "0")
              .replace("None", "0"),
        errors="coerce"
    ).fillna(0)


def find_col(df, candidates):
    for c in df.columns:
        for k in candidates:
            if k.lower() in c.lower():
                return c
    return None


def excel_to_tally_xml(
    file_bytes:   bytes,
    bank_ledger:  str,
    other_ledger: str,
    company:      str,
) -> dict:
    """
    Convert Excel bank statement bytes to Tally XML string.
    Returns dict with xml, voucher_count, skipped, engine.
    """
    # Detect format from magic bytes
    magic = file_bytes[:4]
    if magic[:2] == b'PK':
        engine = 'openpyxl'
    elif magic == b'\xd0\xcf\x11\xe0':
        engine = 'xlrd'
    else:
        raise ValueError(f"Unknown file format. Please upload .xlsx or .xls file.")

    buf = io.BytesIO(file_bytes)
    df  = pd.read_excel(buf, engine=engine)

    # Find columns
    wd  = find_col(df, ['withdrawal', 'debit', 'dr'])  or df.columns[3]
    dep = find_col(df, ['deposit', 'credit', 'cr'])    or df.columns[4]
    dt  = find_col(df, ['date'])                       or df.columns[0]
    nr  = find_col(df, ['narration', 'description', 'particulars', 'remarks']) or df.columns[1]

    df[wd]  = clean_amount(df[wd])
    df[dep] = clean_amount(df[dep])

    N   = '\r\n'
    xml = ''
    vno = 1
    skipped = 0

    for _, row in df.iterrows():
        if pd.isna(row[dt]):
            skipped += 1; continue
        try:
            d = pd.to_datetime(row[dt], dayfirst=True).strftime('%Y%m%d')
        except:
            skipped += 1; continue

        nar     = (str(row[nr])
                   .replace('&', '&amp;')
                   .replace('<', '&lt;')
                   .replace('>', '&gt;'))
        w       = float(row[wd])
        dep_amt = float(row[dep])

        if dep_amt > 0:
            amt = dep_amt; vt = 'Receipt'
            le = (
                N + '<ALLLEDGERENTRIES.LIST>' + N
                + '<LEDGERNAME>' + other_ledger + '</LEDGERNAME>' + N
                + '<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>' + N
                + '<ISPARTYLEDGER>No</ISPARTYLEDGER>' + N
                + '<ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>' + N
                + '<AMOUNT>' + str(amt) + '</AMOUNT>' + N
                + '</ALLLEDGERENTRIES.LIST>' + N
                + '<ALLLEDGERENTRIES.LIST>' + N
                + '<LEDGERNAME>' + bank_ledger + '</LEDGERNAME>' + N
                + '<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>' + N
                + '<ISPARTYLEDGER>Yes</ISPARTYLEDGER>' + N
                + '<ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>' + N
                + '<AMOUNT>-' + str(amt) + '</AMOUNT>' + N
                + '</ALLLEDGERENTRIES.LIST>'
            )
        elif w > 0:
            amt = w; vt = 'Payment'
            le = (
                N + '<ALLLEDGERENTRIES.LIST>' + N
                + '<LEDGERNAME>' + other_ledger + '</LEDGERNAME>' + N
                + '<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>' + N
                + '<ISPARTYLEDGER>No</ISPARTYLEDGER>' + N
                + '<ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>' + N
                + '<AMOUNT>-' + str(amt) + '</AMOUNT>' + N
                + '</ALLLEDGERENTRIES.LIST>' + N
                + '<ALLLEDGERENTRIES.LIST>' + N
                + '<LEDGERNAME>' + bank_ledger + '</LEDGERNAME>' + N
                + '<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>' + N
                + '<ISPARTYLEDGER>Yes</ISPARTYLEDGER>' + N
                + '<ISLASTDEEMEDPOSITIVE>No</ISLASTDEEMEDPOSITIVE>' + N
                + '<AMOUNT>' + str(amt) + '</AMOUNT>' + N
                + '</ALLLEDGERENTRIES.LIST>'
            )
        else:
            skipped += 1; continue

        xml += (
            N + '<TALLYMESSAGE xmlns:UDF="TallyUDF">' + N
            + '<VOUCHER VCHTYPE="' + vt + '" ACTION="Create" OBJVIEW="Accounting Voucher View">' + N
            + '<DATE>' + d + '</DATE>' + N
            + '<EFFECTIVEDATE>' + d + '</EFFECTIVEDATE>' + N
            + '<VOUCHERTYPENAME>' + vt + '</VOUCHERTYPENAME>' + N
            + '<PARTYLEDGERNAME>' + bank_ledger + '</PARTYLEDGERNAME>' + N
            + '<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>' + N
            + '<VOUCHERNUMBER>' + str(vno) + '</VOUCHERNUMBER>' + N
            + '<NARRATION>' + nar + '</NARRATION>'
            + le + N + '</VOUCHER>' + N + '</TALLYMESSAGE>'
        )
        vno += 1

    final_xml = (
        '<ENVELOPE>' + N + '<HEADER>' + N
        + '<TALLYREQUEST>Import Data</TALLYREQUEST>' + N + '</HEADER>' + N
        + '<BODY>' + N + '<IMPORTDATA>' + N + '<REQUESTDESC>' + N
        + '<REPORTNAME>Vouchers</REPORTNAME>' + N
        + '<STATICVARIABLES>' + N
        + '<SVCURRENTCOMPANY>' + company + '</SVCURRENTCOMPANY>' + N
        + '</STATICVARIABLES>' + N + '</REQUESTDESC>' + N
        + '<REQUESTDATA>' + N + xml + N
        + '</REQUESTDATA>' + N + '</IMPORTDATA>' + N
        + '</BODY>' + N + '</ENVELOPE>'
    )

    return {
        'xml':      final_xml,
        'vouchers': vno - 1,
        'skipped':  skipped,
        'engine':   engine,
    }


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.post("/")
async def convert(
    request:      Request,
    file:         UploadFile = File(...),
    bank_ledger:  str = Form("HDFC BANK."),
    other_ledger: str = Form("SUSPENSES"),
    company:      str = Form("My Company"),
):
    """Upload Excel file, get Tally XML back as download."""
    # Auth check
    from auth_middleware import require_auth
    user = await require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Please login first.")

    # Validate file
    if not file.filename.lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Please upload .xlsx or .xls file.")

    file_bytes = await file.read()
    if len(file_bytes) > 10 * 1024 * 1024:  # 10MB limit
        raise HTTPException(status_code=400, detail="File too large. Max 10MB.")

    try:
        result = excel_to_tally_xml(file_bytes, bank_ledger, other_ledger, company)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Return XML as downloadable file
    return Response(
        content=result['xml'].encode('utf-8'),
        media_type='text/xml',
        headers={
            'Content-Disposition': 'attachment; filename="tally_import.xml"',
            'X-Vouchers':  str(result['vouchers']),
            'X-Skipped':   str(result['skipped']),
            'X-Engine':    result['engine'],
        }
    )


@router.post("/push")
async def convert_and_push(
    request:      Request,
    file:         UploadFile = File(...),
    bank_ledger:  str = Form("HDFC BANK."),
    other_ledger: str = Form("SUSPENSES"),
    company:      str = Form("My Company"),
    tally_url:    str = Form("http://localhost:9900"),
):
    """Upload Excel, convert, AND push directly to Tally Prime."""
    from auth_middleware import require_auth
    user = await require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Please login first.")

    file_bytes = await file.read()

    try:
        result = excel_to_tally_xml(file_bytes, bank_ledger, other_ledger, company)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Push to Tally
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                tally_url.rstrip('/'),
                content=result['xml'].encode('utf-8'),
                headers={'Content-Type': 'text/xml'},
            )
        import xml.etree.ElementTree as ET
        try:
            root    = ET.fromstring(resp.text)
            created = int(root.findtext('.//CREATED') or root.findtext('CREATED') or '0')
            errors  = [e.text for e in root.iter('LINEERROR') if e.text]
        except:
            created = 0
            errors  = [resp.text[:200]]

        return JSONResponse({
            'success':  created > 0,
            'created':  created,
            'vouchers': result['vouchers'],
            'skipped':  result['skipped'],
            'engine':   result['engine'],
            'errors':   errors,
        })
    except httpx.ConnectError:
        raise HTTPException(status_code=503,
            detail=f"Cannot connect to Tally at {tally_url}. Is Tally Prime open?")
