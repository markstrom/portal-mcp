"""A small local web service that imitates a customer portal with a login form and cookie session.

Endpoints:
  GET  /login                  login form (test credentials in data.py)
  POST /login                  sets the session cookie `sid` and the CSRF cookie `csrf`
  GET  /api/session            200 when the session is valid, 401 otherwise
  GET  /api/invoices           paginated list (?page=, ?page_size=)
  GET  /api/invoices/{id}/pdf  PDF download
  POST /api/logout             needs the CSRF header; clears the session
  POST /api/reports            needs the CSRF header; creates a report (write example)
  GET  /api/invoices/{id}/delete  side-effecting GET link, must be blocked by discovery
"""

from __future__ import annotations

import secrets

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .data import PASSWORD, USERNAME, fresh_invoices

CSRF_HEADER = "x-csrf-token"


def create_app() -> Starlette:
    sessions: dict[str, str] = {}  # sid -> csrf token
    invoices = fresh_invoices()

    def _sid_valid(request: Request) -> bool:
        return request.cookies.get("sid") in sessions

    def _csrf_valid(request: Request) -> bool:
        sid = request.cookies.get("sid")
        token = request.headers.get(CSRF_HEADER)
        return sid in sessions and token is not None and token == sessions[sid]

    async def login_form(request: Request) -> Response:
        return HTMLResponse(
            "<!doctype html><title>Mock login</title>"
            '<form method="post" action="/login">'
            '<input name="username"><input name="password" type="password">'
            "<button>Log in</button></form>"
        )

    async def login_submit(request: Request) -> Response:
        form = await request.form()
        if form.get("username") != USERNAME or form.get("password") != PASSWORD:
            return HTMLResponse("wrong credentials", status_code=401)
        sid = secrets.token_urlsafe(16)
        csrf = secrets.token_urlsafe(16)
        sessions[sid] = csrf
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("sid", sid, httponly=True, samesite="lax", path="/", max_age=3600)
        response.set_cookie("csrf", csrf, httponly=False, samesite="lax", path="/", max_age=3600)
        return response

    async def session_check(request: Request) -> Response:
        if not _sid_valid(request):
            return JSONResponse({"error": "not logged in"}, status_code=401)
        return JSONResponse({"user": USERNAME})

    async def list_invoices(request: Request) -> Response:
        if not _sid_valid(request):
            return JSONResponse({"error": "not logged in"}, status_code=401)
        page = max(int(request.query_params.get("page", "1")), 1)
        page_size = min(max(int(request.query_params.get("page_size", "2")), 1), 50)
        start = (page - 1) * page_size
        total_pages = max((len(invoices) + page_size - 1) // page_size, 1)
        return JSONResponse(
            {"items": invoices[start : start + page_size], "page": page, "total_pages": total_pages}
        )

    async def invoice_pdf(request: Request) -> Response:
        if not _sid_valid(request):
            return JSONResponse({"error": "not logged in"}, status_code=401)
        invoice_id = request.path_params["invoice_id"]
        if not any(i["id"] == invoice_id for i in invoices):
            return JSONResponse({"error": "not found"}, status_code=404)
        body = b"%PDF-1.4\n% mock invoice " + invoice_id.encode() + b"\n%%EOF\n"
        return Response(
            body,
            media_type="application/pdf",
            headers={"content-disposition": f'attachment; filename="invoice-{invoice_id}.pdf"'},
        )

    async def logout(request: Request) -> Response:
        if not _csrf_valid(request):
            return JSONResponse({"error": "missing or wrong CSRF token"}, status_code=403)
        sessions.pop(request.cookies.get("sid"), None)
        response = Response(status_code=204)
        response.delete_cookie("sid", path="/")
        return response

    async def create_report(request: Request) -> Response:
        if not _csrf_valid(request):
            return JSONResponse({"error": "missing or wrong CSRF token"}, status_code=403)
        return JSONResponse({"id": "report-1", "status": "created"}, status_code=201)

    async def delete_invoice(request: Request) -> Response:
        if not _sid_valid(request):
            return JSONResponse({"error": "not logged in"}, status_code=401)
        invoice_id = request.path_params["invoice_id"]
        before = len(invoices)
        invoices[:] = [i for i in invoices if i["id"] != invoice_id]
        return JSONResponse({"deleted": before != len(invoices)})

    return Starlette(
        routes=[
            Route("/login", login_form, methods=["GET"]),
            Route("/login", login_submit, methods=["POST"]),
            Route("/api/session", session_check, methods=["GET"]),
            Route("/api/invoices", list_invoices, methods=["GET"]),
            Route("/api/invoices/{invoice_id}/pdf", invoice_pdf, methods=["GET"]),
            Route("/api/invoices/{invoice_id}/delete", delete_invoice, methods=["GET"]),
            Route("/api/logout", logout, methods=["POST"]),
            Route("/api/reports", create_report, methods=["POST"]),
        ]
    )
