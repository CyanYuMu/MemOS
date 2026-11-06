CREATE INDEX memory_events_tenant_call ON memory_events(tenant_id, json_extract(payload,'$.CallID'));
