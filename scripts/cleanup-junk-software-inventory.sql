-- One-time cleanup for junk rows in software_inventory (run in Supabase SQL editor).
-- Adjust schema/table name if your project differs.

DELETE FROM software_inventory
WHERE version = 'installed'
   OR software_name ~* '^\\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\\}?$';
