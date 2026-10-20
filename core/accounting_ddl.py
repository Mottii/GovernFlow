IMMUTABLE_FUNCTION = """CREATE OR REPLACE FUNCTION public.accounting_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$ BEGIN
RAISE EXCEPTION 'accounting evidence is append-only'; END $$"""
