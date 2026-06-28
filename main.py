from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from typing import Optional, List
import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Task Management API", version="1.0.0")

# ---------- Manager emails (fixed list) ----------
MANAGER_EMAILS = [
    "mudunurubhavanasai@gmail.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "https://task-management-frontend.vercel.app",
        "https://task-management-frontend-ten-gray.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Firebase setup (same project, new collection) ----------
db = None
firebase_auth = None

try:
    import firebase_admin
    from firebase_admin import credentials, firestore, auth as firebase_auth_module

    if not firebase_admin._apps:
        cred = credentials.Certificate({
            "type": os.getenv("FIREBASE_TYPE"),
            "project_id": os.getenv("FIREBASE_PROJECT_ID"),
            "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
            "private_key": os.getenv("FIREBASE_PRIVATE_KEY").replace("\\n", "\n"),
            "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
            "client_id": os.getenv("FIREBASE_CLIENT_ID"),
            "auth_uri": os.getenv("FIREBASE_AUTH_URI"),
            "token_uri": os.getenv("FIREBASE_TOKEN_URI"),
        })
        firebase_admin.initialize_app(cred)

    db = firestore.client()
    firebase_auth = firebase_auth_module
    logger.info("Firebase connected successfully!")

except Exception as e:
    logger.error(f"Firebase not connected: {e}")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)


def get_current_user(token: str = Depends(oauth2_scheme)):
    if not token:
        logger.warning("Authentication failed - no token provided")
        raise HTTPException(status_code=401, detail="Not authenticated.")
    try:
        decoded = firebase_auth.verify_id_token(token)
        email = decoded.get("email", "unknown")
        decoded["role"] = "manager" if email in MANAGER_EMAILS else "employee"
        logger.info(f"User authenticated: {email} (role: {decoded['role']})")
        return decoded
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token.")


def require_manager(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "manager":
        logger.warning(f"User [{current_user.get('email')}] tried a manager-only action")
        raise HTTPException(status_code=403, detail="Only managers can perform this action.")
    return current_user


# ---------- Models ----------

class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    assigned_to: str  # employee email


class TaskUpdate(BaseModel):
    status: Optional[str] = None        # "pending" | "in_progress" | "completed"
    note: Optional[str] = None          # employee's note when updating


def serialize_task(doc):
    data = doc.to_dict()
    data["id"] = doc.id
    return data


# ---------- Routes ----------

@app.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return {
        "email": current_user.get("email"),
        "name": current_user.get("name"),
        "role": current_user.get("role"),
    }


@app.post("/tasks", status_code=201)
def create_task(body: TaskCreate, current_user: dict = Depends(require_manager)):
    manager_email = current_user.get("email")
    logger.info(f"Manager [{manager_email}] creating task: {body.title} for {body.assigned_to}")

    if not body.title.strip():
        raise HTTPException(status_code=400, detail="Title is required.")
    if not body.assigned_to.strip():
        raise HTTPException(status_code=400, detail="Assigned employee email is required.")
    if not db:
        raise HTTPException(status_code=500, detail="Database not connected.")

    ref = db.collection("tasks").add({
        "title": body.title.strip(),
        "description": body.description.strip() if body.description else "",
        "assigned_to": body.assigned_to.strip().lower(),
        "created_by": manager_email,
        "status": "pending",
        "note": "",
    })
    doc = ref[1].get()
    logger.info(f"Task created: {body.title} (assigned to {body.assigned_to})")
    return {"message": "Task created successfully.", "data": serialize_task(doc)}


@app.get("/tasks")
def get_tasks(current_user: dict = Depends(get_current_user)):
    email = current_user.get("email")
    role = current_user.get("role")
    logger.info(f"User [{email}] (role: {role}) fetching tasks")

    if not db:
        raise HTTPException(status_code=500, detail="Database not connected.")

    if role == "manager":
        # Manager sees ALL tasks
        docs = db.collection("tasks").stream()
    else:
        # Employee sees only tasks assigned to them
        docs = db.collection("tasks").where("assigned_to", "==", email.lower()).stream()

    tasks = [serialize_task(doc) for doc in docs]
    logger.info(f"Returned {len(tasks)} tasks to [{email}]")
    return {"data": tasks}


@app.put("/tasks/{task_id}")
def update_task(task_id: str, body: TaskUpdate, current_user: dict = Depends(get_current_user)):
    email = current_user.get("email")
    role = current_user.get("role")
    logger.info(f"User [{email}] updating task ID: {task_id}")

    if not db:
        raise HTTPException(status_code=500, detail="Database not connected.")

    doc_ref = db.collection("tasks").document(task_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Task not found.")

    task = doc.to_dict()

    # Employees can only update tasks assigned to them; managers can update any task
    if role == "employee" and task.get("assigned_to") != email.lower():
        logger.warning(f"User [{email}] tried to update a task not assigned to them")
        raise HTTPException(status_code=403, detail="You can only update your own tasks.")

    update_data = {}
    if body.status is not None:
        if body.status not in ["pending", "in_progress", "completed"]:
            raise HTTPException(status_code=400, detail="Invalid status value.")
        update_data["status"] = body.status
    if body.note is not None:
        update_data["note"] = body.note.strip()

    doc_ref.update(update_data)
    logger.info(f"User [{email}] updated task ID: {task_id} -> {update_data}")
    return {"message": "Task updated successfully.", "data": serialize_task(doc_ref.get())}


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str, current_user: dict = Depends(require_manager)):
    manager_email = current_user.get("email")
    logger.info(f"Manager [{manager_email}] deleting task ID: {task_id}")

    if not db:
        raise HTTPException(status_code=500, detail="Database not connected.")

    doc_ref = db.collection("tasks").document(task_id)
    if not doc_ref.get().exists:
        raise HTTPException(status_code=404, detail="Task not found.")

    doc_ref.delete()
    logger.info(f"Manager [{manager_email}] deleted task ID: {task_id}")
    return {"message": "Task deleted successfully."}
