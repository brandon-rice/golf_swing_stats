-- Current SkyTrak exports (activity-*.csv) add an FTP (face-to-path) column
-- between PATH and FTT. Legacy exports have no such column and leave it NULL.
ALTER TABLE shots ADD COLUMN IF NOT EXISTS face_to_path_deg NUMERIC(4,1);
