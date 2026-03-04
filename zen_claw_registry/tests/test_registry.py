import os
import shutil

import pytest
from fastapi.testclient import TestClient

from zen_claw_registry.routers.api import app, db


@pytest.fixture(autouse=True)
def setup_teardown_db():
    # Setup test DB
    db.data_dir = ".test_registry_data"
    os.makedirs(db.data_dir, exist_ok=True)
    db.tenants_file = os.path.join(db.data_dir, "tenants.json")
    db.skills_file = os.path.join(db.data_dir, "skills.json")
    db.users_file = os.path.join(db.data_dir, "users.json")
    db._init_db()

    # Mock users
    db.save(db.users_file, {
        "user_pub": {"uid": "user_pub", "tenant_id": "tenant_A", "role": "publisher", "token": "pub_token"},
        "user_rev": {"uid": "user_rev", "tenant_id": "tenant_A", "role": "reviewer", "token": "rev_token"},
        "user_sec": {"uid": "user_sec", "tenant_id": "tenant_A", "role": "security-admin", "token": "sec_token"},
        "user_other": {"uid": "user_other", "tenant_id": "tenant_B", "role": "reviewer", "token": "other_token"}
    })

    yield

    # Teardown
    shutil.rmtree(db.data_dir)

client = TestClient(app)

def test_publish_skill():
    payload = {
        "manifest": {
            "name": "test-skill",
            "version": "1.0.0",
            "description": "A test skill",
            "author": "tester",
            "capabilities": ["network"]
        },
        "payload_sha256": "abcdef123456"
    }

    # Needs auth
    response = client.post("/v1/skills/publish", json=payload)
    assert response.status_code == 422 # missing header

    # Publisher can publish
    response = client.post("/v1/skills/publish", json=payload, headers={"x-api-token": "pub_token"})
    assert response.status_code == 200
    assert response.json()["state"] == "PENDING_REVIEW"

def test_4eyes_principle():
    payload = {"manifest": {"name": "skill-2", "version": "1.0", "description": "", "author": "", "capabilities": []}, "payload_sha256": "x"}
    client.post("/v1/skills/publish", json=payload, headers={"x-api-token": "pub_token"})

    # Publisher cannot review their own
    response = client.post("/v1/skills/skill-2/1.0/review", json={"approved": True, "notes": "ok"}, headers={"x-api-token": "pub_token"})
    assert response.status_code == 403

    # Proper reviewer can
    response = client.post("/v1/skills/skill-2/1.0/review", json={"approved": True, "notes": "ok"}, headers={"x-api-token": "rev_token"})
    assert response.status_code == 200
    assert response.json()["new_state"] == "REVIEWED"

def test_tenant_isolation():
    payload = {"manifest": {"name": "skill-3", "version": "1.0", "description": "", "author": "", "capabilities": []}, "payload_sha256": "x"}
    client.post("/v1/skills/publish", json=payload, headers={"x-api-token": "pub_token"})

    # Reviewer from different tenant cannot review
    response = client.post("/v1/skills/skill-3/1.0/review", json={"approved": True, "notes": "no"}, headers={"x-api-token": "other_token"})
    assert response.status_code == 403

def test_takedown():
    payload = {"manifest": {"name": "skill-4", "version": "1.0", "description": "", "author": "", "capabilities": []}, "payload_sha256": "x"}
    client.post("/v1/skills/publish", json=payload, headers={"x-api-token": "pub_token"})

    # Publisher cannot takedown
    response = client.post("/v1/skills/skill-4/takedown?reason=DMCA", headers={"x-api-token": "pub_token"})
    assert response.status_code == 403

    # Security admin can takedown
    response = client.post("/v1/skills/skill-4/takedown?reason=DMCA", headers={"x-api-token": "sec_token"})
    assert response.status_code == 200

    skills = db.load(db.skills_file)
    assert skills["skill-4@1.0"]["status"] == "YANKED_TAKEDOWN"
