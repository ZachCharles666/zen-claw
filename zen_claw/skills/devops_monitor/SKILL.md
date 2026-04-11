---
name: devops_monitor
description: Monitor CI/CD pipelines, containers, Kubernetes workloads, and service health endpoints.
metadata: {"zen-claw":{"emoji":"📡","scopes":["exec","network"],"requires":{"bins_optional":["gh","kubectl","docker","curl"]}}}
---

# DevOps Monitor Skill

Unified monitoring across GitHub Actions, Docker, Kubernetes, and HTTP health endpoints.

## GitHub Actions (gh CLI)

```bash
# List recent workflow runs
gh run list --repo owner/repo --limit 10

# Watch a specific run
gh run watch <run-id> --repo owner/repo

# View failed step logs
gh run view <run-id> --repo owner/repo --log-failed

# Check PR CI status
gh pr checks <pr-number> --repo owner/repo

# List workflows
gh workflow list --repo owner/repo
```

## Docker

```bash
# List running containers with resource usage
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# Live resource stats
docker stats --no-stream

# Check container logs (last 100 lines)
docker logs --tail 100 <container-name>

# Follow logs
docker logs -f <container-name>

# Inspect a container
docker inspect <container-name> | jq '.[0].State'

# List images and sizes
docker images --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}"
```

## Kubernetes

```bash
# Cluster overview
kubectl get nodes -o wide
kubectl top nodes

# Pod status across namespaces
kubectl get pods -A --field-selector=status.phase!=Running

# Pod logs
kubectl logs <pod-name> -n <namespace> --tail=100

# Describe a failing pod
kubectl describe pod <pod-name> -n <namespace>

# Recent events (warnings only)
kubectl get events -A --field-selector=type=Warning --sort-by='.lastTimestamp'

# Resource usage per pod
kubectl top pods -n <namespace>

# Deployment rollout status
kubectl rollout status deployment/<name> -n <namespace>
```

## HTTP Health Checks

```bash
# Basic health check
curl -sf https://example.com/health && echo "OK" || echo "FAIL"

# Check response time
curl -o /dev/null -s -w "HTTP %{http_code} | Time: %{time_total}s\n" https://example.com

# Check multiple endpoints
for url in https://api1.example.com/health https://api2.example.com/health; do
  status=$(curl -o /dev/null -s -w "%{http_code}" "$url")
  echo "$url → $status"
done
```

## Aggregated Status Summary

When the user asks for a "status overview" or "system health", collect and present:
1. **CI/CD**: Last 5 workflow runs — pass/fail counts.
2. **Containers**: Any containers with status != `Up` or high resource usage.
3. **Kubernetes**: Pods not in `Running`/`Completed` state; recent Warning events.
4. **Health endpoints**: HTTP status codes for known service URLs.

## Guidelines

- Always specify `--repo owner/repo` for `gh` commands when not in a git directory.
- Use `--no-stream` for `docker stats` to get a snapshot instead of a live feed.
- For Kubernetes, always specify `-n <namespace>` unless listing across all namespaces with `-A`.
- If credentials or kubeconfig are missing, ask the user before attempting commands.
- Surface actionable information: failed jobs, crash-looping pods, degraded endpoints — not raw dumps.
