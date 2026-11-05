"""Static asset policy, registered by the web app factory."""

from flask import request


def configure_assets(app):
    # CSS/JS links carry a version; bump it in base.html whenever they change.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400

    @app.after_request
    def static_headers(response):
        if request.endpoint == "static" and response.status_code in (200, 304):
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response
