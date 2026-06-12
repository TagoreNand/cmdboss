# CMDBoss Examples

## Declarative CI types (current model)

CI types are now defined as **data**, not uploaded Python. This removes the
remote-code-execution risk of the old `/models/upload` endpoint and lets every
worker share the same registry via MongoDB.

Register the example `server` type and create a record:

```bash
# 1. Define the CI type (requires a key with the types:write scope)
curl -X POST http://localhost:8000/api/v1/types \
  -H "X-API-Key: $CMDBOSS_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  --data @examples/server_type.json

# 2. Create a server CI (requires ci:write)
curl -X POST http://localhost:8000/api/v1/ci/server \
  -H "X-API-Key: $CMDBOSS_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "hostname": "web-prod-01.example.com",
        "ip_address": "10.0.1.100",
        "os": "Ubuntu 22.04 LTS",
        "environment": "production",
        "owner": "platform-team",
        "rack_unit": 12,
        "tags": ["web", "nginx"]
      }'
# -> 201 Created, with an ETag header carrying the revision

# 3. List with pagination + filter
curl "http://localhost:8000/api/v1/ci/server?environment=production&limit=25" \
  -H "X-API-Key: $CMDBOSS_ADMIN_KEY"

# 4. Update with optimistic concurrency (If-Match = current revision)
curl -X PATCH http://localhost:8000/api/v1/ci/server/<id> \
  -H "X-API-Key: $CMDBOSS_ADMIN_KEY" \
  -H "If-Match: \"1\"" \
  -H "Content-Type: application/json" \
  -d '{"notes": "scheduled for kernel patch"}'

# 5. Inspect change history / lineage
curl http://localhost:8000/api/v1/ci/server/<id>/audit \
  -H "X-API-Key: $CMDBOSS_ADMIN_KEY"
```

## `legacy/`

The original `server.py` (Pydantic model) and `server_hook.py` (hook) for the
removed `exec`-based upload mechanism are preserved here as `.txt` for reference
only. They are no longer used by the application.
