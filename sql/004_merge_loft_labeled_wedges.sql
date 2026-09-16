-- SkyTrak sometimes labels a wedge by its loft ("56°") instead of a club code,
-- which auto-registered it as a club of its own, outside the wedge ordering.
-- 56° is the sand wedge; fold those shots into SW and drop the stray code.
-- The parser now normalizes loft labels up front, so this only ever has rows to
-- fix on databases loaded before that change. Idempotent - safe to re-run.
--
-- chr(176) is the degree sign, spelled out so the statement survives any
-- encoding wobble between this file, the driver, and the server.
UPDATE shots SET club_code = 'SW' WHERE club_code = '56' || chr(176);
DELETE FROM clubs WHERE club_code = '56' || chr(176);
