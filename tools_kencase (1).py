"""Kencase tools — registered onto the existing FastMCP server (see server.py).

Everything here is SIMULATED (fake data, no real external calls) unless stated:
  - DigiLocker      simulated  (digilocker_*)
  - Account Aggr.   simulated  (aa_*)  OFF by default: the platform's native
                    banking_aa connector is used instead. Set env var
                    KENCASE_SIM_AA=1 on Render to turn these tools back on.
  - Case state      real       (case_*)   in-memory + /tmp file, lost on restart
  - KB search       real       (kb_search) reads markdown files from ./kb
  - Doc extraction  real       (ocr_extract) text-layer PDFs only, NOT image OCR
  - Courier         simulated  (courier_*)
  - Appointment     simulated  (slots_*)

Every simulated response carries simulated=true and a provider name ending in
"_mock", so the agent (and judges) can see what is real and what is not.
No tool raises: failures come back as {"ok": False, "kind": ..., "error": ...}.
"""
import base64
import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import time

import httpx
from starlette.responses import JSONResponse

_HERE = pathlib.Path(__file__).resolve().parent
KB_DIR = _HERE / "kb"
CASE_FILE = pathlib.Path("/tmp/kencase_cases.json")


# ---------------------------------------------------------------- helpers
def _enc(prefix: str, payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return prefix + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _dec(prefix: str, token: str):
    try:
        if not token.startswith(prefix):
            return None
        body = token[len(prefix):]
        body += "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _sim(provider: str, extra: dict) -> dict:
    out = {"ok": True, "simulated": True, "provider": provider}
    out.update(extra)
    return out


def _bad(msg: str, kind: str = "bad_request") -> dict:
    return {"ok": False, "kind": kind, "error": msg}


def _has(user_id: str, word: str) -> bool:
    return word in (user_id or "").lower()


def _today() -> datetime.date:
    return datetime.date.today()


def register_kencase_tools(mcp) -> None:
    """Attach every Kencase tool and route to the given FastMCP instance."""

    # ==================================================================
    # 1) DIGILOCKER  (SIMULATED)   consent -> status -> fetch
    # Demo switches inside user_id:  "fail" = consent DENIED,
    #                                "expiring" = passport expires 2027-01-15
    # ==================================================================
    DL_PROVIDER = "digilocker_mock"
    DL_TYPES = ["passport_record", "pan_card", "aadhaar", "insurance_policy"]
    DL = "DLREQ-"

    def _dl_document(doc_type: str, user_id: str) -> dict:
        if doc_type == "passport_record":
            return {
                "name": "TEST APPLICANT",
                "passport_number": "TESTP0001",
                "date_of_birth": "1995-05-15",
                "issue_date": "2021-03-15",
                "expiry_date": "2027-01-15" if _has(user_id, "expiring") else "2031-03-14",
                "place_of_issue": "HYDERABAD",
            }
        if doc_type == "pan_card":
            return {"name": "TEST APPLICANT", "pan_number_masked": "XXXXX1234X"}
        if doc_type == "aadhaar":
            return {"name": "TEST APPLICANT", "aadhaar_masked": "XXXX XXXX 0000", "city": "Hyderabad"}
        return {
            "policy_number": "TEST-INS-0001",
            "insured_name": "TEST APPLICANT",
            "insurer": "Sandbox Insurer",
            "coverage_start": "2026-11-10",
            "coverage_end": "2026-12-05",
        }

    @mcp.tool()
    async def digilocker_create_consent(user_id: str, doc_types: list[str]) -> dict:
        """SIMULATED DigiLocker. Start a consent request for the applicant.
        doc_types: any of passport_record, pan_card, aadhaar, insurance_policy.
        Returns request_id (status PENDING). Next call digilocker_get_status,
        then digilocker_fetch_doc for each document."""
        bad = [d for d in doc_types if d not in DL_TYPES]
        if not doc_types or bad:
            return _bad(f"doc_types must be a non-empty subset of {DL_TYPES}; got {doc_types}")
        return _sim(DL_PROVIDER, {
            "request_id": _enc(DL, {"u": user_id, "d": doc_types}),
            "status": "PENDING",
            "doc_types": doc_types,
            "note": "Simulated consent. No real DigiLocker redirect happens in sandbox mode.",
        })

    @mcp.tool()
    async def digilocker_get_status(request_id: str) -> dict:
        """SIMULATED DigiLocker. Check consent status: APPROVED or DENIED.
        If DENIED, ask the applicant to upload that document manually
        (that item only; do not stall the rest of the checklist)."""
        p = _dec(DL, request_id)
        if not p:
            return _bad("invalid or unknown request_id")
        if _has(p.get("u", ""), "fail"):
            return _sim(DL_PROVIDER, {"request_id": request_id, "status": "DENIED",
                                      "reason": "User did not grant consent", "doc_types": p.get("d", [])})
        return _sim(DL_PROVIDER, {"request_id": request_id, "status": "APPROVED", "doc_types": p.get("d", [])})

    @mcp.tool()
    async def digilocker_fetch_doc(request_id: str, doc_type: str) -> dict:
        """SIMULATED DigiLocker. Fetch ONE consented document as structured
        fields. doc_type must be in the consent request. Returns
        kind=consent_denied if the applicant denied consent."""
        p = _dec(DL, request_id)
        if not p:
            return _bad("invalid or unknown request_id")
        if doc_type not in p.get("d", []):
            return _bad(f"{doc_type} was not in this consent request")
        if _has(p.get("u", ""), "fail"):
            return _bad("User did not grant consent for this document", "consent_denied")
        return _sim(DL_PROVIDER, {"request_id": request_id, "doc_type": doc_type,
                                  "document": _dl_document(doc_type, p.get("u", ""))})

    # ==================================================================
    # 2) ACCOUNT AGGREGATOR  (SIMULATED)   consent -> status -> fetch
    # Demo switches inside user_id:  "fail" = consent DENIED,
    #                                "lowbalance" = balances too low for funds proof
    # OFF by default (KENCASE_SIM_AA=1 enables them). Use only as a fallback if native banking_aa fails.
    # ==================================================================
    SIM_AA = os.environ.get("KENCASE_SIM_AA", "0") == "1"
    # When off, the aa_* functions still exist (selftest can skip them) but are
    # NOT registered as MCP tools, so the agent only sees native banking_aa.
    aa_tool = mcp.tool() if SIM_AA else (lambda fn: fn)
    AA_PROVIDER = "account_aggregator_mock"
    AA_TYPES = ["bank_statement", "mutual_fund", "gst_return", "nps_balance"]
    AA = "AAREQ-"

    def _aa_data(ftype: str, user_id: str) -> dict:
        low = _has(user_id, "lowbalance")
        if ftype == "bank_statement":
            return {
                "account_holder": "TEST APPLICANT",
                "bank": "Sandbox Bank",
                "account_number_masked": "XXXXXX0000",
                "period_from": (_today() - datetime.timedelta(days=180)).isoformat(),
                "period_to": _today().isoformat(),
                "average_monthly_balance_inr": 85000 if low else 640000,
                "closing_balance_inr": 52000 if low else 710000,
                "monthly_salary_credit_inr": 65000,
                "salary_narration": "SALARY ACME TEST PVT LTD",
                "employer_name_on_statement": "ACME TEST PVT LTD",
            }
        if ftype == "mutual_fund":
            return {"holder": "TEST APPLICANT", "folios": 2, "total_value_inr": 40000 if low else 350000}
        if ftype == "gst_return":
            return {"gstin_masked": "XXXXXXXXXXXX1Z5", "filing_period": "last financial year",
                    "turnover_inr": 4800000}
        return {"pran_masked": "XXXXXXXX0000", "corpus_inr": 220000}  # nps_balance

    @aa_tool
    async def aa_request_consent(user_id: str, financial_types: list[str]) -> dict:
        """SIMULATED Account Aggregator. Start a consent request for financial
        proof. financial_types: any of bank_statement, mutual_fund, gst_return,
        nps_balance. Returns request_id (PENDING). Then call aa_get_status and
        aa_fetch_financials."""
        bad = [t for t in financial_types if t not in AA_TYPES]
        if not financial_types or bad:
            return _bad(f"financial_types must be a non-empty subset of {AA_TYPES}; got {financial_types}")
        return _sim(AA_PROVIDER, {
            "request_id": _enc(AA, {"u": user_id, "t": financial_types}),
            "status": "PENDING",
            "financial_types": financial_types,
            "note": "Simulated consent. No real Account Aggregator call is made.",
        })

    @aa_tool
    async def aa_get_status(request_id: str) -> dict:
        """SIMULATED Account Aggregator. Consent status: APPROVED or DENIED.
        If DENIED, fall back to a manual bank statement upload for that item."""
        p = _dec(AA, request_id)
        if not p:
            return _bad("invalid or unknown request_id")
        if _has(p.get("u", ""), "fail"):
            return _sim(AA_PROVIDER, {"request_id": request_id, "status": "DENIED",
                                      "reason": "User did not approve consent",
                                      "financial_types": p.get("t", [])})
        return _sim(AA_PROVIDER, {"request_id": request_id, "status": "APPROVED",
                                  "financial_types": p.get("t", [])})

    @aa_tool
    async def aa_fetch_financials(request_id: str, financial_type: str) -> dict:
        """SIMULATED Account Aggregator. Fetch one consented financial record
        as structured fields (balances, salary credits, employer name on the
        statement, period). Use it for the funds check and the employer-name
        consistency check against the salary slip."""
        p = _dec(AA, request_id)
        if not p:
            return _bad("invalid or unknown request_id")
        if financial_type not in p.get("t", []):
            return _bad(f"{financial_type} was not in this consent request")
        if _has(p.get("u", ""), "fail"):
            return _bad("User did not approve consent for this record", "consent_denied")
        return _sim(AA_PROVIDER, {"request_id": request_id, "financial_type": financial_type,
                                  "data": _aa_data(financial_type, p.get("u", ""))})

    # ==================================================================
    # 3) CASE STATE  (REAL, but in-memory + /tmp file: lost on restart/spin-down)
    # ==================================================================
    def _load_cases() -> dict:
        try:
            return json.loads(CASE_FILE.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _save_cases(cases: dict) -> None:
        try:
            CASE_FILE.write_text(json.dumps(cases))
        except Exception:  # noqa: BLE001
            pass

    def _default_case(user_id: str) -> dict:
        return {
            "user_id": user_id,
            "stage": "S0",
            "user_profile": {"nationality": None, "residence_city": None, "purpose": None,
                             "funding_source": None, "prior_refusal": False, "dependents": []},
            "destination": {"country": None, "visa_type": None, "travel_date_start": None,
                            "travel_date_end": None, "consulate_post": None},
            "execution_flags": {"submission_path": None, "human_escalation_required": False,
                                "escalation_reason": None, "consulate_readiness_score": 0.0},
            "task_graph": [],
            "collected_documents": {},
            "form_data": {},
            "human_touch": {"total": 0, "remaining": 0},
        }

    def _merge(base: dict, patch: dict) -> dict:
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                _merge(base[k], v)
            else:
                base[k] = v
        return base

    @mcp.tool()
    async def case_get(user_id: str) -> dict:
        """Load the applicant's case state (stage, profile, destination, flags,
        task_graph, collected_documents, form_data, human_touch counts). Call
        this at the START of every turn. A new user gets a fresh case at S0."""
        cases = _load_cases()
        case = cases.get(user_id) or _default_case(user_id)
        return {"ok": True, "case": case,
                "note": "State is kept in server memory/tmp; it can reset if the server restarts."}

    @mcp.tool()
    async def case_update(user_id: str, patch: dict) -> dict:
        """Save changes to the case state. `patch` is a JSON object merged into
        the case (nested objects are merged, lists and values are replaced),
        e.g. {"stage": "S3", "human_touch": {"total": 9, "remaining": 4}}.
        Call this at the END of every turn."""
        if not isinstance(patch, dict):
            return _bad("patch must be a JSON object")
        cases = _load_cases()
        case = cases.get(user_id) or _default_case(user_id)
        case = _merge(case, patch)
        case["user_id"] = user_id
        cases[user_id] = case
        _save_cases(cases)
        return {"ok": True, "case": case}

    @mcp.tool()
    async def case_reset(user_id: str) -> dict:
        """Delete this applicant's case state (use for demo re-runs, or when the
        applicant asks to erase their data)."""
        cases = _load_cases()
        existed = cases.pop(user_id, None) is not None
        _save_cases(cases)
        return {"ok": True, "deleted": existed}

    # ==================================================================
    # 4) KNOWLEDGE BASE SEARCH  (REAL: reads ./kb/*.md in the repo)
    # File naming:  <country>_<visa_type>.md   e.g. usa_travel.md, canada_business.md
    # Only needed if the platform's own KB upload does not work for you.
    # ==================================================================
    @mcp.tool()
    async def kb_search(query: str = "", country: str = "", visa_type: str = "") -> dict:
        """Look up verified visa rules from the knowledge base. Preferred:
        country (usa, canada, australia) + visa_type (travel, business) returns
        that whole rules file. Or pass a keyword `query` to search all files.
        Always cite the returned file name and its last_verified date. If
        nothing is found, say "not in my verified rules" and escalate."""
        if not KB_DIR.exists():
            return _bad("kb folder not found on server", "kb_missing")
        files = sorted(p.name for p in KB_DIR.glob("*.md"))
        if country and visa_type:
            stem = re.sub(r"[^a-z0-9_]", "", f"{country}_{visa_type}".lower().replace(" ", "_"))
            p = KB_DIR / f"{stem}.md"
            if p.exists():
                return {"ok": True, "source": "kb", "file": p.name, "content": p.read_text()[:12000]}
            return {"ok": False, "kind": "kb_gap", "error": f"no rules file for {country}/{visa_type}",
                    "available_files": files}
        terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]
        if not terms:
            return _bad("give country+visa_type, or a query")
        hits = []
        for name in files:
            lines = (KB_DIR / name).read_text().splitlines()
            matched = [ln.strip() for ln in lines if any(t in ln.lower() for t in terms)][:5]
            if matched:
                hits.append({"file": name, "snippets": matched})
        if not hits:
            return {"ok": False, "kind": "kb_gap", "error": "nothing matched", "available_files": files}
        return {"ok": True, "source": "kb", "results": hits}

    # ==================================================================
    # 5) DOCUMENT EXTRACTION  (REAL, but text-layer PDFs only — NOT image OCR)
    # Needs `pypdf` in requirements.txt. If missing, only this tool errors.
    # ==================================================================
    @mcp.tool()
    async def ocr_extract(document_url: str = "", document_base64: str = "", doc_type: str = "") -> dict:
        """Extract text and key fields from a PDF that has a text layer (salary
        slip, employer letter, bank letter...). Give EITHER document_url
        (publicly fetchable) OR document_base64. Returns `fields` (Label: value
        lines found) and `text`. Scanned images are NOT supported in demo mode:
        ask the applicant for a text PDF. Use the result for the format,
        expiry and cross-document consistency checks."""
        try:
            from pypdf import PdfReader
        except ImportError:
            return _bad("pypdf is not installed on the server; add 'pypdf' to requirements.txt", "config")
        try:
            if document_base64:
                data = base64.b64decode(document_base64)
            elif document_url:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as h:
                    r = await h.get(document_url, headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                    data = r.content
            else:
                return _bad("provide document_url or document_base64")
            if len(data) > 10 * 1024 * 1024:
                return _bad("file larger than 10 MB", "too_large")
            if not data.startswith(b"%PDF"):
                return _bad("Only text-layer PDFs are supported in demo mode (no image OCR). "
                            "Ask the applicant for a PDF.", "unsupported_format")
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:10])
            if not text.strip():
                return _bad("This PDF has no text layer (it is a scan). Ask for a text PDF.", "no_text_layer")
            fields = {}
            for line in text.splitlines():
                m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 /&._()-]{1,40}?)\s*:\s*(.+?)\s*$", line)
                if m:
                    key = re.sub(r"[^a-z0-9]+", "_", m.group(1).lower()).strip("_")
                    fields.setdefault(key, m.group(2))
            return {"ok": True, "method": "pdf_text_layer", "ocr": False, "doc_type": doc_type,
                    "pages": len(reader.pages), "fields": fields, "text": text[:4000]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "kind": "unknown", "error": str(e)[:500]}

    # ==================================================================
    # 6) COURIER  (SIMULATED)   serviceability -> create -> track
    # Tracking is stateless: status depends on minutes since creation:
    #   <1 min PICKUP_SCHEDULED, <3 min IN_TRANSIT, else DELIVERED.
    # Demo switch: "lost" inside user_id -> DELAYED + requires_human=true.
    # Use only if the platform has no native courier connector.
    # ==================================================================
    CR_PROVIDER = "courier_mock"
    CR = "KENC"

    @mcp.tool()
    async def courier_check_serviceability(pincode: str) -> dict:
        """SIMULATED courier. Check whether a 6-digit pincode is serviceable
        for document pickup/delivery."""
        if not re.fullmatch(r"\d{6}", pincode or ""):
            return _bad("pincode must be 6 digits")
        return _sim(CR_PROVIDER, {"pincode": pincode, "serviceable": True, "pickup_available": True})

    @mcp.tool()
    async def courier_create_shipment(user_id: str, from_pincode: str, to_pincode: str,
                                      description: str = "visa documents") -> dict:
        """SIMULATED courier. Book a pickup for physical originals (applicant
        -> bank -> visa centre). Returns an awb to track with courier_track."""
        for pc in (from_pincode, to_pincode):
            if not re.fullmatch(r"\d{6}", pc or ""):
                return _bad("both pincodes must be 6 digits")
        awb = _enc(CR, {"u": user_id, "t": int(time.time()), "f": from_pincode, "o": to_pincode})
        return _sim(CR_PROVIDER, {"awb": awb, "status": "PICKUP_SCHEDULED",
                                  "description": description, "expected_delivery_days": 2})

    @mcp.tool()
    async def courier_track(awb: str) -> dict:
        """SIMULATED courier. Track a shipment. If requires_human is true
        (delay or lost original), escalate to a human at high priority."""
        p = _dec(CR, awb)
        if not p:
            return _bad("invalid or unknown awb")
        if _has(p.get("u", ""), "lost"):
            return _sim(CR_PROVIDER, {"awb": awb, "status": "DELAYED", "requires_human": True,
                                      "detail": "Shipment delayed or original possibly lost"})
        minutes = (time.time() - p.get("t", 0)) / 60.0
        status = "PICKUP_SCHEDULED" if minutes < 1 else ("IN_TRANSIT" if minutes < 3 else "DELIVERED")
        return _sim(CR_PROVIDER, {"awb": awb, "status": status, "requires_human": False})

    # ==================================================================
    # 7) APPOINTMENT SLOTS  (SIMULATED Tier-2 partner channel)
    # Demo switch: "noslots" inside user_id -> empty list (waitlist path).
    # ==================================================================
    SL_PROVIDER = "tier2_partner_mock"
    SL = "SLOT-"
    SL_COUNTRIES = ["usa", "canada", "australia"]
    SL_VISAS = ["travel", "business"]
    SL_CITIES = ["hyderabad", "mumbai", "delhi", "chennai", "kolkata"]

    @mcp.tool()
    async def slots_lookup(country: str, visa_type: str, city: str, user_id: str = "") -> dict:
        """SIMULATED appointment slots from the authorized partner channel.
        country: usa|canada|australia, visa_type: travel|business,
        city: hyderabad|mumbai|delhi|chennai|kolkata. Shows ONLY this
        applicant's eligible options. Never book without the applicant's
        explicit confirmation (see slots_confirm). Empty list = offer waitlist."""
        c, v, ci = (country or "").lower(), (visa_type or "").lower(), (city or "").lower()
        if c not in SL_COUNTRIES or v not in SL_VISAS or ci not in SL_CITIES:
            return _bad(f"country in {SL_COUNTRIES}, visa_type in {SL_VISAS}, city in {SL_CITIES}")
        if _has(user_id, "noslots"):
            return _sim(SL_PROVIDER, {"slots": [], "waitlist_available": True,
                                      "note": "No slots currently; offer waitlist retry."})
        slots = []
        for days, hhmm in ((14, "09:30"), (21, "11:00"), (35, "14:30")):
            date = (_today() + datetime.timedelta(days=days)).isoformat()
            slots.append({"slot_id": _enc(SL, {"c": c, "v": v, "ci": ci, "d": date, "t": hhmm}),
                          "country": c, "visa_type": v, "city": ci, "date": date, "time": hhmm})
        return _sim(SL_PROVIDER, {"slots": slots, "waitlist_available": True})

    @mcp.tool()
    async def slots_confirm(slot_id: str, user_id: str, applicant_confirmed: bool = False) -> dict:
        """SIMULATED. Confirm ONE slot the applicant chose. Refuses unless
        applicant_confirmed is true, and you may set it true ONLY after the
        applicant has explicitly said yes to this exact slot in the chat."""
        if not applicant_confirmed:
            return _bad("Applicant has not explicitly confirmed this slot. Ask them, then call again "
                        "with applicant_confirmed=true.", "needs_human_confirmation")
        p = _dec(SL, slot_id)
        if not p:
            return _bad("invalid or unknown slot_id")
        conf = "KENC-CONF-" + hashlib.sha1((slot_id + user_id).encode()).hexdigest()[:8].upper()
        return _sim(SL_PROVIDER, {"confirmation_id": conf, "status": "CONFIRMED", "slot": p})

    # ==================================================================
    # BROWSER TEST ROUTES (no tools needed, just open the URL)
    # ==================================================================
    @mcp.custom_route("/digilocker/test", methods=["GET"])
    async def digilocker_test(request):
        """/digilocker/test?user_id=demo_user&doc=passport_record"""
        user_id = request.query_params.get("user_id", "demo_user")
        doc = request.query_params.get("doc", "passport_record")
        created = await digilocker_create_consent(user_id, [doc])
        if not created.get("ok"):
            return JSONResponse(created, status_code=400)
        rid = created["request_id"]
        return JSONResponse({"create": created,
                             "status": await digilocker_get_status(rid),
                             "fetch": await digilocker_fetch_doc(rid, doc)})

    @mcp.custom_route("/kencase/selftest", methods=["GET"])
    async def selftest(request):
        """Runs every tool once. PASS/FAIL/SKIP per tool."""
        results = {}

        async def run(name, coro, ok_kinds=()):
            try:
                r = await coro
                if r.get("ok"):
                    results[name] = "PASS"
                elif r.get("kind") in ok_kinds:
                    results[name] = f"SKIP ({r.get('kind')})"
                else:
                    results[name] = f"FAIL: {r}"
                return r
            except Exception as e:  # noqa: BLE001
                results[name] = f"FAIL: {e}"
                return {}

        dl = await run("digilocker_create_consent", digilocker_create_consent("selftest", ["passport_record"]))
        if dl.get("request_id"):
            await run("digilocker_get_status", digilocker_get_status(dl["request_id"]))
            await run("digilocker_fetch_doc", digilocker_fetch_doc(dl["request_id"], "passport_record"))
        if SIM_AA:
            aa = await run("aa_request_consent", aa_request_consent("selftest", ["bank_statement"]))
            if aa.get("request_id"):
                await run("aa_get_status", aa_get_status(aa["request_id"]))
                await run("aa_fetch_financials", aa_fetch_financials(aa["request_id"], "bank_statement"))
        else:
            results["aa_* (simulated)"] = "SKIP (off; using native banking_aa)"
        await run("case_update", case_update("selftest", {"stage": "S1"}))
        await run("case_get", case_get("selftest"))
        await run("case_reset", case_reset("selftest"))
        await run("kb_search", kb_search(country="usa", visa_type="travel"), ok_kinds=("kb_missing", "kb_gap"))
        await run("ocr_extract", ocr_extract(document_base64=base64.b64encode(b"not a pdf").decode()),
                  ok_kinds=("unsupported_format",))
        await run("courier_check_serviceability", courier_check_serviceability("500001"))
        cr = await run("courier_create_shipment", courier_create_shipment("selftest", "500001", "110001"))
        if cr.get("awb"):
            await run("courier_track", courier_track(cr["awb"]))
        sl = await run("slots_lookup", slots_lookup("usa", "travel", "hyderabad", "selftest"))
        if sl.get("slots"):
            await run("slots_confirm (refuses without yes)",
                      slots_confirm(sl["slots"][0]["slot_id"], "selftest", False),
                      ok_kinds=("needs_human_confirmation",))
        failed = [k for k, v in results.items() if v.startswith("FAIL")]
        return JSONResponse({"all_ok": not failed, "failed": failed, "sim_aa": SIM_AA, "results": results})
