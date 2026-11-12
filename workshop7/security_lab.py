"""FIXED CLASSROOM CODEQL EXAMPLES — NOT AN EDGEER PRODUCTION MODULE.

The preceding commit intentionally contained security flaws for a teaching
exercise. Every example below has now been remediated. The original alerts
are preserved in the CodeQL run and SARIF artifact for the workshop report.
"""
from ipaddress import ip_address
import sqlite3
import subprocess

import requests
from flask import Flask, jsonify, redirect, request, send_from_directory

app = Flask(__name__)

# Only server-controlled destinations are permitted.
ALLOWED_ENDPOINTS = {
    "status": "https://www.example.org/",
    "docs": "https://www.example.org/help",
}
ALLOWED_REDIRECTS = {
    "home": "/",
    "dashboard": "/dashboard",
}


@app.route("/lab/command")
def command_injection_fixed():
    host = request.args.get("host", "127.0.0.1")
    try:
        validated_host = str(ip_address(host))
    except ValueError:
        return jsonify({"error": "host must be an IP address"}), 400
    try:
        subprocess.run(
            ["ping", "-c", "1", validated_host],
            check=True,
            capture_output=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return jsonify({"error": "ping failed"}), 502
    return jsonify({"status": "OK"})


@app.route("/lab/sql")
def sql_injection_fixed():
    user = request.args.get("user", "")
    with sqlite3.connect("workshop7_users.db") as connection:
        rows = connection.execute(
            "SELECT name FROM users WHERE name = ?",
            (user,),
        ).fetchall()
    return jsonify({"names": [row[0] for row in rows]})


@app.route("/lab/ssrf")
def ssrf_fixed():
    service = request.args.get("service", "status")
    url = ALLOWED_ENDPOINTS.get(service)
    if url is None:
        return jsonify({"error": "unknown service"}), 400
    response = requests.get(url, timeout=5)
    return jsonify({"status_code": response.status_code})


@app.route("/lab/path")
def path_traversal_fixed():
    name = request.args.get("name", "")
    return send_from_directory("workshop7_files", name, as_attachment=True)


@app.route("/lab/redirect")
def open_redirect_fixed():
    page = request.args.get("next", "home")
    return redirect(ALLOWED_REDIRECTS.get(page, "/"))


@app.route("/lab/xss")
def reflected_xss_fixed():
    name = request.args.get("name", "")
    return jsonify({"greeting": f"Hello {name}"})


@app.route("/lab/pickle", methods=["POST"])
def insecure_deserialization_fixed():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        return jsonify({"error": "expected a JSON object"}), 400
    message = value.get("message")
    if not isinstance(message, str):
        return jsonify({"error": "message must be a string"}), 400
    return jsonify({"message": message})
