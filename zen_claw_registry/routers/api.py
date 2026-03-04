import time
from typing import List

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from zen_claw_registry.core.models import RBAC, AuditLogger, RegistryDB

app = FastAPI(title="zen-claw Skills Registry", version="0.1.0")

db = RegistryDB(data_dir=".registry_data")
audit = AuditLogger(data_dir=".registry_data")

# --- Dependencies ---
def get_current_user(x_api_token: str = Header(...)):
    user = db.get_user_by_token(x_api_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API Token")
    return user

def require_role(allowed_check_func):
    def role_checker(user: dict = Depends(get_current_user)):
        if not allowed_check_func(user.get("role", "viewer")):
            raise HTTPException(status_code=403, detail="Insufficient RBAC permissions")
        return user
    return role_checker

# --- Models ---
class SkillManifest(BaseModel):
    name: str
    version: str
    description: str
    author: str
    capabilities: List[str]

class PublishRequest(BaseModel):
    manifest: SkillManifest
    payload_sha256: str

class ReviewRequest(BaseModel):
    approved: bool
    notes: str

# --- API Endpoints ---
@app.post("/v1/skills/publish", summary="Upload a new skill for review")
async def publish_skill(
    req: PublishRequest,
    user: dict = Depends(require_role(RBAC.can_publish))
):
    """Publisher uploads a skill, which enters PENIDNG_REVIEW state."""
    tenant_id = user["tenant_id"]
    skill_id = f"{req.manifest.name}@{req.manifest.version}"

    skills = db.load(db.skills_file)

    if skill_id in skills:
        raise HTTPException(status_code=409, detail="Skill version already exists")

    skills[skill_id] = {
        "tenant_id": tenant_id,
        "manifest": req.manifest.model_dump(),
        "payload_sha256": req.payload_sha256,
        "status": "PENDING_REVIEW",
        "publisher_id": user["uid"],
        "created_at": time.time(),
        "history": []
    }

    db.save(db.skills_file, skills)

    audit.log(
        tenant_id=tenant_id,
        actor_id=user["uid"],
        action="PUBLISH_SUBMIT",
        resource=skill_id,
        details={"sha256": req.payload_sha256}
    )

    return {"status": "success", "skill_id": skill_id, "state": "PENDING_REVIEW"}

@app.post("/v1/skills/{skill_name}/{version}/review", summary="4-Eyes check review")
async def review_skill(
    skill_name: str,
    version: str,
    req: ReviewRequest,
    user: dict = Depends(require_role(RBAC.can_review))
):
    """Reviewers approve or reject. Signer TTL starts if approved."""
    skill_id = f"{skill_name}@{version}"
    skills = db.load(db.skills_file)

    if skill_id not in skills:
        raise HTTPException(status_code=404, detail="Skill not found")

    skill = skills[skill_id]
    tenant_id = user["tenant_id"]

    # Cross-tenant boundary check
    if skill["tenant_id"] != tenant_id and user["role"] != "system-admin":
        raise HTTPException(status_code=403, detail="Cross-tenant access denied")

    # 4-eyes principle enforcement
    if skill["publisher_id"] == user["uid"]:
        raise HTTPException(status_code=403, detail="4-Eyes Violation: Publisher cannot review their own package")

    if skill["status"] != "PENDING_REVIEW":
        raise HTTPException(status_code=400, detail=f"Skill is currently in {skill['status']} state")

    skill["status"] = "REVIEWED" if req.approved else "REJECTED"
    skill["reviewer_id"] = user["uid"]
    skill["review_notes"] = req.notes
    skill["reviewed_at"] = time.time()

    db.save(db.skills_file, skills)

    audit.log(
        tenant_id=tenant_id,
        actor_id=user["uid"],
        action="REVIEW_DECISION",
        resource=skill_id,
        details={"approved": req.approved, "notes": req.notes}
    )

    return {"status": "success", "skill_id": skill_id, "new_state": skill["status"]}

# --- Takedown API ---
@app.post("/v1/skills/{skill_name}/takedown", summary="DMCA / Security Takedown")
async def takedown_skill(
    skill_name: str,
    reason: str,
    user: dict = Depends(require_role(RBAC.can_sign))
):
    """Instantly yanks all versions of a skill for compliance reasons."""
    skills = db.load(db.skills_file)
    tenant_id = user["tenant_id"]

    yanked_count = 0
    for sid, skill in skills.items():
        if sid.startswith(f"{skill_name}@") and skill["tenant_id"] == tenant_id:
            skill["status"] = "YANKED_TAKEDOWN"
            skill["takedown_reason"] = reason
            skill["takedown_by"] = user["uid"]
            skill["takedown_at"] = time.time()
            yanked_count += 1

    if yanked_count == 0:
        raise HTTPException(status_code=404, detail="No versions found to yank")

    db.save(db.skills_file, skills)

    audit.log(
        tenant_id=tenant_id,
        actor_id=user["uid"],
        action="TAKEDOWN_EXECUTED",
        resource=skill_name,
        details={"reason": reason, "versions_yanked": yanked_count}
    )

    return {"status": "success", "versions_yanked": yanked_count}
