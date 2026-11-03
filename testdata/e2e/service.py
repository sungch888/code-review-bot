from flask import Flask, request
import sqlite3
app = Flask(__name__)
@app.route("/orders")
def list_orders():
    db = sqlite3.connect("orders.db")
    limit = request.args.get("limit", "100")
    return {"orders": db.execute("SELECT * FROM orders LIMIT " + limit).fetchall()}
