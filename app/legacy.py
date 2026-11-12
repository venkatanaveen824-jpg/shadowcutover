"""Mock legacy system of record.

Exposes a SOAP 1.1 style endpoint that accepts an UpdateAccount envelope, the way
older agency and banking back-office systems do. It is slow on purpose and can be
switched off with the Chaos toggle to show how the proxy and agent behave when the
legacy side is down.
"""
import asyncio
import random
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Request, Response

from .fields import FORM_FIELDS

router = APIRouter(prefix="/legacy", tags=["legacy"])

NS = "http://example.org/legacy/accounts"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

STATE = {"down": False, "min_latency_ms": 900, "max_latency_ms": 1600}
ACCOUNTS: dict[str, dict] = {}


def build_envelope(form: dict) -> str:
    """Build the SOAP request body for UpdateAccount."""
    fields = "".join(f"<acc:{k}>{_esc(form.get(k, ''))}</acc:{k}>" for k in FORM_FIELDS)
    return (
        f'<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:acc="{NS}">'
        f"<soapenv:Body><acc:UpdateAccount>{fields}</acc:UpdateAccount></soapenv:Body>"
        "</soapenv:Envelope>"
    )


def _esc(v) -> str:
    return (str(v) if v is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fault(code: str, msg: str, status: int) -> Response:
    body = (
        f'<soapenv:Envelope xmlns:soapenv="{SOAP_NS}"><soapenv:Body><soapenv:Fault>'
        f"<faultcode>{code}</faultcode><faultstring>{_esc(msg)}</faultstring>"
        "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
    )
    return Response(body, status_code=status, media_type="text/xml")


@router.post("/soap")
async def update_account(request: Request) -> Response:
    if STATE["down"]:
        return _fault("soapenv:Server", "Legacy host unavailable (chaos mode)", 503)

    await asyncio.sleep(random.uniform(STATE["min_latency_ms"], STATE["max_latency_ms"]) / 1000)

    try:
        root = ET.fromstring(await request.body())
        op = root.find(f".//{{{NS}}}UpdateAccount")
        if op is None:
            return _fault("soapenv:Client", "UpdateAccount operation not found", 400)
        record = {k: (op.findtext(f"{{{NS}}}{k}") or "") for k in FORM_FIELDS}
    except ET.ParseError as exc:
        return _fault("soapenv:Client", f"Malformed envelope: {exc}", 400)

    if not record["account_id"]:
        return _fault("soapenv:Client", "account_id is required", 400)

    ACCOUNTS[record["account_id"]] = record
    body = (
        f'<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:acc="{NS}"><soapenv:Body>'
        f"<acc:UpdateAccountResponse><acc:status>OK</acc:status>"
        f"<acc:account_id>{_esc(record['account_id'])}</acc:account_id>"
        "</acc:UpdateAccountResponse></soapenv:Body></soapenv:Envelope>"
    )
    return Response(body, media_type="text/xml")
