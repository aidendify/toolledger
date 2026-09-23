"""ToolLedger: QR-based durable tool custody for local service shops."""

from __future__ import annotations

import os
import secrets

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import helpers as H

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "toolledger-self-hosted-change-me")
app.teardown_appcontext(H.close_db)


def init_db() -> None:
    with app.app_context():
        H.init_schema(H.get_db())


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": H.marketing_url(),
        "smtp_configured": H.smtp_configured(),
        "twilio_configured": H.twilio_configured(),
        "business_name": H.business_name(),
        "owner_locked": bool(H.owner_password()),
        "logged_in": bool(session.get("owner")) or not H.owner_password(),
        "status_label": H.status_label,
        "public_base_url": H.public_base_url(),
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in H.OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not H.owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


def _actor() -> str:
    if session.get("owner"):
        return "owner"
    return "public QR"


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "smtp_configured": H.smtp_configured(),
            "twilio_configured": H.twilio_configured(),
        }
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not H.owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    error = None
    if request.method == "POST":
        provided = (request.form.get("password") or "").encode("utf-8")
        expected = H.owner_password().encode("utf-8")
        ok = len(provided) == len(expected) and secrets.compare_digest(provided, expected)
        if ok:
            session["owner"] = True
            return redirect(nxt)
        error = "Incorrect password."
    return render_template("login.html", next=nxt, error=error, public=True)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    db = H.get_db()
    tools_out = db.execute(
        """
        SELECT t.*, p.name AS holder_name
        FROM tools t
        LEFT JOIN people p ON p.id = t.holder_person_id
        WHERE t.status = 'out'
        ORDER BY t.checked_out_at ASC, t.id
        """
    ).fetchall()
    missing = H.missing_or_overdue(db)
    events = H.recent_events(db, 15)
    return render_template(
        "index.html",
        out_count=H.out_count(db),
        tools_out=tools_out,
        missing=missing,
        events=events,
        overdue_hours=H.overdue_hours(db),
        notify_ready=H.smtp_configured() or H.twilio_configured(),
    )


@app.route("/tools", methods=["GET", "POST"])
def tools_list():
    db = H.get_db()
    if request.method == "POST":
        # people page also uses POST /people; here unused — tools list is GET + import separate
        pass
    tools = H.list_tools(db)
    return render_template("tools.html", tools=tools)


@app.route("/tools/new", methods=["GET", "POST"])
def tools_new():
    if request.method == "GET":
        return render_template("tool_form.html", tool=None, form=None, errors=None)
    form = {
        "name": (request.form.get("name") or "").strip(),
        "asset_tag": (request.form.get("asset_tag") or "").strip(),
        "category": (request.form.get("category") or "").strip(),
        "home_location": (request.form.get("home_location") or "").strip(),
        "notes": (request.form.get("notes") or "").strip(),
    }
    errors: list[str] = []
    if not form["name"]:
        errors.append("Name is required.")
    if errors:
        return render_template("tool_form.html", tool=None, form=form, errors=errors), 400
    db = H.get_db()
    tool_id = H.create_tool(db, **form, actor="owner")
    flash("Tool created.", "ok")
    return redirect(url_for("tool_detail", tool_id=tool_id))


@app.route("/tools/<int:tool_id>", methods=["GET", "POST"])
def tool_detail(tool_id: int):
    db = H.get_db()
    tool = H.get_tool(db, tool_id)
    if not tool:
        flash("Tool not found.", "error")
        return redirect(url_for("tools_list"))

    if request.method == "POST":
        action = (request.form.get("action") or "save").strip()
        if action == "save":
            form = {
                "name": (request.form.get("name") or "").strip(),
                "asset_tag": (request.form.get("asset_tag") or "").strip(),
                "category": (request.form.get("category") or "").strip(),
                "home_location": (request.form.get("home_location") or "").strip(),
                "notes": (request.form.get("notes") or "").strip(),
            }
            if not form["name"]:
                flash("Name is required.", "error")
            else:
                H.update_tool(db, tool_id, **form)
                flash("Tool updated.", "ok")
            return redirect(url_for("tool_detail", tool_id=tool_id))

        if action == "checkout":
            person_raw = (request.form.get("person_id") or "").strip()
            try:
                person_id = int(person_raw)
            except ValueError:
                flash("Choose a person.", "error")
                return redirect(url_for("tool_detail", tool_id=tool_id))
            force = (request.form.get("confirm_transfer") or "") == "1"
            key, msg = H.checkout_tool(
                db,
                tool,
                person_id=person_id,
                job_ref=(request.form.get("job_ref") or "").strip(),
                note=(request.form.get("note") or "").strip(),
                actor="owner",
                force=force,
            )
            if key == "need_confirm":
                flash(msg + " Check confirm and submit again.", "error")
            elif key == "error":
                flash(msg, "error")
            else:
                flash(msg, "ok")
            return redirect(url_for("tool_detail", tool_id=tool_id))

        if action == "return":
            key, msg = H.return_tool(
                db,
                tool,
                note=(request.form.get("note") or "").strip(),
                actor="owner",
            )
            flash(msg, "ok" if key in {"ok", "noop"} else "error")
            return redirect(url_for("tool_detail", tool_id=tool_id))

    tool = H.get_tool(db, tool_id)
    holder = H.get_person(db, int(tool["holder_person_id"])) if tool["holder_person_id"] else None
    people = H.list_people(db)
    events = H.tool_events(db, tool_id)
    return render_template(
        "tool_detail.html",
        tool=tool,
        holder=holder,
        people=people,
        events=events,
        public_url=H.tool_public_url(tool["token"]),
        needs_confirm=(tool["status"] == "out"),
    )


@app.post("/tools/import")
def tools_import():
    db = H.get_db()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a CSV file.", "error")
        return redirect(url_for("tools_list"))
    try:
        text = upload.read().decode("utf-8-sig")
        count = H.import_tools_csv(db, text, actor="owner")
    except Exception as exc:  # noqa: BLE001
        flash(f"Import failed: {exc}", "error")
        return redirect(url_for("tools_list"))
    flash(f"Imported {count} tool(s).", "ok")
    return redirect(url_for("tools_list"))


@app.post("/tools/<int:tool_id>/missing")
def tool_missing(tool_id: int):
    db = H.get_db()
    tool = H.get_tool(db, tool_id)
    if not tool:
        flash("Tool not found.", "error")
        return redirect(url_for("tools_list"))
    mark = (request.form.get("mark") or "1").strip()
    H.mark_missing(db, tool, missing=(mark != "0"), actor="owner")
    flash("Marked missing." if mark != "0" else "Cleared missing.", "ok")
    return redirect(url_for("tool_detail", tool_id=tool_id))


@app.get("/labels")
def labels():
    db = H.get_db()
    tools = H.list_tools(db)
    base = H.public_base_url()
    cells = []
    for t in tools:
        url = H.tool_public_url(t["token"])
        cells.append(
            {
                "name": t["name"],
                "asset_tag": t["asset_tag"] or "",
                "token_short": t["token"][:8],
                "url": url,
                "qr": H.qr_data_uri(url) if base else "",
            }
        )
    return render_template("labels.html", cells=cells, base_url=base)


@app.route("/people", methods=["GET", "POST"])
def people():
    db = H.get_db()
    if request.method == "POST":
        action = (request.form.get("action") or "create").strip()
        form = {
            "name": (request.form.get("name") or "").strip(),
            "role": (request.form.get("role") or "").strip(),
            "phone": (request.form.get("phone") or "").strip(),
            "email": (request.form.get("email") or "").strip(),
            "home_location": (request.form.get("home_location") or "").strip(),
            "notes": (request.form.get("notes") or "").strip(),
        }
        if action == "update":
            try:
                person_id = int(request.form.get("person_id") or "0")
            except ValueError:
                person_id = 0
            if not form["name"] or not person_id:
                flash("Name is required.", "error")
            else:
                H.update_person(db, person_id, **form)
                flash("Person updated.", "ok")
        else:
            if not form["name"]:
                flash("Name is required.", "error")
            else:
                H.create_person(db, **form)
                flash("Person added.", "ok")
        return redirect(url_for("people"))
    return render_template("people.html", people=H.list_people(db))


@app.get("/t/<token>")
def tool_public(token: str):
    db = H.get_db()
    tool = H.get_tool_by_token(db, token)
    if not tool:
        return render_template("public_missing.html", public=True), 404
    holder = H.get_person(db, int(tool["holder_person_id"])) if tool["holder_person_id"] else None
    people = H.list_people(db)
    return render_template(
        "public_tool.html",
        tool=tool,
        holder=holder,
        people=people,
        public=True,
        show_marketing=True,
        need_confirm=False,
        confirm_person_id=None,
        confirm_msg=None,
        form_job_ref="",
        form_note="",
    )


@app.post("/t/<token>/checkout")
def tool_checkout(token: str):
    db = H.get_db()
    tool = H.get_tool_by_token(db, token)
    if not tool:
        return render_template("public_missing.html", public=True), 404

    person_raw = (request.form.get("person_id") or "").strip()
    job_ref = (request.form.get("job_ref") or "").strip()
    note = (request.form.get("note") or "").strip()
    force = (request.form.get("confirm_transfer") or "") == "1"
    try:
        person_id = int(person_raw)
    except ValueError:
        person_id = 0

    holder = H.get_person(db, int(tool["holder_person_id"])) if tool["holder_person_id"] else None
    people = H.list_people(db)

    if not person_id:
        flash("Choose a person.", "error")
        return render_template(
            "public_tool.html",
            tool=tool,
            holder=holder,
            people=people,
            public=True,
            show_marketing=True,
            need_confirm=False,
            confirm_person_id=None,
            confirm_msg=None,
            form_job_ref=job_ref,
            form_note=note,
        ), 400

    key, msg = H.checkout_tool(
        db,
        tool,
        person_id=person_id,
        job_ref=job_ref,
        note=note,
        actor=_actor(),
        force=force,
    )
    tool = H.get_tool_by_token(db, token)
    holder = H.get_person(db, int(tool["holder_person_id"])) if tool["holder_person_id"] else None

    if key == "need_confirm":
        return render_template(
            "public_tool.html",
            tool=tool,
            holder=holder,
            people=people,
            public=True,
            show_marketing=True,
            need_confirm=True,
            confirm_person_id=person_id,
            confirm_msg=msg,
            form_job_ref=job_ref,
            form_note=note,
        )

    flash(msg, "ok" if key in {"ok", "noop"} else "error")
    return redirect(url_for("tool_public", token=token))


@app.post("/t/<token>/return")
def tool_return(token: str):
    db = H.get_db()
    tool = H.get_tool_by_token(db, token)
    if not tool:
        return render_template("public_missing.html", public=True), 404
    key, msg = H.return_tool(
        db,
        tool,
        note=(request.form.get("note") or "").strip(),
        actor=_actor(),
    )
    flash(msg, "ok" if key in {"ok", "noop"} else "error")
    return redirect(url_for("tool_public", token=token))


@app.post("/t/<token>/transfer")
def tool_transfer(token: str):
    """Explicit force-transfer endpoint (same confirm path as checkout with confirm)."""
    db = H.get_db()
    tool = H.get_tool_by_token(db, token)
    if not tool:
        return render_template("public_missing.html", public=True), 404

    person_raw = (request.form.get("person_id") or "").strip()
    job_ref = (request.form.get("job_ref") or "").strip()
    note = (request.form.get("note") or "").strip()
    confirm = (request.form.get("confirm_transfer") or "") == "1"
    try:
        person_id = int(person_raw)
    except ValueError:
        flash("Choose a person.", "error")
        return redirect(url_for("tool_public", token=token))

    if not confirm:
        flash("Force transfer requires explicit confirm.", "error")
        return redirect(url_for("tool_public", token=token))

    key, msg = H.checkout_tool(
        db,
        tool,
        person_id=person_id,
        job_ref=job_ref,
        note=note,
        actor=_actor(),
        force=True,
    )
    flash(msg, "ok" if key in {"ok", "noop"} else "error")
    return redirect(url_for("tool_public", token=token))


@app.get("/export/custody.csv")
def export_custody():
    db = H.get_db()
    data = H.custody_csv(db)
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=custody.csv"},
    )


@app.get("/export/events.csv")
def export_events():
    db = H.get_db()
    data = H.events_csv(db)
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=events.csv"},
    )


@app.post("/alerts/missing")
def alerts_missing():
    db = H.get_db()
    meta = H.notify_missing(db)
    flash(meta.get("message") or "Done.", "ok" if meta.get("email") or meta.get("sms") or meta.get("count") == 0 else "error")
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    db = H.get_db()
    if request.method == "POST":
        raw = (request.form.get("overdue_hours") or "").strip()
        try:
            val = float(raw)
            if val < 0:
                raise ValueError
        except ValueError:
            flash("Overdue hours must be a non-negative number.", "error")
            return redirect(url_for("settings"))
        H.set_setting(db, "overdue_hours", str(val))
        flash("Settings saved.", "ok")
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        overdue_hours=H.overdue_hours(db),
        env_overdue=_env_overdue_display(),
        public_base=H.public_base_url(),
    )


def _env_overdue_display() -> str:
    return os.environ.get("OVERDUE_HOURS", "24").strip() or "24"


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
