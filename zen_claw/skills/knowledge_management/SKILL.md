---
name: knowledge_management
description: Manage a local Markdown knowledge base — create, search, update, and link notes.
metadata: {"zen-claw":{"emoji":"📚","scopes":["filesystem"]}}
---

# Knowledge Management Skill

Manage a local Markdown-based knowledge base. Notes are plain `.md` files organized in a directory tree.

## Default Layout

```
~/notes/
├── inbox/          # Unprocessed quick captures
├── projects/       # Project-specific notes
├── reference/      # Evergreen reference material
├── journal/        # Daily / weekly logs (YYYY-MM-DD.md)
└── archive/        # Retired notes
```

If the user has not specified a notes root, ask before proceeding. Common defaults: `~/notes`, `~/Documents/notes`, `~/Obsidian`.

## Creating Notes

```bash
# Create a new note with today's date header
NOTE_DIR=~/notes/inbox
NOTE_FILE="$NOTE_DIR/$(date +%Y-%m-%d)-title.md"
cat > "$NOTE_FILE" <<'EOF'
---
title: Note Title
date: 2026-04-09
tags: [tag1, tag2]
---

# Note Title

Content here.
EOF
```

## Searching Notes

```bash
# Full-text search across all notes
grep -r "search term" ~/notes/ --include="*.md" -l

# Search with context lines
grep -r "search term" ~/notes/ --include="*.md" -n -C 2

# Search by tag in frontmatter
grep -r "tags:.*mytag" ~/notes/ --include="*.md" -l

# Find notes modified in the last 7 days
find ~/notes -name "*.md" -newer ~/notes -mtime -7
```

## Listing and Navigating

```bash
# List all notes with titles (from frontmatter)
grep -r "^title:" ~/notes/ --include="*.md" -h | sort

# List notes in a category
ls ~/notes/projects/

# Find notes linking to a specific note (backlinks)
grep -r "\[\[target-note\]\]" ~/notes/ --include="*.md" -l
```

## Updating Notes

When updating an existing note:
1. Read the full file first with `read_file`.
2. Make targeted edits — do not rewrite unless necessary.
3. Update the `date:` frontmatter field to today.
4. Append to a `## Changelog` section if the note tracks history.

## Archiving

```bash
# Move a note to archive
mv ~/notes/inbox/old-note.md ~/notes/archive/
```

## Guidelines

- Always read a note before editing it.
- Preserve existing frontmatter fields; only add new ones when needed.
- Use `[[wikilink]]` syntax for cross-note links when the user's notes use Obsidian/Foam style.
- For journal entries, name files `YYYY-MM-DD.md` and place them in `journal/`.
- When creating tags, use lowercase with hyphens (e.g., `machine-learning`, not `MachineLearning`).
- Never delete notes — archive them instead, unless the user explicitly asks to delete.
