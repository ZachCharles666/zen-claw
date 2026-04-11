---
name: database_assistant
description: Query and explore databases using SQLite, PostgreSQL, MySQL, or MongoDB CLI tools.
metadata: {"zen-claw":{"emoji":"🗄️","scopes":["exec"]}}
---

# Database Assistant Skill

Use CLI database clients to run queries, explore schemas, and export data. Choose the appropriate client based on the database type.

## SQLite

```bash
# Run a query
sqlite3 /path/to/database.db "SELECT * FROM table_name LIMIT 10;"

# Show all tables
sqlite3 /path/to/database.db ".tables"

# Describe a table schema
sqlite3 /path/to/database.db ".schema table_name"

# Export to CSV
sqlite3 -csv /path/to/database.db "SELECT * FROM table_name;" > output.csv
```

## PostgreSQL

```bash
# Run a query (non-interactive)
psql -h HOST -U USER -d DATABASE -c "SELECT * FROM table_name LIMIT 10;"

# List all tables
psql -h HOST -U USER -d DATABASE -c "\dt"

# Describe a table
psql -h HOST -U USER -d DATABASE -c "\d table_name"

# Export to CSV
psql -h HOST -U USER -d DATABASE -c "\COPY table_name TO 'output.csv' CSV HEADER;"
```

## MySQL / MariaDB

```bash
# Run a query
mysql -h HOST -u USER -pPASSWORD DATABASE -e "SELECT * FROM table_name LIMIT 10;"

# Show tables
mysql -h HOST -u USER -pPASSWORD DATABASE -e "SHOW TABLES;"

# Describe a table
mysql -h HOST -u USER -pPASSWORD DATABASE -e "DESCRIBE table_name;"
```

## MongoDB

```bash
# Run a query
mongosh "mongodb://HOST:27017/DATABASE" --eval "db.collection.find({}).limit(10).toArray()"

# List collections
mongosh "mongodb://HOST:27017/DATABASE" --eval "db.getCollectionNames()"

# Count documents
mongosh "mongodb://HOST:27017/DATABASE" --eval "db.collection.countDocuments({})"
```

## Guidelines

- Always use `LIMIT` or equivalent when exploring unknown tables to avoid large result sets.
- Never expose or log passwords in output. Use environment variables (`$PGPASSWORD`, `$MYSQL_PWD`) when possible.
- For schema exploration, always check table names and column types before running data queries.
- If the user does not specify connection details, ask for them before proceeding.
- Prefer read-only queries unless the user explicitly requests writes/deletes.
