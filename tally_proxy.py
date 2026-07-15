"""
Tally Suite — Tally Prime Proxy
Add this to your existing licence_server.py OR run standalone on same FastAPI app.

How it works:
  Browser → POST /api/tally/push  (your FastAPI server)
  FastAPI  → POST http://localhost:9000  (Tally Prime XML server)
  Tally    → response XML
  FastAPI  → return result to browser

Tally Prime setup (one time):
  In Tally Prime → F12 Configure → Advanced Configuration
  Enable ODBC / TDL → set port 9000 (default)
  Keep Tally open while using this tool.

Add to licence_server.py:
  from tally_proxy import router as tally_router
  app.include_router(tally_router)
"""

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import xml.etree.ElementTree as ET

router = APIRouter(prefix="/api/tally", tags=["tally"])

TALLY_URL = "http://localhost:9000"   # Tally Prime XML server


class PushRequest(BaseModel):
    xml: str
    machine_id: Optional[str] = None
    tally_url: Optional[str] = None   # allow override for remote Tally


def parse_tally_response(resp_text: str) -> dict:
    """Parse Tally's XML response into a clean dict."""
    try:
        root = ET.fromstring(resp_text)

        # Check for errors
        created = root.findtext(".//CREATED") or "0"
        altered  = root.findtext(".//ALTERED")  or "0"
        deleted  = root.findtext(".//DELETED")  or "0"
        lastenum = root.findtext(".//LASTERRORCOLLECTION//LINEERROR") or ""
        errors   = [e.text for e in root.findall(".//LINEERROR") if e.text]
        warnings = [w.text for w in root.findall(".//LINEWARNING") if w.text]

        return {
            "success":  int(created) > 0,
            "created":  int(created),
            "altered":  int(altered),
            "deleted":  int(deleted),
            "errors":   errors,
            "warnings": warnings,
            "raw":      resp_text,
        }
    except ET.ParseError:
        # Tally sometimes returns plain text on error
        return {
            "success":  False,
            "created":  0,
            "altered":  0,
            "deleted":  0,
            "errors":   [resp_text],
            "warnings": [],
            "raw":      resp_text,
        }


@router.post("/push")
async def push_to_tally(req: PushRequest):
    """
    Receive XML from browser, forward to Tally Prime, return result.
    """
    target = req.tally_url or TALLY_URL

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                target,
                content=req.xml.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8"},
            )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=(
                "Cannot connect to Tally Prime. "
                "Make sure Tally is open and the XML server is enabled on port 9000. "
                f"Target: {target}"
            )
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Tally Prime did not respond within 30 seconds."
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")

    result = parse_tally_response(resp.text)
    return JSONResponse(content=result, status_code=200 if result["success"] else 422)


@router.get("/status")
async def tally_status(tally_url: Optional[str] = None):
    """
    Ping Tally Prime to check if it's reachable.
    Send a minimal company info request.
    """
    target = tally_url or TALLY_URL
    ping_xml = """<ENVELOPE>
<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
<BODY><EXPORTDATA>
<REQUESTDESC>
<REPORTNAME>List of Companies</REPORTNAME>
</REQUESTDESC>
</EXPORTDATA></BODY>
</ENVELOPE>"""

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                target,
                content=ping_xml.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8"},
            )
        # Try to extract company names from response
        companies = []
        try:
            root = ET.fromstring(resp.text)
            companies = [c.text for c in root.findall(".//COMPANY") if c.text]
            if not companies:
                companies = [c.text for c in root.findall(".//NAME") if c.text]
        except Exception:
            pass

        return {
            "connected": True,
            "tally_url": target,
            "companies": companies,
            "raw": resp.text[:500],
        }
    except Exception as e:
        return {
            "connected": False,
            "tally_url": target,
            "error": str(e),
        }
