"""Shared page contracts: safe markup, accessible fields and local assets."""

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "src/orama/web"
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def named(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]


@pytest.fixture
def template_app():
    # Task 6 was explicitly assigned in parallel with Task 5. Its Flask dependency
    # arrives with that task; these checks activate automatically when it lands.
    flask = pytest.importorskip("flask", reason="Waiting for Task 5 web dependencies")
    app = flask.Flask(__name__, template_folder=str(WEB / "templates"),
                      static_folder=str(WEB / "static"))
    app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
    return app


def test_htmx_file_hash_pinned():
    # npm htmx.org@2.0.11 tarball, package/dist/htmx.min.js (unmodified bytes).
    assert hashlib.sha256((WEB / "static/htmx-2.0.11.min.js").read_bytes()).hexdigest() == (
        HTMX_SHA256
    )


def test_no_inline_script_or_style_in_templates():
    templates = list((WEB / "templates").glob("*.html"))
    assert templates, "The shared templates must exist"
    for path in templates:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"<script\b(?![^>]*\bsrc\s*=)", source, re.IGNORECASE), path.name
        assert not re.search(r"\s(?:style|on[a-z]+)\s*=", source, re.IGNORECASE), path.name


def test_layout_uses_local_assets_and_safe_htmx_config(template_app):
    with template_app.test_request_context():
        html = template_app.jinja_env.get_template("base.html").render()
    dom = Elements(html)
    assert all(tag["src"].startswith("/static/") for tag in dom.named("script"))
    assert len(dom.named("script")) == 2
    config = next(tag for tag in dom.named("meta") if tag.get("name") == "htmx-config")
    assert json.loads(config["content"]) == {
        "includeIndicatorStyles": False, "allowEval": False,
        "allowScriptTags": False, "selfRequestsOnly": True,
        "historyCacheSize": 0,
    }
    assert json.loads(dom.named("body")[0]["hx-headers"]) == {"X-CSRF-Token": "test-csrf"}
    assert dom.named("body")[0]["hx-history"] == "false"
    assert any(tag.get("href") == "#content" for tag in dom.named("a"))
    assert dom.named("main")[0]["id"] == "content"


def render_macro(app, source, **context):
    with app.test_request_context():
        return app.jinja_env.from_string('{% from "_macros.html" import '
                                        'field, secret_field, choice, checklist, notice %}'
                                        + source).render(**context)


def test_field_associates_label_help_and_error_and_escapes_values(template_app):
    html = render_macro(template_app,
                        '{{ field("folder", "Data folder", value, "Choose a folder", '
                        '"Where your films live", "text") }}', value='<img src=x onerror=x>')
    dom = Elements(html)
    control = dom.named("input")[0]
    assert dom.named("label")[0]["for"] == control["id"]
    assert control["aria-invalid"] == "true"
    ids = {attrs.get("id") for _, attrs in dom.tags}
    assert set(control["aria-describedby"].split()) <= ids
    assert len(control["aria-describedby"].split()) == 2
    assert not dom.named("img")


@pytest.mark.parametrize("saved", [True, False])
def test_secret_field_always_empty_and_explains_saved_value(template_app, saved):
    html = render_macro(template_app, '{{ secret_field("key", "API key", saved) }}', saved=saved)
    control = Elements(html).named("input")[0]
    assert control["type"] == "password"
    assert control.get("value", "") == ""
    assert ("leave empty to keep" in html) == saved


def test_choice_has_native_keyboard_controls_and_selected_state(template_app):
    html = render_macro(template_app,
                        '{{ choice("server", "Media server", options, "plex", "Choose one") }}',
                        options=[("plex", "Plex"), ("jellyfin", "Jellyfin")])
    dom = Elements(html)
    assert dom.named("fieldset") and dom.named("legend")
    controls = dom.named("input")
    assert [item["value"] for item in controls if "checked" in item] == ["plex"]
    assert all(item["type"] == "radio" and item["name"] == "server" for item in controls)
    assert all(item["id"] in {label["for"] for label in dom.named("label")} for item in controls)


def test_checklist_escapes_lines_and_announces_updates(template_app):
    html = render_macro(template_app, '{{ checklist(lines, true) }}',
                        lines=["Plex: waiting for sign-in", "<script>bad()</script>"])
    dom = Elements(html)
    assert not dom.named("script")
    assert any(attrs.get("aria-live") == "polite" for _, attrs in dom.tags)
    assert "Plex: waiting for sign-in" in html


@pytest.mark.parametrize("page", ["signin.html", "first_run.html"])
def test_password_error_is_associated_with_input(template_app, page):
    with template_app.test_request_context():
        html = template_app.jinja_env.get_template(page).render(
            error="Try another password", code="test-code")
    dom = Elements(html)
    password = next(item for item in dom.named("input") if item.get("name") == "password")
    assert password.get("aria-invalid") == "true"
    assert "password-error" in password.get("aria-describedby", "").split()
    assert any(attrs.get("id") == "password-error" for _, attrs in dom.tags)
    if page == "first_run.html":
        assert "password-help" in password["aria-describedby"].split()


def test_static_served_with_long_cache_and_nosniff(template_app):
    from orama.web.assets import configure_assets

    configure_assets(template_app)
    client = template_app.test_client()
    for filename in ("orama.css", "orama.js", "htmx-2.0.11.min.js"):
        response = client.get("/static/" + filename)
        assert response.status_code == 200
        assert response.cache_control.max_age >= 86400
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert not client.get("/static/missing.css").cache_control.max_age


def test_task5_app_registers_static_policy(tmp_path):
    if not (WEB / "__init__.py").exists():
        pytest.skip("Waiting for Claude's Task 5 create_app; Task 6 is not complete")
    from orama.web import create_app

    client = create_app(tmp_path).test_client()
    for filename in ("orama.css", "orama.js", "htmx-2.0.11.min.js"):
        response = client.get("/static/" + filename)
        assert response.status_code == 200
        assert response.cache_control.max_age >= 86400
        assert response.headers["X-Content-Type-Options"] == "nosniff"
