CREATE TABLE IF NOT EXISTS '_source_info_' ("source_id" INTEGER NOT NULL, "dir_name" TEXT, "base_name" TEXT NOT NULL, "format_name" TEXT NOT NULL, "dst_table" TEXT NOT NULL, size INTEGER, mtime INTEGER);
CREATE TABLE IF NOT EXISTS 'pbp_sample_flat' (key TEXT, value TEXT);
