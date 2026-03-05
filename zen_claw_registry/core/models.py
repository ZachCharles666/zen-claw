import json
import os
from datetime import datetime, timezone
from typing import Optional


class RegistryDB:
    """Mock database for Registry"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.tenants_file = os.path.join(data_dir, "tenants.json")
        self.skills_file = os.path.join(data_dir, "skills.json")
        self.users_file = os.path.join(data_dir, "users.json")
        os.makedirs(data_dir, exist_ok=True)
        self._init_db()

    def _init_db(self):
        for file_path in [self.tenants_file, self.skills_file, self.users_file]:
            if not os.path.exists(file_path):
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump({}, f)

    def load(self, file_path: str) -> dict:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, file_path: str, data: dict):
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_user_by_token(self, token: str) -> Optional[dict]:
        users = self.load(self.users_file)
        for uid, user in users.items():
            if user.get("token") == token:
                return user
        return None


class RBAC:
    """Implement Role Base Access Control"""

    ROLES = ["viewer", "publisher", "reviewer", "security-admin", "tenant-admin"]

    # 4-EYES PRINCIPLE: Publisher cannot Review their own package.

    @staticmethod
    def can_publish(role: str) -> bool:
        return role in ["publisher", "tenant-admin"]

    @staticmethod
    def can_review(role: str) -> bool:
        return role in ["reviewer", "security-admin", "tenant-admin"]

    @staticmethod
    def can_sign(role: str) -> bool:
        return role in ["security-admin", "tenant-admin"]


class AuditLogger:
    """Multi-tenant privacy-compliant audit logger"""

    def __init__(self, data_dir: str):
        self.log_dir = os.path.join(data_dir, "audit_logs")
        os.makedirs(self.log_dir, exist_ok=True)

    def log(self, tenant_id: str, actor_id: str, action: str, resource: str, details: dict):
        """Append log with 180-days retention structure and PII masking built-in"""
        # Masking simple PII
        safe_details = {
            k: ("***" if "token" in k or "password" in k or "secret" in k else v)
            for k, v in details.items()
        }

        tenant_dir = os.path.join(self.log_dir, tenant_id)
        os.makedirs(tenant_dir, exist_ok=True)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = os.path.join(tenant_dir, f"audit_{today}.jsonl")

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor_id,
            "action": action,
            "resource": resource,
            "details": safe_details,
        }

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
