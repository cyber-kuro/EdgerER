"""INTENTIONALLY INSECURE CLASSROOM FIXTURE — NEVER DEPLOY.

These routes are isolated examples for a CodeQL workshop. They are not
imported or executed by the EdgeER application. This file intentionally
contains security flaws so we can identify, document, and remediate them.
Do not merge this vulnerable revision into the production main branch.
"""
import os
import pickle
import sqlite3
import subprocess

import requests
from flask import Flask, redirect, request, send_file

app = Flask(__name__)


@app.route("/lab/command")
def command_injection():
    host = request.args.get("host", "127.0.0.1")
    subprocess.check_output(f"ping -c 1 {host}", shell=True)
    return "OK"


@app.route("/lab/sql")
def sql_injection():
    user = request.args.get("user", "")
    connection = sqlite3.connect("workshop7_users.db")
    rows = connection.execute("SELECT name FROM users WHERE name = '%s'" % user)
    return str(rows.fetchall())


@app.route("/lab/ssrf")
def ssrf():
    url = request.args["url"]
    return requests.get(url, timeout=5).text


@app.route("/lab/path")
def path_traversal():
    name = request.args["name"]
    return send_file(os.path.join("workshop7_files", name))


@app.route("/lab/redirect")
def open_redirect():
    next_url = request.args.get("next", "/")
    return redirect(next_url)


@app.route("/lab/xss")
def reflected_xss():
    name = request.args.get("name", "")
    return "<h1>Hello " + name + "</h1>"


@app.route("/lab/pickle", methods=["POST"])
def insecure_deserialization():
    return str(pickle.loads(request.data))
