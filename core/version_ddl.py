"""Credential version identity is immutable; only owner maintenance may re-encrypt."""

GUARD = """
CREATE OR REPLACE FUNCTION public.credential_version_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
IF TG_OP='DELETE' THEN RAISE EXCEPTION 'credential versions are retained'; END IF;
IF (to_jsonb(OLD)-ARRAY['state','ciphertext','nonce','encryption_key_id']) IS DISTINCT FROM
   (to_jsonb(NEW)-ARRAY['state','ciphertext','nonce','encryption_key_id']) THEN
   RAISE EXCEPTION 'credential version identity is immutable';
END IF;
IF OLD.state='retired' AND NEW.state<>'retired' THEN
   RAISE EXCEPTION 'retired credential cannot be activated';
END IF;
IF OLD.state='active' AND NEW.state='staged' THEN
   RAISE EXCEPTION 'active credential cannot return to staging';
END IF;
IF (OLD.ciphertext,OLD.nonce,OLD.encryption_key_id) IS DISTINCT FROM
   (NEW.ciphertext,NEW.nonce,NEW.encryption_key_id) AND current_user <>
   (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=TG_RELID) THEN
   RAISE EXCEPTION 'credential re-encryption requires maintenance';
END IF;
RETURN NEW;
END $$
"""
TRIGGER = (
    "CREATE TRIGGER credential_version_immutable BEFORE UPDATE OR DELETE ON credential_versions "
    "FOR EACH ROW EXECUTE FUNCTION public.credential_version_guard()"
)
