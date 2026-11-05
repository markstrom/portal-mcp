"""A small local web service that imitates a customer portal with a login form and cookie session.

Endpoints:
  GET  /                       start page with a menu (after login)
  GET  /login                  login form (test credentials in data.py)
  POST /login                  sets the session cookie `sid` and the CSRF cookie `csrf`
  GET  /api/session            200 when the session is valid, 401 otherwise
  GET  /account                page: 200 when logged in, otherwise a redirect to /login that sets
                               a new nonce cookie (like many OpenID Connect sign-ins)
  GET  /api/invoices           paginated list (?page=, ?page_size=)
  GET  /api/invoices/{id}/pdf  PDF download
  POST /api/logout             needs the CSRF header; clears the session
  POST /api/reports            needs the CSRF header; creates a report (JSON write example)
  POST /api/payments           needs the CSRF header; form-encoded payment (binding example)
  GET  /api/payments           payments received so far
  POST /api/messages           needs the CSRF header; sends a message (messaging example)
  GET  /api/invoices/{id}/delete  side-effecting GET link, must be blocked by discovery
  GET  /reports/new            page that posts a JSON report on load (write capture test)
  GET  /payments/new           HTML form that submits a payment on load (write capture test)
  GET  /cases                  page whose data loads only after a click on a tab (explore test)
  GET  /app                    page whose app stores a token in sessionStorage (data_auth test)
  GET  /search?q=              server-rendered HTML search results, with the same results as
                               embedded JSON (HTML read tool test)
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
    payments: list[dict] = []

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

    async def home(request: Request) -> Response:
        if not _sid_valid(request):
            return RedirectResponse("/login", status_code=302)
        return HTMLResponse(
            "<!doctype html><title>Start</title>"
            "<header><nav>"
            '<a href="/invoices-page">Invoices</a>'
            '<a href="/reports/new">New report</a>'
            '<a href="/api/logout">Log out</a>'
            '<a href="/messages/delete-all">Delete all messages</a>'
            '<a href="https://other.example.org/help">Help</a>'
            '<a href="/files/terms.pdf">Terms (PDF)</a>'
            "</nav></header>"
            '<main><a href="/invoices-page#top">Invoices again</a><a href="/profile?tab=1">Profile</a></main>'
            # Like a single-page app: the page loads its data as JSON.
            "<script>fetch('/api/invoices?page=1'); fetch('/api/invoices'); fetch('/api/public-info');</script>"
        )

    async def public_info(request: Request) -> Response:
        # JSON that answers 200 for everyone: no good as a session check.
        return JSONResponse({"opening_hours": "8-17"})

    async def too_large(request: Request) -> Response:
        # What an IIS-style server answers when the Cookie header is too large.
        return HTMLResponse(
            "<h2>Bad Request - Request Too Long</h2><p>HTTP Error 400. The size of the request headers is too long.</p>",
            status_code=400,
        )

    async def page_check(request: Request) -> Response:
        # Like a sign-in system that redirects to the login and sets a new nonce cookie each time.
        if _sid_valid(request):
            return HTMLResponse("<p>logged in</p>")
        response = RedirectResponse("/login", status_code=302)
        response.set_cookie(f"nonce.{secrets.token_urlsafe(12)}", "n", max_age=900, path="/")
        return response

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

    async def create_payment(request: Request) -> Response:
        if not _csrf_valid(request):
            return JSONResponse({"error": "missing or wrong CSRF token"}, status_code=403)
        form = await request.form()
        payment = {"id": f"pay-{len(payments) + 1}", **{k: form[k] for k in form}}
        payments.append(payment)
        return JSONResponse(payment, status_code=201)

    async def list_payments(request: Request) -> Response:
        if not _sid_valid(request):
            return JSONResponse({"error": "not logged in"}, status_code=401)
        return JSONResponse({"items": payments})

    async def send_message(request: Request) -> Response:
        if not _csrf_valid(request):
            return JSONResponse({"error": "missing or wrong CSRF token"}, status_code=403)
        return JSONResponse({"id": "msg-1", "status": "sent"}, status_code=201)

    async def report_form(request: Request) -> Response:
        # A page whose script submits a JSON write on load, like a single-page app would.
        return HTMLResponse(
            "<!doctype html><title>New report</title><p>New report</p><script>"
            "fetch('/api/reports', {method: 'POST', headers: {'content-type': 'application/json',"
            " 'x-csrf-token': 'from-page'}, body: JSON.stringify({title: 'Form title', text: 'Form text'})});"
            "</script>"
        )

    async def payment_form(request: Request) -> Response:
        # A classic HTML form, submitted automatically so tests need no clicks.
        return HTMLResponse(
            "<!doctype html><title>New payment</title>"
            '<form id="f" method="post" action="/api/payments">'
            '<input name="amount" value="125"><input name="recipient" value="Testmottagare">'
            '<input type="hidden" name="currency" value="SEK"></form>'
            "<script>document.getElementById('f').submit();</script>"
        )

    async def cases_page(request: Request) -> Response:
        # Like a single-page app: nothing loads until a tab is opened. The other controls are
        # ones the recorder must never click, or that cause a write that must be captured.
        return HTMLResponse(
            "<!doctype html><title>Cases</title>"
            '<div id="banner"><button type="button" id="all">Acceptera alla</button>'
            '<button type="button" id="nec">Endast nödvändiga</button></div>'
            '<div role="tablist"><button type="button" role="tab" id="open">Open cases</button></div>'
            '<details><summary>More</summary><button type="button" id="more">Show more</button></details>'
            '<button type="button" id="del">Delete all</button>'
            '<button type="button" id="out">Log out</button>'
            '<button type="button" id="mark">Mark as read</button>'
            '<button type="button" id="away">Go to invoices</button>'
            '<form action="/api/payments" method="post"><input name="amount" value="1"><button>Next</button></form>'
            '<button type="button" aria-label=""></button>'
            '<button type="button" id="chev" aria-expanded="false"><svg width="8" height="8"></svg></button>'
            '<form id="search" role="search"><input type="search" name="q" placeholder="Search"></form>'
            "<script>"
            "window.hits = [];"
            "const hit = (n) => { window.hits.push(n); document.title = window.hits.join(','); };"
            "document.getElementById('open').onclick = () => { hit('open'); fetch('/api/invoices?page=2'); };"
            "document.getElementById('more').onclick = () => { hit('more'); fetch('/api/payments'); };"
            "document.getElementById('del').onclick = () => { hit('del'); fetch('/api/invoices/inv-002/delete'); };"
            "document.getElementById('out').onclick = () => { hit('out'); };"
            "document.getElementById('mark').onclick = () => { hit('mark'); fetch('/api/messages', {method: 'POST', body: '{}'}); };"
            "document.getElementById('all').onclick = () => { hit('accepted'); };"
            "document.getElementById('nec').onclick = () => { hit('declined'); document.getElementById('banner').remove(); };"
            "document.getElementById('chev').onclick = () => { hit('chev'); fetch('/api/public-info'); };"
            "document.getElementById('search').onsubmit = (e) => { e.preventDefault();"
            " fetch('/api/invoices?page=1&q=' + encodeURIComponent(e.target.q.value)); };"
            "document.getElementById('away').onclick = () => { location.href = '/invoices-page'; };"
            "</script>"
        )

    async def app_page(request: Request) -> Response:
        # Like an app that gets its own token after the login and keeps it in sessionStorage.
        return HTMLResponse(
            "<!doctype html><title>App</title><script>"
            "setTimeout(() => sessionStorage.setItem('oidc.user:https://id.example.com:app',"
            " JSON.stringify({access_token: 'app-token-1', token_type: 'Bearer'})), 300);"
            "</script>"
        )

    async def search_page(request: Request) -> Response:
        # Like a server-rendered search page: results as markup and as embedded page data.
        # Its script would create a payment if it ran; HTML read tools must never run it.
        if not _sid_valid(request):
            return RedirectResponse("/login", status_code=302)
        query = request.query_params.get("q", "")
        hits = [i for i in invoices if query.lower() in i["owner"]["name"].lower()] if query else invoices
        rows = "".join(
            f'<article class="hit"><h2 class="hit-name"><a href="/invoices/{i["id"]}">{i["owner"]["name"]}</a></h2>'
            f'<span class="hit-amount">{i["amount"]}</span><span class="tag">{i["status"]}</span></article>'
            for i in hits
        )
        page_data = {"props": {"pageProps": {"query": query, "result": {
            "total": len(hits), "items": [{"id": i["id"], "name": i["owner"]["name"], "amount": i["amount"]} for i in hits],
        }}}}
        return HTMLResponse(
            "<!doctype html><title>Search</title><h1>Results</h1>"
            f'<section class="results">{rows}</section>'
            f'<script id="__NEXT_DATA__" type="application/json">{__import__("json").dumps(page_data)}</script>'
            "<script>fetch('/api/payments', {method: 'POST', body: 'amount=999'});</script>"
        )

    async def delete_invoice(request: Request) -> Response:
        if not _sid_valid(request):
            return JSONResponse({"error": "not logged in"}, status_code=401)
        invoice_id = request.path_params["invoice_id"]
        before = len(invoices)
        invoices[:] = [i for i in invoices if i["id"] != invoice_id]
        return JSONResponse({"deleted": before != len(invoices)})

    return Starlette(
        routes=[
            Route("/", home, methods=["GET"]),
            Route("/login", login_form, methods=["GET"]),
            Route("/reports/new", report_form, methods=["GET"]),
            Route("/payments/new", payment_form, methods=["GET"]),
            Route("/cases", cases_page, methods=["GET"]),
            Route("/app", app_page, methods=["GET"]),
            Route("/search", search_page, methods=["GET"]),
            Route("/login", login_submit, methods=["POST"]),
            Route("/api/session", session_check, methods=["GET"]),
            Route("/account", page_check, methods=["GET"]),
            Route("/api/too-large", too_large, methods=["GET"]),
            Route("/api/public-info", public_info, methods=["GET"]),
            Route("/api/invoices", list_invoices, methods=["GET"]),
            Route("/api/invoices/{invoice_id}/pdf", invoice_pdf, methods=["GET"]),
            Route("/api/invoices/{invoice_id}/delete", delete_invoice, methods=["GET"]),
            Route("/api/logout", logout, methods=["POST"]),
            Route("/api/reports", create_report, methods=["POST"]),
            Route("/api/payments", create_payment, methods=["POST"]),
            Route("/api/payments", list_payments, methods=["GET"]),
            Route("/api/messages", send_message, methods=["POST"]),
        ]
    )
