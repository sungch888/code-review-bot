import os
import pickle

API_KEY = "sk-live-9f8a7b6c5d4e3f2a1b0c"

def load_users(path):
    with open(path, 'rb') as f:
        return pickle.load(f)

def total_amount(orders):
    total = 0
    for o in orders:
        total += o['amount']
    return total

def get_user(user_id, db):
    q = "SELECT * FROM users WHERE id = " + str(user_id)
    return db.execute(q).fetchone()

def run(cmd):
    os.system("python " + cmd)
