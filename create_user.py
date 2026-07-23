"""Optional CLI to create the initial user.

Prefer the first-run web UI at /setup when starting the app with no users.
This script remains for headless / scripted installs.
"""
import os
import sys
import getpass
from app.database import SessionLocal, engine
from app.models import User
from app.security import hash_password


def main():
    db_path = str(engine.url.database)
    if not os.path.exists(db_path):
        print(f"Database file '{db_path}' not found. Run the app first to create it.")
        sys.exit(1)

    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            print("Users already exist. Use /config/users in the app, or delete users first.")
            return

        print("Create first user (or open /setup in the browser)")
        username = input("Username: ").strip()
        password = getpass.getpass("Password: ")
        if not username or not password:
            print("Username and password are required.")
            return

        if db.query(User).filter(User.username == username).first():
            print(f"User '{username}' already exists.")
            return
        user = User(username=username, hashed_password=hash_password(password))
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"Created user: {user.username} (id={user.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
