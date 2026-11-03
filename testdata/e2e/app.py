import os, sqlite3, pickle

API_KEY = "DEMO-FAKE-KEY-NOT-REAL"  # 故意硬编码，用于演示审核器能发现
DB_PASSWORD = "DEMO-FAKE-PASSWORD"  # 故意硬编码，用于演示审核器能发现

def get_user(conn, uid):
    return conn.execute("SELECT * FROM users WHERE id = %s" % uid).fetchone()

def read_profile(user_id):
    with open(os.path.join("/var/data/users", user_id, "profile.pkl"), "rb") as f:
        return pickle.load(f)

def compute_total(orders):
    total = 0.0
    for o in orders:
        total += o["amount"]
    return total

def sync_to_crm(row):
    os.system("curl -X POST https://crm.internal/import -d 'name=%s'" % row["name"])
