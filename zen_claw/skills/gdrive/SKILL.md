---
name: gdrive
description: Interact with Google Drive — list, upload, download, and share files using gcloud or gdrive CLI.
metadata: {"zen-claw":{"emoji":"📁","scopes":["exec","filesystem"],"requires":{"bins_optional":["gcloud","gdrive"]}}}
---

# Google Drive Skill

Manage Google Drive files using `gcloud storage` (preferred) or the `gdrive` CLI. Both require prior authentication.

## Authentication

```bash
# gcloud — authenticate (one-time, opens browser)
gcloud auth login
gcloud auth application-default login

# gdrive — authenticate (one-time)
gdrive about   # triggers OAuth flow on first run
```

## Listing Files

```bash
# gcloud storage — list a Drive folder (requires Workspace/GCS bucket)
gcloud storage ls gs://my-bucket/

# gdrive — list files in root
gdrive files list

# gdrive — list files in a specific folder (by folder ID)
gdrive files list --parent <FOLDER_ID>

# gdrive — search by name
gdrive files list --query "name contains 'report'"

# gdrive — search by type
gdrive files list --query "mimeType='application/pdf'"
```

## Downloading Files

```bash
# gdrive — download a file by ID
gdrive files download <FILE_ID>

# gdrive — download to specific path
gdrive files download <FILE_ID> --destination /path/to/local/

# gcloud storage — download from GCS
gcloud storage cp gs://my-bucket/file.csv ./file.csv

# gcloud storage — download a folder recursively
gcloud storage cp -r gs://my-bucket/folder/ ./local-folder/
```

## Uploading Files

```bash
# gdrive — upload a file to Drive root
gdrive files upload /path/to/file.pdf

# gdrive — upload to a specific folder
gdrive files upload /path/to/file.pdf --parent <FOLDER_ID>

# gcloud storage — upload to GCS bucket
gcloud storage cp ./file.csv gs://my-bucket/

# gcloud storage — upload a folder recursively
gcloud storage cp -r ./local-folder/ gs://my-bucket/folder/
```

## Sharing Files

```bash
# gdrive — share a file with a user (reader)
gdrive permissions create <FILE_ID> --role reader --type user --email user@example.com

# gdrive — make a file publicly readable
gdrive permissions create <FILE_ID> --role reader --type anyone

# gdrive — list permissions on a file
gdrive permissions list <FILE_ID>
```

## Getting File Info

```bash
# gdrive — show file metadata
gdrive files info <FILE_ID>

# gdrive — get shareable link
gdrive files info <FILE_ID> | grep "WebContentLink\|WebViewLink"
```

## Finding File IDs

File IDs appear in Google Drive URLs:
`https://drive.google.com/file/d/<FILE_ID>/view`

Or use list with name filter:
```bash
gdrive files list --query "name = 'my-document.pdf'"
```

## Guidelines

- Always check authentication status before running commands; surface auth errors clearly.
- Prefer `gdrive` for Drive-native operations; prefer `gcloud storage` for GCS buckets.
- Never print OAuth tokens or credentials in output.
- When uploading sensitive files, confirm the sharing settings before uploading.
- If the user does not provide a file ID and the name is ambiguous, list matching files and ask which one to use.
- For large uploads/downloads, use `gcloud storage` with resumable transfers (`--no-clobber` to skip existing).
